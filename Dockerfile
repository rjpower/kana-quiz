# syntax=docker/dockerfile:1.7

# ---------- Stage 1: build the Vue SPA ----------
FROM node:20-alpine AS web
WORKDIR /web

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build


# ---------- Stage 2: python runtime ----------
# uv ships a slim distroless-ish image with the uv binary; we copy it into the
# python-slim base so we keep `uv sync` for reproducible installs but run on a
# conventional glibc python at runtime.
FROM python:3.13-slim AS runtime

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1 \
    KANA_QUIZ_DB=/data/kana_quiz.sqlite \
    KANA_QUIZ_STATIC_DIR=/app/web

WORKDIR /app

# Install Python deps first so dependency layers cache independently of source.
COPY pyproject.toml uv.lock ./
COPY backend/ ./backend/
RUN uv sync --frozen --no-dev

# Built SPA from stage 1 — served by FastAPI under /.
COPY --from=web /web/dist /app/web

# Persistent SQLite lives under /data so users can `-v kana-data:/data`.
RUN mkdir -p /data

EXPOSE 8000

# --app-dir so uvicorn finds the kana_quiz package; --host 0.0.0.0 so the
# container is reachable from the host.
CMD ["uv", "run", "--no-dev", \
     "uvicorn", "kana_quiz.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--app-dir", "backend"]
