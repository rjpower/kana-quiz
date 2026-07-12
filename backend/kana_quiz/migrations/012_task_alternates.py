"""word_alternates accepts canonical task ids.

The listening task used the temporary key ``listen`` while it lived outside the
unified task model. Move those rows to ``sentence_listen`` and rebuild the
constraint so future cached Gemini answers use canonical task ids.
"""

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    existing_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='word_alternates'"
    ).fetchone()
    if existing_sql is not None and "'sentence_listen'" in (existing_sql[0] or ""):
        return

    conn.execute(
        """
        CREATE TABLE word_alternates_new (
          id INTEGER PRIMARY KEY,
          word_id INTEGER NOT NULL REFERENCES words(id) ON DELETE CASCADE,
          direction TEXT NOT NULL CHECK(direction IN ('en2ja','ja2en','cloze','sentence_listen')),
          alternate TEXT NOT NULL,
          source TEXT NOT NULL,
          added_at TEXT NOT NULL DEFAULT (datetime('now')),
          UNIQUE(word_id, direction, alternate)
        )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO word_alternates_new
          (id, word_id, direction, alternate, source, added_at)
        SELECT id,
               word_id,
               CASE direction WHEN 'listen' THEN 'sentence_listen' ELSE direction END,
               alternate,
               source,
               added_at
          FROM word_alternates
         WHERE direction IN ('en2ja', 'ja2en', 'cloze', 'listen', 'sentence_listen')
        """
    )
    conn.execute("DROP TABLE word_alternates")
    conn.execute("ALTER TABLE word_alternates_new RENAME TO word_alternates")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_word_alternates_lookup "
        "ON word_alternates(word_id, direction)"
    )
