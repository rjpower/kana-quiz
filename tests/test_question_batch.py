"""Bulk prefetch (/session/batch) and burndown confuser pool (/session/drill).

The batch endpoint is just /session/next run N times with each returned word
folded into the exclude set, so its contract is: duplicate-free, answerable via
the normal /answer path, honours the mode allow-list, and drains cleanly.
"""

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient


def _id_by_english(db_path: Path, english: str) -> int:
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT id FROM words WHERE english = ?", (english,)).fetchone()
    conn.close()
    return row[0]


def test_batch_returns_distinct_questions(loaded_client: TestClient) -> None:
    resp = loaded_client.get("/api/session/batch?n=5")
    assert resp.status_code == 200
    qs = resp.json()["questions"]
    assert len(qs) == 5
    ids = [q["word_id"] for q in qs]
    assert len(set(ids)) == 5  # duplicate-free within a batch
    for q in qs:
        assert q["mode"] in {"mc", "type", "cloze", "cloze_choice", "sentence_listen"}


def test_batch_caps_at_deck_size(loaded_client: TestClient) -> None:
    # 10-word sample deck: asking for more than exists drains to the deck size
    # rather than repeating a word to pad the batch.
    qs = loaded_client.get("/api/session/batch?n=12").json()["questions"]
    ids = {q["word_id"] for q in qs}
    assert len(ids) == len(qs)  # still no duplicates
    assert len(ids) <= 10


def test_batch_respects_exclude(loaded_client: TestClient) -> None:
    # A refill passes the words already buffered; the next batch must not repeat
    # any of them.
    first = loaded_client.get("/api/session/batch?n=4").json()["questions"]
    held = [q["word_id"] for q in first]
    excl = ",".join(str(i) for i in held)
    second = loaded_client.get(f"/api/session/batch?n=4&exclude={excl}").json()["questions"]
    for q in second:
        assert q["word_id"] not in held


def test_batch_empty_deck_returns_empty_list(client: TestClient) -> None:
    qs = client.get("/api/session/batch?n=4").json()["questions"]
    assert qs == []


def test_batch_mc_only_mode(loaded_client: TestClient) -> None:
    qs = loaded_client.get("/api/session/batch?n=6&modes=mc").json()["questions"]
    assert qs
    assert all(q["mode"] == "mc" for q in qs)
    assert all(len(q["choices"]) == 4 for q in qs)


def test_batch_questions_are_answerable(loaded_client: TestClient) -> None:
    # A batched card commits its new-word intro server-side, so it must be
    # answerable through the normal /answer path (no 404 on a missing task row).
    qs = loaded_client.get("/api/session/batch?n=3&modes=mc").json()["questions"]
    assert qs
    q = qs[0]
    resp = loaded_client.post(
        "/api/session/answer",
        json={
            "word_id": q["word_id"],
            "direction": q["direction"],
            "chosen_index": q["correct_index"],
            "correct_index": q["correct_index"],
            "timed_out": False,
            "latency_ms": 1000,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["correct"] is True


def test_drill_pool_seats_session_confusers(
    loaded_client: TestClient, db_path: Path
) -> None:
    dog = _id_by_english(db_path, "dog")
    cat = _id_by_english(db_path, "cat")
    horse = _id_by_english(db_path, "horse")
    resp = loaded_client.get(
        f"/api/session/drill?word_id={dog}&direction=ja2en&pool_ids={cat},{horse}"
    )
    assert resp.status_code == 200
    q = resp.json()
    # ja2en drill prompts the kana and offers english labels; the round's other
    # misses (cat, horse — same POS) are seated as distractors.
    assert "cat" in q["choices"]
    assert "horse" in q["choices"]
    assert len(q["choices"]) == 4


def test_drill_without_pool_still_returns_four(
    loaded_client: TestClient, db_path: Path
) -> None:
    dog = _id_by_english(db_path, "dog")
    q = loaded_client.get(f"/api/session/drill?word_id={dog}&direction=ja2en").json()
    assert len(q["choices"]) == 4
    assert len(set(q["choices"])) == 4


def test_batch_mixes_directions_when_both_due(
    loaded_client: TestClient, db_path: Path
) -> None:
    # With both directions of every word due, the batch alternates its preferred
    # direction so one buffer fill contains recognition *and* production, instead
    # of draining one direction's cohort first (the "all ja->en" monotony).
    conn = sqlite3.connect(db_path)
    past = "2020-01-01T00:00:00+00:00"
    for (wid,) in conn.execute("SELECT id FROM words").fetchall():
        for task in ("en2ja", "ja2en"):
            conn.execute(
                "INSERT OR REPLACE INTO task_state "
                "(word_id, task, ease, interval_days, repetitions, due_at, introduced_at) "
                "VALUES (?, ?, 2.4, 5.0, 3, ?, ?)",
                (wid, task, past, past),
            )
    conn.commit()
    conn.close()

    qs = loaded_client.get("/api/session/batch?n=6&modes=mc").json()["questions"]
    assert len(qs) == 6
    assert {q["direction"] for q in qs} == {"en2ja", "ja2en"}
