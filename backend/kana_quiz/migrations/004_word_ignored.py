"""Per-word ignore flag — hide a word from the quiz queue without deleting it."""

import sqlite3


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def up(conn: sqlite3.Connection) -> None:
    cols = _columns(conn, "words")
    if "ignored_at" not in cols:
        conn.execute("ALTER TABLE words ADD COLUMN ignored_at TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_words_ignored ON words(ignored_at)"
    )
