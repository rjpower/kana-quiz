"""Allow the ``cloze_choice`` task in task_state.

Migration 011 created ``task_state`` with a ``CHECK(task IN (...))`` constraint
listing the four tasks that existed then. Selection-style cloze adds a fifth
task id, ``cloze_choice``, so existing databases need the constraint widened.
SQLite can't ALTER a CHECK constraint in place, so we rebuild the table — the
same dance migration 012 used for ``word_alternates``.

Idempotent: fresh installs run migration 011 against the current ``TASKS``
tuple (which already includes ``cloze_choice``), so the constraint is already
correct and we skip the rebuild.
"""

import sqlite3

# Frozen here rather than read from ``task_state.TASKS`` so this migration's
# result is stable even if the runtime task set changes again later.
_TASKS = ("en2ja", "ja2en", "cloze", "cloze_choice", "sentence_listen")


def up(conn: sqlite3.Connection) -> None:
    existing = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_state'"
    ).fetchone()
    if existing is not None and "'cloze_choice'" in (existing[0] or ""):
        return

    check = ", ".join(f"'{t}'" for t in _TASKS)
    conn.execute(
        f"""
        CREATE TABLE task_state_new (
          word_id INTEGER NOT NULL REFERENCES words(id) ON DELETE CASCADE,
          task TEXT NOT NULL CHECK(task IN ({check})),
          ease REAL NOT NULL DEFAULT 2.5,
          interval_days REAL NOT NULL DEFAULT 0,
          repetitions INTEGER NOT NULL DEFAULT 0,
          due_at TEXT,
          introduced_at TEXT,
          PRIMARY KEY (word_id, task)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO task_state_new
          (word_id, task, ease, interval_days, repetitions, due_at, introduced_at)
        SELECT word_id, task, ease, interval_days, repetitions, due_at, introduced_at
          FROM task_state
        """
    )
    conn.execute("DROP TABLE task_state")
    conn.execute("ALTER TABLE task_state_new RENAME TO task_state")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_task_state_due ON task_state(due_at)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_task_state_task_due ON task_state(task, due_at)"
    )
