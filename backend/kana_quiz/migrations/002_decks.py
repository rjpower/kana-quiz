"""Decks: words gain a deck_id; level orders new-card draw priority."""

import sqlite3


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS decks (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL UNIQUE,
          level INTEGER NOT NULL DEFAULT 5,
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_decks_level ON decks(level)")

    cur = conn.execute(
        "INSERT OR IGNORE INTO decks (name, level) VALUES ('Default', 3)"
    )
    if cur.rowcount and cur.lastrowid:
        default_id = cur.lastrowid
    else:
        default_id = conn.execute(
            "SELECT id FROM decks WHERE name = 'Default'"
        ).fetchone()["id"]

    word_cols = _columns(conn, "words")
    if "deck_id" not in word_cols:
        conn.execute(
            "ALTER TABLE words ADD COLUMN deck_id INTEGER REFERENCES decks(id)"
        )
    conn.execute("UPDATE words SET deck_id = ? WHERE deck_id IS NULL", (default_id,))
    conn.execute("CREATE INDEX IF NOT EXISTS ix_words_deck ON words(deck_id)")
