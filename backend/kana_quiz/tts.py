"""Text-to-speech for quiz prompts.

Audio clips are generated on demand via Google Cloud Text-to-Speech and then
cached as BLOBs in sqlite. A vocabulary list has only a few hundred unique kana
strings, so the cache converges to a few MB and survives restarts.

Why Google Cloud (``ja-JP-Chirp3-HD``) over the old OpenAI ``nova`` voice:
OpenAI's TTS is English-first and gives Japanese no language-specific prosody
or pitch-accent modelling, so it sounded robotic and mis-accented. Google's
``ja-JP`` neural voices are Japanese-trained and markedly more natural. The
switch is contained to ``synthesize``; the cache key ``(text, voice, model)``
is unchanged, so flipping ``DEFAULT_VOICE`` / ``DEFAULT_MODEL`` transparently
re-keys the cache and the prefetch worker refills it (old OpenAI rows linger
but are never served).

Auth uses Application Default Credentials (``google.auth.default``) — the same
machinery ``google-genai`` already relies on — so no extra SDK is pulled in; we
hit the REST endpoint directly. A user-account ADC needs a billing/quota
project, supplied via the ``x-goog-user-project`` header (``KANA_TTS_GCP_PROJECT``).

The generation function is a module-level ``synthesize`` so tests can
monkeypatch it without touching the network.

A background ``prefetch_worker`` task fills the cache eagerly so the
first time the user sees a word, the audio is already a sqlite hit
rather than a synchronous synthesis roundtrip. The worker is woken on
startup and after every CSV import via :func:`schedule_prefetch`.
"""

import asyncio
import base64
import logging
import os
import sqlite3
from datetime import datetime, timezone

# Google Cloud TTS Japanese neural voice. The voice name encodes both the
# language and the model tier (Chirp 3 HD), so there is no separate "model"
# argument the way OpenAI had one — ``DEFAULT_MODEL`` is now purely a cache-key
# label so a future voice/tier change re-keys the cache cleanly.
DEFAULT_VOICE = os.environ.get("KANA_TTS_VOICE", "ja-JP-Chirp3-HD-Aoede")
DEFAULT_MODEL = os.environ.get("KANA_TTS_MODEL", "google-chirp3-hd")
AUDIO_MIME = "audio/mpeg"
AUDIO_FORMAT = "MP3"  # Google audioEncoding enum value
LANGUAGE_CODE = os.environ.get("KANA_TTS_LANGUAGE_CODE", "ja-JP")

# Billing/quota project for the TTS call. A user-account ADC must name one
# (sent as the ``x-goog-user-project`` header) or the API rejects the request;
# a service-account key carries its own project and ignores this. Set via
# ``KANA_TTS_GCP_PROJECT`` (or ``GOOGLE_CLOUD_PROJECT``); when neither is set
# the header is omitted — fine for a service-account key, which carries its own.
GCP_PROJECT = (
    os.environ.get("KANA_TTS_GCP_PROJECT")
    or os.environ.get("GOOGLE_CLOUD_PROJECT")
    or None
)

_TTS_ENDPOINT = "https://texttospeech.googleapis.com/v1/text:synthesize"
_TTS_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_REQUEST_TIMEOUT_S = 30

# Cached ADC credentials object. Reused across calls; refreshed in place when
# the access token expires (google-auth handles the refresh handshake).
_credentials = None


class TTSUnavailable(RuntimeError):
    """Raised when TTS is requested but Google credentials aren't available."""


