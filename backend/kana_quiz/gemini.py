"""LLM-generated example sentences + mnemonics for vocabulary words.

Generation hits Google's Gemini API (Flash tier — fast and cheap) and
persists the result in :data:`sentence_cache`. Once cached, sentences are
served from sqlite forever; we don't re-roll because the first sentence
the user sees often becomes a mnemonic anchor — replacing it with a
different example wipes that association.

A background :func:`prefetch_worker` task fills the cache eagerly so the
first time the user sees a word, the sentence + mnemonic is already a
sqlite hit rather than a synchronous Gemini roundtrip. The worker is
woken on startup, after every CSV import, and any time a new card is
introduced or missed (see :func:`schedule_prefetch`). This mirrors the
TTS prefetch pattern in :mod:`kana_quiz.tts`.

The generation function is module-level so tests can monkeypatch it
without touching the SDK.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from kana_quiz.models import Word
from kana_quiz.schemas import Direction

# Gemini 3 Flash preview — frontier-class reasoning at Flash latency. The
# short JSON-shaped tasks here don't benefit from extended "thinking", so
# we configure thinking_level=MINIMAL on the grader path; the
# preview docs explicitly support that knob.
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")

# Smaller / cheaper Gemini variant used for semantic grading of typed
# answers — we want a sub-second judgment, not deep reasoning. Override
# via env when iterating.
#
# Default switched from gemini-3-flash-preview to gemini-3.1-flash-lite
# on 2026-05-10 after a 98-case stress run (scripts/grader_stress.py)
# showed 97% exact verdict agreement, no pass/fail flips that would
# affect the user's SRS path, and ~½ the per-call cost. In the three
# disagreements observed, lite was if anything the more careful reader
# — flash hallucinated the kana of the typed answer on one of them.
GRADER_MODEL = os.environ.get("GEMINI_GRADER_MODEL", "gemini-3.1-flash-lite")

# The google-genai SDK defaults http_options.timeout to None — i.e. it waits
# forever for a response. These calls run synchronously inside FastAPI's
# bounded worker-thread pool, so a stalled connection doesn't just delay one
# request: the wedged thread is never reclaimed, and a handful of them saturate
# the pool until even fast endpoints queue behind them. That is the app-wide
# "hang on certain transitions". Every call therefore gets a finite ceiling.
#
# NOTE: the SDK forwards http_options.timeout to the API as a *server-side
# deadline*, and the Gemini API rejects any deadline under 10s with a 400
# ("Minimum allowed deadline is 10s"). So 10_000ms is the practical floor — we
# cannot set a tighter cap here even though the frontend aborts each attempt at
# 6s (FETCH_TIMEOUT_MS in frontend/src/stores/session.ts). What this ceiling
# buys is bounded, self-clearing failure instead of a permanent wedge: a
# stalled grade now dies at 10s and frees its worker thread, rather than
# holding it forever and (with the client's retries) marching the pool to
# saturation. _GRADE_TIMEOUT_MS is the floor value for the user-facing
# typed-answer graders; _CLIENT_TIMEOUT_MS is the looser default for background
# work (sentence generation + the prefetch worker).
_CLIENT_TIMEOUT_MS = int(os.environ.get("GEMINI_TIMEOUT_MS", "30000"))
_GRADE_TIMEOUT_MS = int(os.environ.get("GEMINI_GRADE_TIMEOUT_MS", "10000"))

# How many sentence requests the prefetch worker is allowed to have in
# flight at once. Override via env for rate-limit tuning. Sequential
# (=1) is safe; ~8 is comfortable on paid tier without tripping the
# default RPM cap.
PREFETCH_CONCURRENCY = max(1, int(os.environ.get("GEMINI_CONCURRENCY", "8")))

log = logging.getLogger(__name__)


class GeminiUnavailable(RuntimeError):
    """Raised when sentence generation is requested but no key is configured."""


@dataclass(frozen=True)
class Sentence:
    japanese: str
    english: str
    # Short English memory hook tying the sound or shape of the kana to
    # the meaning. Empty string when the cache row predates mnemonics —
    # the UI treats "" as "no mnemonic" and just hides the section.
    mnemonic: str = ""
    # The literal substring of ``japanese`` that represents the target
    # word — usually a conjugated form like 役立ち or 役立ちます for the
    # dictionary form 役立つ. The cloze quiz mode blanks this span and
    # grades against it. Empty string for cache rows that predate the
    # cloze feature; the prefetch worker treats empty as a cache miss
    # and regenerates the row with the form populated.
    target_form: str = ""


_PROMPT_TEMPLATE = """You are helping an English-speaking Japanese vocabulary learner.

Target word:
  Kana: {kana}
  Kanji: {kanji}
  English meaning: {english}

CRITICAL: the sentence MUST use the exact target word above (kanji
{kanji}, reading {kana}, meaning '{english}'). Do NOT substitute a
homophone or a different word with the same reading. Japanese has many
words that share a reading but mean entirely different things (e.g.
しょうにん → 商人 "merchant" vs 承認 "approval"); if you reach for a
neighbor instead of the listed target, the card is useless. When the
kanji field is "(none)" the target is the pure-kana form and must
appear in kana exactly as written above.

Produce THREE things:

1. ONE short, beginner-friendly Japanese example sentence that uses the target
   word in a context that makes its meaning obvious from the situation.
   - 6-14 Japanese words, common everyday vocabulary only.
   - If the kanji form exists, use it.
   - Provide a natural (not literal) English translation.

2. A vivid, memorable English mnemonic (1-2 sentences, max ~30 words) that
   helps the learner remember this word. Lean on sound-alikes, imagery, or
   a tiny mini-story. The mnemonic should connect the KANA reading to the
   English meaning so the learner can recall it under time pressure.
   Example for "ねこ" (cat): "Picture a cat saying NEH-KOH as it knocks
   over a glass." Be concrete and specific — abstract mnemonics don't stick.

3. The literal target_form: the EXACT substring of your Japanese sentence
   that represents the target word, in whatever conjugation you used. For
   the dictionary form 役立つ in a sentence like
   "この辞書は勉強にとても役立ちます。", target_form is "役立ちます" (or
   "役立ち" if you split off the polite suffix). MUST be a verbatim
   substring of the japanese field above — no edits, no romaji, no
   surrounding punctuation. The cloze quiz mode uses this to know which
   span to blank out.

Reply with ONLY a JSON object of the form:
{{"japanese": "<sentence>", "english": "<translation>",
  "mnemonic": "<hook>", "target_form": "<substring>"}}
