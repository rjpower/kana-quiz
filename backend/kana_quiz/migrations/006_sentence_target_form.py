"""sentence_cache.target_form — the surface form of the target word in the sentence.

Gemini's example sentences embed the target word in whatever conjugation
fits the sentence (e.g. 役立ちます for the dictionary form 役立つ). The
cloze quiz mode needs to know which span to blank out, and the grader
needs to accept that surface form as correct — neither is derivable
from the dictionary headword alone.

Default is the empty string so existing rows are detectable as "needs
backfill". The sentence prefetch worker treats empty target_form as a
cache miss and regenerates the entry with the new prompt that returns
target_form alongside the sentence + mnemonic.
"""

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sentence_cache)").fetchall()}
    if "target_form" not in cols:
        conn.execute(
            "ALTER TABLE sentence_cache ADD COLUMN target_form TEXT NOT NULL DEFAULT ''"
        )
