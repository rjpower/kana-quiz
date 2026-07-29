"""`pool` param on /session/next + /session/batch: review-only vs new-only."""

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient


def test_review_pool_204_when_nothing_due(loaded_client: TestClient) -> None:
    # A freshly-imported deck is all unintroduced — a review session has nothing
    # to serve and must NOT introduce a new word to fill the gap.
    r = loaded_client.get("/api/session/next?pool=review")
    assert r.status_code == 204


def test_new_pool_introduces_new_words(loaded_client: TestClient) -> None:
    q = loaded_client.get("/api/session/next?pool=new").json()
    # A brand-new card carries the introduction payload.
    assert q["introduction"] is not None


def test_new_pool_batch_is_distinct_and_all_new(loaded_client: TestClient) -> None:
    b = loaded_client.get("/api/session/batch?pool=new&n=5").json()
    qs = b["questions"]
    assert len(qs) == 5
    assert len({q["word_id"] for q in qs}) == 5  # no dupes
    assert all(q["introduction"] is not None for q in qs)  # every one a fresh word


def _answer(client: TestClient, q: dict) -> None:
    client.post(
        "/api/session/answer",
        json={
            "word_id": q["word_id"],
            "direction": q["direction"],
            "chosen_index": q["correct_index"],
            "correct_index": q["correct_index"],
            "timed_out": False,
            "latency_ms": 1200,
        },
    )


def test_review_pool_serves_introduced_due_without_intro(
    loaded_client: TestClient, monkeypatch
) -> None:
    import kana_quiz.routes.session as rs

    # Off, or the answer below would push this word's other lane 12h out and
    # leave the review pool with nothing due to serve.
    monkeypatch.setattr(rs, "PAIR_SNOOZE_SECONDS", 0)

    # Answer a word dealt by the new pool — answering is what introduces it...
    q = loaded_client.get("/api/session/next?pool=new").json()
    wid = q["word_id"]
    _answer(loaded_client, q)
    # ...then the review pool serves its still-due other lane, introducing nothing.
    r = loaded_client.get("/api/session/next?pool=review").json()
    assert r["introduction"] is None
    assert r["word_id"] == wid


def test_review_pool_ignores_dealt_but_unanswered_words(loaded_client: TestClient) -> None:
    """A card the user never answered must not come back as a review.

    The picker runs ahead of the user: /session/batch deals several cards into
    a client-side buffer at once. Abandoning a round used to leave the undealt
    remainder marked introduced-and-due, so the next review session served
    words the user had never actually seen.
    """
    batch = loaded_client.get("/api/session/batch?pool=new&n=5").json()["questions"]
    dealt = {q["word_id"] for q in batch}
    assert len(dealt) == 5

    # Nothing was answered, so a review session has nothing legitimate to serve.
    assert loaded_client.get("/api/session/next?pool=review").status_code == 204

    # And the whole deck is still unseen — dealing consumed nothing. (The new
    # pool orders by RANDOM(), so ask for all ten rather than the same five.)
    again = loaded_client.get("/api/session/batch?pool=new&n=10").json()["questions"]
    assert len({q["word_id"] for q in again}) == 10
    assert dealt <= {q["word_id"] for q in again}
    assert all(q["introduction"] is not None for q in again)


def test_answering_one_direction_introduces_the_sibling(
    loaded_client: TestClient, db_path: Path
) -> None:
    """Answering one lane must admit the word's other lane to the queue too.

    Both recall rows are reserved when the word is dealt, but the answer route
    only advances the lane being answered. If it stamped only that lane, the
    sibling would keep a NULL introduced_at and — since every due query filters
    on IS NOT NULL — would never be served again.
    """
    q = loaded_client.get("/api/session/next?pool=new").json()
    wid = q["word_id"]
    _answer(loaded_client, q)

    conn = sqlite3.connect(db_path)
    rows = dict(
        conn.execute(
            "SELECT task, introduced_at FROM task_state "
            "WHERE word_id = ? AND task IN ('en2ja', 'ja2en')",
            (wid,),
        ).fetchall()
    )
    conn.close()
    assert set(rows) == {"en2ja", "ja2en"}
    assert all(v is not None for v in rows.values()), rows


def test_new_pool_runs_dry_returns_204(loaded_client: TestClient) -> None:
    # Answer every word — answering is what retires a word from the new pool,
    # so the loop needs no exclude list: a word once answered never comes back.
    seen: set[int] = set()
    for _ in range(200):
        r = loaded_client.get("/api/session/next?pool=new")
        if r.status_code == 204:
            break
        q = r.json()
        assert q["word_id"] not in seen
        seen.add(q["word_id"])
        _answer(loaded_client, q)
    # The sample deck is small; once drained, new pool 204s.
    assert len(seen) == 10
    assert loaded_client.get("/api/session/next?pool=new").status_code == 204
    drained = loaded_client.get("/api/session/batch?pool=new&n=5").json()
    assert drained["questions"] == []


def test_default_pool_still_mixes(loaded_client: TestClient) -> None:
    # No pool param -> mixed: a fresh deck introduces new words as before.
    q = loaded_client.get("/api/session/next").json()
    assert q["introduction"] is not None
