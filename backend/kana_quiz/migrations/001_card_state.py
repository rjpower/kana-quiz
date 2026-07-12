"""Per-direction SRS: split SRS state out of words into card_state(word, direction)."""

import sqlite3


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS card_state (
          word_id INTEGER NOT NULL REFERENCES words(id) ON DELETE CASCADE,
          direction TEXT NOT NULL CHECK(direction IN ('en2ja','ja2en')),
          ease REAL NOT NULL DEFAULT 2.5,
          interval_days REAL NOT NULL DEFAULT 0,
          repetitions INTEGER NOT NULL DEFAULT 0,
          due_at TEXT,
          introduced_at TEXT,
          PRIMARY KEY (word_id, direction)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_card_state_due ON card_state(due_at)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_card_state_introduced ON card_state(introduced_at)"
    )

    word_cols = _columns(conn, "words")
    has_srs = {"ease", "interval_days", "repetitions", "due_at", "introduced_at"}.issubset(
        word_cols
    )

    # Backfill: one row per (word, direction) — but ONLY for words that were
    # actually introduced in the legacy schema. Words with introduced_at IS NULL
    # are still "fresh" and must not gain card_state rows here, otherwise the
    # picker (which treats "no card_state row" as the new-card sentinel) will
    # never surface them again.
    if has_srs:
        rows = conn.execute(
            """
            SELECT id, ease, interval_days, repetitions, due_at, introduced_at
              FROM words
             WHERE introduced_at IS NOT NULL
            """
        ).fetchall()
        for r in rows:
            for direction in ("en2ja", "ja2en"):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO card_state
                      (word_id, direction, ease, interval_days, repetitions,
                       due_at, introduced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        r["id"],
                        direction,
                        r["ease"],
                        r["interval_days"],
                        r["repetitions"],
                        r["due_at"],
                        r["introduced_at"],
                    ),
                )

    # Add `outcome` to reviews and backfill from the legacy correct/timed_out flags.
    review_cols = _columns(conn, "reviews")
    if "outcome" not in review_cols:
        conn.execute(
            "ALTER TABLE reviews ADD COLUMN outcome TEXT NOT NULL DEFAULT 'incorrect'"
        )
        conn.execute(
            """
            UPDATE reviews
               SET outcome = CASE
                 WHEN correct = 1 THEN 'correct'
                 WHEN timed_out = 1 THEN 'timeout'
                 ELSE 'incorrect'
               END
            """
        )

    # Drop SRS columns from words. SQLite 3.35+ supports DROP COLUMN.
    conn.execute("DROP INDEX IF EXISTS ix_words_due")
    conn.execute("DROP INDEX IF EXISTS ix_words_introduced")
    if has_srs:
        for col in ("ease", "interval_days", "repetitions", "due_at", "introduced_at"):
            conn.execute(f"ALTER TABLE words DROP COLUMN {col}")
