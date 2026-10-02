"""Where an example sentence came from.

``sentence_cache.source`` is 'generated' for a Gemini-written sentence and a
tag such as 'hotspot' for a line taken from a show's transcript, which the
study screen labels.
"""

import sqlite3


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def up(conn: sqlite3.Connection) -> None:
    if "source" not in _columns(conn, "sentence_cache"):
        conn.execute(
            "ALTER TABLE sentence_cache ADD COLUMN source TEXT NOT NULL DEFAULT 'generated'"
        )
