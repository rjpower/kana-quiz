"""Session queue composition and distractor selection.

Two responsibilities:

* :func:`pick_next_card` decides what (word, direction) pair the user sees
  next. It prefers overdue reviews but slips in a small ration of
  never-introduced words so the deck grows as the user keeps pace. New
  cards are drawn deck-level-first (lowest ``decks.level`` wins). If
  nothing is due and no fresh words remain it reuses the soonest-due
  introduced card so study never stalls.
* :func:`build_choices` returns a length-4 list containing the target plus
  three plausible distractors. The distractor mix is adaptive: mature targets
  get more phonetically-confusable distractors (same first kana, same mora
  count) while newer targets stay on semantic tag-mates so learning isn't
  sabotaged. The mix falls back to random words when a bucket's pool is too
  thin.
"""

import os
import random
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from kana_quiz.grading import gloss_core
from kana_quiz.models import Word, is_katakana_only, word_from_row
from kana_quiz.task_state import (
    CARD_TASKS,
    TASK_CLOZE,
    TASK_CLOZE_CHOICE,
    TASK_EN2JA,
    TASK_JA2EN,
    TASK_SENTENCE_LISTEN,
    Task,
)

Direction = Literal["en2ja", "ja2en"]

NEW_WORD_TARGET_AT_ONCE = 3
NEW_WORD_LOOKAHEAD_MINUTES = 5
DUE_QUEUE_LIMIT = 40
RECENT_REVIEW_LOOKBACK = 30

# Backlog gate for new-card introduction. We refuse to introduce a brand-new
# word while the user already has this many *distinct words in active learning*
# — recall cards that haven't stabilized yet (interval < 1 day: freshly
# introduced or relearning after a miss). This is the pile the user is still
# working through; piling new words on top of it is how a deck becomes
# unmanageable. Counting the learning pile (rather than just "due right now")
# matters because the soon-due relearn batch from a rough round isn't overdue
# yet but is very much backlog. 0 disables the gate. Env-tunable.
NEW_WORD_BACKLOG_LIMIT = int(os.environ.get("KANA_NEW_WORD_BACKLOG_LIMIT", "20"))

# Mora-count threshold: Japanese words that read in the same number of beats
# tend to be rhythmically confusable. Small kana (ゃゅょっ) don't count as
# their own mora.
_SMALL_KANA = set("ゃゅょぁぃぅぇぉっャュョァィゥェォッ")

# Maturity threshold for distractor mixing, in days of current SRS interval.
MATURE_INTERVAL_DAYS = 14.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _mora_count(kana: str) -> int:
    return sum(1 for c in kana if c not in _SMALL_KANA)


# Verb detection uses the standard dictionary "to X" English-gloss convention.
# A kana-ending heuristic (う-row final char) is tempting but noisy: many
# nouns end in る/う (くるま "car", そら "sky", はる "spring").
def _is_verb(word: Word) -> bool:
    return word.english.lower().startswith("to ")


def _same_pos(a: Word, b: Word) -> bool:
    """Whether two words share part of speech for distractor filtering.

    Only verb/non-verb is distinguished — that's the split that actually
    trivializes a quiz (a "to run" option next to "cat, dog, bird" can be
    eliminated on sight). Finer POS buckets aren't worth the false-positive
    risk without a morphological analyzer.
    """
    return _is_verb(a) == _is_verb(b)


@dataclass(frozen=True)
class _Quota:
    """How many of each distractor flavor to try before falling back to random."""

    phonetic: int
    tag: int

    @property
    def total(self) -> int:
        return self.phonetic + self.tag


def _quota_for(target_interval: float, direction: str) -> _Quota:
    """Scale distractor difficulty to direction + target SRS interval.

    Phonetic distractors only add difficulty when the *choices are kana*
    (en2ja). For ja2en the user picks among English glosses, so kana
    similarity is wasted — semantic confusion is the only axis that matters,
    so we stay tag-heavy regardless of maturity.

    For en2ja we push phonetic confusables hard from day one: tag-mate
    distractors can be solved by elimination on the English side without
    engaging the kana, which defeats the point of the kana-recognition drill.
    """
    if direction == "ja2en":
        return _Quota(phonetic=0, tag=3)
    if target_interval >= MATURE_INTERVAL_DAYS:
        return _Quota(phonetic=3, tag=0)
    return _Quota(phonetic=2, tag=1)


@dataclass(frozen=True)
class CardPick:
    """Result of :func:`pick_next_card` — word + direction + first-sighting flag."""

    word: Word
    direction: Direction
    interval_days: float  # used by build_choices for distractor mix
    repetitions: int  # used by routes to decide MC vs type-in
    just_introduced: bool
    ease: float  # used by routes for ease-based MC->type-in promotion


@dataclass(frozen=True)
class ListeningPick:
    """Result of :func:`pick_listening_card` - word + cached sentence + SRS bits.

    ``ease``/``repetitions``/``interval_days`` are the task_state row
    for an existing listening card, or sentinel defaults (2.5 / 0 / 0)
    when introducing a new listening card. ``introduced_at`` is None for
    new introductions and gets stamped by the caller on the first review.
    """

    word: Word
    sentence_ja: str
    sentence_en: str
    ease: float
    repetitions: int
    interval_days: float
    introduced_at: datetime | None


@dataclass(frozen=True)
class ClozePick:
    """Result of :func:`pick_cloze_card` - word plus cloze-specific SRS bits.

    ``ease``/``repetitions``/``interval_days`` are the task_state row for
    existing cloze cards, or sentinel defaults (2.5 / 0 / 0) when introducing a
    new cloze card. Base recall tasks only unlock this lane; cloze reviews do
    not alter recall state.
    """

    word: Word
    ease: float
    repetitions: int
    interval_days: float
    introduced_at: datetime | None