No prose, no markdown fences, no commentary."""


# Process-wide cached genai client. Constructing the client carries a
# non-trivial cost (TLS + connection pool setup) and reusing it across
# calls is safe — the SDK's HTTP layer is thread-safe. Reset to None
# when GEMINI_API_KEY is missing so a config fix doesn't require a
# process restart.
_client: object | None = None


def _get_client() -> object:
    """Return a cached :class:`google.genai.Client`, creating on first use."""
    global _client
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        _client = None
        raise GeminiUnavailable("GEMINI_API_KEY not configured")
    if _client is None:
        # Lazy import keeps the SDK off the hot path for installs that
        # don't use sentence generation (e.g. CI without network).
        from google import genai
        from google.genai import types

        # A default request timeout so a stalled Gemini connection can never
        # hang a worker thread indefinitely (see _CLIENT_TIMEOUT_MS). The
        # user-facing grader path overrides this per-call with a tighter cap.
        _client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(timeout=_CLIENT_TIMEOUT_MS),
        )
    return _client


def _is_kana(ch: str) -> bool:
    """Hiragana or katakana (including the long-vowel mark and small kana)."""
    if not ch:
        return False
    code = ord(ch)
    # Hiragana (U+3040–U+309F) + katakana (U+30A0–U+30FF) covers gojuon,
    # combining marks, sokuon, small kana, and the long-vowel mark ー.
    return 0x3040 <= code <= 0x30FF


def _is_kanji(ch: str) -> bool:
    if not ch:
        return False
    code = ord(ch)
    return 0x4E00 <= code <= 0x9FFF


_HIRA_KATA_OFFSET = 0x30A0 - 0x3040  # +96


def _hira_to_kata(s: str) -> str:
    """Map each hiragana char (U+3041..U+3096) to its katakana counterpart.

    Leaves katakana, ASCII, kanji, and punctuation untouched. Used to
    bridge loanword deck rows whose `kana` column is hiragana-written
    (コピーする, スケート) but whose example sentences carry the actual
    katakana surface (コピー, スケート).
    """
    out = []
    for c in s:
        cp = ord(c)
        if 0x3041 <= cp <= 0x3096:
            out.append(chr(cp + _HIRA_KATA_OFFSET))
        else:
            out.append(c)
    return "".join(out)


def _kata_to_hira(s: str) -> str:
    """Inverse of :func:`_hira_to_kata`. Maps full katakana → hiragana."""
    out = []
    for c in s:
        cp = ord(c)
        if 0x30A1 <= cp <= 0x30F6:
            out.append(chr(cp - _HIRA_KATA_OFFSET))
        else:
            out.append(c)
    return "".join(out)


def _kanji_stem(s: str) -> str:
    """Leading kanji-only prefix of ``s``. Empty if ``s`` doesn't start with kanji."""
    out: list[str] = []
    for ch in s:
        if _is_kanji(ch):
            out.append(ch)
        else:
            break
    return "".join(out)


def _longest_kanji_run(s: str) -> tuple[int, str]:
    """Longest contiguous kanji run in ``s``, plus its starting index.

    Returns ``(start, run)``; ``("", -1)`` shape when no kanji are
    present. Used to anchor on the kanji block of mixed-script lemmata
    like お腹 (kanji run "腹" at index 1) or 〜気 (kanji run "気" at
    index 1 — leading ASCII tilde is decorative).
    """
    best_start = -1
    best = ""
    cur_start = -1
    cur = ""
    for i, ch in enumerate(s):
        if _is_kanji(ch):
            if cur == "":
                cur_start = i
            cur += ch
            if len(cur) > len(best):
                best = cur
                best_start = cur_start
        else:
            cur = ""
            cur_start = -1
    return best_start, best


def _extend_okurigana(
    sentence: str, start: int, stem_end: int, max_extend: int = 5
) -> int:
    """Extend ``stem_end`` rightward across trailing kana (≤max_extend chars).

    Shared between the kanji-stem search and the kana-stem fallback —
    same okurigana / inflection-tail rule (greedy across hiragana &
    katakana, stop at punctuation/ASCII, cap at ``max_extend`` chars
    beyond the anchor's end).

    Callers anchoring on a noun (whose dictionary form has no okurigana)
    pass ``max_extend=0`` so we don't grab the trailing particle (を/に/
    が/...) along with the noun.
    """
    end = stem_end
    while end < len(sentence):
        if not _is_kana(sentence[end]):
            break
        if end - stem_end >= max_extend:
            break
        end += 1
    return end


def _max_extend_for(word_form: str) -> int:
    """How many trailing-kana chars to swallow after the kanji stem.

    A dictionary form like 食べる has hiragana tail "る" → likely
    inflected (食べました, 食べた…), so we let okurigana extension run
    up to 5 chars. A pure-kanji noun like 食料 has no tail → the next
    kana in the sentence is almost certainly a particle, so we cap
    extension at 0 to avoid swallowing を/に/が/etc.
    """
    if not word_form:
        return 0
    # If the dictionary form already ends in a kana char, the word is
    # inflectable (verb/adjective) and the sentence will carry conjugation.
    last = word_form[-1]
    return 5 if _is_kana(last) else 0


def _variant_kanji(
    conn: sqlite3.Connection | None, kana: str
) -> list[str]:
    """Look up alternate kanji writings for ``kana`` in ``kanji_variants``.

    Returns the empty list when no connection is supplied or the table
    isn't populated. The migration creates the table even on minimal
    installs, so a missing table only happens on stale schemas.
    """
    if conn is None or not kana:
        return []
    try:
        rows = conn.execute(
            "SELECT kanji FROM kanji_variants WHERE kana = ?", (kana,)
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r[0] for r in rows]


def target_form_matches_word(target_form: str, word: Word) -> bool:
    """Sanity-check Gemini's stored ``target_form`` against ``word``.

    A bare substring check against the sentence isn't enough — Gemini
    occasionally emits a ``target_form`` that *is* in the sentence but
    refers to the wrong span (e.g. returning "日" for word 課 because the
    sentence also contains 今**日**). Require the form to overlap with
    the word's kanji stem, internal kanji run, literal kanji form, or
    kana reading. When this returns False the caller should fall back
    to :func:`locate_target_form`.
    """
    if not target_form:
        return False
    if word.kanji:
        stem = _kanji_stem(word.kanji)
        if stem and stem in target_form:
            return True
        _, run = _longest_kanji_run(word.kanji)
        if run and run in target_form:
            return True
        lit = word.kanji.replace("〜", "").replace("～", "").strip()
        if "・" in lit:
            head = lit.split("・", 1)[0].strip()
            if head and head in target_form:
                return True
        elif lit and lit in target_form:
            return True
    kana = (word.kana or "").strip()
    if kana:
        if kana in target_form or target_form in kana:
            return True
        kata = _hira_to_kata(kana)
        if kata != kana and kata in target_form:
            return True
        hira = _kata_to_hira(kana)
        if hira != kana and hira in target_form:
            return True
    return False


