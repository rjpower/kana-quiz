"""Quiz session endpoints: next question + submit answer."""

import json
import logging
import os
import random
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response

from kana_quiz import gemini
from kana_quiz.db import get_conn
from kana_quiz.grading import (
    grade_typed,
    lookup_alternate,
    normalize_english,
    normalize_kana,
)
from kana_quiz.furigana import furigana_segments
from kana_quiz.models import Word, word_from_row
from kana_quiz.schemas import (
    AnswerIn,
    AnswerResult,
    Direction,
    MatchBatch,
    MatchTile,
    NextQuestion,
    QuestionBatch,
    RubySegment,
    SentenceOut,
    WordIntro,
)
from kana_quiz.session import (
    ClozePick,
    ListeningPick,
    build_choices,
    consecutive_failures,
    has_due_cloze,
    has_due_cloze_choice,
    has_due_listening,
    peek_upcoming,
    pick_cloze_card,
    pick_cloze_choice_card,
    pick_listening_card,
    pick_match_batch,
    pick_next_card,
)
from kana_quiz.srs import (
    MATURITY_ORDER,
    SrsState,
    classify_maturity,
    review_rng,
    schedule,
)
from kana_quiz.task_state import (
    CARD_TASKS,
    TASK_CLOZE,
    TASK_CLOZE_CHOICE,
    TASK_JA2EN,
    TASK_SENTENCE_LISTEN,
    sibling_task,
)

log = logging.getLogger(__name__)

router = APIRouter()

# Cards graduate from multiple-choice (recognition) to type-in (recall)
# once their per-direction SRS ease meets this threshold AND they have at
# least one successful repetition. Ease moves with answer quality, so this
# routes shaky cards back into MC even after the user nominally graduated
# them — a card that has dipped to ease<2.5 needs more recognition reps
# before retesting recall.
EASE_PROMOTION_THRESHOLD = 2.5

# Question time windows, in milliseconds. MC stays at a flat 5s — short
# prompts, and recognition shouldn't reward stalling. Type-in scales
# with expected KEYSTROKE count (see _expected_keystrokes below) so the
# clock reflects how many keys the user actually has to press, not how
# many glyphs the answer happens to render as. The same value is
# shipped to the client for the countdown and used here as the SRS
# latency-window denominator, so fast-but-long answers don't get
# falsely penalized as "slow".
MC_WINDOW_MS = 5000
TYPE_BASE_MS = 5000
TYPE_PER_CHAR_MS = 600
TYPE_MIN_MS = 8000
TYPE_MAX_MS = 25000

# Sibling-direction snooze. After a recall answer we push the *other*
# direction of the same word out by this many seconds so the user doesn't get
# the same lexeme back-to-back (e.g. 犬→dog right after dog→犬), which feels
# duplicative and lets them pattern-match instead of recall. ~12h by default
# (effectively "not again today"); set KANA_PAIR_SNOOZE_SECONDS=0 to disable.
PAIR_SNOOZE_SECONDS = int(os.environ.get("KANA_PAIR_SNOOZE_SECONDS", str(12 * 3600)))

# Hepburn romaji for each hiragana, matching what wanakana converts
# *from* by default. Used to estimate keystroke count for the type-in
# timer — typing romaji costs more keystrokes than the kana glyph
# count would suggest (おはようございます is 9 kana but 15 romaji
# letters), and the timer should size to keystrokes.
_ROMAJI_HIRA: dict[str, str] = {
    "あ":"a","い":"i","う":"u","え":"e","お":"o",
    "か":"ka","き":"ki","く":"ku","け":"ke","こ":"ko",
    "が":"ga","ぎ":"gi","ぐ":"gu","げ":"ge","ご":"go",
    "さ":"sa","し":"shi","す":"su","せ":"se","そ":"so",
    "ざ":"za","じ":"ji","ず":"zu","ぜ":"ze","ぞ":"zo",
    "た":"ta","ち":"chi","つ":"tsu","て":"te","と":"to",
    "だ":"da","ぢ":"ji","づ":"zu","で":"de","ど":"do",
    "な":"na","に":"ni","ぬ":"nu","ね":"ne","の":"no",
    "は":"ha","ひ":"hi","ふ":"fu","へ":"he","ほ":"ho",
    "ば":"ba","び":"bi","ぶ":"bu","べ":"be","ぼ":"bo",
    "ぱ":"pa","ぴ":"pi","ぷ":"pu","ぺ":"pe","ぽ":"po",
    "ま":"ma","み":"mi","む":"mu","め":"me","も":"mo",
    "や":"ya","ゆ":"yu","よ":"yo",
    "ら":"ra","り":"ri","る":"ru","れ":"re","ろ":"ro",
    "わ":"wa","を":"o","ん":"n",
    # markers and small-kana standalone forms
    "ー":"-",
    "ぁ":"a","ぃ":"i","ぅ":"u","ぇ":"e","ぉ":"o",
    "ゃ":"ya","ゅ":"yu","ょ":"yo",
}
# Mirror onto katakana (codepoint shift +0x60 from hiragana).
_ROMAJI: dict[str, str] = dict(_ROMAJI_HIRA)
for _h, _r in _ROMAJI_HIRA.items():
    if "ぁ" <= _h <= "ん":
        _ROMAJI[chr(ord(_h) + 0x60)] = _r

_SMALL = set("ゃゅょぁぃぅぇぉャュョァィゥェォ")
_SOKUON = set("っッ")


def _romaji_keystrokes(kana: str) -> int:
    """Estimate Hepburn romaji keystroke count for a kana string.

    Scan kana char-by-char and sum the per-char romaji length. The two
    irregular cases:

      * Small-kana combos (きゃ → "kya"): the small kana absorbs the
        previous syllable's trailing vowel — we subtract 1 (drop the
        vowel) and add the small kana's romaji ("ya"/"yu"/"yo"/...).
      * Sokuon (っ): one extra keystroke; the user types the next
        consonant twice (e.g. かった = "katta").

    Unknown characters fall through at 2 keystrokes — a deliberate
    over-estimate so the timer is generous rather than tight on cards
    using kana we didn't enumerate.
    """
    total = 0
    chars = list(kana)
    for i, ch in enumerate(chars):
        if ch in _SOKUON:
            total += 1
            continue
        if ch in _SMALL and i > 0 and chars[i - 1] in _ROMAJI:
            # Replace prev's trailing vowel with this small kana's romaji.
            total -= 1
            total += len(_ROMAJI.get(ch, "ya"))
            continue
        total += len(_ROMAJI.get(ch, "??"))
    return max(0, total)


def _avg_gloss_len(english: str) -> int:
    """Average length of comma-separated glosses.

    The user only has to type ONE gloss to be marked correct, so the
    timer should size to a typical gloss — not the concatenated length
    of every synonym the dictionary lists. For "important, essential"
    we return 9 (both glosses are 9 chars), not 20 (the full string).
    Rounded down to int; an empty / null gloss returns 0.
    """
    glosses = [g.strip() for g in (english or "").split(",") if g.strip()]
    if not glosses:
        return 0
    return sum(len(g) for g in glosses) // len(glosses)


def _expected_keystrokes(direction: Direction, word: Word) -> int:
    """Keystrokes the typed answer is expected to require.

    Used both for the user-facing countdown (shipped via
    ``NextQuestion.time_limit_ms``) and as the SRS latency-window
    denominator, so a card that gives the user 12 s also expects the
    answer to land inside 12 s for the "fast" SRS bonus.
    """
    if direction == "ja2en":
        return _avg_gloss_len(word.english or "")
    return _romaji_keystrokes(word.kana or "")


