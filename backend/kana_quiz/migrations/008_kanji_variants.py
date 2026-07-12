"""kanji_variants — kana → alternate kanji writings lookup.

Bridges writing-form mismatches in cloze sentence rendering: Gemini may
emit a sentence that uses a kanji variant the deck doesn't carry (e.g.
deck has 食料 but the sentence writes 食糧, both しょくりょう), or a
kanji writing of an ostensibly-kana word (e.g. できるだけ surfaces as
出来るだけ in the sentence). The cloze span locator consults this table
after the standard kanji-stem search fails.

Source: JMdict (via the `jmdict-simplified` project, full jmdict-eng
release). We subset to the kana readings present in our deck (~6k words),
which compresses to ~17k (kana, kanji) pairs / ~250 KB. Stored inline as
``data/kanji_variants.json`` so this migration file stays small and the
upstream regeneration step is auditable — re-run
``scripts/build_kanji_variants.py`` to refresh from a newer JMdict release.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def _data_path() -> Path:
    # The JSON lives in the repo's data/ directory. When the module is
    # imported via ``importlib.util.spec_from_file_location`` the file's
    # ``__file__`` points at the migrations folder — back out three
    # levels: migrations → kana_quiz → backend → repo root.
    return Path(__file__).resolve().parents[3] / "data" / "kanji_variants.json"


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kanji_variants (
          kana TEXT NOT NULL,
          kanji TEXT NOT NULL,
          PRIMARY KEY (kana, kanji)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_kanji_variants_kana ON kanji_variants(kana)"
    )

    path = _data_path()
    if not path.exists():
        # Allow migrations to run on installs that don't ship the data
        # file (e.g. minimal CI checkouts). The cloze locator gracefully
        # falls back to its kanji-stem heuristic when the table is empty.
        return

    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    rows = []
    for kana, kanji_list in data.items():
        for kanji in kanji_list:
            rows.append((kana, kanji))

    conn.executemany(
        "INSERT OR IGNORE INTO kanji_variants (kana, kanji) VALUES (?, ?)",
        rows,
    )
