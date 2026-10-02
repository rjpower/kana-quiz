"""Per-deck draw order for new cards.

``decks.pick_order`` is 'random' (sample within the deck's level, the
behaviour every deck had) or 'listed' (draw in import order, word id
ascending). A deck built from a frequency list sets 'listed' so the common
words come first.
"""

import sqlite3


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def up(conn: sqlite3.Connection) -> None:
    if "pick_order" not in _columns(conn, "decks"):
        conn.execute(
            "ALTER TABLE decks ADD COLUMN pick_order TEXT NOT NULL DEFAULT 'random'"
            " CHECK(pick_order IN ('random', 'listed'))"
        )