def locate_target_form(
    sentence: str,
    word: Word,
    conn: sqlite3.Connection | None = None,
) -> str | None:
    """Find the target word's surface form inside ``sentence``.

    The kanji portion of a Japanese word is invariant under conjugation
    (役立つ → 役立ちます, 食べる → 食べました), so the kanji stem of
    ``word.kanji`` always appears verbatim in any sentence that uses
    the word. We anchor on that stem, then extend rightward across any
    trailing hiragana (the okurigana / inflection tail) to capture the
    full surface form.

    Falls back to a kana-only stem search for words with no kanji
    (e.g. すぐに, ありがとう). Returns ``None`` when neither anchor is
    findable — the caller should treat that as "no cloze for this
    card, surface as type-in".

    When ``conn`` is supplied we additionally consult the
    ``kanji_variants`` table for alternate kanji writings that share
    the word's reading. This bridges cases like:
      * word=食料 / kana=しょくりょう, sentence uses 食糧 (same reading,
        different kanji writing — both are valid lemmata);
      * word has no kanji form (e.g. できるだけ) but Gemini wrote the
        sentence with 出来るだけ.
    The variant lookup runs only if the primary kanji-stem and kana
    searches both fail, so existing well-formed sentences pay zero
    cost.
    """
    if not sentence:
        return None

    # Kanji-anchored search first. Try in order:
    #   1. The leading kanji prefix ("食料" → "食料", "役立つ" → "役立").
    #   2. The longest kanji run anywhere in the kanji field, to handle
    #      mixed-script lemmata like お互い (run = "互") or 〜気 (run
    #      = "気"). When the run is internal we extend the anchor
    #      leftward if the leading non-kanji prefix is also in the
    #      sentence — that's the difference between returning お互い
    #      vs just 互.
    if word.kanji:
        stem = _kanji_stem(word.kanji)
        if stem:
            idx = sentence.find(stem)
            if idx != -1:
                end = _extend_okurigana(
                    sentence,
                    idx,
                    idx + len(stem),
                    max_extend=_max_extend_for(word.kanji),
                )
                return sentence[idx:end]
        # Fall through to internal-kanji-run search.
        run_start, run = _longest_kanji_run(word.kanji)
        if run:
            idx = sentence.find(run)
            if idx != -1:
                anchor_start = idx
                if run_start > 0 and idx >= run_start:
                    prefix = word.kanji[:run_start]
                    if sentence[idx - run_start:idx] == prefix:
                        anchor_start = idx - run_start
                end = _extend_okurigana(
                    sentence,
                    anchor_start,
                    idx + len(run),
                    max_extend=_max_extend_for(word.kanji),
                )
                return sentence[anchor_start:end]
        # Last shot before falling through to kana search: maybe the
        # kanji field is a literal that appears verbatim (after
        # stripping decorative ~/・). Common for katakana lemmata
        # like "スケート" or hyphenated entries like "チェック・する".
        lit = word.kanji.replace("〜", "").replace("～", "").strip()
        if "・" in lit:
            # 'チェック・する' → try the head 'チェック' as the surface.
            head = lit.split("・", 1)[0].strip()
            if head and head in sentence:
                return head
        elif lit and lit in sentence:
            return lit

    # Kana-anchored fallback. Pure-kana words appear verbatim in the
    # sentence (no conjugation surface change for, e.g., adverbs like
    # すぐに or interjections like ありがとう). Verbs without kanji
    # forms are rare in this deck.
    kana = (word.kana or "").strip()
    if kana and kana in sentence:
        return kana

    # Hiragana <-> katakana cross-check for loanwords. Many deck rows
    # store the kana column in hiragana (コピーする → コピーする) but the
    # actual sentence writes the loanword in katakana. Convert and try
    # again. Mixed hiragana+katakana entries (びっくりする) also benefit
    # because hira→kata is identity for the non-loan part.
    if kana:
        kata = _hira_to_kata(kana)
        if kata != kana and kata in sentence:
            return kata
        hira = _kata_to_hira(kana)
        if hira != kana and hira in sentence:
            return hira

    # ~する-verb stem: 'びっくりする' → 'びっくり', 'コピーする' → 'コピー'.
    # Drop the trailing -する and search for the nominal head; if it
    # exists in the sentence, extend rightward to capture the
    # conjugation (びっくりしました).
    for suffix in ("をする", "する"):
        if kana.endswith(suffix) and len(kana) > len(suffix):
            head = kana[: -len(suffix)]
            for variant in {head, _hira_to_kata(head), _kata_to_hira(head)}:
                idx = sentence.find(variant)
                if idx != -1:
                    end = _extend_okurigana(
                        sentence, idx, idx + len(variant), max_extend=5,
                    )
                    return sentence[idx:end]
            break  # don't try shorter suffix once we matched the longer one

    # Last-ditch: kana stem (drop the final hiragana char of the
    # dictionary kana, which is usually the okurigana for verbs without
    # cached kanji). e.g. やくだつ → search for やくだ in sentence.
    if len(kana) >= 2:
        kana_stem = kana[:-1]
        idx = sentence.find(kana_stem)
        if idx != -1:
            end = _extend_okurigana(sentence, idx, idx + len(kana_stem))
            return sentence[idx:end]

    # Final fallback: consult kanji_variants for alternate writings
    # that share this reading. Each variant may be:
    #   - leading-kanji (食糧, 出来るだけ) — kanji-stem search,
    #   - kanji-internal (お腹, ご飯) — anchor on the longest kanji
    #     run anywhere in the variant string,
    #   - all-kana / mixed odd punctuation — skipped.
    #
    # SAFETY: kanji_variants groups all JMdict entries sharing a
    # reading, which includes distinct homophone lemmata (しょうにん
    # maps to 商人 "merchant" AND 承認 "approval" AND a half-dozen
    # others). If the deck word has its own kanji form, we restrict to
    # variants whose leading kanji char matches the deck word's leading
    # kanji char — that keeps 食料 ↔ 食糧 (both lead 食) while
    # rejecting 商人 ↔ 承認 (商 vs 承). When the deck word has no kanji
    # form (e.g. できるだけ → 出来るだけ), we accept any kanji variant —
    # the cost of a false positive there is bounded because the kana
    # search already failed.
    #
    # We pick the longest kanji run across surviving candidates for
    # specificity.
    if conn is not None and kana:
        own_lead_kanji: str | None = None
        if word.kanji:
            own_stem = _kanji_stem(word.kanji)
            if not own_stem:
                # Mixed-script word: take the first kanji char anywhere.
                _, own_run = _longest_kanji_run(word.kanji)
                if own_run:
                    own_lead_kanji = own_run[0]
            else:
                own_lead_kanji = own_stem[0]

        best: tuple[int, str] | None = None  # (run_len, located)
        distinct_matches: set[str] = set()  # surface kanji that hit the sentence
        for alt_kanji in _variant_kanji(conn, kana):
            run_start, run = _longest_kanji_run(alt_kanji)
            if not run:
                continue
            # Homophone filter: when the deck word has a kanji, the
            # variant's first kanji char must match. Skip on mismatch
            # so we don't substitute a different word with the same
            # reading.
            if own_lead_kanji is not None:
                alt_first_kanji = run[0]
                # The leading kanji in the variant string itself may
                # differ from `run[0]` if the run is internal (お腹's
                # first kanji is 腹). Use the variant's actual first
                # kanji wherever it appears.
                for ch in alt_kanji:
                    if _is_kanji(ch):
                        alt_first_kanji = ch
                        break
                if alt_first_kanji != own_lead_kanji:
                    continue
            idx = sentence.find(run)
            if idx == -1:
                continue
            # If the run lives in the middle of the variant (e.g. お腹's
            # 腹 at index 1), back the anchor up by `run_start` chars
            # when those leading chars are also present in the sentence,
            # so we return お腹 not just 腹.
            anchor_start = idx
            tail_start = idx + len(run)
            if run_start > 0 and idx >= run_start:
                prefix = alt_kanji[:run_start]
                if sentence[idx - run_start:idx] == prefix:
                    anchor_start = idx - run_start
            end = _extend_okurigana(
                sentence,
                anchor_start,
                tail_start,
                max_extend=_max_extend_for(alt_kanji),
            )
            located = sentence[anchor_start:end]
            distinct_matches.add(run)
            if best is None or len(run) > best[0]:
                best = (len(run), located)
        if best is not None:
            # When the deck word has no kanji of its own to filter against
            # (own_lead_kanji is None), the variant list is just "all kanji
            # that share this reading". For single-mora readings like か
            # that maps to dozens of unrelated lemmata (課, 日, 蚊, 鹿, …),
            # picking the longest match is no better than guessing. Refuse
            # the cloze and let the caller surface type-in.
            if own_lead_kanji is None and len(distinct_matches) > 1:
                return None
            return best[1]

    return None


