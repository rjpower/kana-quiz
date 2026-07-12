"""Answer-type toggles: the ``modes`` allow-list on /session/next.

The client sends ``?modes=`` to enable/disable answer types. Disabling a
supplemental track just skips it; disabling a core recall mode (mc / type)
collapses every recall card onto the mode that's left — with MC off, even
brand-new cards go straight to type-in (the user opted out of recognition).
"""

from __future__ import annotations

import sqlite3 as _sqlite
from pathlib import Path

from fastapi.testclient import TestClient


def _only_due_recall(db: Path, keep_id: int, *, ease: float, reps: int) -> None:
    """Make ``keep_id``'s en2ja card the single due recall card at (ease, reps)."""
    conn = _sqlite.connect(db)
    now_iso = "2020-01-01T00:00:00+00:00"
    future_iso = "2999-01-01T00:00:00+00:00"
    for (wid,) in conn.execute("SELECT id FROM words").fetchall():
        for d in ("en2ja", "ja2en"):
            conn.execute(
                """
                INSERT OR IGNORE INTO task_state
                  (word_id, task, ease, interval_days, repetitions, due_at, introduced_at)
                VALUES (?, ?, 2.5, 0, 0, ?, ?)
                """,
                (wid, d, now_iso, now_iso),
            )
    conn.execute("UPDATE task_state SET due_at = ? WHERE word_id != ?", (future_iso, keep_id))
    conn.execute(
        "UPDATE task_state SET ease = ?, repetitions = ?, due_at = ? "
        "WHERE word_id = ? AND task = 'en2ja'",
        (ease, reps, now_iso, keep_id),
    )
    # Park the kept word's ja2en so only its en2ja is due (deterministic dir).
    conn.execute(
        "UPDATE task_state SET due_at = ? WHERE word_id = ? AND task = 'ja2en'",
        (future_iso, keep_id),
    )
    conn.commit()
    conn.close()


def test_mc_off_forces_type_in_even_for_new_card(loaded_client: TestClient) -> None:
    """With MC disabled, a brand-new card (which would normally be MC) is
    served as type-in — the literal interpretation of 'MC off'."""
    q = loaded_client.get("/api/session/next?modes=type").json()
    assert q["mode"] == "type", q
    assert q["choices"] == []
    # It really is a fresh introduction, not a graduated card.
    assert q["introduction"] is not None, q


def test_type_off_forces_mc_for_graduated_card(
    loaded_client: TestClient, db_path: Path
) -> None:
    """With type-in disabled, a graduated card that would normally be type-in
    is served as MC (recognition-only practice)."""
    word_id = loaded_client.get("/api/session/next").json()["word_id"]
    _only_due_recall(db_path, word_id, ease=2.6, reps=3)

    nxt = loaded_client.get("/api/session/next?modes=mc").json()
    assert nxt["word_id"] == word_id
    assert nxt["mode"] == "mc", nxt
    assert len(nxt["choices"]) == 4


def test_both_core_modes_off_defensively_serves_type(
    loaded_client: TestClient, db_path: Path
) -> None:
    """Neither mc nor type enabled is a degenerate config the client prevents,
    but the backend must still serve recall — it falls back to type-in."""
    word_id = loaded_client.get("/api/session/next").json()["word_id"]
    _only_due_recall(db_path, word_id, ease=2.6, reps=3)

    nxt = loaded_client.get("/api/session/next?modes=cloze").json()
    assert nxt["mode"] == "type", nxt


def test_unknown_modes_token_falls_back_to_all(loaded_client: TestClient) -> None:
    """A malformed allow-list must never strand the user — it defaults to all
    modes enabled, so a fresh card comes back as MC as usual."""
    q = loaded_client.get("/api/session/next?modes=banana").json()
    assert q["mode"] == "mc", q


def test_empty_modes_value_falls_back_to_all(loaded_client: TestClient) -> None:
    q = loaded_client.get("/api/session/next?modes=").json()
    assert q["mode"] == "mc", q


def test_modes_omitted_keeps_default_behavior(loaded_client: TestClient) -> None:
    """No modes param at all behaves exactly as before (back-compat)."""
    q = loaded_client.get("/api/session/next").json()
    assert q["mode"] == "mc", q
    assert len(q["choices"]) == 4
