#!/usr/bin/env python3
"""Re-spell loanword (外来語) vocab rows so their primary reading is katakana.

The app shows a card's `kana` field as the headword (session builds the ja→en
prompt and every multiple-choice label from `kana`). A chunk of the deck stores
gairaigo with a *hiragana* `kana` reading (こーひー, こーら) even though the word
is conventionally katakana — so those cards render as distracting hiragana.

Two independent sources of the correct spelling, handled as separate tracks:

  Track 1 — "kanji" already holds the katakana (the common case, ~259 rows).
    These rows carry the katakana surface in the `kanji` column (コーヒー) and a
    hiragana transliteration in `kana` (こーひー). The correct reading is sitting
    right there, so `kana := kanji` is exact — no model, no guessing. The now-
    redundant `kanji` is cleared to match how the deck stores a plain katakana
    word (167 of 175 existing katakana rows have an empty `kanji`).

  Track 2 — no `kanji` at all (~a handful). Pure-hiragana rows with an empty
    kanji field can't be fixed deterministically (えびフライ, たばこ), so Gemini
    decides whether each is a katakana loanword and supplies the surface form.
    This is where an LLM earns its keep: こうひい → コーヒー needs the long-vowel
    mark ー inferred, and loanword verbs keep okurigana in hiragana (サボる).

Mixed kanji+katakana compounds (コピーを取る / こぴーをとる) are deliberately NOT
touched — the loan span and the kanji reading can't be separated by either
track without per-row judgement. Run with --report to list them for later.

Safety:
  * Dry-run by default; nothing is written without --apply.
  * --apply snapshots the DB to a timestamped backup first.
  * `kana` is UNIQUE — a proposed spelling already held by another word is
    reported as a CONFLICT and skipped, never force-written.
  * Track 2's model output passes a char whitelist + a phonetic sanity check
    (proposed katakana folded back to kana vs the original reading); low-ratio
    rows are flagged ⚠ for a human but still shown in the dry run.

Run:
  uv run python scripts/fix_loanword_katakana.py                 # dry run, both tracks
  uv run python scripts/fix_loanword_katakana.py --skip-gemini   # deterministic only, no API
  uv run python scripts/fix_loanword_katakana.py --apply         # commit (backs up first)
  uv run python scripts/fix_loanword_katakana.py --list-models   # find the model id
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Make `kana_quiz` importable when run as a bare script (the package lives under
# backend/; pyproject puts it on the path for pytest but not for `python foo.py`).
sys.path.insert(0, str(REPO_ROOT / "backend"))

from kana_quiz.models import is_katakana_only  # noqa: E402

# Hiragana U+3041–U+3096, katakana U+30A1–U+30FF.
_HIRA_LO, _HIRA_HI = 0x3041, 0x3096
_HIRA_KATA_OFFSET = 0x30A0 - 0x3040  # +96


def _has_hiragana(s: str) -> bool:
    return any(_HIRA_LO <= ord(c) <= _HIRA_HI for c in s)


def _has_katakana(s: str) -> bool:
    # Excludes the bare chōonpu (ー, U+30FC) so "ー-only" never counts as katakana.
    return any(0x30A1 <= ord(c) <= 0x30F6 for c in s)


def _has_kanji(s: str) -> bool:
    return any(0x4E00 <= ord(c) <= 0x9FFF for c in s)


def _has_latin(s: str) -> bool:
    return any("a" <= c.lower() <= "z" for c in s)


def _kata_to_hira(s: str) -> str:
    """Fold katakana → hiragana so a proposed spelling can be compared to the
    original reading on the same alphabet. Leaves ー / punctuation untouched."""
    out = []
    for c in s:
        cp = ord(c)
        out.append(chr(cp - _HIRA_KATA_OFFSET) if 0x30A1 <= cp <= 0x30F6 else c)
    return "".join(out)


def _phonetic_ratio(original_kana: str, proposed: str) -> float:
    """Rough similarity of two readings, tolerant of long-vowel spelling. The
    original hiragana may write long vowels out (こうひい) while the katakana
    uses ー (コーヒー); folding to hiragana and dropping every ー from both sides
    removes that difference, so a real re-spelling scores high and an unrelated
    word scores near zero."""
    a = original_kana.replace("ー", "")
    b = _kata_to_hira(proposed).replace("ー", "")
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


@dataclass
class Proposal:
    id: int
    english: str
    old_kana: str
    new_kana: str
    clear_kanji: bool          # Track 1 clears the now-redundant kanji column
    source: str                # "kanji-copy" | "gemini"
    ratio: float
    conflict_with: int | None = None


# ---- Track 1: copy the katakana already sitting in the kanji column ----------

def _track1_proposals(conn: sqlite3.Connection) -> list[Proposal]:
    rows = conn.execute(
        "SELECT id, kana, kanji, english FROM words WHERE kanji IS NOT NULL AND kanji != ''"
    ).fetchall()
    out: list[Proposal] = []
    for r in rows:
        kanji = r["kanji"]
        # Pure-katakana kanji field = the word's real surface is katakana.
        if not is_katakana_only(kanji):
            continue
        if r["kana"] == kanji:
            continue
        out.append(
            Proposal(
                id=r["id"],
                english=r["english"],
                old_kana=r["kana"],
                new_kana=kanji,
                clear_kanji=True,
                source="kanji-copy",
                ratio=_phonetic_ratio(r["kana"], kanji),
            )
        )
    return out


# ---- Track 2: Gemini decides for no-kanji hiragana rows ----------------------

PROMPT_HEADER = """\
You are a Japanese lexicographer cleaning up a vocabulary deck. Each entry
below stores its reading in hiragana. Decide, for each, whether it is a
loanword (外来語 / gairaigo) — a word borrowed from a foreign language
(English, Portuguese, French, etc.), INCLUDING 和製英語 and loanword-derived
verbs (サボる, ダブる, ググる) — that is conventionally written with katakana.