def _type_window_ms(expected_keystrokes: int) -> int:
    """Time budget for a type-in question with the given keystroke count."""
    raw = TYPE_BASE_MS + TYPE_PER_CHAR_MS * max(0, expected_keystrokes)
    return max(TYPE_MIN_MS, min(TYPE_MAX_MS, raw))


# Cloze cards need a hair more time than plain type-in: the user has to
# parse the sentence first. Add a flat 2.4 s on top of the keystroke
# budget — well within the existing clamp. (Still used by the selection
# cloze window below.)
CLOZE_PARSE_OVERHEAD_MS = 2400

# Type-in cloze (sentence completion) gives the user a flat window of twice
# the multiple-choice budget: reading the sentence frame and then producing
# the missing word is meaningfully slower than recognition, and the doubled
# MC window is a simple, predictable amount of time the learner can rely on.
CLOZE_WINDOW_MS = 2 * MC_WINDOW_MS

# How often the picker interleaves a due or newly eligible cloze card into the
# main recall rotation. Cloze is a supplemental production track; when it always
# runs before recall, a large batch of one-day cloze reviews can dominate the
# session and feel like the app is repeating the same prompt family. Keep it
# more frequent than listening because cloze directly reinforces recall.
CLOZE_INTERLEAVE_PROBABILITY = float(
    os.environ.get("KANA_CLOZE_INTERLEAVE_PROBABILITY", "0.35")
)

# How often the picker interleaves a due or newly eligible *selection* cloze
# (cloze_choice) card. Same supplemental-track rationale as the type-in cloze
# probability above; kept on its own knob so the two cloze rungs can be tuned
# independently (and pinned separately from tests).
CLOZE_CHOICE_INTERLEAVE_PROBABILITY = float(
    os.environ.get("KANA_CLOZE_CHOICE_INTERLEAVE_PROBABILITY", "0.35")
)

# Selection cloze is recognition (pick one of four) but the user still has to
# read the sentence frame first, so it gets the flat MC window plus the same
# sentence-parse overhead the type-in cloze window carries.
CLOZE_CHOICE_WINDOW_MS = MC_WINDOW_MS + CLOZE_PARSE_OVERHEAD_MS

# Listening (sentence_listen) mode: hearing the sentence already takes
# ~3-5 seconds; the user then needs time to parse, compose, and type an
# English translation. Window scales with the reference English length
# so a long sentence doesn't time out before the user finishes typing.
LISTEN_BASE_MS = 10000
LISTEN_PER_CHAR_MS = 500
LISTEN_MIN_MS = 12000
LISTEN_MAX_MS = 30000

# How often the picker interleaves a due listening card into the recall
# rotation. Listening is a supplementary track, not the main loop —
# 20% feels frequent enough to keep the user actually exercising audio
# comprehension without crowding out the recall ramp that drives SRS
# learning. Module-level constant so it's trivially monkeypatchable
# from tests and tuneable without redeploys. The env override exists
# for the playwright drive (force listening to surface deterministically
# on a seeded test DB); production should leave it unset.
LISTENING_INTERLEAVE_PROBABILITY = float(
    os.environ.get("KANA_LISTENING_INTERLEAVE_PROBABILITY", "0.2")
)


def _listen_window_ms(reference_english: str) -> int:
    """Time budget for a listening card given the reference English length."""
    raw = LISTEN_BASE_MS + LISTEN_PER_CHAR_MS * max(0, len(reference_english))
    return max(LISTEN_MIN_MS, min(LISTEN_MAX_MS, raw))


def _build_listening_question(pick: ListeningPick) -> NextQuestion:
    """Construct the NextQuestion payload for a listening card."""
    expected = pick.sentence_en or ""
    return NextQuestion(
        word_id=pick.word.id,
        direction="ja2en",
        prompt="",  # withheld pre-answer — the audio is the prompt
        mode="sentence_listen",
        choices=[],
        correct_index=0,
        introduction=None,
        kanji=None,
        failure_streak=0,
        time_limit_ms=_listen_window_ms(expected),
        audio_url=f"/api/audio/sentence/{pick.word.id}",
        expected_translation=expected,
        sentence_japanese=pick.sentence_ja,
        sentence_japanese_ruby=furigana_segments(pick.sentence_ja or ""),
    )


def _build_cloze_question(
    conn: sqlite3.Connection, pick: ClozePick
) -> NextQuestion | None:
    """Construct the NextQuestion payload for a cloze card, if renderable."""
    target = pick.word
    cloze = _cloze_for_word(conn, target, gemini.DEFAULT_MODEL)
    if cloze is None:
        gemini.schedule_prefetch()
        return None
    sentence_ja, target_form = cloze
    cloze_template = _render_cloze_template(sentence_ja, target, target_form)
    cloze_before, cloze_after = _cloze_furigana(cloze_template)
    cloze_window = CLOZE_WINDOW_MS
    return NextQuestion(
        word_id=target.id,
        direction="en2ja",
        prompt=target.english,
        mode="cloze",
        choices=[],
        correct_index=0,
        introduction=None,
        kanji=target.kanji,
        failure_streak=consecutive_failures(conn, target.id, "cloze"),
        time_limit_ms=cloze_window,
        cloze_template=cloze_template,
        cloze_expected=target_form,
        cloze_before=cloze_before,
        cloze_after=cloze_after,
    )


def _build_cloze_choice_question(
    conn: sqlite3.Connection, pick: ClozePick
) -> NextQuestion | None:
    """Construct the NextQuestion payload for a selection cloze card.

    Reuses the type-in cloze sentence/blank machinery for the prompt context,
    but the answer is a 4-way kana choice built by :func:`build_choices` — the
    same distractor logic the en2ja multiple-choice path uses, so every option
    is a dictionary kana form and no kanji-bearing surface form gives the answer
    away. ``cloze_expected`` carries the conjugated surface form only for the
    post-answer reveal/record; grading is index-based like MC.

    Returns ``None`` (falling back to the recall picker) when no usable cached
    sentence exists yet — same contract as :func:`_build_cloze_question`.
    """
    target = pick.word
    cloze = _cloze_for_word(conn, target, gemini.DEFAULT_MODEL)
    if cloze is None:
        gemini.schedule_prefetch()
        return None
    sentence_ja, target_form = cloze
    cloze_template = _render_cloze_template(sentence_ja, target, target_form)
    cloze_before, cloze_after = _cloze_furigana(cloze_template)
    choices = build_choices(
        conn,
        target,
        direction="en2ja",
        target_interval_days=pick.interval_days,
        rng=random.Random(),
    )
    # A choice grid needs a full set of four. build_choices only returns fewer
    # on a deck with <4 usable words — rather than render a broken 2x2 grid (or
    # a near-giveaway 2-option question), fall back to the recall picker, which
    # serves type-in / MC for this word instead.
    if len(choices) < 4:
        return None
    labels = [c.kana for c in choices]
    correct_index = next(i for i, c in enumerate(choices) if c.id == target.id)
    return NextQuestion(
        word_id=target.id,
        direction="en2ja",
        prompt=target.english,
        mode="cloze_choice",
        choices=labels,
        correct_index=correct_index,
        introduction=None,
        kanji=target.kanji,
        failure_streak=consecutive_failures(conn, target.id, TASK_CLOZE_CHOICE),
        time_limit_ms=CLOZE_CHOICE_WINDOW_MS,
        cloze_template=cloze_template,
        cloze_expected=target_form,
        cloze_before=cloze_before,
        cloze_after=cloze_after,
    )