def generate_sentence(word: Word, *, model: str = DEFAULT_MODEL) -> Sentence:
    """Call Gemini to generate one example sentence + mnemonic for ``word``.

    Raises :class:`GeminiUnavailable` if ``GEMINI_API_KEY`` is missing.
    Network errors and JSON parse failures bubble up to the caller.
    """
    from google.genai import types

    client = _get_client()
    prompt = _PROMPT_TEMPLATE.format(
        kana=word.kana,
        kanji=word.kanji or "(none)",
        english=word.english,
    )
    resp = client.models.generate_content(  # type: ignore[attr-defined]
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.7,
        ),
    )
    text = (resp.text or "").strip()
    data = json.loads(text)
    japanese = str(data["japanese"])
    target_form = str(data.get("target_form", "") or "").strip()
    # Belt-and-suspenders: if the model emitted a target_form that isn't a
    # substring of the sentence (very rare, usually a stray punctuation
    # difference), drop it. The prefetch worker will treat the empty
    # string as "needs another shot."
    if target_form and target_form not in japanese:
        target_form = ""
    return Sentence(
        japanese=japanese,
        english=str(data["english"]),
        mnemonic=str(data.get("mnemonic", "")),
        target_form=target_form,
    )


@dataclass(frozen=True)
class SemanticGrade:
    """LLM verdict on a typed answer that the deterministic grader rejected.

    Three tiers:
    - ``correct``: clean correct answer — synonym, alternate reading,
      IME artifact, or simple paraphrase. Caller treats as a full pass
      and (when ``alternates`` is populated) saves them so future
      repeats don't round-trip the LLM.
    - ``accept``: the student likely confused the target with a closely
      related word, but their intent is clear enough to credit. Caller
      treats as a pass but surfaces ``explanation`` so the student
      learns the distinction. We do NOT save alternates on this path —
      the user's word is a different word, not an alternate form.
    - ``incorrect``: the answer is wrong. Caller fails the card and
      surfaces ``explanation``.
    """

    verdict: Literal["correct", "accept", "incorrect"]
    explanation: str
    alternates: tuple[str, ...]
    # Tightened or replacement English gloss for the target word.
    # Gemini may emit one on any verdict whenever the current gloss is
    # ambiguous, awkward, or could simply be sharper. The caller writes
    # it back to ``words.english`` so future cards prompt with the
    # better form and the same confusion doesn't keep round-tripping
    # to the LLM. ``None`` when no change is warranted.
    clarified_gloss: str | None = None


@dataclass(frozen=True)
class ClozeGradeContext:
    """Sentence context for grading a cloze production answer.

    Attributes:
        sentence_japanese: Full Japanese example sentence shown as a cloze.
        target_form: Surface form blanked out in that sentence.
    """

    sentence_japanese: str
    target_form: str


# Shared scaffolding. The two grading prompts only differ in the
# direction-specific bits (target block layout, IME-artifact handling,
# alternates normalization); everything else is identical and kept here
# so the templates can't drift apart.
_GRADE_HEADER = "You are grading a Japanese-vocabulary student."

# The three verdict tiers. Tier definitions are shared verbatim across
# directions so calibration stays consistent.
_GRADE_VERDICT_RULES = """\
Decide one of three verdicts. Read the tier definitions carefully —
"correct" and "accept" both pass the card, but mean different things,
and "accept" must NOT swallow cases that are really "incorrect".

- "correct": the answer is the same word as the target, modulo
  trivial variation. Apply when the student typed:
  * the literal reference gloss, or any single token that appears as
    a comma-separated entry inside it ("evening" matches a gloss of
    "tonight, this evening" — that's correct, not accept);
  * a trivial morphology variation of a gloss entry (drop or add
    leading "to "/"a "/"the "/"this "; "useful" vs "to be useful";
    "evening" vs "the evening"; singular vs plural);
  * a clean synonym or paraphrase of the gloss in the same direction
    (e.g. "handy" for "useful");
  * an alternate kana/kanji reading or accepted spelling of the
    target word;
  * a mechanical IME artifact (trailing latin letter, missed long-
    vowel mark, small-kana that didn't convert).
  In short: the student demonstrated knowledge of THIS exact word.

- "accept": the student typed a DIFFERENT lexical entry from the
  target — different morphology, different compound structure,
  different particles — but the meaning maps cleanly to the prompt.
  Apply when:
  * Different but synonymous Japanese words for the same English
    gloss, e.g. 役立つ vs 役に立つ — these are TWO separate dictionary
    entries that happen to mean "to be useful". Even though the
    meaning is identical, the student typed a different word, so
    pass them but teach the distinction.
  * The English prompt is genuinely ambiguous and fits another
    Japanese word too (法則 vs 法律 for "law").
  Their recall is essentially right but they reached for a neighbor
  of the target. The explanation must name BOTH words with their
  Japanese forms and explain the distinction, so the student learns
  the precise target next time. Do NOT save the student's answer in
  alternates — it's a different word.

- "incorrect": the answer is wrong, unrelated, empty, OR a closely
  related but DIFFERENT word that the student needs to learn the
  distinction for. Specifically:
  * Transitive/intransitive pairs are different words. 起きる ("to
    wake up", intrans) and 起こす ("to wake someone up", trans) are
    INCORRECT, not accept — the student must learn which valence the
    card is asking for.
  * Active/passive, causative/causee, give/receive pairs (あげる/
    もらう, 教える/教わる) — different words, INCORRECT.
  * Words that share kanji or share an English gloss but denote
    distinct concepts (法律 "statute" vs 法則 "scientific law" —
    when the card is asking for the OTHER one and there's no
    ambiguity in the gloss).
  * Anything plainly unrelated or empty.
  In all incorrect cases the explanation must name the actual target
  in one line, so the student walks away with the right answer."""