@dataclass(frozen=True)
class SupplementalTaskConfig:
    """Rules for a task lane that unlocks from existing recall mastery."""

    task: Task
    state_alias: str
    unlock_tasks: tuple[Task, ...]
    ease_threshold: float
    reps_threshold: int
    requires_sentence_audio: bool = False


@dataclass(frozen=True)
class SupplementalTaskPick:
    """Generic task_state-backed pick for a supplemental task lane."""

    task: Task
    word: Word
    ease: float
    repetitions: int
    interval_days: float
    introduced_at: datetime | None
    sentence_ja: str | None = None
    sentence_en: str | None = None


# Mastery floor for promoting a word into the listening track. The card
# has to have at least two correct reps AND a non-degraded ease — we don't
# want to ambush the user with audio comprehension on a word their recall
# is shaky on.
LISTENING_EASE_THRESHOLD = 2.5
LISTENING_REPS_THRESHOLD = 2

# Entry point into the cloze family. The *selection* cloze lane (cloze_choice)
# unlocks from en→ja recall mastery at this bar — the same bar the type-in
# cloze lane used to gate on. The learner now meets contextual cloze first as
# recognition (pick the word) before production (type the word).
CLOZE_UNLOCK_EASE_THRESHOLD = 2.6
CLOZE_UNLOCK_REPS_THRESHOLD = 3

# Mastery floor for promoting a word from the selection cloze lane up to the
# type-in cloze lane. The ladder is recall -> cloze_choice -> cloze: typed
# contextual production only unlocks once the learner can already pick the
# right word in context. Reps bar is lower than the recall->cloze_choice bar
# because the learner has already cleared the harder recall ramp.
CLOZE_TYPED_UNLOCK_EASE_THRESHOLD = 2.6
CLOZE_TYPED_UNLOCK_REPS_THRESHOLD = 2

CLOZE_CHOICE_TASK = SupplementalTaskConfig(
    task=TASK_CLOZE_CHOICE,
    state_alias="ccs",
    unlock_tasks=(TASK_EN2JA,),
    ease_threshold=CLOZE_UNLOCK_EASE_THRESHOLD,
    reps_threshold=CLOZE_UNLOCK_REPS_THRESHOLD,
)
CLOZE_TASK = SupplementalTaskConfig(
    task=TASK_CLOZE,
    state_alias="cls",
    unlock_tasks=(TASK_CLOZE_CHOICE,),
    ease_threshold=CLOZE_TYPED_UNLOCK_EASE_THRESHOLD,
    reps_threshold=CLOZE_TYPED_UNLOCK_REPS_THRESHOLD,
)
SENTENCE_LISTEN_TASK = SupplementalTaskConfig(
    task=TASK_SENTENCE_LISTEN,
    state_alias="ls",
    unlock_tasks=CARD_TASKS,
    ease_threshold=LISTENING_EASE_THRESHOLD,
    reps_threshold=LISTENING_REPS_THRESHOLD,
    requires_sentence_audio=True,
)


def _hydrate_word(conn: sqlite3.Connection, word_id: int) -> Word:
    row = conn.execute("SELECT * FROM words WHERE id = ?", (word_id,)).fetchone()
    return word_from_row(row)


