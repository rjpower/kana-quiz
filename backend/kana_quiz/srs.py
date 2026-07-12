"""SM-2 variant spaced repetition scheduler.

The algorithm tracks three per-word quantities:

* ``ease`` — a multiplier that lengthens future intervals when the user answers
  correctly and shrinks them on mistakes. Bounded to ``[1.3, 2.8]``.
* ``interval_days`` — the gap until the next review. Grows as ``ease * interval``
  once the word has survived two correct answers; resets to ~10 minutes on any
  failure.
* ``repetitions`` — consecutive correct answers. Reset on failure.

A "timeout" (user let the 5 s clock expire) is treated like an incorrect answer
but with a slightly harsher ease penalty, since running out the clock is weaker
evidence of recall than an actively wrong guess.
"""

import os
import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

Outcome = Literal["correct", "incorrect", "timeout", "gave_up"]

EASE_MIN = 1.3
EASE_MAX = 2.8
EASE_BONUS = 0.05
# Latency tiers — applied to correct answers only. The thresholds are
# expressed as a fraction of the question's allotted time so they apply
# uniformly to the 5 s MC window and the 10 s type-in window.
#
# A snap-correct (under 20% of the window) is strong recall evidence and
# earns an extra ease bump; a slow-correct (over 70%) was likely a guess
# or a slow grope and earns a smaller bump than the default. Without a
# latency factor we fall back to the neutral middle bonus.
EASE_BONUS_FAST = 0.08
EASE_BONUS_SLOW = 0.02
LATENCY_FAST_FRACTION = 0.2
LATENCY_SLOW_FRACTION = 0.7
INCORRECT_PENALTY = 0.15
TIMEOUT_PENALTY = 0.20
RELEARN_INTERVAL_DAYS = 10 / (60 * 24)  # 10 minutes expressed in days

# A word is flagged "leech" once it accumulates this many consecutive
# failures since its last correct answer. The threshold is intentionally
# low (Anki defaults to 8) so the warning surfaces before the user has
# given up — at 4 misses in a row, something is off with this card and
# they're probably better served by re-reading it than by drilling more.
LEECH_FAILURE_THRESHOLD = 4

# --- Due-time jitter ---------------------------------------------------------
# A card answered correctly comes due at exactly the same wall-clock time
# ``interval`` days later, so a big evening session lands as one wall of reviews
# the next evening. We nudge each ``due_at`` by a random ± offset so a day's
# reviews trickle in across a window instead of spiking all at once.
#
# The offset is a fraction of the interval (longer intervals can drift further),
# clamped to ``±JITTER_MAX_HOURS``. Sub-day intervals — the ~10-minute relearn
# step after a miss — are left alone so failed cards still resurface promptly.
JITTER_FRACTION = float(os.environ.get("KANA_REVIEW_JITTER_FRACTION", "0.15"))
JITTER_MAX_HOURS = float(os.environ.get("KANA_REVIEW_JITTER_MAX_HOURS", "6"))
JITTER_MIN_INTERVAL_DAYS = 1.0

# Shared generator for production scheduling. Tests inject their own seeded
# ``random.Random`` (or pass ``rng=None`` to disable jitter entirely) so the
# scheduler stays deterministic under test.
review_rng = random.Random()


def jitter_seconds(interval_days: float, rng: random.Random) -> float:
    """Random ± offset (seconds) to desynchronize same-interval reviews.

    Magnitude is ``JITTER_FRACTION`` of the interval, capped at
    ``JITTER_MAX_HOURS``. Returns 0 for sub-day intervals (the relearn step)
    or when jitter is disabled, so a failed card still comes back on schedule.
    """
    if interval_days < JITTER_MIN_INTERVAL_DAYS or JITTER_FRACTION <= 0:
        return 0.0
    cap = JITTER_MAX_HOURS * 3600.0
    magnitude = min(JITTER_FRACTION * interval_days * 86400.0, cap)
    return rng.uniform(-magnitude, magnitude)


@dataclass(frozen=True)
class SrsState:
    ease: float
    interval_days: float
    repetitions: int
    due_at: datetime


def _ease_bonus_for(latency_factor: float | None) -> float:
    """Pick the ease bonus tier based on how much of the time window was used.

    ``latency_factor`` is ``latency_ms / window_ms`` — None when latency
    isn't available (e.g. legacy reviews). The middle band is the default
    so unknown-latency reviews still progress.
    """
    if latency_factor is None:
        return EASE_BONUS
    if latency_factor < LATENCY_FAST_FRACTION:
        return EASE_BONUS_FAST
    if latency_factor > LATENCY_SLOW_FRACTION:
        return EASE_BONUS_SLOW
    return EASE_BONUS


def schedule(
    prev: SrsState,
    outcome: Outcome,
    now: datetime,
    *,
    latency_factor: float | None = None,
    rng: random.Random | None = None,
) -> SrsState:
    """Return the updated SRS state for ``prev`` after ``outcome``.

    Args:
        prev: The word's current SRS state.
        outcome: ``"correct"``, ``"incorrect"``, or ``"timeout"``.
        now: Reference time used to compute ``due_at``.
        latency_factor: ``latency_ms / window_ms`` for the answer, or
            ``None`` if unknown. Only consulted on a correct outcome to
            scale the ease bonus.
        rng: Source of due-time jitter. ``None`` (the default) disables
            jitter, keeping the result deterministic — production passes
            :data:`review_rng`; tests seed their own or leave it off.

    Returns:
        A new :class:`SrsState` with updated fields — ``prev`` is not mutated.
        Note ``interval_days`` records the *nominal* interval; any jitter is
        folded into ``due_at`` only, so it never compounds across reviews.
    """
    if outcome == "correct":
        reps = prev.repetitions + 1
        if reps == 1:
            interval = 1.0
        elif reps == 2:
            interval = 3.0
        else:
            interval = prev.interval_days * prev.ease
        ease = min(prev.ease + _ease_bonus_for(latency_factor), EASE_MAX)
        due_at = now + timedelta(days=interval)
        if rng is not None:
            due_at += timedelta(seconds=jitter_seconds(interval, rng))
        return replace(
            prev,
            ease=ease,
            interval_days=interval,
            repetitions=reps,
            due_at=due_at,
        )

    # gave_up == incorrect from the scheduler's POV; it just records distinctly
    # in reviews so we can tell the user explicitly tapped "I don't know" apart
    # from a guess that happened to be wrong.
    penalty = TIMEOUT_PENALTY if outcome == "timeout" else INCORRECT_PENALTY
    ease = max(prev.ease - penalty, EASE_MIN)
    return replace(
        prev,
        ease=ease,
        interval_days=RELEARN_INTERVAL_DAYS,
        repetitions=0,
        due_at=now + timedelta(minutes=10),
    )
