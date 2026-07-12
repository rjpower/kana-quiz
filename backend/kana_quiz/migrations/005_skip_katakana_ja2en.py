"""Drop ja2en recall state for katakana-only words.

Loanwords/transliterations (コンピュータ, ローマ, パン) are written in pure
katakana; quizzing the student on what コンピュータ means in English is
busy-work because the kana itself is a phonetic rendering of the English
answer. The picker now only stamps the en2ja direction for new katakana
words; this migration deletes the ja2en rows that already exist.
"""

import sqlite3

from kana_quiz.models import is_katakana_only


def up(conn: sqlite3.Connection) -> None:
    # Check BOTH columns — the Anki importer often stuffs lowercased
    # hiragana into `kana` and keeps the real katakana in the `kanji`
    # slot (ぱいなっぷる + パイナップル, あんけーと + アンケート, etc.).
    # A katakana hit on either column is enough to flag the card.
    rows = conn.execute("SELECT id, kana, kanji FROM words").fetchall()
    katakana_ids = [
        r["id"]
        for r in rows
        if is_katakana_only(r["kana"] or "") or is_katakana_only(r["kanji"] or "")
    ]
    if not katakana_ids:
        return

    # Stage the IDs in a temp table so the DELETE doesn't trip SQLite's
    # bound-parameter limit on large decks (the Anki import lands a few
    # thousand katakana words).
    conn.execute("CREATE TEMP TABLE _kk_ids (id INTEGER PRIMARY KEY)")
    conn.executemany(
        "INSERT INTO _kk_ids (id) VALUES (?)",
        [(wid,) for wid in katakana_ids],
    )
    card_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='card_state'"
    ).fetchone()
    if card_exists is not None:
        conn.execute(
            """
            DELETE FROM card_state
             WHERE direction = 'ja2en'
               AND word_id IN (SELECT id FROM _kk_ids)
            """
        )
    task_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_state'"
    ).fetchone()
    if task_exists is not None:
        conn.execute(
            """
            DELETE FROM task_state
             WHERE task = 'ja2en'
               AND word_id IN (SELECT id FROM _kk_ids)
            """
        )
    conn.execute("DROP TABLE _kk_ids")
