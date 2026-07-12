"""Continuous-match backend: the /session/match-batch picker + answer reuse.

The load-bearing invariant: match draws ONLY already-introduced recall cards
and never mints a task_state row, so every tile is recordable through the
unchanged POST /session/answer path and scheduling stays identical to MC.
"""

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient


def _task_state_count(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM task_state").fetchone()[0]
    conn.close()
    return n


def _ts(db_path: Path, word_id: int, task: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM task_state WHERE word_id = ? AND task = ?",
        (word_id, task),
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


def _introduce_and_answer(client: TestClient, n: int) -> set[int]:
    """Introduce + correctly answer ``n`` words so they have recall rows."""
    seen: set[int] = set()
    for _ in range(n):
        q = client.get("/api/session/next").json()
        client.post(
            "/api/session/answer",
            json={
                "word_id": q["word_id"],
                "direction": q["direction"],
                "chosen_index": q["correct_index"],
                "correct_index": q["correct_index"],
                "timed_out": False,
            },
        )
        seen.add(q["word_id"])
    return seen


def test_match_batch_never_introduces(loaded_client: TestClient, db_path: Path) -> None:
    # Fresh deck: nothing introduced -> match serves nothing AND mints no rows.
    assert _task_state_count(db_path) == 0
    resp = loaded_client.get("/api/session/match-batch?n=6")
    assert resp.status_code == 200
    assert resp.json()["tiles"] == []
    assert _task_state_count(db_path) == 0


def test_match_batch_returns_recall_tiles(
    loaded_client: TestClient, db_path: Path
) -> None:
    _introduce_and_answer(loaded_client, 5)
    before = _task_state_count(db_path)

    tiles = loaded_client.get("/api/session/match-batch?n=6").json()["tiles"]
    assert len(tiles) >= 1
    word_ids = [t["word_id"] for t in tiles]
    assert len(word_ids) == len(set(word_ids))  # no word twice -> no both-directions
    for t in tiles:
        assert t["direction"] in ("en2ja", "ja2en")
        assert t["prompt"] and t["answer"]

    # Serving a batch must not create or mutate task_state rows.
    assert _task_state_count(db_path) == before


def test_match_batch_honors_exclude_ids(
    loaded_client: TestClient, db_path: Path
) -> None:
    _introduce_and_answer(loaded_client, 6)
    first = loaded_client.get("/api/session/match-batch?n=3").json()["tiles"]
    assert first
    drop = first[0]["word_id"]
    again = loaded_client.get(
        f"/api/session/match-batch?n=8&exclude_ids={drop}"
    ).json()["tiles"]
    assert all(t["word_id"] != drop for t in again)


def test_match_correct_post_advances_srs_like_mc(
    loaded_client: TestClient, db_path: Path
) -> None:
    q = loaded_client.get("/api/session/next").json()
    wid = q["word_id"]
    before = _ts(db_path, wid, "en2ja")
    resp = loaded_client.post(
        "/api/session/answer",
        json={
            "word_id": wid,
            "direction": "en2ja",
            "mode": "mc",
            "chosen_index": 0,
            "correct_index": 0,
            "timed_out": False,
            "latency_ms": 800,
        },
    )
    body = resp.json()
    assert body["correct"] is True
    assert body["outcome"] == "correct"
    after = _ts(db_path, wid, "en2ja")
    assert after["repetitions"] == before["repetitions"] + 1
    assert after["interval_days"] >= 1.0


def test_match_mismatch_post_records_incorrect(
    loaded_client: TestClient, db_path: Path
) -> None:
    q = loaded_client.get("/api/session/next").json()
    wid = q["word_id"]
    resp = loaded_client.post(
        "/api/session/answer",
        json={
            "word_id": wid,
            "direction": "en2ja",
            "mode": "mc",
            "chosen_index": 1,
            "correct_index": 0,
            "timed_out": False,
            "latency_ms": None,
        },
    )
    body = resp.json()
    assert body["correct"] is False
    assert body["outcome"] == "incorrect"
    assert _ts(db_path, wid, "en2ja")["repetitions"] == 0


def test_answer_on_unintroduced_word_404s(
    loaded_client: TestClient, db_path: Path
) -> None:
    # Proves why pick_match_batch must never serve un-introduced words: the
    # answer endpoint 404s without a task_state row.
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT id FROM words WHERE NOT EXISTS "
        "(SELECT 1 FROM task_state ts WHERE ts.word_id = words.id) LIMIT 1"
    ).fetchone()
    conn.close()
    assert row is not None
    resp = loaded_client.post(
        "/api/session/answer",
        json={
            "word_id": row[0],
            "direction": "en2ja",
            "mode": "mc",
            "chosen_index": 0,
            "correct_index": 0,
            "timed_out": False,
        },
    )
    assert resp.status_code == 404
