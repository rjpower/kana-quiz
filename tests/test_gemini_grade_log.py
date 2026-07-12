"""End-to-end log of Gemini grader calls.

Asserts the `gemini_grade_log` migration runs, that a typed answer
which falls through to the LLM grader produces exactly one row, and
that the row carries inputs + verdict + final outcome so the viewer
can reconstruct what happened.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from kana_quiz import gemini


def _seed_only_card(db: Path, word_id: int, *, ease: float = 2.5, reps: int = 1) -> None:
    conn = sqlite3.connect(db)
    now_iso = "2020-01-01T00:00:00+00:00"
    rows = conn.execute("SELECT id FROM words").fetchall()
    for (wid,) in rows:
        for d in ("en2ja", "ja2en"):
            conn.execute(
                """
                INSERT OR IGNORE INTO task_state
                  (word_id, task, ease, interval_days, repetitions,
                   due_at, introduced_at)
                VALUES (?, ?, 2.5, 0, 1, ?, ?)
                """,
                (wid, d, now_iso, now_iso),
            )
    conn.execute(
        "UPDATE task_state SET ease = ?, repetitions = ?, "
        "due_at = '2020-01-01T00:00:00+00:00' "
        "WHERE word_id = ? AND task = 'en2ja'",
        (ease, reps, word_id),
    )
    conn.execute(
        "UPDATE task_state SET due_at = '2999-01-01T00:00:00+00:00' "
        "WHERE word_id != ? OR task = 'ja2en'",
        (word_id,),
    )
    conn.commit()
    conn.close()


def test_migration_014_creates_gemini_grade_log(
    loaded_client: TestClient, db_path: Path
) -> None:
    conn = sqlite3.connect(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(gemini_grade_log)")}
    conn.close()
    assert cols == {
        "id", "created_at", "model", "latency_ms", "word_id", "direction",
        "typed_answer", "cloze_sentence", "cloze_target_form",
        "verdict", "explanation", "alternates_json", "clarified_gloss",
        "error", "final_correct",
    }


def test_grader_logs_correct_verdict(
    loaded_client: TestClient, db_path: Path
) -> None:
    """A wrong-but-accepted typed answer writes a row with the grader's
    verdict and the final_correct flag the user saw."""
    q0 = loaded_client.get("/api/session/next").json()
    word_id = q0["word_id"]
    _seed_only_card(db_path, word_id)

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["word_id"] == word_id
    # Force the path through the LLM grader by sending a typed answer
    # the deterministic grader rejects.
    fake = gemini.SemanticGrade(
        verdict="accept",
        explanation="close enough — common typo",
        alternates=(),
    )
    with patch.object(gemini, "grade_semantic", return_value=fake):
        body = loaded_client.post(
            "/api/session/answer",
            json={
                "word_id": word_id,
                "direction": nxt["direction"],
                "timed_out": False,
                "typed_answer": "definitely-wrong-typo",
                "mode": nxt["mode"],
                "latency_ms": 1500,
            },
        ).json()
    assert body["correct"] is True  # "accept" passes
    assert body["feedback"]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM gemini_grade_log WHERE word_id = ? ORDER BY id DESC",
        (word_id,),
    ).fetchall()
    conn.close()
    assert len(rows) == 1, rows
    row = dict(rows[0])
    assert row["verdict"] == "accept"
    assert row["explanation"] == "close enough — common typo"
    assert row["typed_answer"] == "definitely-wrong-typo"
    assert row["direction"] == nxt["direction"]
    assert row["final_correct"] == 1
    assert row["error"] is None
    assert row["latency_ms"] >= 0
    assert json.loads(row["alternates_json"]) == []


def test_grader_logs_gemini_unavailable_as_incorrect(
    loaded_client: TestClient, db_path: Path
) -> None:
    """When the LLM grader raises GeminiUnavailable we still log the
    attempt, with error populated and final_correct=0."""
    q0 = loaded_client.get("/api/session/next").json()
    word_id = q0["word_id"]
    _seed_only_card(db_path, word_id)

    nxt = loaded_client.get("/api/session/next").json()
    with patch.object(
        gemini, "grade_semantic",
        side_effect=gemini.GeminiUnavailable("no api key"),
    ):
        body = loaded_client.post(
            "/api/session/answer",
            json={
                "word_id": word_id,
                "direction": nxt["direction"],
                "timed_out": False,
                "typed_answer": "definitely-wrong-typo",
                "mode": nxt["mode"],
                "latency_ms": 1500,
            },
        ).json()
    assert body["correct"] is False

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM gemini_grade_log WHERE word_id = ?",
        (word_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["verdict"] is None
    assert row["error"] == "no api key"
    assert row["final_correct"] == 0


def test_gemini_log_endpoint_returns_rows_newest_first(
    loaded_client: TestClient, db_path: Path
) -> None:
    """GET /api/gemini_log surfaces the rows joined with word fields."""
    q0 = loaded_client.get("/api/session/next").json()
    word_id = q0["word_id"]
    _seed_only_card(db_path, word_id)

    fake_a = gemini.SemanticGrade(
        verdict="correct", explanation="synonym", alternates=("dog",)
    )
    fake_b = gemini.SemanticGrade(
        verdict="incorrect", explanation="different word", alternates=()
    )
    nxt = loaded_client.get("/api/session/next").json()
    with patch.object(gemini, "grade_semantic", return_value=fake_a):
        loaded_client.post(
            "/api/session/answer",
            json={
                "word_id": word_id, "direction": nxt["direction"],
                "timed_out": False, "typed_answer": "wrong-1",
                "mode": nxt["mode"], "latency_ms": 1000,
            },
        )
    _seed_only_card(db_path, word_id)
    nxt2 = loaded_client.get("/api/session/next").json()
    with patch.object(gemini, "grade_semantic", return_value=fake_b):
        loaded_client.post(
            "/api/session/answer",
            json={
                "word_id": word_id, "direction": nxt2["direction"],
                "timed_out": False, "typed_answer": "wrong-2",
                "mode": nxt2["mode"], "latency_ms": 1100,
            },
        )

    resp = loaded_client.get("/api/gemini_log?limit=10").json()
    entries = resp["entries"]
    assert len(entries) >= 2
    # Newest first.
    assert entries[0]["typed_answer"] == "wrong-2"
    assert entries[0]["verdict"] == "incorrect"
    assert entries[1]["typed_answer"] == "wrong-1"
    assert entries[1]["verdict"] == "correct"
    assert entries[1]["alternates"] == ["dog"]
    # Word fields joined in.
    assert entries[0]["word_id"] == word_id
    assert entries[0]["word_english"]
