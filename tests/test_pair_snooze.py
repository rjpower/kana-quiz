"""Paired-card snooze: answering one recall direction pushes the sibling out.

The mechanism lives in ``_record_task_result`` (routes/session.py): after a
recall answer we bump the *other* direction's ``due_at`` forward so the same
lexeme doesn't come back-to-back. The push only ever moves due_at forward
(never pulls a mature sibling in) and never touches supplemental lanes.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient


def _ts(db_path: Path, word_id: int, task: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM task_state WHERE word_id = ? AND task = ?",
        (word_id, task),
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


def _answer_en2ja_correct(client: TestClient, word_id: int):
    return client.post(
        "/api/session/answer",
        json={
            "word_id": word_id,
            "direction": "en2ja",
            "chosen_index": 0,
            "correct_index": 0,
            "timed_out": False,
            "latency_ms": 1000,
        },
    )


def test_sibling_direction_is_snoozed(loaded_client: TestClient, db_path: Path) -> None:
    q = loaded_client.get("/api/session/next").json()
    wid = q["word_id"]
    # A hiragana sample word has both recall rows from introduction.
    assert _ts(db_path, wid, "ja2en"), "expected a sibling ja2en row"

    resp = _answer_en2ja_correct(loaded_client, wid)
    assert resp.status_code == 200

    sib = _ts(db_path, wid, "ja2en")
    due = datetime.fromisoformat(sib["due_at"])
    # Default snooze is 12h; assert it moved well past now.
    assert due > datetime.now(timezone.utc) + timedelta(hours=6)
    # The answered direction itself follows real SRS (first correct -> 1 day).
    assert _ts(db_path, wid, "en2ja")["interval_days"] >= 1.0


def test_snooze_never_clobbers_a_mature_sibling(
    loaded_client: TestClient, db_path: Path
) -> None:
    q = loaded_client.get("/api/session/next").json()
    wid = q["word_id"]
    far = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE task_state SET due_at = ? WHERE word_id = ? AND task = 'ja2en'",
        (far, wid),
    )
    conn.commit()
    conn.close()

    _answer_en2ja_correct(loaded_client, wid)

    due = datetime.fromisoformat(_ts(db_path, wid, "ja2en")["due_at"])
    # The 12h push must not pull a 30-day-out sibling forward.
    assert due > datetime.now(timezone.utc) + timedelta(days=20)


def test_kill_switch_disables_snooze(
    loaded_client: TestClient, db_path: Path, monkeypatch
) -> None:
    import kana_quiz.routes.session as rs

    monkeypatch.setattr(rs, "PAIR_SNOOZE_SECONDS", 0)
    q = loaded_client.get("/api/session/next").json()
    wid = q["word_id"]
    before = _ts(db_path, wid, "ja2en")["due_at"]

    _answer_en2ja_correct(loaded_client, wid)

    assert _ts(db_path, wid, "ja2en")["due_at"] == before


def test_loanword_snooze_is_safe(client: TestClient, db_path: Path) -> None:
    # A katakana-only word gets only an en2ja card — no sibling to snooze.
    sample = "kana,english\nコーヒー,coffee\n".encode("utf-8")
    deck = next(d["id"] for d in client.get("/api/decks").json() if d["name"] == "Default")
    client.post(
        "/api/import",
        files={"file": ("k.csv", sample, "text/csv")},
        data={"deck_id": str(deck)},
    )
    q = client.get("/api/session/next").json()
    wid = q["word_id"]
    assert _ts(db_path, wid, "en2ja"), "loanword should have an en2ja card"
    assert _ts(db_path, wid, "ja2en") == {}, "loanword should have no ja2en card"

    resp = _answer_en2ja_correct(client, wid)
    assert resp.status_code == 200
    # The snooze UPDATE must not conjure a phantom ja2en row.
    assert _ts(db_path, wid, "ja2en") == {}
