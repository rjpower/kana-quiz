"""Katakana-only words skip the ja2en direction."""

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from kana_quiz.models import is_katakana_only


def test_helper_classifies_katakana() -> None:
    assert is_katakana_only("コンピュータ")
    assert is_katakana_only("ローマ")
    assert is_katakana_only("コーヒー")
    assert not is_katakana_only("ねこ")
    assert not is_katakana_only("お茶")
    assert not is_katakana_only("ねコ")
    assert not is_katakana_only("")


def test_new_katakana_word_only_gets_en2ja(client: TestClient, db_path: Path) -> None:
    csv = "kana,english,kanji,tags\nコンピュータ,computer,,tech\n".encode()
    decks = client.get("/api/decks").json()
    default_id = next(d["id"] for d in decks if d["name"] == "Default")
    resp = client.post(
        "/api/import",
        files={"file": ("vocab.csv", csv, "text/csv")},
        data={"deck_id": str(default_id)},
    )
    assert resp.status_code == 200, resp.text

    # Drive the picker once to introduce the word and stamp task_state.
    q = client.get("/api/session/next").json()
    assert q["direction"] == "en2ja"  # only direction available
    assert q["prompt"] == "computer"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT task FROM task_state WHERE word_id = ? AND task IN ('en2ja', 'ja2en')",
        (q["word_id"],),
    ).fetchall()
    conn.close()
    assert {r["task"] for r in rows} == {"en2ja"}


def test_migration_005_drops_existing_ja2en(db_path: Path) -> None:
    """Run migrations on a DB that already has ja2en katakana rows."""
    from kana_quiz.db import init_schema

    # First init brings the schema fully up to date — including 005.
    init_schema()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Insert a katakana word + a hiragana word, plus stale task_state rows
    # mimicking what an earlier-version DB would have left behind.
    conn.execute("BEGIN")
    deck_id = conn.execute("SELECT id FROM decks WHERE name='Default'").fetchone()["id"]
    cur = conn.execute(
        "INSERT INTO words (kana, english, kanji, tags, deck_id) VALUES (?,?,?,?,?)",
        ("コンピュータ", "computer", None, "", deck_id),
    )
    kk_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO words (kana, english, kanji, tags, deck_id) VALUES (?,?,?,?,?)",
        ("ねこ", "cat", "猫", "", deck_id),
    )
    hk_id = cur.lastrowid
    for wid in (kk_id, hk_id):
        for direction in ("en2ja", "ja2en"):
            conn.execute(
                "INSERT INTO task_state (word_id, task, ease, interval_days, "
                "repetitions, due_at, introduced_at) VALUES (?, ?, 2.5, 0, 0, "
                "datetime('now'), datetime('now'))",
                (wid, direction),
            )
    # Roll the version back so re-running init_schema replays migration 005.
    conn.execute("DELETE FROM schema_migrations WHERE version = 5")
    conn.commit()
    conn.close()

    init_schema()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    kk_dirs = {
        r["task"]
        for r in conn.execute(
            "SELECT task FROM task_state WHERE word_id = ? AND task IN ('en2ja', 'ja2en')", (kk_id,)
        )
    }
    hk_dirs = {
        r["task"]
        for r in conn.execute(
            "SELECT task FROM task_state WHERE word_id = ? AND task IN ('en2ja', 'ja2en')", (hk_id,)
        )
    }
    conn.close()
    assert kk_dirs == {"en2ja"}, "katakana ja2en should be dropped"
    assert hk_dirs == {"en2ja", "ja2en"}, "hiragana stays bidirectional"
