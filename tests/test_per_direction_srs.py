"""New per-direction SRS, gave_up outcome, deck-level new-card draw, migration idempotency."""

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient


def _default_deck_id(client: TestClient) -> int:
    decks = client.get("/api/decks").json()
    return next(d["id"] for d in decks if d["name"] == "Default")


def _task_state(db_path: Path, word_id: int, direction: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM task_state WHERE word_id = ? AND task = ?",
        (word_id, direction),
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


def _force_only_due_for_direction(
    db_path: Path, word_id: int, direction: str
) -> None:
    """Make exactly (word_id, direction) the only card the picker can land on."""
    conn = sqlite3.connect(db_path)
    far = "2999-01-01T00:00:00+00:00"
    near = "2020-01-01T00:00:00+00:00"
    # Ensure all words have recall task rows so the introduction branch is dead.
    rows = conn.execute("SELECT id FROM words").fetchall()
    for r in rows:
        for d in ("en2ja", "ja2en"):
            conn.execute(
                """
                INSERT OR IGNORE INTO task_state
                  (word_id, task, ease, interval_days, repetitions,
                   due_at, introduced_at)
                VALUES (?, ?, 2.5, 0, 0, ?, ?)
                """,
                (r[0], d, near, near),
            )
    # OR IGNORE skips rows the picker already reserved, and those carry a NULL
    # introduced_at until the user answers. Row-exists is not introduced-ness,
    # so stamp them or the introduction branch stays alive after all.
    conn.execute(
        "UPDATE task_state SET introduced_at = ? WHERE introduced_at IS NULL", (near,)
    )
    conn.execute("UPDATE task_state SET due_at = ?", (far,))
    conn.execute(
        "UPDATE task_state SET due_at = ? WHERE word_id = ? AND task = ?",
        (near, word_id, direction),
    )
    conn.commit()
    conn.close()


def test_correct_advances_only_target_direction(
    loaded_client: TestClient, db_path: Path
) -> None:
    """Answering en2ja correctly leaves the ja2en task_state row alone."""
    # Pick a word, isolate its en2ja direction.
    q = loaded_client.get("/api/session/next").json()
    word_id = q["word_id"]
    _force_only_due_for_direction(db_path, word_id, "en2ja")

    nxt = loaded_client.get("/api/session/next").json()
    assert nxt["word_id"] == word_id
    assert nxt["direction"] == "en2ja"

    before_other = _task_state(db_path, word_id, "ja2en")

    loaded_client.post(
        "/api/session/answer",
        json={
            "word_id": word_id,
            "direction": "en2ja",
            "chosen_index": nxt["correct_index"],
            "correct_index": nxt["correct_index"],
            "timed_out": False,
            "latency_ms": 1200,
        },
    )

    after_target = _task_state(db_path, word_id, "en2ja")
    after_other = _task_state(db_path, word_id, "ja2en")

    # en2ja advanced
    assert after_target["repetitions"] == 1
    assert after_target["interval_days"] >= 1.0
    # ja2en untouched
    assert after_other["repetitions"] == before_other["repetitions"]
    assert after_other["interval_days"] == before_other["interval_days"]
    assert after_other["ease"] == before_other["ease"]


def test_gave_up_records_outcome_and_drops_ease(
    loaded_client: TestClient, db_path: Path
) -> None:
    q = loaded_client.get("/api/session/next").json()
    word_id = q["word_id"]
    direction = q["direction"]
    before = _task_state(db_path, word_id, direction)

    resp = loaded_client.post(
        "/api/session/answer",
        json={
            "word_id": word_id,
            "direction": direction,
            "chosen_index": None,
            "correct_index": q["correct_index"],
            "timed_out": False,
            "gave_up": True,
        },
    )
    body = resp.json()
    assert body["correct"] is False
    assert body["outcome"] == "gave_up"

    after = _task_state(db_path, word_id, direction)
    assert after["ease"] < before["ease"]  # incorrect-grade ease penalty applied
    assert after["repetitions"] == 0

    # The reviews row records outcome distinctly.
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT outcome FROM reviews WHERE word_id = ? ORDER BY id DESC LIMIT 1",
        (word_id,),
    ).fetchone()
    conn.close()
    assert row[0] == "gave_up"


def test_low_level_deck_drained_before_default(
    client: TestClient, db_path: Path
) -> None:
    """Words from a level-1 deck should be picked before words from level-3 Default."""
    # Default has the sample CSV (10 words).
    sample = (
        "kana,english,kanji,tags\n"
        "いぬ,dog,犬,animal\n"
        "ねこ,cat,猫,animal\n"
    ).encode("utf-8")
    default_id = _default_deck_id(client)
    client.post(
        "/api/import",
        files={"file": ("v.csv", sample, "text/csv")},
        data={"deck_id": str(default_id)},
    )

    # Make a new level-1 deck and import distinct words.
    priority = (
        "kana,english\n"
        "あお,blue\n"
        "あか,red\n"
    ).encode("utf-8")
    client.post(
        "/api/import",
        files={"file": ("p.csv", priority, "text/csv")},
        data={"new_deck_name": "Priority", "new_deck_level": "1"},
    )

    # The next two introductions should come from the level-1 deck.
    seen = set()
    for _ in range(2):
        q = client.get("/api/session/next").json()
        seen.add(q["word_id"])
        # answer correctly so introduction tracking moves on
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

    # The two priority words should both have been introduced before any
    # default-deck word.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    deck_id = conn.execute(
        "SELECT id FROM decks WHERE name = 'Priority'"
    ).fetchone()["id"]
    priority_ids = {
        r["id"] for r in conn.execute(
            "SELECT id FROM words WHERE deck_id = ?", (deck_id,)
        ).fetchall()
    }
    conn.close()
    assert seen == priority_ids


def test_init_schema_is_idempotent(tmp_path, monkeypatch) -> None:
    """Running init_schema twice on a fresh DB doesn't raise or double-apply."""
    from kana_quiz.db import init_schema, connect

    path = tmp_path / "kana_quiz.sqlite"
    monkeypatch.setenv("KANA_QUIZ_DB", str(path))
    init_schema()
    init_schema()  # should be a no-op

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        # Each migration should appear exactly once.
        versions = [r["version"] for r in rows]
        assert versions == sorted(set(versions))
        assert 1 in versions
        assert 2 in versions
        # Default deck created exactly once.
        decks = conn.execute("SELECT COUNT(*) AS n FROM decks").fetchone()["n"]
        assert decks == 1
    finally:
        conn.close()
