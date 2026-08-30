"""Parse and upsert a vocabulary CSV into the ``words`` table.

The CSV is expected to carry a header row with at least ``kana`` and ``english``
columns. ``kanji`` and ``tags`` are optional. Rows without a non-empty kana
and english are counted as ``skipped`` rather than raising — the import view
surfaces the counts back to the user.

SRS state columns (``interval_days``, ``ease``, ``repetitions``, ``due_at``,
``introduced_at``) are also optional. When present they let users seed the
scheduler with state exported from another system (e.g. Anki) so mature cards
don't have to re-learn from zero. Seeded SRS state is applied to *both*
recall task_state rows since the CSV doesn't distinguish them. On re-import
of the same kana, SRS state columns are only overwritten when the incoming
CSV actually supplies them.
"""

import csv
import io
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from kana_quiz.task_state import CARD_TASKS

REQUIRED_COLUMNS = ("kana", "english")
SRS_COLUMNS = ("interval_days", "ease", "repetitions", "due_at", "introduced_at")


@dataclass(frozen=True)
class ImportReport:
    inserted: int
    updated: int
    skipped: int


def _normalize_tags(raw: str | None) -> str | None:
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return None
    return ",".join(parts)


def _parse_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    return float(raw)


def _parse_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    return int(raw)


def _parse_dt(raw: str | None) -> str | None:
    """Accept ISO-8601 timestamps; normalize to UTC ISO for storage."""
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _seed_task_state(
    conn: sqlite3.Connection,
    word_id: int,
    *,
    ease: float | None,
    interval_days: float | None,
    repetitions: int | None,
    due_at: str | None,
    introduced_at: str | None,
) -> None:
    """Stamp SRS state on both recall task rows for a freshly-inserted word.

    Only called when the CSV actually supplied SRS columns — otherwise the
    word stays "fresh" and gets its recall state rows lazily from the picker.
    """
    if all(v is None for v in (ease, interval_days, repetitions, due_at, introduced_at)):
        return
    for task in CARD_TASKS:
        conn.execute(
            """
            INSERT OR REPLACE INTO task_state
              (word_id, task, ease, interval_days, repetitions,
               due_at, introduced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                word_id,
                task,
                ease if ease is not None else 2.5,
                interval_days if interval_days is not None else 0.0,
                repetitions if repetitions is not None else 0,
                due_at,
                introduced_at,
            ),
        )


def _update_task_state(
    conn: sqlite3.Connection,
    word_id: int,
    *,
    ease: float | None,
    interval_days: float | None,
    repetitions: int | None,
    due_at: str | None,
    introduced_at: str | None,
) -> None:
    """Mirror partial SRS-column updates onto both recall task rows.

    Only the columns the importer actually supplied are written, so a CSV
    that tweaks english/kanji without SRS state leaves the live scheduler
    alone.
    """
    sets: list[str] = []
    params: list[object] = []
    if ease is not None:
        sets.append("ease = ?")
        params.append(ease)
    if interval_days is not None:
        sets.append("interval_days = ?")
        params.append(interval_days)
    if repetitions is not None:
        sets.append("repetitions = ?")
        params.append(repetitions)
    if due_at is not None:
        sets.append("due_at = ?")
        params.append(due_at)
    if introduced_at is not None:
        sets.append("introduced_at = ?")
        params.append(introduced_at)
    if not sets:
        return
    for task in CARD_TASKS:
        # If the row doesn't exist yet, INSERT it; otherwise UPDATE.
        existing = conn.execute(
            "SELECT 1 FROM task_state WHERE word_id = ? AND task = ?",
            (word_id, task),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO task_state
                  (word_id, task, ease, interval_days, repetitions,
                   due_at, introduced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    word_id,
                    task,
                    ease if ease is not None else 2.5,
                    interval_days if interval_days is not None else 0.0,
                    repetitions if repetitions is not None else 0,
                    due_at,
                    introduced_at,
                ),
            )
        else:
            conn.execute(
                f"UPDATE task_state SET {', '.join(sets)} "
                f"WHERE word_id = ? AND task = ?",
                (*params, word_id, task),
            )


def import_csv(
    conn: sqlite3.Connection,
    payload: bytes,
    deck_id: int,
    insert_only: bool = False,
) -> ImportReport:
    """Upsert each CSV row, keyed by kana. Returns row-level counts.

    ``deck_id`` is required and is applied to *new* rows only — re-imports
    leave the existing ``deck_id`` untouched so users can move words
    between decks via the UI without an export round-trip.

    ``insert_only`` skips rows whose kana already exists instead of updating
    them. A machine-built export (kaku's episode decks) sets it so a
    contextual translation can never overwrite a curated gloss that already
    lives in another deck.

    A ``kind`` column of ``sentence`` marks a full-line card: the ``kana``
    column carries the sentence as written and ``english`` its translation.
    Absent or empty means ``word``.
    """
    text = payload.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")
    missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
    if missing:
        raise ValueError(f"CSV missing required columns: {missing}")

    inserted = updated = skipped = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    conn.execute("BEGIN")
    try:
        for row in reader:
            kana = (row.get("kana") or "").strip()
            english = (row.get("english") or "").strip()
            if not kana or not english:
                skipped += 1
                continue
            kanji = (row.get("kanji") or "").strip() or None
            tags = _normalize_tags(row.get("tags"))
            kind = (row.get("kind") or "").strip() or "word"
            if kind not in ("word", "sentence"):
                raise ValueError(f"unknown kind {kind!r} for {kana!r}")

            interval_days = _parse_float(row.get("interval_days"))
            ease = _parse_float(row.get("ease"))
            repetitions = _parse_int(row.get("repetitions"))
            due_at = _parse_dt(row.get("due_at"))
            introduced_at = _parse_dt(row.get("introduced_at"))

            if due_at is None and (interval_days is not None or repetitions is not None):
                due_at = now_iso
            if introduced_at is None and (
                (interval_days is not None and interval_days > 0)
                or (repetitions is not None and repetitions > 0)
            ):
                introduced_at = now_iso

            existing = conn.execute(
                "SELECT id FROM words WHERE kana = ?", (kana,)
            ).fetchone()
            if existing is None:
                cur = conn.execute(
                    """
                    INSERT INTO words (kana, english, kanji, tags, deck_id, kind)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (kana, english, kanji, tags, deck_id, kind),
                )
                _seed_task_state(
                    conn,
                    cur.lastrowid,  # type: ignore[arg-type]
                    ease=ease,
                    interval_days=interval_days,
                    repetitions=repetitions,
                    due_at=due_at,
                    introduced_at=introduced_at,
                )
                inserted += 1
            elif insert_only:
                skipped += 1
                continue
            else:
                conn.execute(
                    "UPDATE words SET english = ?, kanji = ?, tags = ? WHERE id = ?",
                    (english, kanji, tags, existing["id"]),
                )
                _update_task_state(
                    conn,
                    existing["id"],
                    ease=ease,
                    interval_days=interval_days,
                    repetitions=repetitions,
                    due_at=due_at,
                    introduced_at=introduced_at,
                )
                updated += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return ImportReport(inserted=inserted, updated=updated, skipped=skipped)
