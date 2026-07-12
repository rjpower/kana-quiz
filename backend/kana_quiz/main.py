"""FastAPI app factory.

The app exposes three concerns under ``/api``:

* ``/api/import`` — CSV upload that populates the ``words`` table.
* ``/api/session/*`` — Duolingo-style quiz flow: fetch a question, post the answer.
* ``/api/stats`` — progress summary used by the Stats view.

The schema is created on startup so running ``uvicorn`` against a fresh
``data/`` directory Just Works.

When ``KANA_QUIZ_STATIC_DIR`` points at a built frontend (``frontend/dist``)
the app also serves the SPA from ``/`` with a client-side-routing catch-all.
This is the mode the Docker image uses so the container ships a single port.
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s:    %(name)s: %(message)s")
# Our api_timing middleware logs every /api/* request with method, path,
# status, and duration — uvicorn's default access log would just duplicate
# that (without timing), so we silence it.
logging.getLogger("uvicorn.access").disabled = True
api_logger = logging.getLogger("kana_quiz.api")

# Requests slower than this are logged at WARNING so they stand out from the
# steady stream of fast INFO lines in `docker logs`. The backend normally
# answers in well under 300ms; anything past a second is worth a look (the
# usual culprit is the synchronous Gemini grade on /api/session/answer).
SLOW_REQUEST_MS = 1000.0

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from kana_quiz import auth as auth_module
from kana_quiz import gemini, tts
from kana_quiz.db import init_schema
from kana_quiz.routes import audio, decks, import_, session, stats, words


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_schema()
    tts_task = asyncio.create_task(tts.prefetch_worker())
    sentence_task = asyncio.create_task(gemini.prefetch_worker())
    try:
        yield
    finally:
        for task in (tts_task, sentence_task):
            task.cancel()
        for task in (tts_task, sentence_task):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def _mount_spa(app: FastAPI, static_dir: Path) -> None:
    """Serve the built SPA with history-mode fallback to ``index.html``."""
    assets = static_dir / "static"
    if assets.is_dir():
        app.mount("/static", StaticFiles(directory=assets), name="static")

    index = static_dir / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        # API routes are registered before this catch-all and take priority.
        candidate = static_dir / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        if not index.is_file():
            raise HTTPException(status_code=404, detail="frontend not built")
        return FileResponse(index)


def create_app() -> FastAPI:
    app = FastAPI(title="kana-quiz", lifespan=lifespan)
    # The rsbuild dev server proxies /api, but allow direct hits for tools
    # like curl / httpie when running the backend alone.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    @app.middleware("http")
    async def auth_gate(request: Request, call_next):
        path = request.url.path
        # The auth endpoints themselves and everything outside /api/ stay
        # public — see the docstring on kana_quiz.auth for why the SPA
        # shell isn't gated.
        if path.startswith("/api/") and not path.startswith("/api/auth/"):
            if not auth_module.is_authed(request):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.middleware("http")
    async def api_timing(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - start) * 1000
            api_logger.exception("%s %s -> error in %.1fms", request.method, path, elapsed_ms)
            raise
        elapsed_ms = (time.perf_counter() - start) * 1000
        level = logging.WARNING if elapsed_ms >= SLOW_REQUEST_MS else logging.INFO
        api_logger.log(
            level, "%s %s -> %d in %.1fms", request.method, path, response.status_code, elapsed_ms
        )
        return response

    app.include_router(auth_module.router)
    app.include_router(import_.router, prefix="/api")
    app.include_router(session.router, prefix="/api")
    app.include_router(stats.router, prefix="/api")
    app.include_router(audio.router, prefix="/api")
    app.include_router(decks.router, prefix="/api")
    app.include_router(words.router, prefix="/api")

    static_dir = os.environ.get("KANA_QUIZ_STATIC_DIR")
    if static_dir:
        path = Path(static_dir)
        if path.is_dir():
            _mount_spa(app, path)
    return app


app = create_app()
