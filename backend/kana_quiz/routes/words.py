"""Per-word management endpoints: quick add, ignore/unignore, the gloss corpus."""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Response

from kana_quiz import pins
from kana_quiz import gemini, tts
from kana_quiz.db import get_conn
from kana_quiz.schemas import QuickAddIn, QuickAddOut

# Where a quick add files its word. Created on first use when the deck is
# missing; migration 019 renamed the Takoboto deck to it.
QUICK_DECK = "Quick"
QUICK_DECK_LEVEL = 5

router = APIRouter()


@router.get("/glosses", response_model=list[str])
def all_glosses(conn: sqlite3.Connection = Depends(get_conn)) -> list[str]:
    """Distinct English gloss tokens across the deck.

    Comma-separated entries (e.g. "image, impression") are split so the
    autocomplete in the type-in input sees them as standalone tokens.
    Returned lowercased and de-duplicated; the client does its own
    normalization for prefix matching ("to compete" -> "compete").

    ~7k entries / ~30 KB gzipped at our current deck size — small
    enough to fetch once on first ja2en type-in and cache module-level.
    """
    glosses: set[str] = set()
    for r in conn.execute(
        "SELECT english FROM words WHERE ignored_at IS NULL AND kind = 'word'"
    ):
        raw = r["english"] or ""
        for piece in raw.split(","):
            s = piece.strip().lower()
            if s:
                glosses.add(s)
    return sorted(glosses)


def _quick_deck_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT id FROM decks WHERE name = ?", (QUICK_DECK,)).fetchone()
    if row is not None:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO decks (name, level, profile, created_at) VALUES (?, ?, 'standard', ?)",
        (QUICK_DECK, QUICK_DECK_LEVEL, pins.sql_now()),
    )
    return cur.lastrowid  # type: ignore[return-value]


def _found(conn: sqlite3.Connection, row: sqlite3.Row, created: bool) -> QuickAddOut:
    deck = conn.execute("SELECT name FROM decks WHERE id = ?", (row["deck_id"],)).fetchone()
    return QuickAddOut(
        id=row["id"],
        kana=row["kana"],
        kanji=row["kanji"],
        english=row["english"],
        deck_id=row["deck_id"],
        deck_name=deck["name"] if deck is not None else "",
        created=created,
    )


@router.post("/words/quick", response_model=QuickAddOut)
def quick_add(
    payload: QuickAddIn,
    conn: sqlite3.Connection = Depends(get_conn),
) -> QuickAddOut:
    """Make a card from one typed term, English or Japanese.

    Gemini supplies the kana, the kanji and the gloss. A term whose kana is
    already a card answers that card with ``created`` false, whichever deck
    it is in, so a word met twice is filed once. A gloss another card
    already uses is sent back to the model once with the clash named, since
    the gloss is the en→ja prompt and has to be unique.
    """
    term = " ".join(payload.text.split())
    if not term:
        raise HTTPException(status_code=400, detail="text is required")
    direct = conn.execute(
        "SELECT * FROM words WHERE kana = ? OR (kanji IS NOT NULL AND kanji = ?)",
        (term, term),
    ).fetchone()
    if direct is not None:
        return _found(conn, direct, created=False)
    try:
        entry = gemini.resolve_term(term)
    except gemini.GeminiUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # A する verb and its noun are one card: 目撃する is not filed beside 目撃.
    stem = entry.kana.removesuffix("する")
    existing = conn.execute(
        "SELECT * FROM words WHERE kana IN (?, ?)", (stem, stem + "する")
    ).fetchone()
    if existing is not None:
        return _found(conn, existing, created=False)
    clash = conn.execute(
        "SELECT kana FROM words WHERE english = ?", (entry.english,)
    ).fetchone()
    if clash is not None:
        try:
            entry = gemini.resolve_term(term, taken=(entry.english,))
        except gemini.GeminiUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        again = conn.execute(
            "SELECT kana FROM words WHERE english = ?", (entry.english,)
        ).fetchone()
        if again is not None:
            raise HTTPException(
                status_code=409,
                detail=f"the gloss {entry.english!r} is already used by {again['kana']}",
            )
    deck_id = _quick_deck_id(conn)
    cur = conn.execute(
        "INSERT INTO words (kana, english, kanji, deck_id) VALUES (?, ?, ?, ?)",
        (entry.kana, entry.english, entry.kanji, deck_id),
    )
    row = conn.execute("SELECT * FROM words WHERE id = ?", (cur.lastrowid,)).fetchone()
    tts.schedule_prefetch()
    gemini.schedule_prefetch()
    return _found(conn, row, created=True)


@router.post("/words/{word_id}/ignore", status_code=204)
def ignore_word(
    word_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    """Mark a word as ignored — picker skips it on subsequent rounds."""
    row = conn.execute("SELECT id FROM words WHERE id = ?", (word_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="word not found")
    now = pins.now().isoformat()
    conn.execute(
        "UPDATE words SET ignored_at = ? WHERE id = ?", (now, word_id)
    )
    return Response(status_code=204)


@router.delete("/words/{word_id}/ignore", status_code=204)
def unignore_word(
    word_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    """Re-enable a previously-ignored word."""
    row = conn.execute("SELECT id FROM words WHERE id = ?", (word_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="word not found")
    conn.execute(
        "UPDATE words SET ignored_at = NULL, archived_at = NULL WHERE id = ?",
        (word_id,),
    )
    return Response(status_code=204)
