"""Deck CRUD endpoints."""

import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response

from kana_quiz.db import get_conn
from kana_quiz.schemas import (
    DeckIn,
    DeckOut,
    DeckPatch,
    DeckWord,
    DeckWordState,
    IgnoredWord,
)
from kana_quiz.task_state import CARD_TASKS

router = APIRouter()


def _stats_for(conn: sqlite3.Connection, deck_id: int) -> tuple[int, int, int, int]:
    """Return (word_count, new_count, due_count, ignored_count) for a deck.

    ``word_count`` is the *active* size — ignored words are accounted for
    separately so the user can see e.g. "Default · 1235 words · 32 ignored".
    ``new_count`` and ``due_count`` already exclude ignored via the picker
    queries, but we mirror that here so the deck card stays consistent.
    """
    word_count = conn.execute(
        "SELECT COUNT(*) AS n FROM words WHERE deck_id = ? AND ignored_at IS NULL",
        (deck_id,),
    ).fetchone()["n"]
    ignored_count = conn.execute(
        "SELECT COUNT(*) AS n FROM words WHERE deck_id = ? AND ignored_at IS NOT NULL",
        (deck_id,),
    ).fetchone()["n"]
    new_count = conn.execute(
        """
        SELECT COUNT(*) AS n FROM words w
         WHERE w.deck_id = ?
           AND w.ignored_at IS NULL
           AND NOT EXISTS (
             SELECT 1 FROM task_state ts
              WHERE ts.word_id = w.id AND ts.task IN (?, ?)
           )
        """,
        (deck_id, *CARD_TASKS),
    ).fetchone()["n"]
    now_iso = datetime.now(timezone.utc).isoformat()
    due_count = conn.execute(
        """
        SELECT COUNT(*) AS n FROM task_state ts
          JOIN words w ON w.id = ts.word_id
         WHERE w.deck_id = ?
           AND w.ignored_at IS NULL
           AND ts.task IN (?, ?)
           AND ts.introduced_at IS NOT NULL
           AND ts.due_at <= ?
        """,
        (deck_id, *CARD_TASKS, now_iso),
    ).fetchone()["n"]
    return word_count, new_count, due_count, ignored_count


def _row_to_deck(conn: sqlite3.Connection, row: sqlite3.Row) -> DeckOut:
    word_count, new_count, due_count, ignored_count = _stats_for(conn, row["id"])
    return DeckOut(
        id=row["id"],
        name=row["name"],
        level=row["level"],
        created_at=row["created_at"],
        word_count=word_count,
        new_count=new_count,
        due_count=due_count,
        ignored_count=ignored_count,
    )


@router.get("/decks", response_model=list[DeckOut])
def list_decks(conn: sqlite3.Connection = Depends(get_conn)) -> list[DeckOut]:
    rows = conn.execute(
        "SELECT id, name, level, created_at FROM decks ORDER BY level ASC, name ASC"
    ).fetchall()
    return [_row_to_deck(conn, r) for r in rows]


@router.post("/decks", response_model=DeckOut, status_code=201)
def create_deck(
    payload: DeckIn,
    conn: sqlite3.Connection = Depends(get_conn),
) -> DeckOut:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    existing = conn.execute(
        "SELECT id FROM decks WHERE name = ?", (name,)
    ).fetchone()
    if existing is not None:
        raise HTTPException(status_code=409, detail="deck name already exists")
    cur = conn.execute(
        "INSERT INTO decks (name, level) VALUES (?, ?)",
        (name, payload.level),
    )
    deck_id = cur.lastrowid
    row = conn.execute(
        "SELECT id, name, level, created_at FROM decks WHERE id = ?",
        (deck_id,),
    ).fetchone()
    return _row_to_deck(conn, row)