_GRADE_EXPLANATION_RULES = """\
Write a SHORT one-line English explanation (<= 30 words) the student can
read and learn from. Japanese forms may appear inline in quotes when
naming a word, e.g. "役立つ (やくだつ)".
- For "correct": a brief confirmation. Often just empty string is fine.
- For "accept": name BOTH the target and the student's word with their
  English glosses, and state the relationship. Example:
  "Card targets 役立つ (to be useful); you typed 役に立つ — both mean
  the same thing, just different forms."
- For "incorrect": name the actual answer in one line. Example:
  "Target was 起きる (to wake up); 起こす means 'to rouse someone'."
"""

# Gloss-tightening rules. Gemini may revise the English gloss on ANY
# verdict whenever it spots a sharper way to phrase it. The caller
# overwrites ``words.english`` with this value so the next card for the
# word prompts more precisely. Keep this conservative — only emit when
# the change actually adds clarity, not for stylistic preference.
_GRADE_GLOSS_RULES = """\
Optionally produce a "clarified_gloss" string: an improved English gloss
for the TARGET word that you'd recommend showing on future cards in
place of the current "{english_field}" field. Write it ONLY when the
current gloss is ambiguous (collides with another Japanese word),
awkward, or missing important sense disambiguation. Style: comma-
separated, lowercase, short noun phrases or "to <verb>" forms, 1-5
items. Example: for 法則 currently glossed "law, rule, principle", a
good clarified_gloss is "scientific law, natural law, principle" — it
still fits 法則, no longer fits 法律. Leave clarified_gloss as an empty
string when the existing gloss is fine."""

_GRADE_ALTERNATES_RULES_JA2EN = """\
If verdict is "correct", list up to 5 normalized English variants
(lowercase, no articles, no trailing punctuation) that should be
accepted in the future as synonymous with the target. For "accept" or
"incorrect" leave alternates empty — those words are NOT alternates of
the target, even when accepted, and saving them would pollute future
grading."""

_GRADE_ALTERNATES_RULES_EN2JA = """\
If verdict is "correct", list up to 5 acceptable kana variants for the
TARGET word (plain kana, no spaces, no punctuation) — synonyms,
alternate readings, or genuine IME artifacts. For "accept" or
"incorrect" leave alternates empty — the student's answer was a
different word, not a variant of the target."""

# f-strings to splice in the shared blocks at definition time; the
# downstream `.format(...)` call substitutes kana/kanji/english/typed,
# which is why those slots are double-braced.
_GRADE_PROMPT_JA2EN = f"""{_GRADE_HEADER}

Target word:
  Kana: {{kana}}
  Kanji: {{kanji}}
  Reference English meaning: {{english}}

The student was shown the Japanese form and typed this English answer:
  "{{typed}}"

{_GRADE_VERDICT_RULES}

{_GRADE_EXPLANATION_RULES}

{_GRADE_ALTERNATES_RULES_JA2EN}

{_GRADE_GLOSS_RULES.format(english_field="Reference English meaning")}"""

_GRADE_PROMPT_EN2JA = f"""{_GRADE_HEADER}

Target word:
  English prompt shown to student: {{english}}
  Reference kana: {{kana}}
  Reference kanji: {{kanji}}

The student typed this Japanese answer (kana):
  "{{typed}}"

The student types romaji that an IME converts to kana on the fly, so
their answer can carry small mechanical artifacts: a trailing latin
letter (e.g. "こんばn" instead of "こんばん"), a missed long-vowel mark
(ローマ vs ろうま), or a small-kana that didn't convert (きよう vs
きょう). If the only thing wrong with the answer is one of these
romaji-input artifacts AND the underlying word is clearly the target,
return "correct".

The English prompt is sometimes a comma-separated gloss list ("law,
rule, principle") that legitimately fits several distinct Japanese
words (e.g. 法則 vs 法律 — both translate as "law"). If the student's
answer is a different Japanese word that ALSO fits the English prompt,
return "accept" and use clarified_gloss to tighten the prompt for next
time so the same collision stops happening.

{_GRADE_VERDICT_RULES}

{_GRADE_EXPLANATION_RULES}

{_GRADE_ALTERNATES_RULES_EN2JA}

{_GRADE_GLOSS_RULES.format(english_field="English prompt")}"""


_GRADE_PROMPT_CLOZE_EN2JA = f"""{_GRADE_HEADER}

Target word:
  English prompt shown to student: {{english}}
  Reference dictionary kana: {{kana}}
  Reference kanji: {{kanji}}

The student was shown this Japanese cloze sentence:
  {{sentence_japanese}}

The blanked target surface form in that sentence was:
  {{target_form}}

The student typed this Japanese answer (kana):
  "{{typed}}"

Grade this as a contextual cloze answer, not as bare dictionary-form
recall. Return "correct" when the student typed:
  * the exact blanked surface form from the sentence;
  * the dictionary form of the target word;
  * any grammatically valid conjugated or inflected form of THIS target
    word that fits the sentence context, including common polite/plain,
    te-form, stem, negative, past, or adjective inflections when the
    surrounding sentence makes them natural;
  * a mechanical romaji-input artifact where the intended target form
    is clear.

If the student typed a different Japanese word that could also fit the
English prompt or the sentence, return "accept" only when it is a close
lexical neighbor worth crediting but still teaching. If the word is
unrelated or changes the sentence meaning, return "incorrect".

For "correct" alternates, include up to 5 kana forms that should pass
future cloze attempts for this same word in this same sentence: the
student's form when valid, the blanked sentence form, and other natural
inflections if appropriate. Use plain kana/kanji text with no spaces or
punctuation. These alternates are cached under the cloze task.

{_GRADE_VERDICT_RULES}

{_GRADE_EXPLANATION_RULES}

{_GRADE_GLOSS_RULES.format(english_field="English prompt")}"""


