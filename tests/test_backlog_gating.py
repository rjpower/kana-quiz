"""Backlog gating: stop introducing new words while the learning pile is full.

``pick_next_card`` refuses to introduce a brand-new word once the user has
``NEW_WORD_BACKLOG_LIMIT`` distinct words still in active learning (recall rows
with interval < 1 day — freshly introduced or relearning). When gated it falls
through to serving an already-introduced card, never 204 while cards exist.

To exercise the gate cleanly we seed the learning pile as cards that are *not*
due in the next 5 minutes (so the picker's separate "active pool is thin"
short-circuit doesn't fire first) but still in learning (interval < 1).
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

import kana_quiz.session as sess


def _all_word_ids(db_path: Path) -> list[int]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT id FROM words ORDER BY id ASC").fetchall()
    conn.close()
    return [r[0] for r in rows]


def _introduced_ids(db_path: Path) -> set[int]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT DISTINCT word_id FROM task_state "
        "WHERE introduced_at IS NOT NULL AND task IN ('en2ja', 'ja2en')"
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


def _seed_learning(db_path: Path, word_ids: list[int], *, hours_out: int = 1) -> None:
    """Introduce ``word_ids`` as in-learning cards due ``hours_out`` from now."""
    conn = sqlite3.connect(db_path)
    due = (datetime.now(timezone.utc) + timedelta(hours=hours_out)).isoformat()
    intro = datetime.now(timezone.utc).isoformat()
    for wid in word_ids:
        for task in ("en2ja", "ja2en"):
            conn.execute(
                """
                INSERT OR REPLACE INTO task_state
                  (word_id, task, ease, interval_days, repetitions, due_at, introduced_at)
                VALUES (?, ?, 2.5, 0.5, 0, ?, ?)
                """,
                (wid, task, due, intro),
            )
    conn.commit()
    conn.close()


def test_gate_blocks_new_when_backlog_at_limit(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(sess, "NEW_WORD_BACKLOG_LIMIT", 3)
    seed = _all_word_ids(db_path)[:3]
    _seed_learning(db_path, seed)
    assert _introduced_ids(db_path) == set(seed)

    q = loaded_client.get("/api/session/next").json()
    # backlog (3) == limit -> no new word; reuse a seeded card instead.
    assert _introduced_ids(db_path) == set(seed)
    assert q["word_id"] in seed
    assert q.get("introduction") is None


def test_gate_allows_new_below_limit(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(sess, "NEW_WORD_BACKLOG_LIMIT", 4)
    seed = _all_word_ids(db_path)[:3]
    _seed_learning(db_path, seed)

    q = loaded_client.get("/api/session/next").json()
    # backlog (3) < limit (4) -> introduce a fresh word.
    assert q.get("introduction") is not None
    assert q["word_id"] not in seed
    assert len(_introduced_ids(db_path)) == 4


def test_gate_does_not_starve_empty_queue(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    # An empty queue must still introduce the first card (backlog 0 < limit).
    monkeypatch.setattr(sess, "NEW_WORD_BACKLOG_LIMIT", 1)
    q = loaded_client.get("/api/session/next")
    assert q.status_code == 200
    assert q.json().get("introduction") is not None


def test_gate_disabled_introduces_despite_backlog(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(sess, "NEW_WORD_BACKLOG_LIMIT", 0)
    seed = _all_word_ids(db_path)[:5]
    _seed_learning(db_path, seed)

    q = loaded_client.get("/api/session/next").json()
    # Gate disabled -> a new word is introduced even with 5 in learning.
    assert q.get("introduction") is not None
    assert q["word_id"] not in seed