def _render_cloze_template(sentence: str, word: Word, target_form: str) -> str:
    """Return ``sentence`` with the visual cloze span replaced by ``{blank}``.

    Gemini's ``target_form`` is intentionally broad for grading: it may include
    the full sentence surface form, such as ``奇妙な`` or ``報告します``. For the
    rendered prompt, keeping predictable surrounding grammar visible gives the
    learner a cleaner sentence frame while still accepting the full
    ``target_form`` on submit.
    """
    # Blank covers the full surface form Gemini chose — including any
    # conjugation suffix like ています / な. Hiding only the lemma head
    # while leaving the suffix visible used to confuse learners: they'd
    # type just the stem to fit the visible "ています" tail, then the
    # reveal would show the whole conjugated form as if they had typed it
    # wrong. Now "what's hidden == what you type" with no slack between
    # blank and grader.
    del word  # kept in the signature for callers; no longer needed
    target_form = target_form.strip()
    start = sentence.find(target_form)
    if start != -1:
        return f"{sentence[:start]}{{blank}}{sentence[start + len(target_form):]}"
    return sentence.replace(target_form, "{blank}", 1)


def _cloze_furigana(template: str) -> tuple[list[RubySegment], list[RubySegment]]:
    """Furigana for the sentence text on either side of the ``{blank}``.

    The blanked span is the answer, so it carries no reading. Each half is
    annotated independently — they meet at the word boundary the blank cut, so
    tokenizing them separately can't bleed a reading across the gap.
    """
    idx = template.find("{blank}")
    if idx == -1:
        return furigana_segments(template), []
    before = template[:idx]
    after = template[idx + len("{blank}"):]
    return furigana_segments(before), furigana_segments(after)


def _cloze_for_word(
    conn: sqlite3.Connection, word: Word, model: str,
) -> tuple[str, str] | None:
    """Return ``(japanese_sentence, target_form)`` if a usable sentence is cached.

    Prefers the stored target_form (Gemini's own answer from the
    generation that produced the row) but falls back to the
    :func:`locate_target_form` heuristic when the stored field is
    empty — that's the case for any sentence cached before the cloze
    feature shipped. The heuristic runs in microseconds (substring
    + ≤5-char extend), so computing it on every cloze read is
    cheaper than persisting it as a write-on-read side effect.

    Returns ``None`` when there's no cached sentence OR when neither
    Gemini's stored form nor the heuristic can pin down a span. The
    caller falls back to plain type-in.
    """
    row = conn.execute(
        "SELECT japanese, target_form FROM sentence_cache "
        "WHERE word_id = ? AND model = ?",
        (word.id, model),
    ).fetchone()
    if row is None:
        return None
    japanese = row["japanese"]
    if not japanese:
        return None
    target_form = (row["target_form"] or "").strip()
    # Two ways the stored form can be unusable:
    #   - missing or not a substring of the sentence (legacy / corrupt rows);
    #   - present in the sentence but pointing at the wrong span — Gemini
    #     occasionally emits a one-char form that matches an unrelated kanji
    #     elsewhere (e.g. "日" for word 課, matched against 今日). The
    #     overlap check rejects that case and triggers the deterministic
    #     locator.
    if (
        not target_form
        or target_form not in japanese
        or not gemini.target_form_matches_word(target_form, word)
    ):
        located = gemini.locate_target_form(japanese, word, conn)
        if not located:
            return None
        target_form = located
    return japanese, target_form


