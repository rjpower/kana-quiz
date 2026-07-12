"""word_alternates accepts ``direction = 'listen'``.

The listening / sentence-translation quiz mode also wants to cache
Gemini-blessed user answers so a repeat submission doesn't burn
another LLM round-trip. We reuse the existing word_alternates table
(one row per ``(word_id, direction, alternate)``) rather than spinning
up a parallel one — the lookup pattern is identical, only the
direction value is new.

SQLite can't ALTER a CHECK constraint in place, so we rebuild the
table: create the new shape, copy rows, drop the old table, rename.
Idempotent via PRAGMA table_info: skip the work if the constraint
already includes 'listen'.
"""

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    # Cheap idempotency probe: the SQL of the current table is recorded
    # in sqlite_master. If it already mentions 'listen', we're already
    # at the target shape (the migration ran in a previous boot).
    existing_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='word_alternates'"
    ).fetchone()
    if existing_sql is not None and "'listen'" in (existing_sql[0] or ""):
        return

    conn.execute(
        """
        CREATE TABLE word_alternates_new (
          id INTEGER PRIMARY KEY,
          word_id INTEGER NOT NULL REFERENCES words(id) ON DELETE CASCADE,
          direction TEXT NOT NULL CHECK(direction IN ('en2ja','ja2en','listen')),
          alternate TEXT NOT NULL,
          source TEXT NOT NULL,
          added_at TEXT NOT NULL DEFAULT (datetime('now')),
          UNIQUE(word_id, direction, alternate)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO word_alternates_new (id, word_id, direction, alternate, source, added_at)
        SELECT id, word_id, direction, alternate, source, added_at FROM word_alternates
        """
    )
    conn.execute("DROP TABLE word_alternates")
    conn.execute("ALTER TABLE word_alternates_new RENAME TO word_alternates")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_word_alternates_lookup "
        "ON word_alternates(word_id, direction)"
    )