@router.patch("/decks/{deck_id}", response_model=DeckOut)
def update_deck(
    deck_id: int,
    payload: DeckPatch,
    conn: sqlite3.Connection = Depends(get_conn),
) -> DeckOut:
    row = conn.execute(
        "SELECT id, name, level, created_at FROM decks WHERE id = ?", (deck_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="deck not found")
    sets: list[str] = []
    params: list[object] = []
    if payload.name is not None:
        new_name = payload.name.strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="name cannot be empty")
        clash = conn.execute(
            "SELECT id FROM decks WHERE name = ? AND id != ?",
            (new_name, deck_id),
        ).fetchone()
        if clash is not None:
            raise HTTPException(status_code=409, detail="deck name already exists")
        sets.append("name = ?")
        params.append(new_name)
    if payload.level is not None:
        sets.append("level = ?")
        params.append(payload.level)
    if sets:
        params.append(deck_id)
        conn.execute(f"UPDATE decks SET {', '.join(sets)} WHERE id = ?", tuple(params))
    row = conn.execute(
        "SELECT id, name, level, created_at FROM decks WHERE id = ?", (deck_id,)
    ).fetchone()
    return _row_to_deck(conn, row)


@router.delete("/decks/{deck_id}", status_code=204)
def delete_deck(
    deck_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    row = conn.execute("SELECT id FROM decks WHERE id = ?", (deck_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="deck not found")
    word_count = conn.execute(
        "SELECT COUNT(*) AS n FROM words WHERE deck_id = ?", (deck_id,)
    ).fetchone()["n"]
    if word_count > 0:
        raise HTTPException(
            status_code=409, detail=f"deck has {word_count} words"
        )
    conn.execute("DELETE FROM decks WHERE id = ?", (deck_id,))
    return Response(status_code=204)


@router.get("/decks/{deck_id}/words", response_model=list[DeckWord])
def list_deck_words(
    deck_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[DeckWord]:
    """Every word in the deck — active and ignored — for the detail view.

    Sorted by id ASC so the table reflects import order (which mirrors
    the SRS introduction order). Decks of any reasonable size fit in a
    single response; the heaviest deck in the wild is ~1.2k rows / ~80
    KB JSON which still rounds-trips in under 50 ms.
    """
    if conn.execute("SELECT 1 FROM decks WHERE id = ?", (deck_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="deck not found")
    # Join both recall task_state rows for each word in a single query (one
    # row per word_id x task pair). LEFT JOIN so katakana-only
    # cards — which never get a ja2en row — still come back with a
    # plain "no state" indicator on that side.
    rows = conn.execute(
        """
        SELECT w.id, w.kana, w.english, w.kanji, w.ignored_at,
               ts.task AS direction, ts.ease, ts.repetitions, ts.introduced_at
          FROM words w
          LEFT JOIN task_state ts
            ON ts.word_id = w.id AND ts.task IN (?, ?)
         WHERE w.deck_id = ?
         ORDER BY w.id ASC
        """,
        (*CARD_TASKS, deck_id),
    ).fetchall()

    by_id: dict[int, dict] = {}
    for r in rows:
        word = by_id.setdefault(
            r["id"],
            {
                "id": r["id"],
                "kana": r["kana"],
                "english": r["english"],
                "kanji": r["kanji"],
                "ignored": r["ignored_at"] is not None,
                "en2ja": None,
                "ja2en": None,
            },
        )
        if r["direction"] is None:
            continue
        state = DeckWordState(
            ease=r["ease"],
            repetitions=r["repetitions"],
            introduced=r["introduced_at"] is not None,
        )
        word[r["direction"]] = state
    return [DeckWord(**w) for w in by_id.values()]


@router.get("/decks/{deck_id}/ignored", response_model=list[IgnoredWord])
def list_ignored(
    deck_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[IgnoredWord]:
    """Return the deck's ignored words so the user can re-enable them."""
    if conn.execute("SELECT 1 FROM decks WHERE id = ?", (deck_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="deck not found")
    rows = conn.execute(
        """
        SELECT id, kana, english, kanji, ignored_at FROM words
         WHERE deck_id = ? AND ignored_at IS NOT NULL
         ORDER BY ignored_at DESC
        """,
        (deck_id,),
    ).fetchall()
    return [
        IgnoredWord(
            id=r["id"],
            kana=r["kana"],
            english=r["english"],
            kanji=r["kanji"],
            ignored_at=r["ignored_at"],
        )
        for r in rows
    ]
