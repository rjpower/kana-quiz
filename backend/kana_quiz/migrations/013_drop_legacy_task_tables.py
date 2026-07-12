"""Drop legacy SRS task tables after task_state backfill.

Migration 011 copied card, cloze, and listening SRS state into the unified
``task_state`` table. The runtime no longer reads the legacy tables, so keeping
them around would invite stale writes and make the schema look split-brained.
"""

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    """Remove legacy task-state tables now that task_state is canonical."""
    conn.execute("DROP TABLE IF EXISTS card_state")
    conn.execute("DROP TABLE IF EXISTS cloze_state")
    conn.execute("DROP TABLE IF EXISTS listening_state")
