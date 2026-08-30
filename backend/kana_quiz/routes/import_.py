"""POST /api/import — multipart CSV upload."""

import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile

from kana_quiz import gemini, tts
from kana_quiz.csv_import import import_csv
from kana_quiz.db import get_conn
from kana_quiz.schemas import ImportReportOut

router = APIRouter()


@router.post("/import", response_model=ImportReportOut)
async def upload_csv(
    file: UploadFile,
    deck_id: int | None = Form(None),
    new_deck_name: str | None = Form(None),
    new_deck_level: int = Form(5),
    new_deck_profile: str = Form("standard"),
    insert_only: bool = Form(False),
    conn: sqlite3.Connection = Depends(get_conn),
) -> ImportReportOut:
    """Upload a vocabulary CSV (header row required, kana + english mandatory).

    Either ``deck_id`` (existing deck) or ``new_deck_name`` (creates a new
    deck at ``new_deck_level``, with ``new_deck_profile`` — 'standard' or
    'sprint') must be supplied. Imported words land in that deck.
    ``insert_only`` skips rows whose kana already exists instead of
    updating them; a machine-built export sets it so it can never clobber a
    curated gloss.
    """
    target_deck_id: int
    if deck_id is not None and new_deck_name is not None:
        raise HTTPException(
            status_code=400,
            detail="provide either deck_id or new_deck_name, not both",
        )
    if deck_id is not None:
        row = conn.execute("SELECT id FROM decks WHERE id = ?", (deck_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="deck not found")
        target_deck_id = deck_id
    elif new_deck_name is not None:
        name = new_deck_name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="new_deck_name cannot be empty")
        if new_deck_profile not in ("standard", "sprint"):
            raise HTTPException(
                status_code=400, detail="new_deck_profile must be standard or sprint"
            )
        clash = conn.execute(
            "SELECT id FROM decks WHERE name = ?", (name,)
        ).fetchone()
        if clash is not None:
            raise HTTPException(status_code=409, detail="deck name already exists")
        cur = conn.execute(
            "INSERT INTO decks (name, level, profile) VALUES (?, ?, ?)",
            (name, new_deck_level, new_deck_profile),
        )
        target_deck_id = cur.lastrowid  # type: ignore[assignment]
    else:
        raise HTTPException(
            status_code=400,
            detail="deck_id or new_deck_name is required",
        )

    payload = await file.read()
    try:
        report = import_csv(conn, payload, target_deck_id, insert_only=insert_only)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    tts.schedule_prefetch()
    gemini.schedule_prefetch()
    deck_row = conn.execute(
        "SELECT name FROM decks WHERE id = ?", (target_deck_id,)
    ).fetchone()
    return ImportReportOut(
        inserted=report.inserted,
        updated=report.updated,
        skipped=report.skipped,
        deck_id=target_deck_id,
        deck_name=deck_row["name"] if deck_row is not None else "",
    )
