"""Apply a rigorized-gloss pass back into the words table.

Reads the per-chunk JSON produced by the LLM pass, validates every
proposal against the invariants the app depends on, then (with --apply)
backs up the DB, promotes the new gloss into ``words.english``, and
demotes the old gloss into ``word_alternates`` so typed answers that
used to grade correct still do.

Usage:
    uv run python scripts/gloss_apply.py --in DIR              # dry run
    uv run python scripts/gloss_apply.py --in DIR --apply
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from kana_quiz.grading import accepted_meanings, normalize_english  # noqa: E402

SOURCE = "gloss-rigorize"  # matches the earlier gloss-tighten / gloss-audit passes
MAX_LEN = 80
BAD_CHARS = set(",;/")


def validate(gloss: str) -> str | None:
    """Return a rejection reason, or None if the gloss is usable."""
    if not gloss or not gloss.strip():
        return "empty"
    if gloss != gloss.strip():
        return "untrimmed"
    if len(gloss) > MAX_LEN:
        return f"too long ({len(gloss)})"
    if not gloss.isascii():
        return "non-ascii"
    if BAD_CHARS & set(gloss):
        return "contains , ; or /"
    if gloss.count("(") != gloss.count(")"):
        return "unbalanced parens"
    if gloss.count("(") > 1:
        return "multiple parens"
    if "(" in gloss:
        # A trailing "(...)" is the disambiguator; a mid-string one is an
        # inline placeholder ("to get (something) done with"). Both are fine
        # — PAREN_RE strips either, so the bare core still grades correct —
        # but the text outside the parens has to carry the meaning on its own.
        if not gloss[: gloss.index("(")].strip():
            return "empty core before paren"
        if gloss.index("(") > gloss.index(")"):
            return "closing paren before opening"
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("KANA_QUIZ_DB", "data/kana_quiz.sqlite"))
    ap.add_argument("--in", dest="indir", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--report", default=None, help="write a full before/after TSV here")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    live = {
        r["id"]: dict(r)
        for r in conn.execute(
            "SELECT id, kana, kanji, english FROM words WHERE ignored_at IS NULL"
        )
    }

    # ---- load proposals -------------------------------------------------
    proposals: dict[int, dict] = {}
    dupe_ids: list[int] = []
    bad_files: list[str] = []
    for path in sorted(Path(args.indir).glob("chunk_*.json")):
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
            assert isinstance(entries, list)
        except Exception as exc:  # noqa: BLE001
            bad_files.append(f"{path.name}: {exc}")
            continue
        for e in entries:
            wid = e.get("id")
            if wid in proposals:
                dupe_ids.append(wid)
                continue
            proposals[wid] = e

    # ---- validate -------------------------------------------------------
    rejects: list[tuple[int, str, str]] = []
    accepted: dict[int, dict] = {}
    for wid, e in proposals.items():
        if wid not in live:
            rejects.append((wid, str(e.get("gloss", "")), "unknown/ignored word id"))
            continue
        gloss = (e.get("gloss") or "").strip()
        reason = validate(gloss)
        if reason:
            rejects.append((wid, gloss, reason))
            continue
        accepted[wid] = {"gloss": gloss, "alts": e.get("alts") or [], "note": e.get("note") or ""}

    missing = sorted(set(live) - set(proposals))

    # ---- cross-chunk collision check (full string, case-insensitive) ----
    final = {wid: accepted[wid]["gloss"] if wid in accepted else live[wid]["english"]
             for wid in live}
    by_gloss: dict[str, list[int]] = defaultdict(list)
    for wid, g in final.items():
        by_gloss[g.strip().lower()].append(wid)
    collisions = {g: ids for g, ids in by_gloss.items() if len(ids) > 1}

    # collisions on the *bare core* are fine and expected; report separately
    by_core: dict[str, list[int]] = defaultdict(list)
    for wid, g in final.items():
        by_core[normalize_english(g.split("(")[0])].append(wid)
    core_collisions = sum(len(v) for v in by_core.values() if len(v) > 1)

    # ---- POS-prefix flips (load-bearing for _is_verb distractor filter) --
    flips = [
        (wid, live[wid]["english"], a["gloss"])
        for wid, a in accepted.items()
        if live[wid]["english"].lower().startswith("to ") != a["gloss"].lower().startswith("to ")
    ]
    changed = [wid for wid, a in accepted.items() if a["gloss"] != live[wid]["english"]]

    # ---- report ---------------------------------------------------------
    print(f"db              : {args.db}")
    print(f"active words    : {len(live)}")
    print(f"proposals       : {len(proposals)} from {len(list(Path(args.indir).glob('chunk_*.json')))} files")
    print(f"  accepted      : {len(accepted)}  ({len(changed)} actually change the gloss)")
    print(f"  rejected      : {len(rejects)}")
    print(f"  missing ids   : {len(missing)}")
    print(f"  duplicate ids : {len(dupe_ids)}")
    print(f"unparseable     : {len(bad_files)}")
    print(f"exact-gloss collisions remaining: {sum(len(v) for v in collisions.values())} words in {len(collisions)} groups")
    print(f"shared bare core (expected, grading-friendly): {core_collisions} words")
    print(f"verb-prefix flips: {len(flips)}")

    if bad_files:
        print("\n-- unparseable files --")
        for b in bad_files[:20]:
            print("  ", b)
    if rejects:
        print("\n-- rejects (first 30) --")
        for wid, g, why in rejects[:30]:
            print(f"   {wid:>5} [{why}] {g!r}")
    if collisions:
        print("\n-- remaining exact collisions (first 20 groups) --")
        for g, ids in list(collisions.items())[:20]:
            forms = " | ".join(f"{live[i]['kanji'] or live[i]['kana']}" for i in ids)
            print(f"   {g!r}: {forms}")
    if flips:
        print("\n-- verb-prefix flips (first 20) --")
        for wid, old, new in flips[:20]:
            print(f"   {live[wid]['kanji'] or live[wid]['kana']}: {old!r} -> {new!r}")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write("id\tkana\tkanji\told\tnew\tnote\n")
            for wid in sorted(changed):
                w, a = live[wid], accepted[wid]
                fh.write(f"{wid}\t{w['kana']}\t{w['kanji'] or ''}\t{w['english']}\t{a['gloss']}\t{a['note']}\n")
        print(f"\nwrote {args.report}")

    if not args.apply:
        print("\n(dry run — pass --apply to write)")
        return 0

    # ---- apply ----------------------------------------------------------
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{args.db}.pre-gloss-{stamp}"
    shutil.copy2(args.db, backup)
    print(f"\nbacked up -> {backup}")

    alt_rows: set[tuple[int, str, str, str]] = set()
    for wid, a in accepted.items():
        if a["gloss"] == live[wid]["english"]:
            continue
        # every form of the OLD gloss keeps grading correct
        for variant in accepted_meanings(live[wid]["english"]):
            alt_rows.add((wid, "ja2en", variant, SOURCE))
        for alt in a["alts"]:
            n = normalize_english(str(alt))
            if n:
                alt_rows.add((wid, "ja2en", n, SOURCE))

    cur = conn.cursor()
    cur.execute("BEGIN")
    cur.executemany(
        "UPDATE words SET english = ? WHERE id = ?",
        [(accepted[wid]["gloss"], wid) for wid in changed],
    )
    cur.executemany(
        "INSERT OR IGNORE INTO word_alternates (word_id, direction, alternate, source) "
        "VALUES (?, ?, ?, ?)",
        sorted(alt_rows),
    )
    conn.commit()
    print(f"updated {len(changed)} glosses; inserted up to {len(alt_rows)} backup alternates")

    after = Counter(
        r[0] for r in conn.execute(
            "SELECT lower(trim(english)) FROM words WHERE ignored_at IS NULL"
        )
    )
    print(f"post-apply exact collisions: {sum(n for n in after.values() if n > 1)} words")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
