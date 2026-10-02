"""Pydantic request/response schemas for the public API."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from kana_quiz.srs import MaturityBucket

Direction = Literal["en2ja", "ja2en"]
QuestionMode = Literal["mc", "type", "cloze", "cloze_choice", "sentence_listen"]
Outcome = Literal["correct", "incorrect", "timeout", "gave_up"]


class RubySegment(BaseModel):
    """One run of a furigana-annotated sentence.

    ``t`` is the surface text; ``r`` is its hiragana reading when ``t`` is a
    kanji run, or ``None`` for plain kana / punctuation / ASCII that needs no
    annotation. The frontend renders ``r``-bearing segments as ``<ruby>`` and
    the rest as plain text. Field names are kept terse — these ship in bulk on
    every cloze/sentence payload.
    """

    t: str
    r: str | None = None


class ImportReportOut(BaseModel):
    inserted: int
    updated: int
    skipped: int
    # The deck the rows landed in — callers that create a deck by name (the
    # kaku exporter) read these to link straight to it.
    deck_id: int = 0
    deck_name: str = ""


class WordIntro(BaseModel):
    """Pre-exposure payload for a word the user has never seen before.

    Only populated on the first sighting (when ``pick_next_card`` stamped
    ``introduced_at`` for this request). The frontend renders a "meet this
    word" card with this data before starting the quiz timer.
    """

    kana: str
    english: str
    kanji: str | None = None


class SentenceOut(BaseModel):
    """Cached LLM-generated example sentence + mnemonic for a single word."""

    word_id: int
    japanese: str
    english: str
    mnemonic: str = ""
    # Furigana for ``japanese`` — the example-sentence reveal renders this as
    # ruby. Empty list when the sentence is pure kana (no kanji to annotate).
    japanese_ruby: list[RubySegment] = Field(default_factory=list)


class NextQuestion(BaseModel):
    word_id: int
    direction: Direction
    prompt: str
    # Type-in questions don't ship choices — the field stays present and empty
    # so the response shape is uniform across modes.
    mode: QuestionMode = "mc"
    # 'word' or 'sentence' — a sentence card renders a full line as the
    # prompt and sizes the answer box accordingly.
    kind: str = "word"
    choices: list[str] = Field(default_factory=list, max_length=4)
    correct_index: int = 0
    # Present only on first-ever sighting of a word. Safe to ship: the intro
    # card is shown and dismissed *before* the quiz starts, so revealing the
    # answer here is the whole point.
    introduction: WordIntro | None = None
    # The word's kanji form (or None if hiragana-only). Shipped on every
    # response and surfaced by the client *only after* the answer locks, so
    # post-answer reveal can show "みる (見る)" for context. Showing kanji
    # before lock would give an MC shortcut on en2ja questions.
    kanji: str | None = None
    # Consecutive failures since this (word, direction) pair's last correct
    # review. The client uses this on the round-summary missed list to badge
    # leeches; the value itself isn't shown during the question to avoid
    # giving anything away.
    failure_streak: int = 0
    # Per-question time budget in milliseconds — scales with expected answer
    # length so long type-ins (e.g. おはようございます) don't time out before
    # the user can finish typing. The frontend reads this for the countdown
    # and sends ``latency_ms`` against the same window for SRS ease tuning.
    time_limit_ms: int = 5000
    # Populated on cloze and cloze_choice cards. ``cloze_template`` is the
    # Japanese sentence with the target word's surface form replaced by the
    # literal token ``{blank}``; the frontend splits on it and renders an inline
    # input (cloze) or a static slot above the choice grid (cloze_choice).
    # ``cloze_expected`` is that surface form (e.g. "役立ち" /
    # "役立ちます") — the grader accepts it OR the dictionary form, and the
    # reveal screen shows it so the user passively learns the conjugation.
    cloze_template: str | None = None
    cloze_expected: str | None = None
    # Furigana for the two halves of the cloze sentence around the ``{blank}``
    # (the blanked word itself gets no reading — it's the answer). Each is a
    # list of ``RubySegment`` the frontend renders as ruby in place of the raw
    # ``cloze_template`` split. None on non-cloze cards.
    cloze_before: list[RubySegment] | None = None
    cloze_after: list[RubySegment] | None = None
    # Listening (sentence_listen) mode payload. ``audio_url`` points at
    # the cached sentence TTS endpoint; the client auto-plays it on
    # card mount. ``expected_translation`` is the reference English the
    # grader (and the reveal screen) compares against. ``sentence_japanese``
    # is the cached Japanese text — withheld from the rendered prompt
    # while the question is live, then surfaced on the reveal so the
    # user can see what they actually heard.
    audio_url: str | None = None
    expected_translation: str | None = None
    sentence_japanese: str | None = None
    # Furigana for ``sentence_japanese``, surfaced on the listening reveal so
    # the user can read what they heard with kanji readings. None pre-reveal /
    # on non-listening cards.
    sentence_japanese_ruby: list[RubySegment] | None = None
    # New-card auto-reveal: on a word's first-ever sighting (when
    # ``introduction`` is set) we ship the cached example sentence + mnemonic
    # inline so the intro "meet this word" card can render it immediately
    # without a second /sentence round-trip. ``None`` on every other card and
    # on a sentence-cache miss (the client falls back to fetching on demand).
    sentence: SentenceOut | None = None


class AnswerIn(BaseModel):
    word_id: int
    direction: Direction
    chosen_index: int | None = None  # None on timeout or for typed answers
    correct_index: int | None = None
    timed_out: bool
    latency_ms: int | None = None
    typed_answer: str | None = None
    # The user explicitly tapped "I don't know" rather than guessing. Same
    # ease penalty as a wrong answer but recorded distinctly so we can tell
    # genuine confusion apart from a missed guess.
    gave_up: bool = False
    # Mode of the in-flight question. The frontend always knows what it
    # rendered, so it relays the same value /session/next handed back.
    # Used today only to tell cloze-grading from regular type-in
    # grading; older clients that omit it get default type/mc behavior.
    mode: QuestionMode | None = None
    # On cloze cards: the conjugated surface form the user was asked to
    # produce (e.g. "役立ちます" for 役立つ). Relayed verbatim from the
    # NextQuestion payload so the grader can accept either it or the
    # dictionary form without a second cache lookup.
    cloze_expected: str | None = None


class AnswerResult(BaseModel):
    correct: bool
    outcome: Outcome
    new_due_at: datetime
    interval_days: float
    ease: float
    expected: str | None = None
    # Optional human-readable explanation surfaced when the semantic grader
    # weighs in on a typed answer (e.g. "起きる means 'wake up'; 起こす means
    # 'rouse someone'."). Always None for MC, timeouts, gave_up.
    feedback: str | None = None
    # Where this card sits after the answer, and whether that's a promotion.
    # The quiz toasts the bucket on every answer and only celebrates when
    # ``maturity_up`` — so the sparkle marks an actual advance rather than
    # firing on every correct rep. Derived, not stored.
    maturity: MaturityBucket = "learning"
    maturity_up: bool = False
    # A sprint-deck card that just cleared its second spaced review and left
    # the rotation. The client celebrates and strikes it from the round.
    archived: bool = False


class Stats(BaseModel):
    total_words: int
    introduced: int
    due_now: int
    mastered: int
    reviews_last_7_days: int
    accuracy_last_7_days: float | None


class LatencyBucket(BaseModel):
    """One bar in the response-time histogram.

    ``lower_ms`` is inclusive, ``upper_ms`` is exclusive except for the final
    open-ended bucket where ``upper_ms`` is None.
    """

    lower_ms: int
    upper_ms: int | None
    count: int


class DirectionState(BaseModel):
    """Per-direction SRS state for a single word."""

    ease: float
    interval_days: float
    repetitions: int
    due_at: datetime | None
    introduced_at: datetime | None
    maturity: MaturityBucket
    failure_streak: int = 0
    leech: bool = False


class WordState(BaseModel):
    id: int
    kana: str
    english: str
    kanji: str | None
    deck_id: int | None = None
    deck_name: str | None = None
    directions: dict[Direction, DirectionState]
    # Overall maturity = max of the two direction maturities (used for sorting
    # the stats table).
    maturity: MaturityBucket


class DetailedStats(BaseModel):
    summary: Stats
    latency_histogram: list[LatencyBucket]
    median_latency_ms: int | None
    words: list[WordState]
    maturity_counts: dict[str, int]


class DueWindows(BaseModel):
    """Rolling review-load lookahead for the landing summary.

    Counts are of *reviews* (recall lanes), matching ``Stats.due_now``: a word
    due in both directions contributes two. ``next_hour`` / ``next_24h`` are
    the additional reviews that come due within that window (exclusive of what's
    already due now), so the three add up to the total ready in 24h.
    """

    due_now: int
    next_hour: int
    next_24h: int


class MaturityTier(BaseModel):
    """One mastery bucket in the landing distribution.

    ``count`` is words whose stronger direction lands in this tier;
    ``avg_ease`` is the mean SRS ease across those words' introduced
    directions — a rough "how easily this sticks" efficiency read — and is
    None for an empty tier or the (un-introduced) ``new`` tier.
    """

    maturity: MaturityBucket
    count: int
    avg_ease: float | None


class StatsOverview(BaseModel):
    """Compact landing-page rollup — cheap enough to fetch on every visit.

    Distinct from :class:`DetailedStats` (which ships every word): this is the
    at-a-glance summary the study screen shows instead of the per-deck table
    (that breakdown now lives on the Decks page). All counts are over the
    *active* study set — ignored words are excluded throughout.
    """

    total_words: int
    introduced: int
    new_available: int
    due: DueWindows
    maturity: list[MaturityTier]
    reviews_last_7_days: int
    accuracy_last_7_days: float | None
    median_latency_ms: int | None
    # Words first answered since the learner's local midnight, and since the
    # midnight six days before that.
    learned_today: int = 0
    learned_this_week: int = 0


class MatchTile(BaseModel):
    """One word in a continuous-match board batch.

    ``prompt`` is the side the player reads (English for en2ja, kana for
    ja2en); ``answer`` is the side they must tap to make the pair. Direction is
    relayed back on the answer POST so the match is recorded on the right
    recall lane. ``failure_streak`` lets the UI badge leeches.
    """

    word_id: int
    direction: Direction
    prompt: str
    answer: str
    kanji: str | None = None
    failure_streak: int = 0


class MatchBatch(BaseModel):
    """A batch of tiles to seed or refill a continuous-match board."""

    tiles: list[MatchTile] = Field(default_factory=list)


class QuestionBatch(BaseModel):
    """A run of upcoming questions for the bulk prefetch (``/session/batch``).

    The client buffers these and serves them locally so answering never blocks
    on a per-card fetch. May be shorter than requested (or empty) when the deck
    runs dry; each question is a normal ``NextQuestion`` and is answered through
    the usual ``POST /session/answer`` path.
    """

    questions: list[NextQuestion] = Field(default_factory=list)


class DeckIn(BaseModel):
    name: str
    level: int = 5
    profile: str = "standard"
    pick_order: str = "random"


class DeckPatch(BaseModel):
    name: str | None = None
    level: int | None = None
    profile: str | None = None
    pick_order: str | None = None


class DeckOut(BaseModel):
    id: int
    name: str
    level: int
    created_at: str
    # 'standard' or 'sprint' — a sprint deck archives a card after its second
    # successful spaced review.
    profile: str = "standard"
    # 'random' draws new cards in any order within the deck's level; 'listed'
    # draws them in import order, for a deck sorted by frequency.
    pick_order: str = "random"
    # word_count excludes ignored words — the headline number a user reads as
    # "deck size" should match what the picker can serve.
    word_count: int = 0
    new_count: int = 0
    due_count: int = 0
    ignored_count: int = 0
    # Sprint cards that cleared the deck; a subset of neither word_count nor
    # ignored_count (the stats split them apart).
    archived_count: int = 0


class IgnoredWord(BaseModel):
    id: int
    kana: str
    english: str
    kanji: str | None = None
    ignored_at: str


class DeckWordState(BaseModel):
    """Per-direction SRS snapshot for the deck-detail view's status pills.

    A lean projection of the full ``DirectionState`` (defined above for
    the /stats/detailed payload) — just the fields the deck-detail
    table renders. Kept as a separate type so the deck endpoint and the
    stats endpoint can evolve independently.

    ``introduced`` is True once the user has seen the card at least
    once in this direction (the picker stamps ``introduced_at`` on
    first pick). Until then ``ease`` carries the default value but
    means nothing yet, so the UI renders a "new" pill.
    """

    ease: float
    repetitions: int
    introduced: bool


class DeckWord(BaseModel):
    """A single word row in the deck-detail view's table.

    ``ignored`` is the boolean derived from ``words.ignored_at`` — the
    detail view shows every card whether ignored or not, with a toggle,
    so the frontend doesn't need the timestamp. ``archived`` marks a
    sprint graduate: it also carries ``ignored`` (that is what keeps it
    out of every queue), and the view names it cleared rather than
    ignored.

    ``en2ja`` / ``ja2en`` are ``None`` when no task_state row exists
    for that direction (e.g. katakana-only cards never get a ja2en
    row), so the UI can distinguish "never scheduled" from "introduced
    but at default ease".
    """

    id: int
    kana: str
    english: str
    kanji: str | None = None
    ignored: bool
    archived: bool = False
    en2ja: DeckWordState | None = None
    ja2en: DeckWordState | None = None
