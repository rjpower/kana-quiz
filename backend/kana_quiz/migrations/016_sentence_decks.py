"""Sentence cards and sprint decks.

Three additions for episode decks imported from kaku transcripts:

* ``words.kind`` — 'word' (default) or 'sentence'. A sentence card holds a
  full transcript line in ``kana`` and its translation in ``english``; it is
  quizzed ja→en type-in only and stays out of MC choice pools, the match
  game, and the supplemental (cloze/listening) lanes.
* ``decks.profile`` — 'standard' or 'sprint'. A sprint deck retires a card
  after its second successful spaced review instead of holding it in
  rotation to the 21-day mastery bar.
* ``words.archived_at`` — when a sprint card retired. An archived card also
  gets ``ignored_at`` stamped so every existing active-card filter excludes
  it without change; ``archived_at`` is what tells "cleared" apart from
  "ignored by hand" in the deck view.
"""

import sqlite3


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def up(conn: sqlite3.Connection) -> None:
    if "kind" not in _columns(conn, "words"):
        conn.execute(
            "ALTER TABLE words ADD COLUMN kind TEXT NOT NULL DEFAULT 'word'"
            " CHECK(kind IN ('word', 'sentence'))"
        )
    if "archived_at" not in _columns(conn, "words"):
        conn.execute("ALTER TABLE words ADD COLUMN archived_at TEXT")
    if "profile" not in _columns(conn, "decks"):
        conn.execute(
            "ALTER TABLE decks ADD COLUMN profile TEXT NOT NULL DEFAULT 'standard'"
            " CHECK(profile IN ('standard', 'sprint'))"
        )
