"""Find glosses that still collide after a chunked pass, and emit fixup chunks.

Each chunk in the main pass only guarantees uniqueness *within itself* —
two agents working in parallel can independently land on "opportunity
(a lucky break)". This walks the merged result, groups the survivors by
exact gloss, and writes fixup chunks so a second pass can pull them
apart with full sight of the conflict.

Usage:
    uv run python scripts/gloss_collisions.py --in DIR --out DIR
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("KANA_QUIZ_DB", "data/kana_quiz.sqlite"))
    ap.add_argument("--in", dest="indir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--size", type=int, default=40, help="max words per fixup chunk")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    live = {
        r["id"]: dict(r)
        for r in conn.execute(
            """SELECT w.id, w.kana, w.kanji, w.english, w.tags, d.name AS deck
                 FROM words w LEFT JOIN decks d ON d.id = w.deck_id
                WHERE w.ignored_at IS NULL"""
        )
    }

    proposed: dict[int, dict] = {}
    for path in sorted(Path(args.indir).glob("chunk_*.json")):
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for e in entries:
            if isinstance(e, dict) and e.get("id") in live:
                proposed.setdefault(e["id"], e)

    final = {
        wid: (proposed[wid].get("gloss") or live[wid]["english"]) if wid in proposed
        else live[wid]["english"]
        for wid in live
    }

    groups: dict[str, list[int]] = defaultdict(list)
    for wid, gloss in final.items():
        groups[gloss.strip().lower()].append(wid)
    colliding = {g: ids for g, ids in sorted(groups.items()) if len(ids) > 1}

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("chunk_*.json"):
        old.unlink()

    chunks: list[list[dict]] = []
    cur: list[dict] = []
    for gloss, ids in colliding.items():
        members = [
            {
                "id": wid,
                "kana": live[wid]["kana"],
                "kanji": live[wid]["kanji"] or "",
                "current": final[wid],
                "original": live[wid]["english"],
                "tags": live[wid]["tags"] or "",
                "deck": live[wid]["deck"] or "",
            }
            for wid in ids
        ]
        if cur and len(cur) + len(members) > args.size:
            chunks.append(cur)
            cur = []
        cur.extend(members)
    if cur:
        chunks.append(cur)

    for i, chunk in enumerate(chunks):
        keys = [w["current"].strip().lower() for w in chunk]
        (out / f"chunk_{i:03d}.json").write_text(
            json.dumps(
                {"chunk": i, "collision_keys": sorted(set(keys)), "words": chunk},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )

    n = sum(len(v) for v in colliding.values())
    print(f"{len(proposed)}/{len(live)} words have a proposal")
    print(f"{n} words still collide across {len(colliding)} glosses -> {len(chunks)} fixup chunks in {out}")
    for gloss, ids in list(colliding.items())[:25]:
        forms = " | ".join(live[i]["kanji"] or live[i]["kana"] for i in ids)
        print(f"   {gloss!r}: {forms}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
