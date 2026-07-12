"""Word alternates: cached Gemini-blessed answer variants for type-in grading.

When the user types an answer that fails the deterministic grader but the
semantic grader judges correct (e.g. a valid synonym we didn't ship), we
persist the normalized variant here so the *next* time they type the same
thing it short-circuits before the LLM call. One row per
(word, direction, alternate) — uniqueness is on the normalized form so
duplicate Gemini suggestions collapse cleanly.
"""

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS word_alternates (
          id INTEGER PRIMARY KEY,
          word_id INTEGER NOT NULL REFERENCES words(id) ON DELETE CASCADE,
          direction TEXT NOT NULL CHECK(direction IN ('en2ja','ja2en')),
          alternate TEXT NOT NULL,
          source TEXT NOT NULL,
          added_at TEXT NOT NULL DEFAULT (datetime('now')),
          UNIQUE(word_id, direction, alternate)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_word_alternates_lookup ON word_alternates(word_id, direction)"
    )