def pick_next_card(
    conn: sqlite3.Connection,
    exclude_ids: set[int] | None = None,
    prefer_direction: Direction | None = None,
    mode: str = "mixed",
    deck_id: int | None = None,
) -> CardPick | None:
    """Pick the next (word, direction) to show, or ``None`` if nothing's available.

    ``mode`` selects the session flavour:

    * ``"mixed"`` (default) — the classic behaviour: serve due cards, and
      trickle in brand-new words when the active pile is thin.
    * ``"review"`` — reviews only. Serve due cards; NEVER introduce a fresh
      word. Returns ``None`` the moment nothing is due, so a review round can't
      stall waiting on new material.
    * ``"new"`` — a focused learn session. Skip due cards entirely and only
      introduce never-seen words (bypassing the backlog gate); ``None`` once the
      deck has no unseen words left.

    Due-now cards (introduced, ``due_at <= now``) take priority in mixed/review.
    In mixed, if fewer than :data:`NEW_WORD_TARGET_AT_ONCE` cards are due in the
    next few minutes we introduce a brand-new word, debuting it in the
    *recognition* direction (ja→en) so meaning is learned before production
    (see the receptive→productive progression).

    Handing out a new word creates both recall rows but leaves their
    ``introduced_at`` NULL; the answer route stamps them on the first answer.
    This matters because the picker runs *ahead* of the user — ``/session/batch``
    deals up to a dozen cards into a client-side buffer, and a round abandoned
    mid-buffer used to leave the undealt remainder marked introduced-and-due.
    Those words then surfaced in the next **review** session, which is supposed
    to serve only material the user has actually met. Deferring the stamp keeps
    "introduced" meaning "the user answered it at least once".

    ``exclude_ids`` are word_ids the client already has in hand (the on-screen
    card, plus anything already buffered by the bulk-prefetch batch). Both
    directions of each are skipped — surfacing the same word back-to-back, even
    on the opposite direction, feels duplicative, and a batch must never hand
    back the same word twice.

    ``prefer_direction`` biases the *due* pick toward one recall direction when
    that direction also has a card due. All due cards are overdue, so serving
    the preferred one instead of the globally-soonest costs nothing in SRS terms
    but lets the caller interleave recognition/production within a session
    rather than draining one direction's cohort first (the "all ja→en today"
    monotony). Ignored for new-word intros and the caught-up reuse fallback.

    ``deck_id`` scopes the whole pick — due cards, the backlog gate, and
    new-word intros — to one deck, so a deck session studies that deck and
    nothing else.
    """
    now = _now()
    now_iso = now.isoformat()
    deck_clause = "" if deck_id is None else " AND w.deck_id = ?"
    deck_params: tuple[int, ...] = () if deck_id is None else (deck_id,)

    excl_clause, excl_params = _word_set_exclusion_clause(
        "ts.word_id", exclude_ids or set()
    )
    # The new-word query selects from `words`, so it needs the exclusion keyed on
    # a different column. It genuinely needs one now: a word the picker has
    # handed out but the user hasn't answered is still "unseen" (see below), so
    # without this a single batch could deal the same fresh word n times.
    new_excl_clause, new_excl_params = _word_set_exclusion_clause(
        "w.id", exclude_ids or set()
    )

    due_row = conn.execute(
        f"""
        SELECT ts.* FROM task_state ts
          JOIN words w ON w.id = ts.word_id
         WHERE ts.task IN (?, ?)
           AND ts.introduced_at IS NOT NULL AND ts.due_at <= ?
           AND w.ignored_at IS NULL{excl_clause}{deck_clause}
         ORDER BY ts.due_at ASC
         LIMIT 1
        """,
        (*CARD_TASKS, now_iso, *excl_params, *deck_params),
    ).fetchone()

    # Direction balancing: if the caller asked for a direction and one is due in
    # it, serve that instead of the globally-soonest card. ``serve_row`` is what
    # the due branches below hand back; ``due_row`` still gates new-word intros
    # (it answers "is anything due at all?", which is direction-agnostic).
    serve_row = due_row
    if prefer_direction is not None and due_row is not None:
        pref_row = conn.execute(
            f"""
            SELECT ts.* FROM task_state ts
              JOIN words w ON w.id = ts.word_id
             WHERE ts.task = ?
               AND ts.introduced_at IS NOT NULL AND ts.due_at <= ?
               AND w.ignored_at IS NULL{excl_clause}{deck_clause}
             ORDER BY ts.due_at ASC
             LIMIT 1
            """,
            (prefer_direction, now_iso, *excl_params, *deck_params),
        ).fetchone()
        if pref_row is not None:
            serve_row = pref_row

    soon_iso = (now + timedelta(minutes=NEW_WORD_LOOKAHEAD_MINUTES)).isoformat()
    active_count = conn.execute(
        f"""
        SELECT COUNT(*) AS n FROM task_state ts
          JOIN words w ON w.id = ts.word_id
         WHERE ts.task IN (?, ?)
           AND ts.introduced_at IS NOT NULL AND ts.due_at <= ?
           AND w.ignored_at IS NULL{excl_clause}{deck_clause}
        """,
        (*CARD_TASKS, soon_iso, *excl_params, *deck_params),
    ).fetchone()["n"]

    def _served(row: sqlite3.Row) -> CardPick:
        return CardPick(
            word=_hydrate_word(conn, row["word_id"]),
            direction=row["task"],
            interval_days=row["interval_days"],
            repetitions=row["repetitions"],
            just_introduced=False,
            ease=row["ease"],
        )

    # Review sessions serve due cards and nothing else — no new-word intros, no
    # drilling-ahead reuse. Empty the instant nothing's due.
    if mode == "review":
        return _served(serve_row) if serve_row is not None else None

    # Mixed with a healthy due pile: serve it, don't pull in a fresh word.
    if mode != "new" and due_row is not None and active_count >= NEW_WORD_TARGET_AT_ONCE:
        return _served(serve_row)

    # Backlog gate: count distinct words still in active learning (introduced
    # recall rows that haven't reached a 1-day interval — new or relearning).
    # When that pile is large we stop pulling in fresh words and let the user
    # work down what they've already got. ``exclude`` rides along so the card
    # on screen doesn't inflate the count.
    # The backlog gate throttles *mixed* auto-introduction; a deliberate "new"
    # session bypasses it (the user is explicitly asking to learn new words).
    backlog = (
        conn.execute(
            f"""
            SELECT COUNT(DISTINCT ts.word_id) AS n FROM task_state ts
              JOIN words w ON w.id = ts.word_id
             WHERE ts.task IN (?, ?)
               AND ts.introduced_at IS NOT NULL
               AND ts.interval_days < 1.0
               AND w.ignored_at IS NULL{excl_clause}{deck_clause}
            """,
            (*CARD_TASKS, *excl_params, *deck_params),
        ).fetchone()["n"]
        if mode != "new" and NEW_WORD_BACKLOG_LIMIT > 0
        else 0
    )

    # Either nothing's due or active pool is thin — try to introduce a word
    # that has zero recall task rows yet, unless the learning backlog is full.
    # Order: lowest deck.level first (finish easier decks before harder ones),
    # then RANDOM() to sample within a level. The old id tiebreaker drained a
    # deck in import order, which front-loaded whatever the CSV happened to list
    # first (e.g. a run of katakana loanwords); random within-level keeps the
    # level progression while giving a varied mix from the get-go. A deck
    # whose pick_order is 'listed' opts back into import order, for a CSV
    # sorted by frequency.
    # "Unseen" means no recall row that has actually been *introduced* — not
    # merely no row at all. The picker seeds rows at hand-out time (it has to:
    # both directions are created together, and the answer path only ever
    # touches the direction being answered), but leaves introduced_at NULL
    # until the user answers. A word that was dealt into a prefetch buffer and
    # never reached the screen therefore stays in this pool instead of being
    # stranded with rows nothing will ever query.
    new_word_row = (
        conn.execute(
            f"""
            SELECT w.* FROM words w
             LEFT JOIN decks d ON d.id = w.deck_id
             WHERE w.ignored_at IS NULL
               AND NOT EXISTS (
               SELECT 1 FROM task_state ts
                WHERE ts.word_id = w.id AND ts.task IN ('en2ja', 'ja2en')
                  AND ts.introduced_at IS NOT NULL
             ){new_excl_clause}{deck_clause}
             ORDER BY COALESCE(d.level, 999) ASC,
                      CASE WHEN w.kind = 'sentence' OR d.pick_order = 'listed'
                           THEN w.id END,
                      RANDOM()
             LIMIT 1
            """,
            (*new_excl_params, *deck_params),
        ).fetchone()
        if mode == "new" or NEW_WORD_BACKLOG_LIMIT <= 0 or backlog < NEW_WORD_BACKLOG_LIMIT
        else None
    )

    if new_word_row is not None:
        word_id = new_word_row["id"]
        # Pure-katakana loanwords (コンピュータ, ローマ) only get an en2ja
        # card — quizzing ja→en on a phonetic transliteration of the
        # answer is busywork. We check BOTH columns: the Anki import
        # often stores the loanword's lowercased hiragana in `kana` (e.g.
        # ぱいなっぷる) and keeps the real katakana form in the `kanji`
        # slot (パイナップル). Reading either column as pure katakana is
        # enough to flag the card as loanword-only.
        kana = new_word_row["kana"] or ""
        kanji = new_word_row["kanji"] or ""
        if new_word_row["kind"] == "sentence":
            directions: tuple[Direction, ...] = (TASK_JA2EN,)
        elif is_katakana_only(kana) or is_katakana_only(kanji):
            directions = (TASK_EN2JA,)
        else:
            directions = (TASK_EN2JA, TASK_JA2EN)
        for direction in directions:
            # introduced_at stays NULL until the user answers (the answer route
            # stamps every recall row for the word). Until then these rows exist
            # only to reserve the pair of directions; every due/backlog query
            # filters on introduced_at IS NOT NULL, so an unanswered card cannot
            # leak into a review session as though it had already been studied.
            # ON CONFLICT because a previous session may have dealt this same
            # word and never had it answered.
            conn.execute(
                """
                INSERT INTO task_state
                  (word_id, task, ease, interval_days, repetitions,
                   due_at, introduced_at)
                VALUES (?, ?, 2.5, 0, 0, ?, NULL)
                ON CONFLICT(word_id, task) DO NOTHING
                """,
                (word_id, direction, now_iso),
            )
        # Recognition-first: debut a new word in ja→en (recognition) when that
        # direction exists, so the learner meets the word's meaning before being
        # asked to produce it. Katakana-only loanwords have only en→ja, so they
        # fall through to that.
        chosen_dir: Direction = TASK_JA2EN if TASK_JA2EN in directions else directions[0]
        return CardPick(
            word=word_from_row(new_word_row),
            direction=chosen_dir,
            interval_days=0.0,
            repetitions=0,
            just_introduced=True,
            ease=2.5,
        )

    # New-cards session with no unseen words left: end of stream. Never fall
    # through to reviews — this session is new-only.
    if mode == "new":
        return None

    if due_row is not None:
        return _served(serve_row)

    # Deck fully caught up and no fresh cards left. Pull the introduced card
    # with the soonest due_at so the user can keep drilling.
    reuse_row = conn.execute(
        f"""
        SELECT ts.* FROM task_state ts
          JOIN words w ON w.id = ts.word_id
         WHERE ts.task IN (?, ?)
           AND ts.introduced_at IS NOT NULL
           AND w.ignored_at IS NULL{excl_clause}{deck_clause}
         ORDER BY ts.due_at ASC
         LIMIT 1
        """,
        (*CARD_TASKS, *excl_params, *deck_params),
    ).fetchone()
    if reuse_row is not None:
        word = _hydrate_word(conn, reuse_row["word_id"])
        return CardPick(
            word=word,
            direction=reuse_row["task"],
            interval_days=reuse_row["interval_days"],
            repetitions=reuse_row["repetitions"],
            just_introduced=False,
            ease=reuse_row["ease"],
        )

    if exclude_ids:
        return pick_next_card(conn, exclude_ids=None, deck_id=deck_id)
    return None


