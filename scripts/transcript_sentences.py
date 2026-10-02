"""Replace a deck's generated example sentences with lines from a transcript.

Reads a TSV with ``kana`` and ``example`` columns (examples separated by ｜),
picks one line per word, asks Gemini for the translation, mnemonic and
target form, and upserts ``sentence_cache`` under the default model with the
given ``source`` tag. Words whose line Gemini cannot locate the word in keep
their generated sentence. Re-running skips words already tagged.

Usage:
    uv run python scripts/transcript_sentences.py --deck "Hot Spot" \
        --lines scratch/hotspot/hotspot_final_full.tsv --source hotspot
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from kana_quiz import gemini  # noqa: E402
from kana_quiz.db import connect, init_schema  # noqa: E402
from kana_quiz.models import word_from_row  # noqa: E402

QUOTES = re.compile(r"[「」『』“”\"⸺…]+")


def clean(line: str) -> str:
    line = QUOTES.sub(" ", line)
    line = re.sub(r"\s+", " ", line).strip(" 　-－‐")
    return line


def pick_line(examples: str, forms: list[str]) -> str | None:
    """The longest line of a sensible length that shows the word, else the longest."""
    lines = [clean(x) for x in examples.split("｜")]
    lines = [x for x in lines if 6 <= len(x) <= 45]
    if not lines:
        return None
    with_word = [x for x in lines if any(f and f in x for f in forms)]
    pool = with_word or lines
    return max(pool, key=len)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deck", required=True)
    ap.add_argument("--lines", required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    init_schema()
    conn = connect()
    deck = conn.execute("SELECT id FROM decks WHERE name = ?", (args.deck,)).fetchone()
    if deck is None:
        print(f"no deck named {args.deck}", file=sys.stderr)
        return 1
    examples = {
        r["kana"]: r["example"]
        for r in csv.DictReader(open(args.lines, encoding="utf-8"), delimiter="\t")
    }
    rows = conn.execute(
        """
        SELECT w.* FROM words w
          LEFT JOIN sentence_cache s ON s.word_id = w.id AND s.model = ?
         WHERE w.deck_id = ? AND (s.id IS NULL OR s.source != ?)
         ORDER BY w.id
        """,
        (gemini.DEFAULT_MODEL, deck["id"], args.source),
    ).fetchall()
    words = [word_from_row(r) for r in rows]
    print(f"{len(words)} words to annotate", file=sys.stderr)

    def annotate(word):
        line = pick_line(examples.get(word.kana, ""), [word.kanji or "", word.kana])
        if line is None:
            return word, None, "no usable line"
        try:
            sentence = gemini.annotate_sentence(word, line)
        except Exception as exc:  # one bad call must not stop the batch
            return word, None, f"gemini: {exc}"
        if not sentence.target_form:
            return word, None, f"word not located in: {line}"
        return word, sentence, ""

    done = kept = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for word, sentence, why in pool.map(annotate, words):
            if sentence is None:
                kept += 1
                print(f"keep generated  {word.kana}: {why}", file=sys.stderr)
                continue
            conn.execute(
                """
                INSERT INTO sentence_cache
                    (word_id, model, japanese, english, mnemonic, target_form, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(word_id, model) DO UPDATE SET
                    japanese = excluded.japanese, english = excluded.english,
                    mnemonic = excluded.mnemonic, target_form = excluded.target_form,
                    source = excluded.source, created_at = excluded.created_at
                """,
                (
                    word.id, gemini.DEFAULT_MODEL, sentence.japanese, sentence.english,
                    sentence.mnemonic, sentence.target_form, args.source,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            done += 1
            if done % 25 == 0:
                print(f"{done} written", file=sys.stderr)
    print(f"written {done}, kept generated {kept}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
