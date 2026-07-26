"""Generous matching for typed answers.

For en2ja the user types kana (live romaji->kana conversion happens in the
browser via wanakana, so the backend only ever sees kana). For ja2en the user
types English. In both directions matching is forgiving: we normalize, accept
synonyms split on common separators, and fall back to a fuzzy ratio so that
small typos don't punish the SRS state.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata

from rapidfuzz import fuzz

from kana_quiz.models import Word
from kana_quiz.schemas import Direction

DEFAULT_THRESHOLD = 80
ARTICLE_RE = re.compile(r"^(to\s+|a\s+|an\s+|the\s+)")
WHITESPACE_RE = re.compile(r"\s+")
TRAILING_PUNCT_RE = re.compile(r"[\s\.,;:!?]+$")
MEANING_SEPARATORS = "/;,"
PAREN_RE = re.compile(r"\s*\([^)]*\)\s*")
CLOZE_STRIPPABLE_SUFFIXES = (
    "ください",
    "下さい",
    "でいませんでした",
    "ませんでした",
    "でいません",
    "でいました",
    "でいます",
    "ていません",
    "ていました",
    "ています",
    "ました",
    "ません",
    "ます",
    "でした",
    "です",
    "な",
    "に",
)


def normalize_kana(s: str) -> str:
    """Strip whitespace, NFKC, and unify long-vowel marks.

    Users who type the prolonged-sound mark as ASCII ``-`` (or full-width
    minus) instead of the proper ``ー`` shouldn't be marked wrong.
    """
    s = unicodedata.normalize("NFKC", s).strip()
    s = s.replace("-", "ー").replace("‐", "ー").replace("—", "ー")
    return s


def normalize_english(s: str) -> str:
    """Lowercase, strip articles, collapse whitespace, drop trailing punctuation."""
    s = unicodedata.normalize("NFKC", s).strip().lower()
    s = WHITESPACE_RE.sub(" ", s)
    s = TRAILING_PUNCT_RE.sub("", s)
    s = ARTICLE_RE.sub("", s)
    return s


def split_meanings(english: str) -> list[str]:
    """Split on ``/;,`` at paren depth zero only.

    Disambiguated glosses carry their distinguishing hint in a trailing
    parenthetical (``"order (sequence, arrangement)"``). A naive split
    would cut that in half and leave ``"order (sequence"``, which has no
    closing paren for :data:`PAREN_RE` to strip — so the bare ``"order"``
    the user actually types would never be accepted.
    """
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in english:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch in MEANING_SEPARATORS and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [p for p in (s.strip() for s in out) if p]


def gloss_core(english: str) -> str:
    """The gloss with its disambiguating parenthetical removed, normalized.

    ``"order (a command)"`` and ``"order (sequence)"`` share the core
    ``"order"``. Two such words make a coin-flip multiple-choice card, so
    the distractor picker dedups on this rather than the full string.
    """
    first = split_meanings(english)
    return normalize_english(PAREN_RE.sub(" ", first[0])) if first else ""


def accepted_meanings(english: str) -> list[str]:
    """Split a vocabulary entry's English field into accepted synonyms.

    For each comma/semicolon/slash-separated meaning we also accept a
    parenthetical-stripped variant — JMdict-style entries like
    ``"minister (cabinet)"`` or ``"visit (to a sick person)"`` should grade
    correct when the user just types ``"minister"`` or ``"visit"``.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in split_meanings(english):
        for variant in (raw, PAREN_RE.sub(" ", raw)):
            n = normalize_english(variant)
            if n and n not in seen:
                seen.add(n)
                out.append(n)
    return out


def _fuzzy_match(typed: str, target: str, threshold: int) -> bool:
    if not typed or not target:
        return False
    if typed == target:
        return True
    return fuzz.ratio(typed, target) >= threshold


def _is_kana(ch: str) -> bool:
    return "\u3040" <= ch <= "\u30ff"


def _trailing_kana_suffix(s: str) -> str:
    i = len(s)
    while i > 0 and _is_kana(s[i - 1]):
        i -= 1
    return s[i:]


def _strip_written_decorations(s: str) -> str:
    return s.replace("〜", "").replace("～", "").strip()