def _word_exclusion_clause(
    alias: str, exclude: int | None
) -> tuple[str, tuple[int, ...]]:
    """Return a SQL snippet that excludes a whole word when requested."""
    if exclude is None:
        return "", ()
    return f" AND {alias}.word_id != ?", (exclude,)


def _word_set_exclusion_clause(
    column: str, ids: set[int]
) -> tuple[str, tuple[int, ...]]:
    """Return a SQL snippet excluding a *set* of word ids (``NOT IN``).

    ``column`` is a fully-qualified reference rather than a table alias: the
    word id is ``task_state.word_id`` in the due queries but ``words.id`` in
    the new-word query, and both need excluding.
    """
    if not ids:
        return "", ()
    placeholders = ",".join("?" for _ in ids)
    return f" AND {column} NOT IN ({placeholders})", tuple(ids)


def pick_match_batch(
    conn: sqlite3.Connection,
    n: int,
    exclude_ids: set[int] | None = None,
) -> list[CardPick]:
    """Pick up to ``n`` recall cards for the continuous-match game.

    Unlike :func:`pick_next_card` this **never introduces a new word** — it
    draws only from cards that already have an introduced recall row. Two
    reasons: (1) match should drill words the user has actually met, and
    (2) the answer endpoint 404s on a ``(word, direction)`` with no
    ``task_state`` row, so handing back an un-introduced card would make the
    match unrecordable. Each returned word appears at most once (a word and
    its sibling direction never share a board), and ``exclude_ids`` (the words
    already on the board) are skipped so refills don't duplicate.

    Prefers due-now cards, then falls back to the soonest-due introduced cards
    so a caught-up user can still play. Returns fewer than ``n`` (or empty)
    when the pool is exhausted — the caller renders a "caught up" board rather
    than padding with bogus tiles.
    """
    if n <= 0:
        return []
    excluded = set(exclude_ids or ())
    now_iso = _now().isoformat()
    picks: list[CardPick] = []

    def _consume(rows: list[sqlite3.Row]) -> bool:
        for row in rows:
            wid = row["word_id"]
            if wid in excluded:
                continue
            excluded.add(wid)
            picks.append(
                CardPick(
                    word=_hydrate_word(conn, wid),
                    direction=row["task"],
                    interval_days=row["interval_days"],
                    repetitions=row["repetitions"],
                    just_introduced=False,
                    ease=row["ease"],
                )
            )
            if len(picks) >= n:
                return True
        return False

    excl_clause, excl_params = _word_set_exclusion_clause("ts.word_id", excluded)
    fetch_limit = (n + len(excluded) + 1) * 2

    due_rows = conn.execute(
        f"""
        SELECT ts.* FROM task_state ts
          JOIN words w ON w.id = ts.word_id
         WHERE ts.task IN (?, ?)
           AND ts.introduced_at IS NOT NULL AND ts.due_at <= ?
           AND w.ignored_at IS NULL AND w.kind = 'word'{excl_clause}
         ORDER BY ts.due_at ASC
         LIMIT ?
        """,
        (*CARD_TASKS, now_iso, *excl_params, fetch_limit),
    ).fetchall()
    if _consume(due_rows):
        return picks

    # Recompute exclusion now that due-now words have been consumed, then top
    # up from the soonest-due introduced cards (any due_at).
    excl_clause, excl_params = _word_set_exclusion_clause("ts.word_id", excluded)
    reuse_rows = conn.execute(
        f"""
        SELECT ts.* FROM task_state ts
          JOIN words w ON w.id = ts.word_id
         WHERE ts.task IN (?, ?)
           AND ts.introduced_at IS NOT NULL
           AND w.ignored_at IS NULL AND w.kind = 'word'{excl_clause}
         ORDER BY ts.due_at ASC
         LIMIT ?
        """,
        (*CARD_TASKS, *excl_params, (n + len(excluded) + 1) * 2),
    ).fetchall()
    _consume(reuse_rows)
    return picks


