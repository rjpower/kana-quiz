#!/usr/bin/env python3
"""One-shot import of an Anki .colpkg into the kana-quiz DB.

Pulls the "Japanese Vocabulary" notetype (mid 1091735104) out of an Anki
collection and merges it into our `words` + `task_state` schema. Each
note becomes one word; each of its two cards becomes a recall task_state row.

Anki ord 0 = "Japanese to English" template = ja2en (recognition).
Anki ord 1 = "English to Japanese" template = en2ja (recall).

Anki sub-decks under "Japanese Vocabulary" become kana-quiz decks. The
DECK_LEVELS table below maps each name to a level (lower = drawn first
when introducing fresh cards). Edit it before running if you want a
different priority.

Existing words (matched by kana) keep their kana-quiz SRS state by
default — only english/kanji/tags refresh. Pass --overwrite-task-state
to clobber with Anki's SRS instead.

Run:
  uv run python scripts/import_anki_colpkg.py anki.colpkg
"""

import argparse
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

JAPANESE_VOCAB_MID = 1091735104

DECK_LEVELS: dict[str, int] = {
    "N5": 1,
    "Core 2K": 1,
    "N4": 2,
    "N3": 3,
    "Core 3k": 4,
    "Self": 5,
    "Chat": 5,
    "Core 4k": 5,
    "Core 5k": 6,
    "Takoboto": 7,
    "A Satori": 7,
    "Known Words": 9,
}
DEFAULT_LEVEL = 5

# Sub-deck name prefixes to skip outright (Jlab listening decks etc.).
SKIP_DECK_PREFIXES: tuple[str, ...] = ("Listening", "Numbers and Counting")

RUBY_RE = re.compile(r"<rt>.*?</rt>", re.DOTALL)
HTML_RE = re.compile(r"<[^>]+>")
SOUND_RE = re.compile(r"\[sound:[^\]]+\]")
WHITESPACE_RE = re.compile(r"\s+")

log = logging.getLogger("import_anki")


def strip_html(s: str) -> str:
    if not s:
        return ""
    s = SOUND_RE.sub("", s)
    s = RUBY_RE.sub("", s)
    s = HTML_RE.sub("", s)
    return WHITESPACE_RE.sub(" ", s).strip()


def deck_leaf(name: str) -> str:
    parts = name.split("\x1f")
    if parts and parts[0] == "Japanese Vocabulary":
        parts = parts[1:]
    return "::".join(parts) if parts else "Japanese Vocabulary"


def extract_collection(colpkg: Path, workdir: Path) -> Path:
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(colpkg) as zf:
        zf.extractall(workdir)
    compressed = workdir / "collection.anki21b"
    legacy = workdir / "collection.anki2"
    if compressed.exists():
        out = workdir / "collection.sqlite"
        subprocess.run(["zstd", "-df", str(compressed), "-o", str(out)], check=True)
        return out
    if legacy.exists():
        return legacy
    raise SystemExit("colpkg has neither collection.anki21b nor collection.anki2")