def _parse_exclude(exclude: str | None) -> int | None:
    """Parse the ``exclude`` query param to a word_id.

    Accepts either a bare ``"<word_id>"`` or the legacy ``"<word_id>:<dir>"``
    format. In both cases we exclude the *whole word* (both directions) from
    the next pick — preventing word X en2ja immediately following word X
    ja2en, which feels duplicative even when technically distinct cards.
    """
    if exclude is None:
        return None
    head = exclude.split(":", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


def _parse_exclude_set(raw: str | None) -> set[int]:
    """Parse a comma-separated ``exclude_ids`` query param into a set of ids.

    Used by the continuous-match endpoint to skip the words already on the
    player's board. Unparseable tokens are dropped rather than erroring — a
    malformed beacon must never break a refill.
    """
    if not raw:
        return set()
    out: set[int] = set()
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.add(int(tok))
        except ValueError:
            continue
    return out


# Every answer-type the picker can emit. The client sends an allow-list via the
# ``modes`` query param (a comma-separated subset of these). Omitted means "all
# enabled", which keeps older clients, the deck probe, and tests on the full
# rotation without having to opt in.
ALL_MODES: tuple[str, ...] = ("mc", "type", "cloze", "cloze_choice", "sentence_listen")


def _parse_modes(modes: str | None) -> set[str]:
    """Parse the ``modes`` allow-list query param into a set of enabled modes.

    Unknown tokens are dropped. ``None`` or an all-unknown/empty value falls
    back to every mode enabled — a malformed param must never strand the user
    with nothing to study.
    """
    if modes is None:
        return set(ALL_MODES)
    parsed = {m.strip() for m in modes.split(",") if m.strip()} & set(ALL_MODES)
    return parsed or set(ALL_MODES)


def _other_direction(d: Direction) -> Direction:
    return "en2ja" if d == "ja2en" else "ja2en"


def _balanced_direction(conn: sqlite3.Connection) -> Direction:
    """The recall direction to lead the next pick(s) with, to keep sessions mixed.

    The two directions of a card drift onto different days (the 12h pair-snooze
    is deliberate sibling-burying), so on any given day one direction's cohort
    can dominate the due queue — draining it strictly by due-time gives a long
    run of, e.g., all ja→en. We instead look at the last few recall reviews and
    lead with whichever direction has been served *less*, nudging each session
    back toward a 50/50 recognition/production mix. Ties and a cold history
    default to ja→en — recognition first.
    """
    rows = conn.execute(
        """
        SELECT direction FROM reviews
         WHERE direction IN ('en2ja', 'ja2en')
         ORDER BY id DESC LIMIT 8
        """
    ).fetchall()
    en = sum(1 for r in rows if r["direction"] == "en2ja")
    ja = sum(1 for r in rows if r["direction"] == "ja2en")
    if en > ja:
        return "ja2en"
    if ja > en:
        return "en2ja"
    return "ja2en"


def _srs_from_row(row: sqlite3.Row | None, now: datetime) -> tuple[SrsState, datetime]:
    """Return scheduler state plus introduced_at for a task row.

    Args:
        row: Existing ``task_state`` row, or None for a newly introduced task.
        now: Timestamp used for sentinel due/introduced values.

    Returns:
        Pair of scheduler state and introduced timestamp to persist.
    """
    if row is None:
        return (
            SrsState(ease=2.5, interval_days=0.0, repetitions=0, due_at=now),
            now,
        )
    due_at = datetime.fromisoformat(row["due_at"]) if row["due_at"] else now
    introduced_raw = row["introduced_at"]
    introduced_at = datetime.fromisoformat(introduced_raw) if introduced_raw else now
    return (
        SrsState(
            ease=row["ease"],
            interval_days=row["interval_days"],
            repetitions=row["repetitions"],
            due_at=due_at,
        ),
        introduced_at,
    )


def _upsert_task_state(
    conn: sqlite3.Connection,
    word_id: int,
    task: str,
    nxt: SrsState,
    introduced_at: datetime,
) -> None:
    """Persist scheduler output for a single unified task row."""
    conn.execute(
        """
        INSERT INTO task_state
          (word_id, task, ease, interval_days, repetitions, due_at, introduced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(word_id, task) DO UPDATE SET
          ease = excluded.ease,
          interval_days = excluded.interval_days,
          repetitions = excluded.repetitions,
          due_at = excluded.due_at,
          introduced_at = COALESCE(task_state.introduced_at, excluded.introduced_at)
        """,
        (
            word_id,
            task,
            nxt.ease,
            nxt.interval_days,
            nxt.repetitions,
            nxt.due_at.isoformat(),
            introduced_at.isoformat(),
        ),
    )


@dataclass(frozen=True)
class _TypedGradeResult:
    correct: bool
    outcome: str
    feedback: str | None


# A semantic grade is a *synchronous* call on the answer path, so anything
# this slow is a user-visible hang — flag it at WARNING so it's greppable in
# `docker logs` next to the slow /api/session/answer line it caused.
SLOW_GRADE_MS = 1500

_grade_logger = logging.getLogger("kana_quiz.api")


def _grade_word_typed_answer(
    conn: sqlite3.Connection,
    word: Word,
    typed_answer: str,
    direction: Direction,
    *,
    alternate_task: str,
    cloze_expected: str | None = None,
    cloze_sentence: str | None = None,
) -> _TypedGradeResult:
    """Grade a typed word-production answer with shared Gemini fallback."""
    correct = grade_typed(
        typed_answer,
        word,
        direction,
        conn=conn,
        cloze_expected=cloze_expected,
        alternate_directions=(direction, alternate_task),
    )
    feedback: str | None = None
    if correct:
        return _TypedGradeResult(correct=True, outcome="correct", feedback=None)

    cloze_context = (
        gemini.ClozeGradeContext(
            sentence_japanese=cloze_sentence,
            target_form=cloze_expected,
        )
        if direction == "en2ja" and cloze_sentence and cloze_expected
        else None
    )
    t0 = time.perf_counter()
    error: str | None = None
    try:
        grade = gemini.grade_semantic(
            word,
            typed_answer,
            direction,
            cloze_context=cloze_context,
        )
    except gemini.GeminiUnavailable as exc:
        grade = None
        error = str(exc)
    latency_ms = int((time.perf_counter() - t0) * 1000)
    _grade_logger.log(
        logging.WARNING if latency_ms >= SLOW_GRADE_MS else logging.INFO,
        "gemini grade word=%s dir=%s -> %s in %dms%s",
        word.id,
        direction,
        "unavailable" if grade is None else grade.verdict,
        latency_ms,
        f" ({error})" if error else "",
    )

    if grade is None:
        _log_gemini_grade(
            conn,
            word_id=word.id,
            direction=direction,
            typed=typed_answer,
            cloze_context=cloze_context,
            grade=None,
            error=error,
            latency_ms=latency_ms,
            final_correct=False,
        )
        return _TypedGradeResult(correct=False, outcome="incorrect", feedback=None)

    feedback = grade.explanation or None
    if grade.verdict == "correct":
        _save_alternates(conn, word.id, alternate_task, grade.alternates)
        correct = True
    elif grade.verdict == "accept":
        # "accept" passes the card but the user typed a different word.
        # Feedback is mandatory so they learn the distinction, and we
        # deliberately don't save alternates.
        correct = True

    if grade.clarified_gloss:
        _update_word_gloss(conn, word.id, word.english, grade.clarified_gloss)

    _log_gemini_grade(
        conn,
        word_id=word.id,
        direction=direction,
        typed=typed_answer,
        cloze_context=cloze_context,
        grade=grade,
        error=None,
        latency_ms=latency_ms,
        final_correct=correct,
    )

    return _TypedGradeResult(
        correct=correct,
        outcome="correct" if correct else "incorrect",
        feedback=feedback,
    )


def _log_gemini_grade(
    conn: sqlite3.Connection,
    *,
    word_id: int,
    direction: str,
    typed: str,
    cloze_context: "gemini.ClozeGradeContext | None",
    grade: "gemini.SemanticGrade | None",
    error: str | None,
    latency_ms: int,
    final_correct: bool,
) -> None:
    """Append one row to ``gemini_grade_log`` for posterity.

    Best-effort: a failure to log must never affect the user-visible
    grading outcome, so we swallow exceptions and warn instead.
    """
    try:
        conn.execute(
            """
            INSERT INTO gemini_grade_log
              (created_at, model, latency_ms, word_id, direction,
               typed_answer, cloze_sentence, cloze_target_form,
               verdict, explanation, alternates_json, clarified_gloss,
               error, final_correct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                gemini.GRADER_MODEL,
                latency_ms,
                word_id,
                direction,
                typed,
                cloze_context.sentence_japanese if cloze_context else None,
                cloze_context.target_form if cloze_context else None,
                grade.verdict if grade else None,
                grade.explanation if grade else None,
                json.dumps(list(grade.alternates)) if grade else None,
                grade.clarified_gloss if grade else None,
                error,
                1 if final_correct else 0,
            ),
        )
    except Exception:
        logging.getLogger(__name__).exception("failed to write gemini_grade_log row")


# The intro answer plus two successful spaced reviews. schedule() counts the
# intro as repetition 1, so a lane at 3 has held across the one-day and the
# three-day gaps — the "second mastery attempt" a sprint deck retires on.
SPRINT_ARCHIVE_REPETITIONS = 3


def _grade_sentence_answer(
    conn: sqlite3.Connection,
    word: Word,
    typed_answer: str,
) -> _TypedGradeResult:
    """Grade a typed translation of a sentence card.

    Same shape as the listening lane's grading: blessed alternates first,
    then the Gemini translation grader, then strict equality when no grader
    is configured. The card's own ``kana``/``english`` are the sentence and
    its reference translation.
    """
    typed = typed_answer.strip()
    if not typed:
        return _TypedGradeResult(correct=False, outcome="incorrect", feedback=None)
    normalized = normalize_english(typed)
    if normalized and lookup_alternate(conn, word.id, TASK_JA2EN, normalized):
        return _TypedGradeResult(correct=True, outcome="correct", feedback=None)
    try:
        grade = gemini.grade_translation(word.kana, word.english, typed)
    except gemini.GeminiUnavailable:
        grade = None
    if grade is None:
        correct = normalized == normalize_english(word.english)
        return _TypedGradeResult(
            correct=correct,
            outcome="correct" if correct else "incorrect",
            feedback=None,
        )
    correct = grade.verdict in ("correct", "accept")
    if correct and grade.alternates:
        _save_alternates(conn, word.id, TASK_JA2EN, grade.alternates)
    return _TypedGradeResult(
        correct=correct,
        outcome="correct" if correct else "incorrect",
        feedback=grade.explanation or None,
    )


def _maybe_archive_sprint_card(
    conn: sqlite3.Connection, word_id: int, now: datetime
) -> bool:
    """Retire a sprint-deck card once every recall lane has held twice.

    A sprint deck trades the 21-day mastery bar for throughput: when each of
    the card's existing recall lanes reaches
    :data:`SPRINT_ARCHIVE_REPETITIONS`, the card is archived —
    ``ignored_at`` takes it out of every queue and ``archived_at`` records
    that it was cleared rather than dismissed by hand. Drilling ahead in a
    caught-up session can clear a card early; a sprint deck is deliberately
    permissive about that.
    """
    row = conn.execute(
        """
        SELECT 1 FROM words w
          JOIN decks d ON d.id = w.deck_id
         WHERE w.id = ? AND w.archived_at IS NULL AND d.profile = 'sprint'
        """,
        (word_id,),
    ).fetchone()
    if row is None:
        return False
    lanes = conn.execute(
        """
        SELECT MIN(repetitions) AS floor, COUNT(*) AS n FROM task_state
         WHERE word_id = ? AND task IN (?, ?) AND introduced_at IS NOT NULL
        """,
        (word_id, *CARD_TASKS),
    ).fetchone()
    if not lanes["n"] or lanes["floor"] is None:
        return False
    if lanes["floor"] < SPRINT_ARCHIVE_REPETITIONS:
        return False
    now_iso = now.isoformat()
    conn.execute(
        """
        UPDATE words
           SET archived_at = ?, ignored_at = COALESCE(ignored_at, ?)
         WHERE id = ?
        """,
        (now_iso, now_iso, word_id),
    )
    return True


def _record_task_result(
    conn: sqlite3.Connection,
    *,
    word_id: int,
    task: str,
    review_direction: str,
    payload: AnswerIn,
    state_row: sqlite3.Row | None,
    correct: bool,
    outcome: str,
    expected: str,
    feedback: str | None,
    window_ms: int,
) -> AnswerResult:
    """Persist one task review and return the API response."""
    latency_factor = (
        payload.latency_ms / window_ms if payload.latency_ms is not None else None
    )
    now = datetime.now(timezone.utc)
    prev, introduced_at = _srs_from_row(state_row, now)
    nxt = schedule(prev, outcome, now, latency_factor=latency_factor, rng=review_rng)  # type: ignore[arg-type]

    # Maturity before/after, so the client can toast the new bucket and only
    # celebrate a genuine promotion. "Introduced" has to be read off the row as
    # it stood *before* this answer — `_srs_from_row` hands back `now` as the
    # introduced_at sentinel for a first sighting, which would otherwise make
    # every new card look like it had already been introduced.
    was_introduced = state_row is not None and state_row["introduced_at"] is not None
    prev_maturity = classify_maturity(prev.interval_days, prev.repetitions, was_introduced)
    maturity = classify_maturity(nxt.interval_days, nxt.repetitions, True)
    # Gated on `correct` deliberately: missing a brand-new card still moves it
    # new -> learning, which is a promotion by index but the opposite of
    # something to congratulate. A celebration has to be earned by a right
    # answer.
    maturity_up = correct and (
        MATURITY_ORDER.index(maturity) > MATURITY_ORDER.index(prev_maturity)
    )

    conn.execute(
        """
        INSERT INTO reviews
          (word_id, asked_at, direction, correct, timed_out, latency_ms, outcome)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            word_id,
            now.isoformat(),
            review_direction,
            1 if correct else 0,
            1 if payload.timed_out else 0,
            payload.latency_ms,
            outcome,
        ),
    )
    _upsert_task_state(conn, word_id, task, nxt, introduced_at)

    # A recall word counts as introduced the moment the user answers *either*
    # direction — the picker hands both rows out with introduced_at NULL and
    # leaves the stamping to us (see pick_next_card). The upsert above only
    # touches the direction just answered, so without this the untouched
    # sibling would keep a NULL introduced_at and, since every due query
    # filters on IS NOT NULL, would never be served again.
    if task in CARD_TASKS:
        conn.execute(
            """
            UPDATE task_state
               SET introduced_at = ?
             WHERE word_id = ? AND task IN (?, ?) AND introduced_at IS NULL
            """,
            (now.isoformat(), word_id, *CARD_TASKS),
        )

    # Snooze the sibling recall direction so the same word doesn't immediately
    # resurface in the other direction. Recall lanes only (supplemental tasks
    # have no sibling). The CASE guard only ever pushes due_at *forward* — a
    # sibling already scheduled further out (a mature card) is never pulled in.
    # The introduced_at filter no-ops for katakana loanwords (no ja2en row).
    if PAIR_SNOOZE_SECONDS > 0 and task in CARD_TASKS:
        sibling = sibling_task(task)
        if sibling is not None:
            floor = (now + timedelta(seconds=PAIR_SNOOZE_SECONDS)).isoformat()
            conn.execute(
                """
                UPDATE task_state
                   SET due_at = CASE WHEN due_at < ? THEN ? ELSE due_at END
                 WHERE word_id = ? AND task = ? AND introduced_at IS NOT NULL
                """,
                (floor, floor, word_id, sibling),
            )

    archived = False
    if correct and task in CARD_TASKS:
        archived = _maybe_archive_sprint_card(conn, word_id, now)

    return AnswerResult(
        correct=correct,
        outcome=outcome,  # type: ignore[arg-type]
        new_due_at=nxt.due_at,
        interval_days=nxt.interval_days,
        ease=nxt.ease,
        expected=expected,
        feedback=feedback,
        maturity=maturity,
        maturity_up=maturity_up,
        archived=archived,
    )