def has_due_supplemental_task(
    conn: sqlite3.Connection,
    config: SupplementalTaskConfig,
    exclude: int | None = None,
) -> bool:
    """Return whether a supplemental task has a due or introducible card."""
    now_iso = _now().isoformat()
    excl_clause, excl_params = _word_exclusion_clause(config.state_alias, exclude)
    row = conn.execute(
        f"""
        SELECT 1 FROM task_state {config.state_alias}
          JOIN words w ON w.id = {config.state_alias}.word_id
         WHERE {config.state_alias}.due_at <= ?
           AND {config.state_alias}.task = ?
           AND w.ignored_at IS NULL{excl_clause}
         LIMIT 1
        """,
        (now_iso, config.task, *excl_params),
    ).fetchone()
    if row is not None:
        return True
    return _eligible_new_supplemental_word_id(conn, config, exclude=exclude) is not None


def has_due_listening(conn: sqlite3.Connection, exclude: int | None = None) -> bool:
    """Cheap probe: is at least one listening card due or ready to introduce?"""
    return has_due_supplemental_task(conn, SENTENCE_LISTEN_TASK, exclude=exclude)


def has_due_cloze(conn: sqlite3.Connection, exclude: int | None = None) -> bool:
    """Cheap probe: is at least one type-in cloze card due or introducible?"""
    return has_due_supplemental_task(conn, CLOZE_TASK, exclude=exclude)


def has_due_cloze_choice(conn: sqlite3.Connection, exclude: int | None = None) -> bool:
    """Cheap probe: is at least one selection cloze card due or introducible?"""
    return has_due_supplemental_task(conn, CLOZE_CHOICE_TASK, exclude=exclude)


def _eligible_new_supplemental_word_id(
    conn: sqlite3.Connection,
    config: SupplementalTaskConfig,
    exclude: int | None = None,
) -> int | None:
    """Return the lowest-id word that qualifies for a fresh supplemental task."""
    excl_clause = "" if exclude is None else " AND w.id != ?"
    audio_join = (
        "JOIN audio_cache ac ON ac.text = sc.japanese"
        if config.requires_sentence_audio
        else ""
    )
    task_placeholders = ",".join("?" for _ in config.unlock_tasks)
    params: tuple[object, ...] = (
        *config.unlock_tasks,
        config.ease_threshold,
        config.reps_threshold,
        config.task,
    )
    if exclude is not None:
        params = (*params, exclude)
    row = conn.execute(
        f"""
        SELECT w.id FROM words w
          JOIN task_state cs ON cs.word_id = w.id
          JOIN sentence_cache sc ON sc.word_id = w.id
          {audio_join}
         WHERE w.ignored_at IS NULL AND w.kind = 'word'
           AND cs.task IN ({task_placeholders})
           AND cs.ease >= ?
           AND cs.repetitions >= ?
           AND sc.japanese IS NOT NULL AND sc.japanese != ''
           AND NOT EXISTS (
             SELECT 1 FROM task_state existing
              WHERE existing.word_id = w.id AND existing.task = ?
           ){excl_clause}
         ORDER BY w.id ASC
         LIMIT 1
        """,
        params,
    ).fetchone()
    return row["id"] if row is not None else None


def _eligible_new_cloze_word_id(
    conn: sqlite3.Connection, exclude: int | None = None
) -> int | None:
    """Return the lowest-id word that qualifies for a fresh cloze card."""
    return _eligible_new_supplemental_word_id(conn, CLOZE_TASK, exclude=exclude)


