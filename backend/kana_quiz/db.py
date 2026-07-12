"""SQLite connection + schema.

The database path is taken from the ``KANA_QUIZ_DB`` env var and falls back to
``data/kana_quiz.sqlite`` relative to the current working directory. Each
request gets a short-lived :class:`sqlite3.Connection` via :func:`get_conn`,
which is used as a FastAPI dependency.
"""

import os
import sqlite3
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS words (
  id INTEGER PRIMARY KEY,
  kana TEXT NOT NULL UNIQUE,
  english TEXT NOT NULL,
  kanji TEXT,
  tags TEXT,
  ease REAL NOT NULL DEFAULT 2.5,
  interval_days REAL NOT NULL DEFAULT 0,
  repetitions INTEGER NOT NULL DEFAULT 0,
  due_at TEXT NOT NULL,
  introduced_at TEXT
);
-- NOTE: SRS-column indexes (ix_words_due, ix_words_introduced) were here
-- pre-migration-001. Later migrations move SRS into task_state; recreating
-- the old words indexes here would fail on re-init since DROP COLUMN took the
-- columns away. Fresh installs run migrations immediately so the indexes never
-- mattered for them anyway.

CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY,
  word_id INTEGER NOT NULL REFERENCES words(id) ON DELETE CASCADE,
  asked_at TEXT NOT NULL,
  direction TEXT NOT NULL,
  correct INTEGER NOT NULL,
  timed_out INTEGER NOT NULL,
  latency_ms INTEGER
);
CREATE INDEX IF NOT EXISTS ix_reviews_asked ON reviews(asked_at);
CREATE INDEX IF NOT EXISTS ix_reviews_word ON reviews(word_id);

CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL,
  ended_at TEXT
);

-- TTS BLOBs, keyed by the exact (text, voice, model) tuple so we can swap
-- voice/model without invalidating existing rows. Clips are small (~5-30 KB
-- MP3) and there are only hundreds of them, so storing inline keeps the
-- deployment surface to a single sqlite file.
CREATE TABLE IF NOT EXISTS audio_cache (
  id INTEGER PRIMARY KEY,
  text TEXT NOT NULL,
  voice TEXT NOT NULL,
  model TEXT NOT NULL,
  mime TEXT NOT NULL,
  blob BLOB NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(text, voice, model)
);

-- Cached LLM-generated example sentences, one per (word, model) tuple.
-- Generated on demand from the answer-reveal screen and persisted forever:
-- sentences are cheap to make but expensive to re-roll if the first one
-- happened to be a particularly memorable mnemonic for the user.
CREATE TABLE IF NOT EXISTS sentence_cache (
  id INTEGER PRIMARY KEY,
  word_id INTEGER NOT NULL REFERENCES words(id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  japanese TEXT NOT NULL,
  english TEXT NOT NULL,
  mnemonic TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(word_id, model)
);
CREATE INDEX IF NOT EXISTS ix_sentence_cache_word ON sentence_cache(word_id);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Lightweight in-place migrations for schemas predating new columns.

    Only `mnemonic` so far — added after sentence_cache was already in the
    wild on the user's prod DB. ALTER TABLE on a missing column raises, so
    we probe pragma_table_info first and add as needed.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sentence_cache)")}
    if "mnemonic" not in cols:
        conn.execute(
            "ALTER TABLE sentence_cache ADD COLUMN mnemonic TEXT NOT NULL DEFAULT ''"
        )


def db_path() -> Path:
    """Resolve the sqlite path from ``KANA_QUIZ_DB`` or the default location."""
    env = os.environ.get("KANA_QUIZ_DB")
    if env:
        return Path(env)
    return Path.cwd() / "data" / "kana_quiz.sqlite"


def connect() -> sqlite3.Connection:
    """Open a sqlite3 connection with sensible pragmas."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def init_schema() -> None:
    """Create tables and indexes if they don't already exist."""
    from kana_quiz.migrations import run_migrations

    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        run_migrations(conn)
    finally:
        conn.close()


def get_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency that yields a connection and closes it afterwards."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