def _cloze_kana_surface(target: Word, cloze_expected: str) -> str | None:
    """Infer the kana reading of a cached cloze surface form.

    ``sentence_cache.target_form`` stores the written form from the example
    sentence, often with kanji and conjugation (``扱ってください``). The deck
    already has the dictionary reading in ``words.kana``. We combine the
    invariant written head with the dictionary kana stem to produce surface
    readings such as ``あつかってください`` without an LLM call.
    """
    written = _strip_written_decorations(target.kanji or "")
    kana = (target.kana or "").strip()
    if not written or not kana:
        return None
    if "・" in written:
        return None

    written_suffix = _trailing_kana_suffix(written)
    written_head = written[: len(written) - len(written_suffix)]
    if not written_head or not cloze_expected.startswith(written_head):
        return None

    if written_suffix and kana.endswith(written_suffix):
        kana_head = kana[: -len(written_suffix)]
    else:
        kana_head = kana
    return kana_head + cloze_expected[len(written_head):]


def _cloze_candidate_variants(candidate: str) -> list[str]:
    """Return a cloze candidate plus useful shorter sentence-form variants."""
    normalized = normalize_kana(candidate)
    out = [normalized] if normalized else []
    for suffix in CLOZE_STRIPPABLE_SUFFIXES:
        if normalized.endswith(suffix) and len(normalized) > len(suffix):
            stripped = normalized[: -len(suffix)]
            if len(stripped) >= 2:
                out.append(stripped)
    return out


def _cloze_kana_candidates(target: Word, cloze_expected: str | None) -> list[str]:
    if not cloze_expected:
        return []
    candidates: list[str] = []
    for candidate in (cloze_expected, _cloze_kana_surface(target, cloze_expected)):
        if not candidate:
            continue
        candidates.extend(_cloze_candidate_variants(candidate))
    return candidates


def lookup_alternate(
    conn: sqlite3.Connection,
    word_id: int,
    direction: str,
    normalized: str,
) -> bool:
    """Exact-match the user's already-normalized answer against saved alternates.

    The alternates table stores the *normalized* form (kana for en2ja,
    lowercase English for ja2en), so callers must run the same
    normalization on the user's input before passing it in.
    """
    if not normalized:
        return False
    row = conn.execute(
        "SELECT 1 FROM word_alternates WHERE word_id = ? AND direction = ? AND alternate = ? LIMIT 1",
        (word_id, direction, normalized),
    ).fetchone()
    return row is not None


def grade_typed(
    typed: str,
    target: Word,
    direction: Direction,
    threshold: int = DEFAULT_THRESHOLD,
    *,
    conn: sqlite3.Connection | None = None,
    cloze_expected: str | None = None,
    alternate_directions: tuple[str, ...] | None = None,
) -> bool:
    """Return True if ``typed`` is close enough to the answer for ``direction``.

    When ``conn`` is supplied, a cache hit against ``word_alternates``
    (Gemini-blessed variants persisted from previous sessions) also counts
    as correct. The fuzzy path runs first; the alternates lookup is a
    fallback so cached variants never *block* a match the fuzzy grader
    would have accepted on its own.

    ``cloze_expected`` is the conjugated surface form Gemini used in the
    example sentence (e.g. "役立ち" / "役立ちます" for the dictionary form
    "役立つ"). When set, it joins the candidate list so cloze cards
    accept either the conjugated form OR the dictionary form — the user
    may type whichever they remember; the reveal screen shows the
    canonical surface form so they passively learn the conjugation.
    """
    lookup_directions = alternate_directions or (direction,)

    if direction == "en2ja":
        guess = normalize_kana(typed)
        candidates = [normalize_kana(target.kana)]
        if target.kanji:
            candidates.append(normalize_kana(target.kanji))
        if cloze_expected:
            candidates.extend(_cloze_kana_candidates(target, cloze_expected))
        if any(_fuzzy_match(guess, c, threshold) for c in candidates):
            return True
        if conn is not None:
            return any(
                lookup_alternate(conn, target.id, alternate_direction, guess)
                for alternate_direction in lookup_directions
            )
        return False

    guess = normalize_english(typed)
    if any(_fuzzy_match(guess, c, threshold) for c in accepted_meanings(target.english)):
        return True
    if conn is not None:
        return any(
            lookup_alternate(conn, target.id, alternate_direction, guess)
            for alternate_direction in lookup_directions
        )
    return False
