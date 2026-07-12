"""Unit tests for the SM-2 variant scheduler."""

import random
from datetime import datetime, timedelta, timezone

import pytest

from kana_quiz import srs
from kana_quiz.srs import EASE_MAX, EASE_MIN, SrsState, schedule


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _initial() -> SrsState:
    return SrsState(ease=2.5, interval_days=0.0, repetitions=0, due_at=NOW)


def test_first_correct_schedules_one_day_out():
    state = schedule(_initial(), "correct", NOW)
    assert state.repetitions == 1
    assert state.interval_days == pytest.approx(1.0)
    assert state.due_at == NOW + timedelta(days=1)
    assert state.ease == pytest.approx(2.55)


def test_second_correct_is_three_days():
    s1 = schedule(_initial(), "correct", NOW)
    s2 = schedule(s1, "correct", NOW + timedelta(days=1))
    assert s2.repetitions == 2
    assert s2.interval_days == pytest.approx(3.0)


def test_correct_streak_multiplies_by_ease():
    state = _initial()
    t = NOW
    for _ in range(4):
        state = schedule(state, "correct", t)
        t = state.due_at
    # After 4 corrects: intervals should have grown past 3 * ease.
    assert state.repetitions == 4
    assert state.interval_days > 3.0
    assert state.ease <= EASE_MAX


def test_ease_capped_at_max():
    state = SrsState(ease=2.78, interval_days=30.0, repetitions=10, due_at=NOW)
    after = schedule(state, "correct", NOW)
    assert after.ease == pytest.approx(EASE_MAX)


def test_incorrect_resets_reps_and_shortens_interval():
    prev = SrsState(ease=2.5, interval_days=14.0, repetitions=5, due_at=NOW)
    after = schedule(prev, "incorrect", NOW)
    assert after.repetitions == 0
    assert after.interval_days < 1.0
    assert after.due_at == NOW + timedelta(minutes=10)
    assert after.ease == pytest.approx(2.35)


def test_timeout_penalizes_more_than_incorrect():
    base = SrsState(ease=2.5, interval_days=14.0, repetitions=5, due_at=NOW)
    wrong = schedule(base, "incorrect", NOW)
    timeout = schedule(base, "timeout", NOW)
    assert timeout.ease < wrong.ease


def test_ease_has_lower_floor():
    prev = SrsState(ease=1.35, interval_days=5.0, repetitions=2, due_at=NOW)
    after = schedule(prev, "timeout", NOW)
    assert after.ease == pytest.approx(EASE_MIN)
    # A second failure at the floor stays at the floor.
    again = schedule(after, "incorrect", NOW)
    assert again.ease == pytest.approx(EASE_MIN)


def test_snap_correct_earns_larger_ease_bonus():
    """A snap-correct (<20% of the window) should bump ease more than the
    default — fast recall is strong evidence the card is well-learned."""
    snap = schedule(_initial(), "correct", NOW, latency_factor=0.1)
    normal = schedule(_initial(), "correct", NOW)  # no latency → middle bonus
    assert snap.ease > normal.ease


def test_slow_correct_earns_smaller_ease_bonus():
    """A slow-correct (>70% of the window) is weaker recall evidence — the
    user likely groped to it — so the bonus should shrink."""
    slow = schedule(_initial(), "correct", NOW, latency_factor=0.85)
    normal = schedule(_initial(), "correct", NOW)
    assert slow.ease < normal.ease


def test_latency_irrelevant_for_failures():
    """Wrong/timeout outcomes shouldn't be affected by latency_factor — a
    fast-wrong answer is still wrong."""
    prev = SrsState(ease=2.5, interval_days=10.0, repetitions=3, due_at=NOW)
    fast_wrong = schedule(prev, "incorrect", NOW, latency_factor=0.1)
    no_lat_wrong = schedule(prev, "incorrect", NOW)
    assert fast_wrong.ease == pytest.approx(no_lat_wrong.ease)
    assert fast_wrong.interval_days == pytest.approx(no_lat_wrong.interval_days)


# --- Due-time jitter ---------------------------------------------------------


def test_no_jitter_without_rng():
    """Default (rng=None) stays deterministic — due_at is exactly on interval."""
    state = schedule(_initial(), "correct", NOW)
    assert state.due_at == NOW + timedelta(days=1)


def test_jitter_offsets_due_at_within_the_interval_fraction():
    """A 1-day review jitters by at most JITTER_FRACTION of the interval."""
    rng = random.Random(0)
    offsets = set()
    for _ in range(200):
        state = schedule(_initial(), "correct", NOW, rng=rng)
        offset = state.due_at - (NOW + timedelta(days=1))
        offsets.add(offset)
        # 15% of a 1-day interval = 3.6h, comfortably under the 6h cap.
        assert abs(offset) <= timedelta(hours=srs.JITTER_FRACTION * 24)
    # It actually varies (not a constant) and lands on both sides of nominal.
    assert len(offsets) > 100
    assert any(o > timedelta(0) for o in offsets)
    assert any(o < timedelta(0) for o in offsets)


def test_jitter_is_capped_at_six_hours_for_long_intervals():
    """Long intervals would jitter past 6h on fraction alone; the cap holds."""
    rng = random.Random(1)
    # reps>=2 so interval = prev.interval_days * ease = 100 * 2.5 = 250 days;
    # 15% of that is ~37 days, so every draw must be clamped to the 6h cap.
    prev = SrsState(ease=2.5, interval_days=100.0, repetitions=5, due_at=NOW)
    for _ in range(200):
        state = schedule(prev, "correct", NOW, rng=rng)
        offset = state.due_at - (NOW + timedelta(days=250))
        assert abs(offset) <= timedelta(hours=srs.JITTER_MAX_HOURS)


def test_relearn_step_is_never_jittered():
    """A miss reschedules the ~10-minute relearn step exactly — no jitter, so
    failed cards resurface promptly."""
    rng = random.Random(2)
    prev = SrsState(ease=2.5, interval_days=14.0, repetitions=5, due_at=NOW)
    after = schedule(prev, "incorrect", NOW, rng=rng)
    assert after.due_at == NOW + timedelta(minutes=10)


def test_jitter_does_not_compound_across_reviews():
    """interval_days stays nominal so jitter never accumulates review-to-review:
    the second interval is 3 days regardless of the first review's jitter."""
    rng = random.Random(3)
    s1 = schedule(_initial(), "correct", NOW, rng=rng)
    assert s1.interval_days == pytest.approx(1.0)
    s2 = schedule(s1, "correct", NOW + timedelta(days=1), rng=rng)
    assert s2.interval_days == pytest.approx(3.0)