def grade_semantic(
    word: Word,
    typed: str,
    direction: Direction,
    *,
    model: str = GRADER_MODEL,
    cloze_context: ClozeGradeContext | None = None,
) -> SemanticGrade:
    """LLM grade a typed answer the deterministic grader rejected.

    Args:
        word: Target vocabulary word.
        typed: Student's typed answer.
        direction: Recall direction being graded.
        model: Gemini model id to use for grading.
        cloze_context: Optional sentence context for cloze-mode en->ja grading.

    Returns a :class:`SemanticGrade`. Raises :class:`GeminiUnavailable` on
    missing key, network failure, or schema mismatch — never returns a
    partial / guessed grade so the caller can fall through to the original
    "incorrect" outcome cleanly.
    """
    from google.genai import types

    try:
        client = _get_client()
    except GeminiUnavailable:
        raise

    if direction == "ja2en":
        prompt = _GRADE_PROMPT_JA2EN.format(
            kana=word.kana,
            kanji=word.kanji or "(none)",
            english=word.english,
            typed=typed,
        )
    elif cloze_context is not None:
        prompt = _GRADE_PROMPT_CLOZE_EN2JA.format(
            english=word.english,
            kana=word.kana,
            kanji=word.kanji or "(none)",
            sentence_japanese=cloze_context.sentence_japanese,
            target_form=cloze_context.target_form,
            typed=typed,
        )
    else:
        prompt = _GRADE_PROMPT_EN2JA.format(
            english=word.english,
            kana=word.kana,
            kanji=word.kanji or "(none)",
            typed=typed,
        )

    schema = types.Schema(
        type=types.Type.OBJECT,
        required=["verdict", "explanation", "alternates"],
        properties={
            "verdict": types.Schema(
                type=types.Type.STRING,
                enum=["correct", "accept", "incorrect"],
            ),
            "explanation": types.Schema(type=types.Type.STRING),
            "alternates": types.Schema(
                type=types.Type.ARRAY,
                items=types.Schema(type=types.Type.STRING),
            ),
            # Optional — only populated on en2ja ambiguous-gloss "correct"
            # verdicts. Empty string everywhere else; we treat empty as "no
            # change" on the caller side.
            "clarified_gloss": types.Schema(type=types.Type.STRING),
        },
    )

    try:
        resp = client.models.generate_content(  # type: ignore[attr-defined]
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
                temperature=0.0,
                # Grading is a short, deterministic classification — extended
                # reasoning here just adds latency on the user-facing answer
                # path. MINIMAL is the floor the SDK exposes; lower than that
                # isn't an option on Gemini 3 flash.
                thinking_config=types.ThinkingConfig(
                    thinking_level=types.ThinkingLevel.MINIMAL,
                ),
                # Bound the user-facing grade at the API's 10s deadline floor
                # (see _GRADE_TIMEOUT_MS) so a stalled grade dies and frees its
                # worker thread instead of hanging the pool indefinitely.
                http_options=types.HttpOptions(timeout=_GRADE_TIMEOUT_MS),
            ),
        )
        text = (resp.text or "").strip()
        data = json.loads(text)
        verdict = data["verdict"]
        if verdict not in ("correct", "accept", "incorrect"):
            raise ValueError(f"unexpected verdict: {verdict!r}")
        explanation = str(data["explanation"]).strip()
        raw_alts = data.get("alternates") or []
        if not isinstance(raw_alts, list):
            raise ValueError("alternates must be a list")
        alternates = tuple(str(a) for a in raw_alts[:5])
        if verdict != "correct":
            alternates = ()
        clarified_raw = str(data.get("clarified_gloss", "") or "").strip()
        clarified_gloss = clarified_raw if clarified_raw else None
    except GeminiUnavailable:
        raise
    except Exception as exc:
        raise GeminiUnavailable(f"grade_semantic failed: {exc}") from exc

    return SemanticGrade(
        verdict=verdict,  # type: ignore[arg-type]
        explanation=explanation,
        alternates=alternates,
        clarified_gloss=clarified_gloss,
    )


# Fuzzy threshold for translations is intentionally looser than the
# default 80% used on bare-word grading: a sentence translation has more
# legitimate variance (article placement, word order, polite vs casual
# rendering) so 60% catches the trivial "spelled it slightly differently"
# cases without overfiring on actually wrong translations.
TRANSLATION_FUZZY_THRESHOLD = 60


_GRADE_TRANSLATION_PROMPT = f"""{_GRADE_HEADER}

The student heard a Japanese sentence read aloud and typed an English
translation. Grade the translation on meaning fidelity — exact wording
need not match. Paraphrase, reordering, and synonym choices are fine as
long as the core meaning lands.

Reference Japanese sentence:
  {{japanese}}

Reference English translation:
  {{reference_english}}

The student typed:
  "{{typed}}"

Decide one of three verdicts:

- "correct": the student's translation captures the meaning of the
  reference Japanese. Paraphrase, alternate word order, casual vs polite
  rendering, and minor article/pluralization differences are all
  "correct" — what matters is whether someone hearing the student's
  English would understand what the Japanese said.

- "accept": the student got the gist but dropped a non-trivial detail
  (e.g. omitted a modifier, the subject, a particle's implication) OR
  paraphrased to a sentence whose meaning is close but not identical.
  Pass them but the explanation must name the missing detail so they
  learn.

- "incorrect": the translation is wrong — different meaning, opposite
  polarity, unrelated content, or empty. The explanation must give the
  reference English in one line so the student walks away knowing the
  right answer.

Write a SHORT one-line English explanation (<= 30 words). For "correct"
a brief confirmation or empty string is fine. For "accept" and
"incorrect" the explanation MUST quote the reference English so the
student can compare.

If verdict is "correct" OR "accept", also produce an "alternates" list:
up to 5 normalized English paraphrases (lowercase, no trailing
punctuation) that should auto-pass on future submissions for this
sentence. Include the student's own wording (if it captured the meaning)
plus a couple of natural rephrasings. These get cached so a returning
student doesn't pay another grader round-trip on the same sentence. For
"incorrect" leave alternates empty — saving wrong translations would
poison future grading.

Reply with ONLY a JSON object of the form:
{{{{"verdict": "<correct|accept|incorrect>", "explanation": "<text>", "alternates": ["<en1>", "<en2>"]}}}}
No prose, no markdown fences."""