def open_anki(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    # Anki uses a custom 'unicase' collation; provide a stub so queries work.
    conn.create_collation("unicase", lambda a, b: (a > b) - (a < b))
    conn.row_factory = sqlite3.Row
    return conn


def card_due_at(card: sqlite3.Row, col_crt: int) -> str | None:
    """Translate an Anki card's `due` to a UTC ISO timestamp.

    Returns None when the card has no meaningful schedule (new or suspended);
    callers should skip writing task_state in that case.
    """
    type_ = card["type"]
    queue = card["queue"]
    due = card["due"]
    if queue == -1 or queue == -2 or queue == -3:
        return None
    if type_ == 0:
        return None
    if type_ == 2 and queue == 2:
        ts = col_crt + due * 86400
    elif type_ in (1, 3) or queue in (1, 3):
        # (Re)learning queue: `due` is unix seconds (sometimes ms in older
        # Anki builds — coerce when out of plausible range).
        ts = due // 1000 if due > 4_000_000_000 else due
    else:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("colpkg", type=Path, help="path to anki.colpkg")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(__file__).parent.parent / "data" / "kana_quiz.sqlite",
        help="kana-quiz sqlite path (default: ../data/kana_quiz.sqlite)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-backup", action="store_true")
    parser.add_argument(
        "--overwrite-task-state",
        action="store_true",
        help="Replace task_state for existing words too (default keeps them).",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    db_path = args.db.resolve()
    if not db_path.exists():
        log.error("DB not found: %s", db_path)
        return 1
    if not args.colpkg.exists():
        log.error("colpkg not found: %s", args.colpkg)
        return 1

    if not args.dry_run and not args.skip_backup:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = db_path.with_name(f"{db_path.stem}.sqlite.backup-{ts}")
        shutil.copy2(db_path, backup)
        log.info("backup: %s", backup)

    # Make sure migrations are applied before we touch the DB.
    sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
    os.environ["KANA_QUIZ_DB"] = str(db_path)
    from kana_quiz.db import init_schema
    from kana_quiz.task_state import TASK_EN2JA, TASK_JA2EN

    init_schema()
    ord_to_task = {0: TASK_JA2EN, 1: TASK_EN2JA}

    sqlite_path = extract_collection(args.colpkg, Path("/tmp/kq_anki_extract"))
    anki = open_anki(sqlite_path)
    col_crt = anki.execute("SELECT crt FROM col").fetchone()["crt"]
    log.info(
        "Anki col.crt = %d (%s)",
        col_crt,
        datetime.fromtimestamp(col_crt, timezone.utc).date(),
    )

    deck_names: dict[int, str] = {}
    for r in anki.execute("SELECT id, name FROM decks"):
        deck_names[r["id"]] = deck_leaf(r["name"])

    target = sqlite3.connect(db_path, isolation_level=None)
    target.row_factory = sqlite3.Row

    target_decks: dict[str, int] = {
        r["name"]: r["id"] for r in target.execute("SELECT id, name FROM decks")
    }

    notes = anki.execute(
        "SELECT id, flds, tags FROM notes WHERE mid = ?",
        (JAPANESE_VOCAB_MID,),
    ).fetchall()
    log.info("Japanese Vocabulary notes: %d", len(notes))

    note_ids = [n["id"] for n in notes]
    cards_by_note: dict[int, list[sqlite3.Row]] = {}
    for chunk_start in range(0, len(note_ids), 500):
        chunk = note_ids[chunk_start : chunk_start + 500]
        placeholders = ",".join("?" * len(chunk))
        for c in anki.execute(
            f"SELECT * FROM cards WHERE nid IN ({placeholders})", chunk
        ):
            cards_by_note.setdefault(c["nid"], []).append(c)

    decks_created = 0
    inserted_words = 0
    updated_words = 0
    inserted_task_state = {TASK_EN2JA: 0, TASK_JA2EN: 0}
    preserved_task_state = 0
    skipped_no_text = 0
    skipped_skip_deck = 0
    skipped_task_state_new = 0
    skipped_task_state_suspended = 0

    target.execute("BEGIN")
    try:
        for n in notes:
            cards = cards_by_note.get(n["id"], [])
            if not cards:
                continue
            leaf = deck_names.get(cards[0]["did"], "")
            if not leaf or any(leaf.startswith(p) for p in SKIP_DECK_PREFIXES):
                skipped_skip_deck += 1
                continue

            fields = n["flds"].split("\x1f")
            term = fields[0] if len(fields) > 0 else ""
            reading = fields[1] if len(fields) > 1 else ""
            meaning = fields[2] if len(fields) > 2 else ""
            kana = strip_html(reading) or strip_html(term)
            english = strip_html(meaning)
            if not kana or not english:
                skipped_no_text += 1
                continue
            term_clean = strip_html(term)
            kanji = term_clean if term_clean and term_clean != kana else None
            tags = (n["tags"] or "").strip()
            tags = ",".join(tags.split()) if tags else None

            if leaf not in target_decks:
                level = DECK_LEVELS.get(leaf, DEFAULT_LEVEL)
                cur = target.execute(
                    "INSERT INTO decks (name, level) VALUES (?, ?)",
                    (leaf, level),
                )
                target_decks[leaf] = cur.lastrowid  # type: ignore[assignment]
                decks_created += 1
                log.info("created deck %r at level %d", leaf, level)
            target_deck_id = target_decks[leaf]

            existing = target.execute(
                "SELECT id FROM words WHERE kana = ?", (kana,)
            ).fetchone()
            if existing is None:
                cur = target.execute(
                    """INSERT INTO words (kana, english, kanji, tags, deck_id)
                       VALUES (?, ?, ?, ?, ?)""",
                    (kana, english, kanji, tags, target_deck_id),
                )
                word_id = cur.lastrowid
                inserted_words += 1
                seed_task_state = True
            else:
                word_id = existing["id"]
                target.execute(
                    """UPDATE words
                          SET english = ?, kanji = ?, tags = COALESCE(?, tags)
                        WHERE id = ?""",
                    (english, kanji, tags, word_id),
                )
                updated_words += 1
                seed_task_state = args.overwrite_task_state

            for c in cards:
                task = ord_to_task.get(c["ord"])
                if task is None:
                    continue
                if c["queue"] in (-1, -2, -3):
                    skipped_task_state_suspended += 1
                    continue
                due_at = card_due_at(c, col_crt)
                if due_at is None:
                    skipped_task_state_new += 1
                    continue
                if not seed_task_state:
                    preserved_task_state += 1
                    continue
                ease = c["factor"] / 1000 if c["factor"] else 2.5
                ease = max(1.3, min(2.8, ease))
                interval_days = float(c["ivl"]) if c["ivl"] >= 0 else 0.0
                repetitions = int(c["reps"])
                introduced_at = datetime.fromtimestamp(
                    col_crt, timezone.utc
                ).isoformat()
                target.execute(
                    """INSERT OR REPLACE INTO task_state
                         (word_id, task, ease, interval_days,
                          repetitions, due_at, introduced_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        word_id,
                        task,
                        ease,
                        interval_days,
                        repetitions,
                        due_at,
                        introduced_at,
                    ),
                )
                inserted_task_state[task] += 1

        if args.dry_run:
            log.info("[dry run] rolling back")
            target.execute("ROLLBACK")
        else:
            target.execute("COMMIT")
    except Exception:
        target.execute("ROLLBACK")
        raise

    log.info("=== summary ===")
    log.info("decks created: %d", decks_created)
    log.info(
        "words inserted: %d, updated: %d, skipped (no text/Jlab): %d/%d",
        inserted_words,
        updated_words,
        skipped_no_text,
        skipped_skip_deck,
    )
    log.info(
        "task_state seeded: en2ja=%d, ja2en=%d  (preserved on existing words: %d)",
        inserted_task_state[TASK_EN2JA],
        inserted_task_state[TASK_JA2EN],
        preserved_task_state,
    )
    log.info(
        "task_state skipped: new-in-anki=%d, suspended=%d",
        skipped_task_state_new,
        skipped_task_state_suspended,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