def _build_next_question(
    conn: sqlite3.Connection,
    exclude_ids: set[int],
    allowed: set[str],
    reveal: int,
    prefer_direction: Direction | None = None,
    mode: str = "mixed",
    deck_id: int | None = None,
) -> NextQuestion | None:
    """Build the next question for the recall flow, or ``None`` when nothing's due.

    Shared by ``GET /session/next`` (single card) and ``GET /session/batch``
    (bulk prefetch). ``exclude_ids`` are the words already in the client's hand
    — the on-screen card plus anything the batch has already buffered this pass.
    Recall picks skip the whole set in SQL; the interleave (cloze / listening)
    tracks are post-filtered against it, so a batch never emits two questions for
    the same word. Callers turn a ``None`` into a 204 (single) or a short/empty
    batch (bulk). ``prefer_direction`` biases the recall pick toward one direction
    (see :func:`pick_next_card`) so callers can keep the session's directions mixed.
    """
    # Type-in cloze track: the top cloze rung. A word that has graduated here
    # prefers it over selection cloze, so it's checked first. Recall mastery
    # gates entry into the cloze family but each cloze lane carries its own SRS
    # state, so contextual misses never downgrade basic word recall.
    if (
        mode != "new"
        and deck_id is None
        and "cloze" in allowed
        and has_due_cloze(conn)
        and random.random() < CLOZE_INTERLEAVE_PROBABILITY
    ):
        cp = pick_cloze_card(conn)
        if cp is not None and cp.word.id not in exclude_ids:
            cloze_question = _build_cloze_question(conn, cp)
            if cloze_question is not None:
                return cloze_question

    # Selection cloze track: the recognition rung below type-in cloze. Same
    # interleave shape; its own SRS lane and probability knob.
    if (
        mode != "new"
        and deck_id is None
        and "cloze_choice" in allowed
        and has_due_cloze_choice(conn)
        and random.random() < CLOZE_CHOICE_INTERLEAVE_PROBABILITY
    ):
        ccp = pick_cloze_choice_card(conn)
        if ccp is not None and ccp.word.id not in exclude_ids:
            cc_question = _build_cloze_choice_question(conn, ccp)
            if cc_question is not None:
                return cc_question

    # Listening interleaver: with low probability swap in a due listening
    # card before the recall picker fires. Order matters — checking
    # `has_due_listening` first means we don't spend the random budget
    # rolling for a card type that has no candidates.
    if (
        mode != "new"
        and deck_id is None
        and "sentence_listen" in allowed
        and has_due_listening(conn)
        and random.random() < LISTENING_INTERLEAVE_PROBABILITY
    ):
        lp = pick_listening_card(conn)
        if lp is not None and lp.word.id not in exclude_ids:
            return _build_listening_question(lp)

    pick = pick_next_card(
        conn,
        exclude_ids=exclude_ids,
        prefer_direction=prefer_direction,
        mode=mode,
        deck_id=deck_id,
    )
    # pick_next_card falls back to an already-held card when nothing else is
    # available (better a repeat than a dead round); for us that means "nothing
    # fresh", so treat an excluded pick as end-of-stream — the single route 204s,
    # the batch just stops early rather than handing back a duplicate.
    if pick is None or pick.word.id in exclude_ids:
        return None
    target = pick.word
    direction: Direction = pick.direction  # type: ignore[assignment]
    intro = (
        WordIntro(kana=target.kana, english=target.english, kanji=target.kanji)
        if pick.just_introduced
        else None
    )
    streak = consecutive_failures(conn, target.id, direction)

    # New card or active leech surfacing — nudge the sentence prefetch
    # worker. The worker scans for any uncached words and fills them; if
    # this card is already cached, the wake is a cheap no-op.
    if (pick.just_introduced or streak > 0) and target.kind == "word":
        gemini.schedule_prefetch()

    # New-card auto-reveal: ship the cached example sentence + mnemonic inline
    # on a first sighting so the intro card teaches before it quizzes. Strictly
    # cache-only (get_cached_sentence) — generating here would add seconds to
    # every new-card fetch; on a miss the client fetches on demand as before.
    reveal_sentence: SentenceOut | None = None
    if pick.just_introduced and reveal:
        cached = gemini.get_cached_sentence(conn, target)
        if cached is not None:
            reveal_sentence = SentenceOut(
                word_id=target.id,
                japanese=cached.japanese,
                english=cached.english,
                mnemonic=cached.mnemonic,
                japanese_ruby=furigana_segments(cached.japanese),
            )

    rng = random.Random()
    prompt = target.english if direction == "en2ja" else target.kana

    type_window = _type_window_ms(_expected_keystrokes(direction, target))

    # Natural promotion rule: brand-new card, fewer than one rep landed, or ease
    # has slipped below the promotion bar -> MC (recognition); anything else ->
    # type-in (recall). The answer-type toggles then override:
    #   * both core modes on  -> natural rule
    #   * type-in off         -> always MC (recognition-only practice)
    #   * MC off              -> always type-in, even brand-new cards (the
    #                            user explicitly opted out of recognition)
    # The client guarantees at least one core mode is on; if somehow neither is,
    # we fall back to type-in so recall still functions.
    natural_mc = (
        pick.just_introduced
        or pick.repetitions < 1
        or pick.ease < EASE_PROMOTION_THRESHOLD
    )
    mc_ok = "mc" in allowed
    type_ok = "type" in allowed
    if mc_ok and type_ok:
        use_mc = natural_mc
    elif type_ok:
        use_mc = False
    else:
        use_mc = mc_ok
    if target.kind == "sentence":
        use_mc = False

    if use_mc:
        choices = build_choices(
            conn,
            target,
            direction=direction,
            target_interval_days=pick.interval_days,
            rng=rng,
        )
        labels = (
            [c.kana for c in choices]
            if direction == "en2ja"
            else [c.english for c in choices]
        )
        correct_index = next(i for i, c in enumerate(choices) if c.id == target.id)
        return NextQuestion(
            word_id=target.id,
            direction=direction,
            prompt=prompt,
            mode="mc",
            choices=labels,
            correct_index=correct_index,
            introduction=intro,
            kanji=target.kanji,
            failure_streak=streak,
            time_limit_ms=MC_WINDOW_MS,
            sentence=reveal_sentence,
        )

    return NextQuestion(
        word_id=target.id,
        direction=direction,
        prompt=prompt,
        mode="type",
        choices=[],
        correct_index=0,
        introduction=intro,
        kanji=target.kanji,
        kind=target.kind,
        failure_streak=streak,
        time_limit_ms=type_window,
        sentence=reveal_sentence,
    )


