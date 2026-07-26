"""Export active vocabulary into LLM-sized chunks for a gloss-rigorizing pass.

The point of the chunking is *co-location*: a model can only sharpen
"order" into "order (a command)" vs "order (sequence)" if it sees every
word currently competing for that gloss at once. So:

  1. Words are bucketed by ``normalize_english`` (the grader's own key,
     which already folds "to finish"/"finish"). A bucket with >1 member
     is a hard collision group and is never split across chunks.
  2. Buckets are sorted by a crude stem of their head token, so
     morphological relatives ("save"/"saving"/"savings") land
     alphabetically adjacent and usually share a chunk even though they
     aren't exact collisions.
  3. Chunks are filled sequentially from that sorted list.

Usage:
    uv run python scripts/gloss_chunks.py [--size 50] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from kana_quiz.grading import normalize_english  # noqa: E402

STOPWORDS = {
    "a", "an", "the", "to", "of", "in", "on", "at", "for", "with", "by",
    "be", "is", "are", "and", "or", "one", "s",
}
TOKEN_RE = re.compile(r"[a-z0-9]+")


def stem(tok: str) -> str:
    """Crude suffix strip — enough to collide save/saves/saving/savings."""
    for suf in ("ings", "ing", "ies", "es", "ed", "s"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            return tok[: -len(suf)]
    return tok


def sort_key(gloss: str) -> tuple[str, ...]:
    toks = [stem(t) for t in TOKEN_RE.findall(normalize_english(gloss))]
    toks = [t for t in toks if t not in STOPWORDS] or toks
    return tuple(toks) or ("",)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("KANA_QUIZ_DB", "data/kana_quiz.sqlite"))
    ap.add_argument("--size", type=int, default=50)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT w.id, w.kana, w.kanji, w.english, w.tags, d.name AS deck
          FROM words w LEFT JOIN decks d ON d.id = w.deck_id
         WHERE w.ignored_at IS NULL
         ORDER BY w.id
        """
    ).fetchall()

    # 1. hard collision buckets, keyed the same way the grader normalizes.
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        rec = {
            "id": r["id"],
            "kana": r["kana"],
            "kanji": r["kanji"] or "",
            "current": r["english"],
            "tags": r["tags"] or "",
            "deck": r["deck"] or "",
        }
        buckets.setdefault(normalize_english(r["english"]), []).append(rec)

    # 2. sort buckets so morphological relatives sit next to each other.
    ordered = sorted(buckets.items(), key=lambda kv: (sort_key(kv[0]), kv[0]))

    # 3. fill chunks, never splitting a bucket.
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    for _key, members in ordered:
        if cur and len(cur) + len(members) > args.size:
            chunks.append(cur)
            cur = []
        cur.extend(members)
    if cur:
        chunks.append(cur)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("chunk_*.json"):
        old.unlink()

    for i, chunk in enumerate(chunks):
        keys = [normalize_english(w["current"]) for w in chunk]
        collisions = sorted({k for k in keys if keys.count(k) > 1})
        (out / f"chunk_{i:03d}.json").write_text(
            json.dumps({"chunk": i, "collision_keys": collisions, "words": chunk},
                       ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

    n_collide = sum(len(v) for v in buckets.values() if len(v) > 1)
    print(f"{len(rows)} words -> {len(chunks)} chunks (size<={args.size}) in {out}")
    print(f"{n_collide} words sit in a {sum(1 for v in buckets.values() if len(v)>1)}-way-or-more collision bucket")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