def grade_translation(
    japanese: str,
    reference_english: str,
    typed: str,
    *,
    model: str = GRADER_MODEL,
) -> SemanticGrade:
    """LLM grade an English translation of a Japanese sentence.

    Used by the listening / sentence-translation quiz mode. A fuzzy
    pre-check against ``reference_english`` at a generous threshold
    (TRANSLATION_FUZZY_THRESHOLD) short-circuits obvious matches before
    we pay for a Gemini round-trip; the LLM is only called when the
    student's wording diverges meaningfully from the reference.

    Returns a :class:`SemanticGrade`. On a "correct" or "accept" verdict
    ``alternates`` carries up to 5 acceptable English translations of
    the sentence — the caller persists these in ``word_alternates``
    (direction=listen) so a returning student types something the
    grader has already blessed and we short-circuit before paying for
    another Gemini call. ``clarified_gloss`` is always None: there's no
    per-word gloss to tighten on a sentence card. Raises
    :class:`GeminiUnavailable` on missing key, network failure, or
    schema mismatch — never a partial grade so the caller falls
    through to a clean "incorrect" outcome.
    """
    from rapidfuzz import fuzz

    typed_clean = (typed or "").strip()
    ref_clean = (reference_english or "").strip()

    # Cheap fuzzy bypass — sentence translations have legitimate
    # paraphrase variance, so we run a generous-threshold ratio first
    # and skip Gemini on a clear match. The threshold (60) is well
    # below the per-word grader's 80 because punctuation, word order,
    # and minor pluralization changes routinely drop sentence ratios
    # into the 60-75 band even when the meaning is identical.
    if typed_clean and ref_clean:
        ratio = fuzz.ratio(typed_clean.lower(), ref_clean.lower())
        if ratio >= TRANSLATION_FUZZY_THRESHOLD:
            return SemanticGrade(
                verdict="correct",
                explanation="",
                alternates=(),
                clarified_gloss=None,
            )

    from google.genai import types

    client = _get_client()

    prompt = _GRADE_TRANSLATION_PROMPT.format(
        japanese=japanese,
        reference_english=reference_english,
        typed=typed_clean,
    )

    schema = types.Schema(
        type=types.Type.OBJECT,
        required=["verdict", "explanation", "alternates"],
        properties={
            "verdict": types.Schema(
                type=types.Type.STRING,
                enum=["correct", "accept", "incorrect"],
            ),
            "explanation": types.Schema(type=types.Type.STRING),
            "alternates": types.Schema(
                type=types.Type.ARRAY,
                items=types.Schema(type=types.Type.STRING),
            ),
        },
    )

    try:
        resp = client.models.generate_content(  # type: ignore[attr-defined]
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
                temperature=0.0,
                thinking_config=types.ThinkingConfig(
                    thinking_level=types.ThinkingLevel.MINIMAL,
                ),
                # Same tight cap as grade_semantic — this is the listening-mode
                # answer path and is subject to the same client abort/retry.
                http_options=types.HttpOptions(timeout=_GRADE_TIMEOUT_MS),
            ),
        )
        text = (resp.text or "").strip()
        data = json.loads(text)
        verdict = data["verdict"]
        if verdict not in ("correct", "accept", "incorrect"):
            raise ValueError(f"unexpected verdict: {verdict!r}")
        explanation = str(data.get("explanation", "")).strip()
        raw_alts = data.get("alternates") or []
        if not isinstance(raw_alts, list):
            raise ValueError("alternates must be a list")
        alternates = tuple(str(a).strip() for a in raw_alts[:5] if str(a).strip())
        # Only cache on a pass verdict — saving "incorrect" wording as an
        # accepted alternate would poison the cache for the next student.
        if verdict == "incorrect":
            alternates = ()
    except GeminiUnavailable:
        raise
    except Exception as exc:
        raise GeminiUnavailable(f"grade_translation failed: {exc}") from exc

    return SemanticGrade(
        verdict=verdict,  # type: ignore[arg-type]
        explanation=explanation,
        alternates=alternates,
        clarified_gloss=None,
    )


def get_cached_sentence(
    conn: sqlite3.Connection,
    word: Word,
    *,
    model: str = DEFAULT_MODEL,
) -> Sentence | None:
    """Return the cached sentence for ``word``, or ``None`` on a cache miss.

    Unlike :func:`get_or_create_sentence` this never calls Gemini — it's the
    read used on hot paths (e.g. the new-card auto-reveal in ``/session/next``)
    where a synchronous generation call would add seconds of latency. Callers
    that miss should fall back to ``None`` and let the background prefetch
    worker fill the row for next time.
    """
    row = conn.execute(
        "SELECT japanese, english, mnemonic, target_form "
        "FROM sentence_cache WHERE word_id = ? AND model = ?",
        (word.id, model),
    ).fetchone()
    if row is None:
        return None
    return Sentence(
        japanese=row["japanese"],
        english=row["english"],
        mnemonic=row["mnemonic"] or "",
        target_form=row["target_form"] or "",
    )


