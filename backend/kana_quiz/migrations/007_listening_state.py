"""listening_state — per-word SRS track for sentence-translation listening.

Listening drills (hear a Japanese sentence, type the English meaning)
exercise audio comprehension as a separate skill from recall. We park
them on their own SRS state so a card's listening progress doesn't
distort the recall ease/interval bookkeeping and vice versa.

One row per word_id; direction is implicit (JA audio → EN typed
translation). The columns mirror card_state so the existing
``srs.schedule()`` machinery applies without translation.
"""

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS listening_state (
          word_id INTEGER PRIMARY KEY REFERENCES words(id) ON DELETE CASCADE,
          ease REAL NOT NULL DEFAULT 2.5,
          interval_days REAL NOT NULL DEFAULT 0,
          repetitions INTEGER NOT NULL DEFAULT 0,
          due_at TEXT,
          introduced_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_listening_state_due ON listening_state(due_at)"
    )
