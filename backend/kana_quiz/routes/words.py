"""Per-word management endpoints (ignore/unignore + the gloss corpus)."""

import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response

from kana_quiz.db import get_conn

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
        "SELECT english FROM words WHERE ignored_at IS NULL"
    ):
        raw = r["english"] or ""
        for piece in raw.split(","):
            s = piece.strip().lower()
            if s:
                glosses.add(s)
    return sorted(glosses)


@router.post("/words/{word_id}/ignore", status_code=204)
def ignore_word(
    word_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    """Mark a word as ignored — picker skips it on subsequent rounds."""
    row = conn.execute("SELECT id FROM words WHERE id = ?", (word_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="word not found")
    now = datetime.now(timezone.utc).isoformat()
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
        "UPDATE words SET ignored_at = NULL WHERE id = ?", (word_id,)
    )
    return Response(status_code=204)