def get_or_create_sentence(
    conn: sqlite3.Connection,
    word: Word,
    *,
    model: str = DEFAULT_MODEL,
) -> Sentence:
    """Return cached sentence for ``word``, generating + persisting on miss.

    A cache row with empty ``target_form`` (pre-cloze entry) still
    counts as a HIT — the cloze pipeline backfills target_form via
    :func:`locate_target_form` rather than re-rolling Gemini.
    """
    row = conn.execute(
        "SELECT japanese, english, mnemonic, target_form "
        "FROM sentence_cache WHERE word_id = ? AND model = ?",
        (word.id, model),
    ).fetchone()
    if row is not None:
        return Sentence(
            japanese=row["japanese"],
            english=row["english"],
            mnemonic=row["mnemonic"] or "",
            target_form=row["target_form"] or "",
        )

    sentence = generate_sentence(word, model=model)
    conn.execute(
        """
        INSERT OR IGNORE INTO sentence_cache
            (word_id, model, japanese, english, mnemonic, target_form, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            word.id,
            model,
            sentence.japanese,
            sentence.english,
            sentence.mnemonic,
            sentence.target_form,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return sentence


# --- Background prefetch worker ---------------------------------------------
#
# Mirrors :func:`kana_quiz.tts.prefetch_worker`. One asyncio task runs for
# the lifetime of the app, sleeping on an Event. Anything that introduces
# work (boot, CSV import, new card shown, wrong answer) calls
# :func:`schedule_prefetch` to wake it. The worker scans for words missing
# from `sentence_cache` and synthesizes them sequentially.

_wakeup: asyncio.Event | None = None
# Live worker telemetry. Updated by `prefetch_worker` at each step so the
# debug view can show real progress (which words are in flight and how
# far through the batch we are) instead of just a "wakeup queued" bool.
_worker_state: dict = {
    "busy": False,
    "inflight": [],        # kanas currently being generated (concurrency-aware)
    "batch_done": 0,       # filled this wake-up cycle
    "batch_total": 0,      # missing count when this cycle started
    "concurrency": PREFETCH_CONCURRENCY,
    "last_error": None,    # last exception message, if any
}


def _get_wakeup() -> asyncio.Event:
    global _wakeup
    if _wakeup is None:
        _wakeup = asyncio.Event()
    return _wakeup


def schedule_prefetch() -> None:
    """Nudge the background worker to scan for words needing a sentence.

    No-op if the worker isn't running yet — the lifespan startup also
    triggers an initial scan, so the first request after boot is covered.
    """
    if _wakeup is not None:
        _wakeup.set()


def _missing_word_ids(conn: sqlite3.Connection, model: str) -> list[int]:
    """Return word IDs without a cached sentence for ``model``.

    Note that we do NOT surface rows with empty ``target_form`` — those
    are pre-cloze cache entries, and we backfill them via the
    :func:`locate_target_form` heuristic at quiz time rather than
    burning Gemini calls to regenerate sentences whose content is
    already fine.

    Order: never-seen-yet words first (smallest id, since the deck is
    introduced in id order), then words that already have an
    ``introduced_at`` — the user is more likely to encounter the early
    ones, so warming them first beats warming a tail-end word the user
    won't see for days.
    """
    rows = conn.execute(
        """
        SELECT w.id FROM words w
        LEFT JOIN sentence_cache s
          ON s.word_id = w.id AND s.model = ?
        WHERE s.id IS NULL
        ORDER BY w.id ASC
        """,
        (model,),
    ).fetchall()
    return [r["id"] for r in rows]


def _load_word(conn: sqlite3.Connection, word_id: int) -> Word | None:
    from kana_quiz.models import word_from_row

    row = conn.execute("SELECT * FROM words WHERE id = ?", (word_id,)).fetchone()
    return word_from_row(row) if row is not None else None


def prefetch_status(
    conn: sqlite3.Connection, *, model: str = DEFAULT_MODEL
) -> dict:
    """Counts + worker state for the debug view.

    Reports total / cached / missing for ``model``, key presence, and
    live worker telemetry: whether it's actively generating, what word
    it's on, batch progress, and the last error if any.
    """
    total = conn.execute("SELECT COUNT(*) AS n FROM words").fetchone()["n"]
    cached = conn.execute(
        "SELECT COUNT(*) AS n FROM sentence_cache WHERE model = ?",
        (model,),
    ).fetchone()["n"]
    return {
        "model": model,
        "total_words": total,
        "cached": cached,
        "missing": max(0, total - cached),
        "key_configured": bool(os.environ.get("GEMINI_API_KEY")),
        "worker_busy": bool(_worker_state["busy"]),
        "worker_inflight": list(_worker_state["inflight"]),
        "worker_batch_done": _worker_state["batch_done"],
        "worker_batch_total": _worker_state["batch_total"],
        "worker_concurrency": _worker_state["concurrency"],
        "worker_last_error": _worker_state["last_error"],
        "worker_pending_wake": bool(_wakeup is not None and _wakeup.is_set()),
    }


async def prefetch_worker(
    *,
    model: str = DEFAULT_MODEL,
    concurrency: int = PREFETCH_CONCURRENCY,
) -> None:
    """Continuously generate sentences for words missing from the cache.

    Runs in the FastAPI lifespan. Sleeps on an :class:`asyncio.Event` and
    wakes on startup or whenever :func:`schedule_prefetch` is called.

    Up to ``concurrency`` requests are in flight at once via a semaphore.
    Each task hops to a worker thread (`asyncio.to_thread`) so the event
    loop stays responsive while the genai SDK does its synchronous HTTP
    work. The shared `_get_client()` keeps connection pools warm across
    requests. If the API key is unconfigured the worker exits cleanly.
    """
    from kana_quiz.db import connect

    wakeup = _get_wakeup()
    wakeup.set()  # initial scan on startup
    sem = asyncio.Semaphore(concurrency)
    _worker_state["concurrency"] = concurrency

    # Sentinel raised inside a task to abort the whole batch when the key
    # is missing — no point spinning all N workers on a config error.
    abort = asyncio.Event()

    async def _process_one(word_id: int) -> bool:
        """Generate + cache one sentence. Returns True on success."""
        if abort.is_set():
            return False
        async with sem:
            if abort.is_set():
                return False
            conn = connect()
            try:
                word = _load_word(conn, word_id)
                if word is None:  # deleted between scan and processing
                    return False
                _worker_state["inflight"].append(word.kana)
                try:
                    await asyncio.to_thread(
                        get_or_create_sentence, conn, word, model=model
                    )
                    _worker_state["batch_done"] += 1
                    log.info(
                        "sentence prefetch: %d/%d %r",
                        _worker_state["batch_done"],
                        _worker_state["batch_total"],
                        word.kana,
                    )
                    return True
                finally:
                    try:
                        _worker_state["inflight"].remove(word.kana)
                    except ValueError:
                        pass
            except GeminiUnavailable:
                _worker_state["last_error"] = "GEMINI_API_KEY not configured"
                abort.set()
                return False
            except Exception as exc:
                log.exception("sentence prefetch failed for word_id=%s", word_id)
                _worker_state["last_error"] = f"{type(exc).__name__}: {exc}"
                # Brief backoff so a flapping API doesn't burn quota.
                await asyncio.sleep(1.0)
                return False
            finally:
                conn.close()

    while True:
        try:
            await wakeup.wait()
            wakeup.clear()
            conn = connect()
            try:
                missing = _missing_word_ids(conn, model)
            finally:
                conn.close()
            if not missing:
                log.info("sentence prefetch: cache up to date")
                _worker_state.update(
                    busy=False, inflight=[], batch_done=0, batch_total=0,
                )
                continue
            log.info(
                "sentence prefetch: %d sentence(s) pending (concurrency=%d)",
                len(missing), concurrency,
            )
            _worker_state.update(
                busy=True, inflight=[], batch_done=0, batch_total=len(missing),
            )
            abort.clear()
            await asyncio.gather(*(_process_one(wid) for wid in missing))
            if abort.is_set():
                log.info("Gemini unavailable; stopping sentence prefetch worker")
                _worker_state.update(busy=False, inflight=[])
                return
            log.info(
                "sentence prefetch: batch complete (%d/%d generated)",
                _worker_state["batch_done"], len(missing),
            )
            _worker_state.update(busy=False, inflight=[])
        except asyncio.CancelledError:
            _worker_state.update(busy=False, inflight=[])
            raise
        except Exception as exc:
            log.exception("sentence prefetch loop error")
            _worker_state["last_error"] = f"{type(exc).__name__}: {exc}"
            _worker_state.update(busy=False, inflight=[])
            await asyncio.sleep(5.0)
