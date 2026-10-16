"""The quick-add deck.

Words dropped in through ``POST /api/words/quick`` land in a deck named
``Quick``. The ``Takoboto`` deck held the same kind of word, entered by hand
from the Takoboto dictionary app, so it takes the new name.
"""

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    taken = conn.execute("SELECT id FROM decks WHERE name = 'Quick'").fetchone()
    if taken is None:
        conn.execute("UPDATE decks SET name = 'Quick' WHERE name = 'Takoboto'")