For every entry return an object with:
  - id: the integer id exactly as given
  - is_loanword: true ONLY if it is a gairaigo conventionally written in katakana
  - katakana: the correct conventional spelling. Use katakana with ー for long
      vowels (コーヒー, not コウヒイ). For loanword verbs keep the inflecting
      okurigana in hiragana (サボる, ダブる — not サボル). Empty string when
      is_loanword is false.

Native Japanese (和語), Sino-Japanese (漢語), grammatical words, pronouns,
counters, and onomatopoeia/mimetics conventionally written in hiragana are NOT
loanwords — set is_loanword=false for them.

Examples:
  coffee / こーひー         -> is_loanword=true,  katakana=コーヒー
  to skip class / さぼる     -> is_loanword=true,  katakana=サボる
  fried shrimp / えびフライ  -> is_loanword=true,  katakana=エビフライ
  rice bowl / ちゃわん       -> is_loanword=false, katakana=
  laughing loudly / げらげら -> is_loanword=false, katakana=

Entries:
"""


def _select_track2_candidates(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT id, kana, english FROM words WHERE (kanji IS NULL OR kanji = '') ORDER BY id"
    ).fetchall()
    cands = [r for r in rows if _has_hiragana(r["kana"])]
    return cands[:limit] if limit > 0 else cands


def _make_client(timeout_ms: int):
    from google import genai
    from google.genai import types

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("GEMINI_API_KEY not set (checked env and repo .env)")
    return genai.Client(api_key=key, http_options=types.HttpOptions(timeout=timeout_ms))


def _classify_batch(client, model: str, batch: list[sqlite3.Row], attempts: int = 3) -> dict[int, dict]:
    from google.genai import types

    prompt = PROMPT_HEADER + "\n".join(
        f"{r['id']} (english: {r['english']!r}): {r['kana']}" for r in batch
    )
    schema = types.Schema(
        type=types.Type.ARRAY,
        items=types.Schema(
            type=types.Type.OBJECT,
            required=["id", "is_loanword", "katakana"],
            properties={
                "id": types.Schema(type=types.Type.INTEGER),
                "is_loanword": types.Schema(type=types.Type.BOOLEAN),
                "katakana": types.Schema(type=types.Type.STRING),
            },
        ),
    )
    config = types.GenerateContentConfig(
        response_mime_type="application/json", response_schema=schema, temperature=0.0
    )
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            resp = client.models.generate_content(model=model, contents=prompt, config=config)
            data = json.loads((resp.text or "").strip())
            if not isinstance(data, list):
                raise ValueError("expected a JSON array")
            return {
                int(it["id"]): {
                    "is_loanword": bool(it.get("is_loanword")),
                    "katakana": str(it.get("katakana") or "").strip(),
                }
                for it in data
            }
        except Exception as e:  # noqa: BLE001 — batch-level retry, surface after N tries
            last_err = e
            if i < attempts - 1:
                time.sleep(1.5 * (i + 1))
    print(f"  ! batch failed after {attempts} tries: {last_err}", file=sys.stderr)
    return {}


def _validate_katakana(new_kana: str) -> str | None:
    if not new_kana:
        return "empty"
    if not _has_katakana(new_kana):
        return "no katakana"
    if _has_kanji(new_kana):
        return "contains kanji"
    if _has_latin(new_kana):
        return "contains latin"
    return None


def _track2_proposals(
    conn: sqlite3.Connection, client, model: str, batch_size: int, limit: int
) -> tuple[list[Proposal], list[tuple[sqlite3.Row, str]]]:
    candidates = _select_track2_candidates(conn, limit)
    print(f"Track 2: {len(candidates)} no-kanji hiragana candidates → {model}", file=sys.stderr)
    proposals: list[Proposal] = []
    rejected: list[tuple[sqlite3.Row, str]] = []
    by_id = {r["id"]: r for r in candidates}
    total = (len(candidates) + batch_size - 1) // batch_size
    for b in range(total):
        batch = candidates[b * batch_size : (b + 1) * batch_size]
        print(f"  [batch {b + 1}/{total}] {len(batch)} words", file=sys.stderr)
        for rid, v in _classify_batch(client, model, batch).items():
            row = by_id.get(rid)
            if row is None or not v["is_loanword"]:
                continue
            new_kana = v["katakana"]
            if new_kana == row["kana"]:
                continue
            reason = _validate_katakana(new_kana)
            if reason is not None:
                rejected.append((row, reason))
                continue
            proposals.append(
                Proposal(
                    id=rid,
                    english=row["english"],
                    old_kana=row["kana"],
                    new_kana=new_kana,
                    clear_kanji=False,
                    source="gemini",
                    ratio=_phonetic_ratio(row["kana"], new_kana),
                )
            )
    return proposals, rejected


# ---- Merge, conflict detection, reporting, apply -----------------------------

def _finalize(conn: sqlite3.Connection, proposals: list[Proposal]) -> list[Proposal]:
    """Flag UNIQUE(kana) conflicts against existing rows and within the set."""
    kana_owner = {r["kana"]: r["id"] for r in conn.execute("SELECT id, kana FROM words")}
    claimed: dict[str, int] = {}
    for p in proposals:
        owner = kana_owner.get(p.new_kana)
        if owner is not None and owner != p.id:
            p.conflict_with = owner
        elif p.new_kana in claimed:
            p.conflict_with = claimed[p.new_kana]  # two proposals want the same spelling
        else:
            claimed[p.new_kana] = p.id
    proposals.sort(key=lambda p: (p.source, p.conflict_with is not None, p.ratio))
    return proposals


def _print_proposals(proposals: list[Proposal], review_below: float) -> None:
    if not proposals:
        print("\nNo re-spellings proposed.")
        return
    print(f"\n{len(proposals)} proposed re-spellings "
          f"(⚠ = phonetic ratio < {review_below:.2f} or UNIQUE conflict):\n")
    print(f"  {'id':>5}  {'src':<10}  {'old':<14} → {'new':<14}  {'ratio':>5}  english")
    print(f"  {'-'*5}  {'-'*10}  {'-'*14}   {'-'*14}  {'-'*5}  {'-'*7}")
    for p in proposals:
        flag = " "
        note = ""
        if p.conflict_with is not None:
            flag, note = "⚠", f"  CONFLICT: {p.new_kana!r} already on word #{p.conflict_with} (skipped)"
        elif p.ratio < review_below:
            flag = "⚠"
        print(f"{flag} {p.id:>5}  {p.source:<10}  {p.old_kana:<14} → {p.new_kana:<14}  {p.ratio:>5.2f}  {p.english}{note}")


def _apply(conn: sqlite3.Connection, db_path: Path, proposals: list[Proposal]) -> int:
    writable = [p for p in proposals if p.conflict_with is None]
    if not writable:
        print("\nNothing to apply (all proposals were conflicts).")
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_name(f"{db_path.name}.pre-katakana-{stamp}")
    shutil.copy2(db_path, backup)
    print(f"\nBacked up DB → {backup}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        for p in writable:
            if p.clear_kanji:
                conn.execute("UPDATE words SET kana = ?, kanji = NULL WHERE id = ?", (p.new_kana, p.id))
            else:
                conn.execute("UPDATE words SET kana = ? WHERE id = ?", (p.new_kana, p.id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    print(f"Applied {len(writable)} re-spellings.")
    return len(writable)


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def main() -> None:
    _load_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    default_db = os.environ.get("KANA_QUIZ_DB") or str(REPO_ROOT / "data" / "kana_quiz.sqlite")
    ap.add_argument("--db", default=default_db, help=f"sqlite path (default: {default_db})")
    ap.add_argument("--model", default="gemini-3.6-flash", help="Gemini model id (Track 2)")
    ap.add_argument("--batch-size", type=int, default=40)
    ap.add_argument("--limit", type=int, default=0, help="cap Track 2 candidates (0 = all)")
    ap.add_argument("--review-below", type=float, default=0.34, help="flag proposals below this phonetic ratio")
    ap.add_argument("--skip-gemini", action="store_true", help="Track 1 only (deterministic, no API)")
    ap.add_argument("--exclude-ids", default="", help="comma-separated word ids to drop from the proposal set")
    ap.add_argument("--out", default=None, help="write a JSON report of proposals to this path")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--timeout-ms", type=int, default=60000)
    ap.add_argument("--list-models", action="store_true", help="list Gemini model ids and exit")
    args = ap.parse_args()

    if args.list_models:
        for m in _make_client(args.timeout_ms).models.list():
            print(getattr(m, "name", m))
        return

    db_path = Path(args.db)
    if not db_path.exists():
        sys.exit(f"DB not found: {db_path}")
    conn = _connect(db_path)

    proposals = _track1_proposals(conn)
    print(f"Track 1: {len(proposals)} rows where kanji already holds the katakana (kana:=kanji)", file=sys.stderr)

    rejected: list[tuple[sqlite3.Row, str]] = []
    if not args.skip_gemini:
        client = _make_client(args.timeout_ms)
        t2, rejected = _track2_proposals(conn, client, args.model, args.batch_size, args.limit)
        proposals.extend(t2)

    excluded = {int(x) for x in args.exclude_ids.split(",") if x.strip()}
    if excluded:
        proposals = [p for p in proposals if p.id not in excluded]
        print(f"Excluded {len(excluded)} id(s) by request: {sorted(excluded)}", file=sys.stderr)

    proposals = _finalize(conn, proposals)
    _print_proposals(proposals, args.review_below)
    if rejected:
        print(f"\n{len(rejected)} model outputs rejected by validation:")
        for row, reason in rejected:
            print(f"    #{row['id']} {row['kana']} ({row['english']}): {reason}")

    if args.out:
        Path(args.out).write_text(json.dumps({
            "model": args.model,
            "db": str(db_path),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "proposals": [vars(p) for p in proposals],
            "rejected": [{"id": r["id"], "kana": r["kana"], "english": r["english"], "reason": why} for r, why in rejected],
        }, ensure_ascii=False, indent=2))
        print(f"\nReport written → {args.out}")

    if args.apply:
        _apply(conn, db_path, proposals)
    else:
        writable = sum(1 for p in proposals if p.conflict_with is None)
        print(f"\nDRY RUN — no changes written. Re-run with --apply to commit {writable} re-spelling(s).")


if __name__ == "__main__":
    main()
