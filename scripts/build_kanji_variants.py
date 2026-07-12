"""Rebuild data/kanji_variants.json from a JMdict-simplified release.

Reads a JMdict-eng JSON file (full or "common" subset) from the path
passed as argv[1], intersects against the deck's distinct kana readings
in the live SQLite DB, and writes the per-kana sorted kanji-variant
list to ``data/kanji_variants.json``. Migration 008 reads that file.

Usage:
    uv run python scripts/build_kanji_variants.py /tmp/jmdict-eng-3.6.2.json
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def main(jmdict_path: str) -> None:
    data = json.load(open(jmdict_path, encoding="utf-8"))

    # kana_text -> set of kanji writings that share that reading.
    kana_to_kanji: dict[str, set[str]] = defaultdict(set)
    for entry in data["words"]:
        kanji_list = [k["text"] for k in entry.get("kanji", [])]
        if not kanji_list:
            continue
        for k_entry in entry.get("kana", []):
            kana_text = k_entry["text"]
            applies = k_entry.get("appliesToKanji", ["*"])
            if applies == ["*"]:
                for ktext in kanji_list:
                    kana_to_kanji[kana_text].add(ktext)
            else:
                for ktext in applies:
                    kana_to_kanji[kana_text].add(ktext)

    db_path = REPO / "data" / "kana_quiz.sqlite"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    deck = conn.execute("SELECT kana FROM words").fetchall()
    conn.close()

    subset: dict[str, list[str]] = {}
    for r in deck:
        kana = r["kana"]
        if kana in kana_to_kanji:
            subset[kana] = sorted(kana_to_kanji[kana])

    out = REPO / "data" / "kanji_variants.json"
    with out.open("w", encoding="utf-8") as fh:
        json.dump(subset, fh, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    pairs = sum(len(v) for v in subset.values())
    print(f"wrote {out} — {len(subset)} kanas, {pairs} (kana,kanji) pairs")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: build_kanji_variants.py <jmdict-eng-*.json>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