def pick_supplemental_task(
    conn: sqlite3.Connection,
    config: SupplementalTaskConfig,
    exclude: int | None = None,
) -> SupplementalTaskPick | None:
    """Pick the next due supplemental task, or introduce a newly eligible one."""
    now_iso = _now().isoformat()
    excl_clause, excl_params = _word_exclusion_clause(config.state_alias, exclude)
    sentence_columns = (
        ", sc.japanese, sc.english" if config.requires_sentence_audio else ""
    )
    sentence_join = (
        "JOIN sentence_cache sc ON sc.word_id = "
        f"{config.state_alias}.word_id"
        if config.requires_sentence_audio
        else ""
    )
    audio_join = (
        "JOIN audio_cache ac ON ac.text = sc.japanese"
        if config.requires_sentence_audio
        else ""
    )
    due_row = conn.execute(
        f"""
        SELECT {config.state_alias}.*{sentence_columns}
          FROM task_state {config.state_alias}
          JOIN words w ON w.id = {config.state_alias}.word_id
          {sentence_join}
          {audio_join}
         WHERE {config.state_alias}.due_at <= ?
           AND {config.state_alias}.task = ?
           AND w.ignored_at IS NULL{excl_clause}
         ORDER BY {config.state_alias}.due_at ASC
         LIMIT 1
        """,
        (now_iso, config.task, *excl_params),
    ).fetchone()
    if due_row is not None:
        introduced_at = (
            datetime.fromisoformat(due_row["introduced_at"])
            if due_row["introduced_at"]
            else None
        )
        return SupplementalTaskPick(
            task=config.task,
            word=_hydrate_word(conn, due_row["word_id"]),
            ease=due_row["ease"],
            repetitions=due_row["repetitions"],
            interval_days=due_row["interval_days"],
            introduced_at=introduced_at,
            sentence_ja=due_row["japanese"] if config.requires_sentence_audio else None,
            sentence_en=(
                (due_row["english"] or "") if config.requires_sentence_audio else None
            ),
        )

    new_word_id = _eligible_new_supplemental_word_id(conn, config, exclude=exclude)
    if new_word_id is None:
        return None
    sentence_ja: str | None = None
    sentence_en: str | None = None
    if config.requires_sentence_audio:
        row = conn.execute(
            """
            SELECT japanese, english FROM sentence_cache
             WHERE word_id = ? AND japanese IS NOT NULL AND japanese != ''
             ORDER BY id ASC LIMIT 1
            """,
            (new_word_id,),
        ).fetchone()
        if row is None:
            return None
        sentence_ja = row["japanese"]
        sentence_en = row["english"] or ""
    return SupplementalTaskPick(
        task=config.task,
        word=_hydrate_word(conn, new_word_id),
        ease=2.5,
        repetitions=0,
        interval_days=0.0,
        introduced_at=None,
        sentence_ja=sentence_ja,
        sentence_en=sentence_en,
    )


def pick_cloze_card(
    conn: sqlite3.Connection, exclude: int | None = None
) -> ClozePick | None:
    """Pick the next due type-in cloze card, or introduce a newly eligible one."""
    pick = pick_supplemental_task(conn, CLOZE_TASK, exclude=exclude)
    if pick is None:
        return None
    return ClozePick(
        word=pick.word,
        ease=pick.ease,
        repetitions=pick.repetitions,
        interval_days=pick.interval_days,
        introduced_at=pick.introduced_at,
    )


def pick_cloze_choice_card(
    conn: sqlite3.Connection, exclude: int | None = None
) -> ClozePick | None:
    """Pick the next due selection cloze card, or introduce a newly eligible one.

    Shares the :class:`ClozePick` shape with the type-in cloze lane — the two
    differ only in how the question is rendered and graded, not in the SRS bits
    the picker hands back.
    """
    pick = pick_supplemental_task(conn, CLOZE_CHOICE_TASK, exclude=exclude)
    if pick is None:
        return None
    return ClozePick(
        word=pick.word,
        ease=pick.ease,
        repetitions=pick.repetitions,
        interval_days=pick.interval_days,
        introduced_at=pick.introduced_at,
    )


def _eligible_new_listening_word_id(
    conn: sqlite3.Connection,
    exclude: int | None = None,
) -> int | None:
    """Return the lowest-id word that qualifies for a fresh listening card."""
    return _eligible_new_supplemental_word_id(
        conn, SENTENCE_LISTEN_TASK, exclude=exclude
    )


def pick_listening_card(
    conn: sqlite3.Connection, exclude: int | None = None
) -> ListeningPick | None:
    """Pick the next due listening card, or None if none qualifies.

    Two-pass: prefer an introduced-already card whose ``due_at`` has
    passed; fall back to introducing a brand-new listening card on a
    word that has cleared the recall mastery floor.

    For the new-card branch the caller does NOT need to insert the
    sentence-listening ``task_state`` row. The route handler that processes
    the answer creates it lazily, mirroring cloze introductions.
    """
    pick = pick_supplemental_task(conn, SENTENCE_LISTEN_TASK, exclude=exclude)
    if pick is None or pick.sentence_ja is None:
        return None
    return ListeningPick(
        word=pick.word,
        sentence_ja=pick.sentence_ja,
        sentence_en=pick.sentence_en or "",
        ease=pick.ease,
        repetitions=pick.repetitions,
        interval_days=pick.interval_days,
        introduced_at=pick.introduced_at,
    )


def consecutive_failures(
    conn: sqlite3.Connection, word_id: int, direction: str
) -> int:
    """Failures-since-last-correct for a single (word, direction) pair.

    Used to flag leeches — a card with several misses in a row probably
    needs the user to re-read its meaning rather than keep guessing. A new
    card with zero reviews returns 0.
    """
    rows = conn.execute(
        """
        SELECT correct FROM reviews
         WHERE word_id = ? AND direction = ?
         ORDER BY id DESC
        """,
        (word_id, direction),
    ).fetchall()
    streak = 0
    for r in rows:
        if r["correct"]:
            break
        streak += 1
    return streak


