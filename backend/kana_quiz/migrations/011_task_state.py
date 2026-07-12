"""Unified SRS task_state table backfilled from existing task-specific lanes."""

import sqlite3

from kana_quiz.task_state import TASKS, TASK_CLOZE, TASK_SENTENCE_LISTEN


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS task_state (
          word_id INTEGER NOT NULL REFERENCES words(id) ON DELETE CASCADE,
          task TEXT NOT NULL CHECK(task IN ({_quoted_tasks()})),
          ease REAL NOT NULL DEFAULT 2.5,
          interval_days REAL NOT NULL DEFAULT 0,
          repetitions INTEGER NOT NULL DEFAULT 0,
          due_at TEXT,
          introduced_at TEXT,
          PRIMARY KEY (word_id, task)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_task_state_due ON task_state(due_at)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_task_state_task_due ON task_state(task, due_at)"
    )

    conn.execute(
        """
        INSERT OR IGNORE INTO task_state
          (word_id, task, ease, interval_days, repetitions, due_at, introduced_at)
        SELECT word_id, direction, ease, interval_days, repetitions, due_at, introduced_at
          FROM card_state
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO task_state
          (word_id, task, ease, interval_days, repetitions, due_at, introduced_at)
        SELECT word_id, ?, ease, interval_days, repetitions, due_at, introduced_at
          FROM cloze_state
        """,
        (TASK_CLOZE,),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO task_state
          (word_id, task, ease, interval_days, repetitions, due_at, introduced_at)
        SELECT word_id, ?, ease, interval_days, repetitions, due_at, introduced_at
          FROM listening_state
        """,
        (TASK_SENTENCE_LISTEN,),
    )


def _quoted_tasks() -> str:
    return ", ".join(f"'{task}'" for task in TASKS)