def _tts_disabled() -> bool:
    """Whether TTS is explicitly switched off via ``KANA_TTS_DISABLED``.

    A hard kill switch independent of credential availability. Tests set it so
    the prefetch worker never spends real TTS quota even though dev machines
    carry working Application Default Credentials; it also lets a local run skip
    synthesis entirely.
    """
    return os.environ.get("KANA_TTS_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _load_credentials():
    """Return cached ADC credentials, resolving them on first use.

    Raises :class:`TTSUnavailable` when no Application Default Credentials are
    configured (mirrors the old "no OPENAI_API_KEY" degraded path).
    """
    global _credentials
    if _credentials is not None:
        return _credentials
    try:
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError

        try:
            creds, _ = google.auth.default(scopes=[_TTS_SCOPE])
        except DefaultCredentialsError as exc:
            raise TTSUnavailable(f"Google ADC not configured: {exc}") from exc
    except ImportError as exc:  # pragma: no cover - google-auth ships with genai
        raise TTSUnavailable(f"google-auth unavailable: {exc}") from exc
    _credentials = creds
    return creds


def _access_token() -> str:
    """Return a valid OAuth access token, refreshing the cached creds if stale."""
    from google.auth.transport.requests import Request

    creds = _load_credentials()
    if not creds.valid:
        try:
            creds.refresh(Request())
        except Exception as exc:  # network / revoked creds / etc.
            raise TTSUnavailable(f"failed to refresh Google credentials: {exc}") from exc
    return creds.token


def credentials_available() -> bool:
    """Cheap probe for the debug view: are Google credentials resolvable?"""
    try:
        _load_credentials()
        return True
    except TTSUnavailable:
        return False


def synthesize(text: str, *, voice: str, model: str) -> bytes:
    """Generate MP3 audio for ``text`` via Google Cloud Text-to-Speech.

    ``model`` is retained for signature/cache compatibility but isn't sent to
    the API (the Chirp 3 HD tier is selected by the ``voice`` name). Raises
    :class:`TTSUnavailable` when credentials aren't configured.
    """
    if _tts_disabled():
        raise TTSUnavailable("TTS disabled via KANA_TTS_DISABLED")
    import requests

    token = _access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    if GCP_PROJECT:
        headers["x-goog-user-project"] = GCP_PROJECT
    body = {
        "input": {"text": text},
        "voice": {"languageCode": LANGUAGE_CODE, "name": voice},
        "audioConfig": {"audioEncoding": AUDIO_FORMAT},
    }
    resp = requests.post(
        _TTS_ENDPOINT, headers=headers, json=body, timeout=_REQUEST_TIMEOUT_S
    )
    if resp.status_code != 200:
        # 401/403 here is almost always a missing/insufficient credential or an
        # unbilled quota project — surface it as TTSUnavailable so the prefetch
        # worker stops cleanly instead of hammering a doomed request.
        detail = resp.text[:300]
        if resp.status_code in (401, 403):
            raise TTSUnavailable(f"Google TTS auth error {resp.status_code}: {detail}")
        raise RuntimeError(f"Google TTS HTTP {resp.status_code}: {detail}")
    audio_b64 = resp.json().get("audioContent")
    if not audio_b64:
        raise RuntimeError("Google TTS returned no audioContent")
    return base64.b64decode(audio_b64)


def get_or_create_audio(
    conn: sqlite3.Connection,
    text: str,
    *,
    voice: str = DEFAULT_VOICE,
    model: str = DEFAULT_MODEL,
) -> tuple[bytes, str]:
    """Return cached audio for ``text``, synthesizing + persisting on cache miss."""
    row = conn.execute(
        "SELECT blob, mime FROM audio_cache WHERE text = ? AND voice = ? AND model = ?",
        (text, voice, model),
    ).fetchone()
    if row is not None:
        return bytes(row["blob"]), row["mime"]

    audio = synthesize(text, voice=voice, model=model)
    conn.execute(
        """
        INSERT OR IGNORE INTO audio_cache (text, voice, model, mime, blob, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (text, voice, model, AUDIO_MIME, audio, datetime.now(timezone.utc).isoformat()),
    )
    return audio, AUDIO_MIME


# --- Background prefetch worker ---------------------------------------------

log = logging.getLogger(__name__)

_wakeup: asyncio.Event | None = None
# Live telemetry for the debug view. Mirrors `gemini._worker_state`.
_worker_state: dict = {
    "busy": False,
    "current": None,
    "batch_done": 0,
    "batch_total": 0,
    "last_error": None,
}


def _get_wakeup() -> asyncio.Event:
    global _wakeup
    if _wakeup is None:
        _wakeup = asyncio.Event()
    return _wakeup


def schedule_prefetch() -> None:
    """Nudge the background worker to scan for newly-imported words.

    No-op if the worker isn't running yet (the lifespan startup also
    triggers an initial scan, so the first request after boot is covered).
    """
    if _wakeup is not None:
        _wakeup.set()


def _missing_kana(conn: sqlite3.Connection, voice: str, model: str) -> list[str]:
    """Return word kana strings that need audio synthesized.

    Only covers per-word kana; example sentence audio is generated on demand
    when a listening card is actually played.
    """
    rows = conn.execute(
        """
        SELECT w.kana AS text FROM words w
        LEFT JOIN audio_cache a
          ON a.text = w.kana AND a.voice = ? AND a.model = ?
        WHERE a.id IS NULL
        ORDER BY w.id ASC
        """,
        (voice, model),
    ).fetchall()
    return [r["text"] for r in rows]


def prefetch_status(
    conn: sqlite3.Connection,
    *,
    voice: str = DEFAULT_VOICE,
    model: str = DEFAULT_MODEL,
) -> dict:
    """Counts + worker state for the debug view (mirrors gemini.prefetch_status)."""
    total = conn.execute("SELECT COUNT(DISTINCT kana) AS n FROM words").fetchone()["n"]
    cached = conn.execute(
        """
        SELECT COUNT(DISTINCT a.text) AS n
          FROM audio_cache a
          JOIN words w ON w.kana = a.text
         WHERE a.voice = ? AND a.model = ?
        """,
        (voice, model),
    ).fetchone()["n"]
    return {
        "voice": voice,
        "model": model,
        "total_words": total,
        "cached": cached,
        "missing": max(0, total - cached),
        "key_configured": credentials_available(),
        "worker_busy": bool(_worker_state["busy"]),
        "worker_current": _worker_state["current"],
        "worker_batch_done": _worker_state["batch_done"],
        "worker_batch_total": _worker_state["batch_total"],
        "worker_last_error": _worker_state["last_error"],
        "worker_pending_wake": bool(_wakeup is not None and _wakeup.is_set()),
    }


async def prefetch_worker(
    *,
    voice: str = DEFAULT_VOICE,
    model: str = DEFAULT_MODEL,
) -> None:
    """Continuously synthesize audio for words missing from the cache.

    Runs in the FastAPI lifespan. Sleeps on an :class:`asyncio.Event` and
    wakes on startup or whenever :func:`schedule_prefetch` is called.
    Synthesis is sequential to keep API spend predictable; each call hops
    to a worker thread so the event loop stays responsive.
    """
    # Local import avoids a circular dependency at module load time.
    from kana_quiz.db import connect

    wakeup = _get_wakeup()
    wakeup.set()  # initial scan on startup
    while True:
        try:
            await wakeup.wait()
            wakeup.clear()
            conn = connect()
            try:
                missing = _missing_kana(conn, voice, model)
            finally:
                conn.close()
            if not missing:
                log.info("audio prefetch: cache up to date")
                _worker_state.update(busy=False, current=None, batch_done=0, batch_total=0)
                continue
            log.info("audio prefetch: %d clip(s) pending", len(missing))
            _worker_state.update(
                busy=True, current=None, batch_done=0, batch_total=len(missing),
            )
            done = 0
            for kana in missing:
                conn = connect()
                try:
                    _worker_state["current"] = kana
                    await asyncio.to_thread(
                        get_or_create_audio, conn, kana, voice=voice, model=model
                    )
                    done += 1
                    _worker_state["batch_done"] = done
                    log.info("audio prefetch: %d/%d %r", done, len(missing), kana)
                except TTSUnavailable:
                    log.info("TTS unavailable; stopping prefetch worker")
                    _worker_state.update(
                        busy=False, current=None,
                        last_error="OPENAI_API_KEY not configured",
                    )
                    return
                except Exception as exc:
                    log.exception("audio prefetch failed for %r", kana)
                    _worker_state["last_error"] = f"{type(exc).__name__}: {exc}"
                    await asyncio.sleep(1.0)
                finally:
                    conn.close()
            log.info("audio prefetch: batch complete (%d synthesized)", done)
            _worker_state.update(busy=False, current=None)
        except asyncio.CancelledError:
            _worker_state.update(busy=False, current=None)
            raise
        except Exception as exc:
            log.exception("audio prefetch loop error")
            _worker_state["last_error"] = f"{type(exc).__name__}: {exc}"
            _worker_state.update(busy=False, current=None)
            await asyncio.sleep(5.0)