def all_consecutive_failures(
    conn: sqlite3.Connection,
) -> dict[tuple[int, str], int]:
    """Return ``{(word_id, direction): streak}`` for every reviewed card.

    Cheaper than calling :func:`consecutive_failures` per card for the stats
    page: one full table scan instead of N small ones.
    """
    rows = conn.execute(
        """
        SELECT word_id, direction, correct FROM reviews
         ORDER BY word_id, direction, id DESC
        """
    ).fetchall()
    out: dict[tuple[int, str], int] = {}
    cur_key: tuple[int, str] | None = None
    cur_streak = 0
    counting = False
    for row in rows:
        key = (row["word_id"], row["direction"])
        if key != cur_key:
            if cur_key is not None:
                out[cur_key] = cur_streak
            cur_key = key
            cur_streak = 0
            counting = True
        if not counting:
            continue
        if row["correct"]:
            counting = False
        else:
            cur_streak += 1
    if cur_key is not None:
        out[cur_key] = cur_streak
    return out


def peek_upcoming(conn: sqlite3.Connection, n: int) -> list[int]:
    """Return up to ``n`` distinct word IDs the next picks are likely to land on.

    Used by the audio prefetcher; correctness here is not load-bearing.
    """
    if n <= 0:
        return []

    now_iso = _now().isoformat()
    seen: dict[int, None] = {}

    for row in conn.execute(
        """
        SELECT DISTINCT ts.word_id FROM task_state ts
          JOIN words w ON w.id = ts.word_id
         WHERE ts.task IN (?, ?)
           AND ts.introduced_at IS NOT NULL AND ts.due_at <= ?
           AND w.ignored_at IS NULL
         ORDER BY ts.due_at ASC
         LIMIT ?
        """,
        (*CARD_TASKS, now_iso, n * 2),
    ).fetchall():
        seen[row["word_id"]] = None
        if len(seen) >= n:
            break

    if len(seen) < n:
        for row in conn.execute(
            """
            SELECT w.id FROM words w
             WHERE w.ignored_at IS NULL
               AND NOT EXISTS (
               SELECT 1 FROM task_state ts
                WHERE ts.word_id = w.id AND ts.task IN ('en2ja', 'ja2en')
                  AND ts.introduced_at IS NOT NULL
             )
             ORDER BY w.id ASC
             LIMIT ?
            """,
            (n - len(seen),),
        ).fetchall():
            seen[row["id"]] = None

    if len(seen) < n:
        for row in conn.execute(
            """
            SELECT DISTINCT ts.word_id FROM task_state ts
              JOIN words w ON w.id = ts.word_id
             WHERE ts.task IN (?, ?)
               AND ts.introduced_at IS NOT NULL
               AND w.ignored_at IS NULL
             ORDER BY ts.due_at ASC
             LIMIT ?
            """,
            (*CARD_TASKS, n * 2),
        ).fetchall():
            if row["word_id"] in seen:
                continue
            seen[row["word_id"]] = None
            if len(seen) >= n:
                break

    return list(seen.keys())[:n]


