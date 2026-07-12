"""cloze_state — per-word SRS track for sentence cloze production.

Cloze drills ask the user to produce the Japanese target form inside a
sentence. That is a contextual production skill, not the same signal as basic
word recall, so it gets its own SRS lane. The columns mirror ``card_state`` and
``listening_state`` so the existing scheduler can update it directly.
"""

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cloze_state (
          word_id INTEGER PRIMARY KEY REFERENCES words(id) ON DELETE CASCADE,
          ease REAL NOT NULL DEFAULT 2.5,
          interval_days REAL NOT NULL DEFAULT 0,
          repetitions INTEGER NOT NULL DEFAULT 0,
          due_at TEXT,
          introduced_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_cloze_state_due ON cloze_state(due_at)")
