"""Triage the LLM-flagged junk rows into repair / clear-kanji / drop / keep.

The `junk` flag conflates several problems, and some of them are worth
fixing rather than dropping — まニアう has 23 reps and is one character
away from correct. Explicit lists beat a heuristic here: it's 66 rows.
"""
import sqlite3
import sys

DB = "data/kana_quiz.sqlite"

# kana/kanji typos where the intended word is unambiguous
REPAIR = {
    3988: ("kana", "まにあう"),      # まニアう — katakana ニア spliced into 間に合う
    5860: ("kana", "ほしゅ"),        # ほゅ — dropped し in 保守
    4197: ("kana", "じゅうなん"),     # じゅう난 — hangul 난 mojibake in 柔軟
    1747: ("kana", "おねがいする"),    # おね願いする — kanji leaked into the kana field
    634:  ("kanji", "偶数"),         # 偶 — kanji truncated; kana ぐうすう is right
}

# real entries whose kanji field swallowed an example sentence
CLEAR_KANJI = [4140, 4149, 4150, 4131]

# flagged but actually fine as cards
KEEP = [349, 762, 1639, 1871, 2872]


def main() -> int:
    apply = "--apply" in sys.argv
    flagged = [int(x) for x in open(sys.argv[1]).read().split()]
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, kana, kanji, english FROM words WHERE ignored_at IS NULL"
        )
    }

    drop = [i for i in flagged
            if i not in REPAIR and i not in CLEAR_KANJI and i not in KEEP and i in rows]

    print(f"=== REPAIR ({len(REPAIR)}) ===")
    for wid, (field, val) in REPAIR.items():
        r = rows[wid]
        print(f"  {wid:>5} {field}: {r[field]!r} -> {val!r}   ({r['english'][:40]})")

    print(f"\n=== CLEAR KANJI ({len(CLEAR_KANJI)}) ===")
    for wid in CLEAR_KANJI:
        r = rows[wid]
        print(f"  {wid:>5} {r['kana']}  kanji={r['kanji'][:48]!r} -> NULL")

    print(f"\n=== KEEP ({len(KEEP)}) ===")
    for wid in KEEP:
        r = rows[wid]
        print(f"  {wid:>5} {r['kana']} / {r['kanji'] or ''}  {r['english'][:40]}")

    print(f"\n=== DROP -> ignored_at ({len(drop)}) ===")
    reps = 0
    for wid in drop:
        r = rows[wid]
        n = conn.execute(
            "SELECT COALESCE(SUM(repetitions),0) n FROM task_state WHERE word_id=?", (wid,)
        ).fetchone()["n"]
        reps += n
        flag = f"  <-- {n} reps" if n else ""
        print(f"  {wid:>5} {r['kana'][:36]:<36} {r['english'][:34]}{flag}")
    print(f"\n  {len(drop)} rows, {reps} total reps affected")

    if not apply:
        print("\n(dry run — pass --apply)")
        return 0

    cur = conn.cursor()
    cur.execute("BEGIN")
    for wid, (field, val) in REPAIR.items():
        cur.execute(f"UPDATE words SET {field} = ? WHERE id = ?", (val, wid))
    cur.executemany("UPDATE words SET kanji = NULL WHERE id = ?", [(i,) for i in CLEAR_KANJI])
    cur.executemany(
        "UPDATE words SET ignored_at = datetime('now') WHERE id = ?", [(i,) for i in drop]
    )
    conn.commit()
    print(f"\napplied: {len(REPAIR)} repaired, {len(CLEAR_KANJI)} kanji cleared, {len(drop)} ignored")
    print("active words now:",
          conn.execute("SELECT COUNT(*) FROM words WHERE ignored_at IS NULL").fetchone()[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