def _parse_pool(pool: str | None) -> str:
    """Session flavour for the picker: 'review' | 'new' | 'mixed' (default)."""
    return pool if pool in ("review", "new", "mixed") else "mixed"


@router.get("/session/next", response_model=NextQuestion)
def next_question(
    exclude: str | None = None,
    modes: str | None = None,
    reveal: int = 1,
    pool: str | None = None,
    deck: int | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> NextQuestion | Response:
    """Return the next question, or 204 when no cards are due.

    ``exclude`` is ``"<word_id>:<direction>"`` of a question already on
    screen — used by the client to prefetch the *following* question
    without risking a duplicate. A bare word_id is also accepted for
    backward compatibility with older clients.

    ``modes`` is the client's answer-type allow-list (see :func:`_parse_modes`).
    A disabled supplemental track is simply skipped; disabling a core recall
    mode (mc / type) collapses every recall card onto the mode that's left.
    """
    parsed_exclude = _parse_exclude(exclude)
    exclude_ids = {parsed_exclude} if parsed_exclude is not None else set()
    question = _build_next_question(
        conn, exclude_ids, _parse_modes(modes), reveal,
        prefer_direction=_balanced_direction(conn),
        mode=_parse_pool(pool),
        deck_id=deck,
    )
    if question is None:
        return Response(status_code=204)
    return question


@router.get("/session/batch", response_model=QuestionBatch)
def question_batch(
    n: int = 4,
    exclude: str | None = None,
    modes: str | None = None,
    reveal: int = 1,
    pool: str | None = None,
    deck: int | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> QuestionBatch:
    """Return up to ``n`` distinct upcoming questions in one round-trip.

    The client keeps a small buffer of these so answering never blocks on a
    per-card network round-trip (important on flaky mobile links). Semantically
    it's just ``/session/next`` run ``n`` times with each returned word folded
    into the exclude set, so the batch is duplicate-free and — exactly like the
    existing one-card prefetch, just deeper — commits new-word introductions up
    to ``n`` cards ahead. ``exclude`` (comma-separated word_ids) seeds the set
    with whatever the client already holds so a refill never repeats a buffered
    card. Returns fewer than ``n`` (or empty) once the deck runs dry.
    """
    allowed = _parse_modes(modes)
    exclude_ids = _parse_exclude_set(exclude)
    picker_mode = _parse_pool(pool)
    capped = max(1, min(n, 12))
    # Alternate the preferred direction across the batch (seeded from recent
    # history) so a single buffer fill mixes recognition and production instead
    # of front-loading one direction — the balance survives from card to card
    # even though reviews aren't written until the user answers.
    lead = _balanced_direction(conn)
    questions: list[NextQuestion] = []
    for i in range(capped):
        prefer = lead if i % 2 == 0 else _other_direction(lead)
        question = _build_next_question(
            conn, exclude_ids, allowed, reveal, prefer_direction=prefer,
            mode=picker_mode, deck_id=deck,
        )
        if question is None:
            break
        questions.append(question)
        exclude_ids.add(question.word_id)
    return QuestionBatch(questions=questions)


@router.get("/session/drill", response_model=NextQuestion)
def drill_question(
    word_id: int,
    direction: Direction | None = None,
    pool_ids: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> NextQuestion:
    """Build an MC question for a specific (word, direction), no side effects.

    Used by the round-summary "drill misses" flow — pure retrieval practice
    on cards the user just got wrong, with no SRS update, no review record,
    no round-counter movement. Always MC so the user isn't ambushed by
    type-in mode during a remediation pass.

    ``pool_ids`` is the comma-separated set of the round's other missed words;
    the choice builder seats a couple of them as distractors so the burndown
    drills the exact cluster the user is confusing (see :func:`build_choices`).
    """
    row = conn.execute(
        "SELECT * FROM words WHERE id = ? AND ignored_at IS NULL", (word_id,)
    ).fetchone()
    if row is None:
        # Word gone or ignored (a miss the user ignored before the burndown):
        # 404 so the client's drill loop skips it instead of drilling it.
        raise HTTPException(status_code=404, detail="word not found or ignored")
    target = word_from_row(row)
    rng = random.Random()
    chosen_dir: Direction = direction or ("en2ja" if rng.random() < 0.5 else "ja2en")
    prompt = target.english if chosen_dir == "en2ja" else target.kana
    cs = conn.execute(
        "SELECT interval_days FROM task_state WHERE word_id = ? AND task = ?",
        (word_id, chosen_dir),
    ).fetchone()
    target_interval = cs["interval_days"] if cs else 0.0
    prefer_ids = _parse_exclude_set(pool_ids) - {word_id}
    choices = build_choices(
        conn,
        target,
        direction=chosen_dir,
        target_interval_days=target_interval,
        prefer_ids=prefer_ids or None,
        rng=rng,
    )
    labels = [c.kana for c in choices] if chosen_dir == "en2ja" else [c.english for c in choices]
    correct_index = next(i for i, c in enumerate(choices) if c.id == target.id)
    return NextQuestion(
        word_id=target.id,
        direction=chosen_dir,
        prompt=prompt,
        mode="mc",
        choices=labels,
        correct_index=correct_index,
        kanji=target.kanji,
        failure_streak=consecutive_failures(conn, target.id, chosen_dir),
        time_limit_ms=MC_WINDOW_MS,
    )


@router.get("/words/{word_id}/sentence", response_model=SentenceOut)
def word_sentence(
    word_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> SentenceOut:
    """Return a cached LLM-generated example sentence, generating on miss.

    Called from the answer-reveal screen — the user has already committed an
    answer, so revealing the word in context is pure post-hoc reinforcement
    and can't function as a hint. 404 if the word doesn't exist; 503 if
    Gemini isn't configured (the UI hides the button in that case but a
    direct API hit should still get a clean error).
    """
    row = conn.execute("SELECT * FROM words WHERE id = ?", (word_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="word not found")
    word = word_from_row(row)
    if word.kind == "sentence":
        # The card IS a sentence; an example sentence for it is noise and a
        # wasted model call.
        raise HTTPException(status_code=404, detail="sentence cards have no example")
    try:
        sentence = gemini.get_or_create_sentence(conn, word)
    except gemini.GeminiUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception:
        log.exception("sentence generation failed for word_id=%s", word_id)
        raise HTTPException(status_code=502, detail="sentence generation failed")
    return SentenceOut(
        word_id=word.id,
        japanese=sentence.japanese,
        english=sentence.english,
        mnemonic=sentence.mnemonic,
        japanese_ruby=furigana_segments(sentence.japanese),
    )


@router.get("/session/upcoming")
def upcoming(
    n: int = 25,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, list[int]]:
    """Peek at the next ``n`` word IDs without consuming them (audio prefetch)."""
    capped = max(0, min(n, 100))
    return {"word_ids": peek_upcoming(conn, capped)}


@router.get("/session/match-batch", response_model=MatchBatch)
def match_batch(
    n: int = 6,
    exclude_ids: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> MatchBatch:
    """Return a batch of recall cards for the continuous-match game.

    Draws only already-introduced words (see :func:`pick_match_batch`) so every
    tile is recordable via ``POST /session/answer`` and the game can never mint
    a new card. ``exclude_ids`` is the comma-separated list of word ids already
    on the player's board so a refill doesn't duplicate a visible tile. The
    batch is its own presentation of the recall lanes — it reuses the normal
    answer/SRS write path unchanged, so scheduling stays identical to MC.
    """
    capped = max(1, min(n, 12))
    excluded = _parse_exclude_set(exclude_ids)
    picks = pick_match_batch(conn, capped, exclude_ids=excluded)
    tiles: list[MatchTile] = []
    for pick in picks:
        target = pick.word
        direction = pick.direction
        if direction == "en2ja":
            prompt, answer = target.english, target.kana
        else:
            prompt, answer = target.kana, target.english
        tiles.append(
            MatchTile(
                word_id=target.id,
                direction=direction,
                prompt=prompt,
                answer=answer,
                kanji=target.kanji,
                failure_streak=consecutive_failures(conn, target.id, direction),
            )
        )
    return MatchBatch(tiles=tiles)


@router.post("/session/answer", response_model=AnswerResult)
def submit_answer(
    payload: AnswerIn,
    conn: sqlite3.Connection = Depends(get_conn),
) -> AnswerResult:
    """Record the user's answer and update the card's per-direction SRS state."""
    word_row = conn.execute(
        "SELECT * FROM words WHERE id = ?", (payload.word_id,)
    ).fetchone()
    if word_row is None:
        raise HTTPException(status_code=404, detail="word not found")
    word = word_from_row(word_row)

    # Listening mode short-circuits the per-direction recall task path —
    # listening progress lives on its own task row so it doesn't distort the
    # recall ease/interval bookkeeping.
    if payload.mode == "sentence_listen":
        return _submit_listening_answer(conn, payload, word)
    if payload.mode == "cloze":
        return _submit_cloze_answer(conn, payload, word)
    if payload.mode == "cloze_choice":
        return _submit_cloze_choice_answer(conn, payload, word)

    cs_row = conn.execute(
        "SELECT * FROM task_state WHERE word_id = ? AND task = ?",
        (payload.word_id, payload.direction),
    ).fetchone()
    if cs_row is None:
        raise HTTPException(status_code=404, detail="task_state not found")

    if payload.gave_up:
        outcome: str = "gave_up"
        correct = False
        feedback = None
    elif payload.timed_out:
        outcome = "timeout"
        correct = False
        feedback = None
    elif payload.typed_answer is not None:
        if word.kind == "sentence":
            grade = _grade_sentence_answer(conn, word, payload.typed_answer)
        else:
            grade = _grade_word_typed_answer(
                conn,
                word,
                payload.typed_answer,
                payload.direction,
                alternate_task=payload.direction,
            )
        correct = grade.correct
        outcome = grade.outcome
        feedback = grade.feedback
    else:
        correct = payload.chosen_index == payload.correct_index
        outcome = "correct" if correct else "incorrect"
        feedback = None

    if payload.typed_answer is not None:
        window_ms = _type_window_ms(_expected_keystrokes(payload.direction, word))
    else:
        window_ms = MC_WINDOW_MS

    if not correct:
        gemini.schedule_prefetch()

    expected = word.kana if payload.direction == "en2ja" else word.english
    return _record_task_result(
        conn,
        word_id=word.id,
        task=payload.direction,
        review_direction=payload.direction,
        payload=payload,
        state_row=cs_row,
        correct=correct,
        outcome=outcome,
        expected=expected,
        feedback=feedback,
        window_ms=window_ms,
    )


def _submit_listening_answer(
    conn: sqlite3.Connection,
    payload: AnswerIn,
    word: Word,
) -> AnswerResult:
    """Grade + SRS-update a listening (sentence_listen) submission.

    Pulls the cached sentence fresh from ``sentence_cache`` rather than
    trusting the client-supplied ``expected_translation`` — the round
    trip from /next is the same process, so the values match, but the
    DB read costs ~nothing and removes a trust dependency from the
    grading path.

    The sentence-listening task row is created on first review (mirrors the
    "introducing a listening card" flow in :func:`pick_listening_card`).
    """
    sc_row = conn.execute(
        """
        SELECT japanese, english FROM sentence_cache
         WHERE word_id = ? AND japanese IS NOT NULL AND japanese != ''
         ORDER BY id ASC LIMIT 1
        """,
        (word.id,),
    ).fetchone()
    if sc_row is None:
        # The card was served but its sentence cache row vanished between
        # /next and /answer — extremely rare but not crashable.
        raise HTTPException(status_code=409, detail="sentence cache missing")
    japanese = sc_row["japanese"]
    reference_english = sc_row["english"] or ""

    feedback: str | None = None
    typed = (payload.typed_answer or "").strip()

    if payload.gave_up:
        outcome: str = "gave_up"
        correct = False
    elif typed:
        # Cache check first: if Gemini has previously blessed an answer
        # variant for this sentence, the user's normalized input might
        # match it directly — skip the round-trip. The alternates that
        # populate this table come from grade_translation's curated
        # list, same persistence shape as the recall grader.
        normalized = normalize_english(typed)
        grade = None
        if normalized and lookup_alternate(conn, word.id, TASK_SENTENCE_LISTEN, normalized):
            correct = True
        else:
            try:
                grade = gemini.grade_translation(
                    japanese, reference_english, typed,
                )
            except gemini.GeminiUnavailable:
                # No grader available — fall back to a strict-equality
                # check so the answer pipeline still completes, but
                # flag as incorrect for anything that doesn't match.
                grade = None
            if grade is None:
                correct = normalized == normalize_english(reference_english)
            else:
                correct = grade.verdict in ("correct", "accept")
                feedback = grade.explanation or None
                # Persist Gemini's curated alternates the same way the
                # recall path does. Both "correct" and "accept" are
                # passes that mean "this is a valid translation of
                # this sentence," so both should cache.
                if correct and grade.alternates:
                    _save_alternates(
                        conn, word.id, TASK_SENTENCE_LISTEN, grade.alternates,
                    )
        outcome = "correct" if correct else "incorrect"
    elif payload.timed_out:
        outcome = "timeout"
        correct = False
    else:
        # Empty submission without explicit gave_up/timed_out — treat
        # as incorrect so an erroneous client send can't pass.
        correct = False
        outcome = "incorrect"

    window_ms = _listen_window_ms(reference_english)
    ls_row = conn.execute(
        "SELECT * FROM task_state WHERE word_id = ? AND task = ?",
        (word.id, TASK_SENTENCE_LISTEN),
    ).fetchone()

    return _record_task_result(
        conn,
        word_id=word.id,
        task=TASK_SENTENCE_LISTEN,
        review_direction=TASK_SENTENCE_LISTEN,
        payload=payload,
        state_row=ls_row,
        correct=correct,
        outcome=outcome,
        expected=reference_english,
        feedback=feedback,
        window_ms=window_ms,
    )


def _submit_cloze_answer(
    conn: sqlite3.Connection,
    payload: AnswerIn,
    word: Word,
) -> AnswerResult:
    """Grade + SRS-update a cloze submission without touching recall state."""
    cloze_sentence: str | None = None
    cloze_expected = payload.cloze_expected
    cloze = _cloze_for_word(conn, word, gemini.DEFAULT_MODEL)
    if cloze is not None:
        cloze_sentence, cloze_expected = cloze

    feedback: str | None = None
    if payload.gave_up:
        outcome: str = "gave_up"
        correct = False
        feedback = None
    elif payload.timed_out:
        outcome = "timeout"
        correct = False
        feedback = None
    elif payload.typed_answer is not None:
        grade = _grade_word_typed_answer(
            conn,
            word,
            payload.typed_answer,
            "en2ja",
            alternate_task=TASK_CLOZE,
            cloze_expected=cloze_expected,
            cloze_sentence=cloze_sentence,
        )
        correct = grade.correct
        outcome = grade.outcome
        feedback = grade.feedback
    else:
        correct = False
        outcome = "incorrect"
        feedback = None

    # Mirror the question's countdown (see _build_cloze_question) so the
    # SRS "fast answer" bonus is measured against the same window the user saw.
    window_ms = CLOZE_WINDOW_MS

    cls_row = conn.execute(
        "SELECT * FROM task_state WHERE word_id = ? AND task = ?",
        (word.id, TASK_CLOZE),
    ).fetchone()

    if not correct:
        gemini.schedule_prefetch()

    return _record_task_result(
        conn,
        word_id=word.id,
        task=TASK_CLOZE,
        review_direction=TASK_CLOZE,
        payload=payload,
        state_row=cls_row,
        correct=correct,
        outcome=outcome,
        expected=cloze_expected or word.kana,
        feedback=feedback,
        window_ms=window_ms,
    )


def _submit_cloze_choice_answer(
    conn: sqlite3.Connection,
    payload: AnswerIn,
    word: Word,
) -> AnswerResult:
    """Grade + SRS-update a selection cloze submission on its own task lane.

    Recognition like MC: graded on the chosen index against the correct index
    the client relays back (same "us to us" trust boundary the MC path uses).
    Recorded on the ``cloze_choice`` task so a contextual-recognition miss never
    perturbs the type-in cloze or base recall lanes. No Gemini round-trip — the
    answer is a fixed multiple choice, not free text.
    """
    # The surface form is only needed for the post-answer reveal; fall back to
    # the dictionary kana if the cached sentence vanished between /next and
    # /answer (extremely rare, but not crashable).
    cloze = _cloze_for_word(conn, word, gemini.DEFAULT_MODEL)
    expected = cloze[1] if cloze is not None else word.kana

    if payload.gave_up:
        outcome: str = "gave_up"
        correct = False
    elif payload.timed_out:
        outcome = "timeout"
        correct = False
    elif payload.chosen_index is not None and payload.correct_index is not None:
        correct = payload.chosen_index == payload.correct_index
        outcome = "correct" if correct else "incorrect"
    else:
        correct = False
        outcome = "incorrect"

    ccs_row = conn.execute(
        "SELECT * FROM task_state WHERE word_id = ? AND task = ?",
        (word.id, TASK_CLOZE_CHOICE),
    ).fetchone()

    if not correct:
        gemini.schedule_prefetch()

    return _record_task_result(
        conn,
        word_id=word.id,
        task=TASK_CLOZE_CHOICE,
        review_direction=TASK_CLOZE_CHOICE,
        payload=payload,
        state_row=ccs_row,
        correct=correct,
        outcome=outcome,
        expected=expected,
        feedback=None,
        window_ms=CLOZE_CHOICE_WINDOW_MS,
    )


def _update_word_gloss(
    conn: sqlite3.Connection,
    word_id: int,
    old_gloss: str,
    new_gloss: str,
) -> None:
    """Replace ``words.english`` with a clarified gloss from the LLM.

    Only fires on the en2ja ambiguous-gloss "correct" path — when Gemini
    accepted the student's answer because the prompt itself was vague,
    and proposed a tightened gloss that still fits the target. Persisting
    it here means the next en2ja card for this word shows the precise
    prompt and the same collision doesn't keep round-tripping to the LLM.

    No-op if the new gloss is empty or identical to the old one. We log
    every change so a Gemini regression that mass-edits glosses is
    visible in the access log.
    """
    cleaned = new_gloss.strip()
    if not cleaned or cleaned == old_gloss:
        return
    conn.execute(
        "UPDATE words SET english = ? WHERE id = ?",
        (cleaned, word_id),
    )
    log.info(
        "gloss-clarified word_id=%d %r -> %r",
        word_id, old_gloss, cleaned,
    )


def _save_alternates(
    conn: sqlite3.Connection,
    word_id: int,
    direction: str,
    alternates: tuple[str, ...],
) -> None:
    """Persist Gemini-blessed answer variants so future repeats short-circuit.

    Variants are normalized through the same path as the grader so an
    exact match against the user's normalized typed input wins on a later
    submission without another LLM round-trip.
    """
    norm = normalize_kana if direction in ("en2ja", TASK_CLOZE) else normalize_english
    for alt in alternates:
        cleaned = norm(alt)
        if not cleaned:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO word_alternates
              (word_id, direction, alternate, source, added_at)
            VALUES (?, ?, ?, 'gemini', datetime('now'))
            """,
            (word_id, direction, cleaned),
        )