def _recent_review_ids(conn: sqlite3.Connection, limit: int) -> list[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT word_id FROM reviews
         ORDER BY id DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [r["word_id"] for r in rows]


def _phonetic_candidates(
    conn: sqlite3.Connection, target: Word, exclude_ids: set[int]
) -> list[Word]:
    """Words that sound confusable: same first kana first, same mora count next.

    Ordered strongest-first so the caller can take the head. First-kana matches
    are the strongest signal (user has to read past the first character rather
    than matching by silhouette); mora-count matches are a softer rhythmic
    confusion used as a filler.
    """
    if not target.kana:
        return []
    first_kana = target.kana[0]
    target_mora = _mora_count(target.kana)

    rows = conn.execute(
        """
        SELECT * FROM words
         WHERE id != ? AND kind = 'word'
           AND (substr(kana, 1, 1) = ? OR abs(length(kana) - ?) <= 1)
         LIMIT 80
        """,
        (target.id, first_kana, len(target.kana)),
    ).fetchall()

    first_kana_matches: list[Word] = []
    mora_matches: list[Word] = []
    for row in rows:
        if row["id"] in exclude_ids:
            continue
        w = word_from_row(row)
        if w.kana.startswith(first_kana):
            first_kana_matches.append(w)
        elif _mora_count(w.kana) == target_mora:
            mora_matches.append(w)
    return first_kana_matches + mora_matches


def _tag_candidates(
    conn: sqlite3.Connection, target: Word, exclude_ids: set[int]
) -> list[Word]:
    """Words sharing at least one tag with the target — semantic cousins."""
    if not target.tags:
        return []
    tag_clauses = " OR ".join("instr(',' || tags || ',', ?) > 0" for _ in target.tags)
    tag_params = tuple(f",{t}," for t in target.tags)
    rows = conn.execute(
        f"""
        SELECT * FROM words
         WHERE id != ? AND kind = 'word'
           AND tags IS NOT NULL
           AND ({tag_clauses})
         LIMIT 50
        """,
        (target.id, *tag_params),
    ).fetchall()
    return [word_from_row(r) for r in rows if r["id"] not in exclude_ids]


def _random_candidates(
    conn: sqlite3.Connection, target: Word, exclude_ids: set[int]
) -> list[Word]:
    rows = conn.execute(
        "SELECT * FROM words WHERE id != ? AND kind = 'word' ORDER BY RANDOM() LIMIT 20",
        (target.id,),
    ).fetchall()
    return [word_from_row(r) for r in rows if r["id"] not in exclude_ids]


def _take(
    pool: list[Word],
    n: int,
    picked: dict[int, Word],
    rng: random.Random,
    used_labels: set[str],
    label_of: Callable[[Word], str],
) -> None:
    """Shuffle ``pool`` and add up to ``n`` unseen words into ``picked``.

    Skips any word whose *displayed label* already appears among the chosen
    options: two options that render identically — e.g. two words both glossed
    "only" — make a broken question (one right, one wrong, same text). Words
    with an empty label are also skipped so a missing gloss can't collide.
    ``used_labels`` grows as words are taken so later pools stay distinct too.
    """
    if n <= 0 or not pool:
        return
    rng.shuffle(pool)
    for w in pool:
        if len(picked) >= 3 or n <= 0:
            return
        if w.id in picked:
            continue
        lbl = label_of(w)
        if not lbl or lbl in used_labels:
            continue
        picked[w.id] = w
        used_labels.add(lbl)
        n -= 1


def build_choices(
    conn: sqlite3.Connection,
    target: Word,
    *,
    direction: str = "en2ja",
    target_interval_days: float = 0.0,
    prefer_ids: set[int] | None = None,
    prefer_take: int = 2,
    rng: random.Random | None = None,
) -> list[Word]:
    """Return four words (target + 3 adaptive distractors) in randomized order.

    The distractor mix is driven by direction and ``target_interval_days``
    (see :func:`_quota_for`): en2ja leans phonetic from day one, ja2en stays
    semantic regardless of maturity.

    ``prefer_ids`` biases up to ``prefer_take`` distractor slots toward a caller-
    supplied word pool (the burndown passes the round's other misses, so the
    quick-fire drills the exact set the user is confusing rather than random
    deck words). Same-POS filtered for plausibility; any shortfall falls through
    to the adaptive pools below, so a sparse pool never shrinks the choice list.

    Any shortfall is backfilled with recent-review words and finally random
    words so we always return a length-4 choice list even on a sparse deck.
    """
    rng = rng or random.Random()

    quota = _quota_for(target_interval_days, direction)
    picked: dict[int, Word] = {}

    # Distinct displayed labels. The option text is a word's *displayed* side —
    # the English gloss for ja2en, the kana for en2ja — so two different words
    # that render the same string (ただ vs だけ, both "only"; はし vs はし) would
    # produce two identical options, one marked right and one wrong. Dedup every
    # distractor on that rendered string, seeded with the target's own label.
    #
    # For ja2en we dedup on the gloss *core*, not the full string: disambiguated
    # glosses differ only in their trailing parenthetical, and offering both
    # "order (a command)" and "order (sequence)" on one card is a coin flip
    # dressed up as discrimination.
    def label_of(w: Word) -> str:
        if direction == "ja2en":
            return gloss_core(w.english)
        return (w.kana or "").strip()

    used_labels: set[str] = {label_of(target)}

    # POS filter: verb targets should only get verb distractors (and vice
    # versa), otherwise a "to run" option next to "cat, dog, bird" can be
    # eliminated on sight. Applied to every bucket; if the filter starves the
    # pool we fall through to a cross-POS random backfill below rather than
    # returning fewer than 4 choices.
    def same_pos(pool: list[Word]) -> list[Word]:
        return [w for w in pool if _same_pos(w, target)]

    # Session/burndown confusers: seat up to ``prefer_take`` distractors from the
    # caller's pool before the adaptive buckets fire, so the drill leans on the
    # words the user actually mixed up this round. Capped (not all 3) so genuine
    # phonetic/semantic distractors still get a slot — it's a mix, not a swap.
    if prefer_ids:
        pool_ids = [i for i in prefer_ids if i != target.id]
        if pool_ids:
            placeholders = ",".join("?" for _ in pool_ids)
            rows = conn.execute(
                f"SELECT * FROM words WHERE id IN ({placeholders})",
                tuple(pool_ids),
            ).fetchall()
            _take(
                same_pos([word_from_row(r) for r in rows]),
                prefer_take, picked, rng, used_labels, label_of,
            )

    # Phonetic pool is already ordered strongest-first; we want to prefer those
    # strong matches deterministically, so take without shuffling the head.
    phonetic_pool = same_pos(_phonetic_candidates(conn, target, exclude_ids={target.id}))
    for w in phonetic_pool:
        if quota.phonetic <= 0 or len(picked) >= 3:
            break
        if w.id in picked:
            continue
        lbl = label_of(w)
        if not lbl or lbl in used_labels:
            continue
        picked[w.id] = w
        used_labels.add(lbl)
        quota = _Quota(phonetic=quota.phonetic - 1, tag=quota.tag)

    tag_pool = same_pos(_tag_candidates(conn, target, exclude_ids={target.id, *picked}))
    _take(tag_pool, quota.tag, picked, rng, used_labels, label_of)

    # Backfill shortfall from recent-review words (creates helpful "wait, I
    # just saw that one" second-guessing) and finally random words. Same-POS
    # preferred; cross-POS only as a last resort so we always return 4 choices.
    if len(picked) < 3:
        recent_ids = [i for i in _recent_review_ids(conn, RECENT_REVIEW_LOOKBACK)
                      if i != target.id and i not in picked]
        if recent_ids:
            placeholders = ",".join("?" for _ in recent_ids)
            rows = conn.execute(
                f"SELECT * FROM words WHERE id IN ({placeholders})",
                tuple(recent_ids),
            ).fetchall()
            recent_words = [word_from_row(r) for r in rows]
            _take(same_pos(recent_words), 3 - len(picked), picked, rng, used_labels, label_of)

    if len(picked) < 3:
        random_pool = _random_candidates(conn, target, exclude_ids={target.id, *picked})
        _take(same_pos(random_pool), 3 - len(picked), picked, rng, used_labels, label_of)

    # Last-resort cross-POS fill when the deck simply doesn't have enough
    # matching-POS candidates (e.g. user has imported only one verb).
    if len(picked) < 3:
        _take(
            _random_candidates(conn, target, exclude_ids={target.id, *picked}),
            3 - len(picked),
            picked,
            rng,
            used_labels,
            label_of,
        )

    distractors = list(picked.values())
    # Nudge distractors toward the target's length on the *displayed* side so
    # the user can't shortcut by picking the longest card.
    if direction == "en2ja":
        distractors.sort(key=lambda w: abs(len(w.kana) - len(target.kana)))
    else:
        distractors.sort(key=lambda w: abs(len(w.english) - len(target.english)))
    distractors = distractors[:3]

    choices = [target, *distractors]
    rng.shuffle(choices)
    return choices
