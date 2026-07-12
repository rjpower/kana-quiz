"""Persist every Gemini semantic-grading call for posterity.

We instrument the LLM grader so its decisions can be audited over time:
inputs (word + typed answer + direction + optional cloze context),
the model's verdict / explanation / alternates / clarified gloss, the
final correct/incorrect outcome the caller settled on, plus latency and
model name. Rows are append-only — there's no eviction logic here so the
table can be inspected back to the first call.
"""

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gemini_grade_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at TEXT NOT NULL,
          model TEXT NOT NULL,
          latency_ms INTEGER NOT NULL,
          word_id INTEGER NOT NULL,
          direction TEXT NOT NULL,
          typed_answer TEXT NOT NULL,
          cloze_sentence TEXT,
          cloze_target_form TEXT,
          verdict TEXT,
          explanation TEXT,
          alternates_json TEXT,
          clarified_gloss TEXT,
          error TEXT,
          final_correct INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_gemini_grade_log_word_id "
        "ON gemini_grade_log(word_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_gemini_grade_log_created_at "
        "ON gemini_grade_log(created_at)"
    )
