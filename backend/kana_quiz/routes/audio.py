"""Audio endpoints: lazy TTS with sqlite-backed caching.

Two flavors: ``/audio/word/{id}`` for a single word's kana
pronunciation, and ``/audio/sentence/{id}`` for the cached example
sentence — the latter drives the listening / sentence-translation
quiz mode. Both end up in the same ``audio_cache`` table keyed by
text, so a sentence and its constituent words don't share blobs but
neither do they conflict.
"""

import hashlib
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from kana_quiz import gemini
from kana_quiz.db import get_conn
from kana_quiz.tts import TTSUnavailable, get_or_create_audio

router = APIRouter()


def _send_audio(audio: bytes, mime: str, request: Request) -> Response:
    """Shared ETag + long-cache response shape for both audio endpoints."""
    etag = '"' + hashlib.sha1(audio).hexdigest() + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(
        content=audio,
        media_type=mime,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": etag,
        },
    )


@router.get("/audio/word/{word_id}")
def word_audio(
    word_id: int,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    """Return an MP3 of the word's kana pronunciation.

    First hit synthesizes via Google Cloud TTS and writes the BLOB; subsequent
    hits serve from sqlite. Long cache headers + ETag let the browser skip the
    network entirely after the first play.
    """
    row = conn.execute("SELECT kana FROM words WHERE id = ?", (word_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="word not found")

    try:
        audio, mime = get_or_create_audio(conn, row["kana"])
    except TTSUnavailable as exc:
        # 503 rather than 500: the feature is optional and the frontend
        # treats this as "hide the speaker button".
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return _send_audio(audio, mime, request)


@router.get("/audio/sentence/{word_id}")
def sentence_audio(
    word_id: int,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    """Return an MP3 of the cached Japanese example sentence for ``word_id``.

    Drives the listening / sentence-translation quiz mode. We pull the
    sentence text from ``sentence_cache`` (which the Gemini prefetch
    worker fills) and run it through the same audio cache as word
    audio — the cache key is the literal text, so a sentence gets its
    own row independent of any individual word's kana blob.

    Returns 404 if no sentence is cached for this word; the listening
    picker is supposed to gate on sentence availability so this branch
    is a defensive fallback for direct API hits.
    """
    row = conn.execute(
        "SELECT japanese FROM sentence_cache WHERE word_id = ? AND model = ?",
        (word_id, gemini.DEFAULT_MODEL),
    ).fetchone()
    if row is None or not row["japanese"]:
        raise HTTPException(status_code=404, detail="no cached sentence")

    try:
        audio, mime = get_or_create_audio(conn, row["japanese"])
    except TTSUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return _send_audio(audio, mime, request)
