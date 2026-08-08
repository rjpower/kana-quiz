"""`maturity` / `maturity_up` on the answer result — what the quiz toasts."""

import pytest
from fastapi.testclient import TestClient

from kana_quiz.srs import MATURITY_ORDER, classify_maturity


def _answer(client: TestClient, q: dict, *, correct: bool = True) -> dict:
    chosen = q["correct_index"] if correct else (q["correct_index"] + 1) % 4
    r = client.post(
        "/api/session/answer",
        json={
            "word_id": q["word_id"],
            "direction": q["direction"],
            "chosen_index": chosen,
            "correct_index": q["correct_index"],
            "timed_out": False,
            "latency_ms": 1200,
        },
    )
    assert r.status_code == 200
    return r.json()


# ---- the classifier itself ----------------------------------------------


@pytest.mark.parametrize(
    "interval,reps,introduced,expected",
    [
        (0.0, 0, False, "new"),
        (0.0, 0, True, "learning"),
        (0.5, 1, True, "learning"),
        (1.0, 1, True, "young"),
        (6.9, 3, True, "young"),
        (7.0, 4, True, "mature"),
        (20.9, 5, True, "mature"),
        (21.0, 6, True, "mastered"),
    ],
)
def test_classify_boundaries(interval, reps, introduced, expected):
    assert classify_maturity(interval, reps, introduced) == expected


def test_lapse_drops_to_learning_despite_long_interval():
    # A missed mature card keeps a stale interval until the scheduler rewrites
    # it; repetitions==0 is what marks it as back in the learning pile. Guards
    # the ordering of the two conditions in classify_maturity.
    assert classify_maturity(30.0, 0, True) == "learning"


# ---- wired through the answer route -------------------------------------


def test_first_answer_is_a_promotion_off_new(loaded_client: TestClient) -> None:
    q = loaded_client.get("/api/session/next?pool=new").json()
    res = _answer(loaded_client, q)
    # Never-introduced -> answered once correctly: new -> young (1.0d interval).
    assert res["maturity"] == "young"
    assert res["maturity_up"] is True


def test_wrong_answer_is_not_a_promotion(loaded_client: TestClient) -> None:
    q = loaded_client.get("/api/session/next?pool=new").json()
    res = _answer(loaded_client, q, correct=False)
    assert res["maturity"] == "learning"
    assert res["maturity_up"] is False


def test_maturity_climbs_and_only_flags_up_on_a_bucket_change(
    loaded_client: TestClient,
) -> None:
    """Repeated correct answers must promote monotonically, and `maturity_up`
    must be True exactly on the answers where the bucket actually changed."""
    q = loaded_client.get("/api/session/next?pool=new").json()
    word_id, direction = q["word_id"], q["direction"]

    seen: list[tuple[str, bool]] = []
    for _ in range(6):
        res = _answer(
            loaded_client,
            {"word_id": word_id, "direction": direction, "correct_index": 0},
        )
        seen.append((res["maturity"], res["maturity_up"]))

    buckets = [m for m, _ in seen]
    # Monotonic: a correct answer never demotes.
    idx = [MATURITY_ORDER.index(b) for b in buckets]
    assert idx == sorted(idx), buckets
    # It actually got somewhere.
    assert buckets[-1] in ("mature", "mastered"), buckets
    # maturity_up is set iff the bucket differs from the previous answer's.
    prev = "new"
    for bucket, up in seen:
        assert up is (bucket != prev), (bucket, up, prev)
        prev = bucket


def test_lapse_after_mastery_demotes_without_flagging_up(
    loaded_client: TestClient,
) -> None:
    q = loaded_client.get("/api/session/next?pool=new").json()
    word_id, direction = q["word_id"], q["direction"]
    stub = {"word_id": word_id, "direction": direction, "correct_index": 0}

    for _ in range(6):
        _answer(loaded_client, stub)
    missed = _answer(loaded_client, stub, correct=False)

    assert missed["maturity"] == "learning"
    assert missed["maturity_up"] is False
