#!/usr/bin/env bash
# Run the kana-quiz backend and frontend in parallel with shared lifecycle.
#
# Each side is started as a backgrounded job; `set -m` puts every job in its
# own process group so we can take down the *whole* tree (uv → uvicorn,
# npm → rsbuild → node, plus the prefix pipelines) with a single signal to
# the group leader. Without that, child processes started by `uv run` or
# `npm run` get reparented and survive a plain `kill`.
#
# Triggers that bring both sides down together:
#   - Ctrl+C (SIGINT) or SIGTERM at the script
#   - either side exiting on its own (handled via `wait -n`)

set -uo pipefail
set -m

cd "$(dirname "$0")/.."

api_pgid=
web_pgid=

cleanup() {
    trap - INT TERM EXIT
    printf '\n[dev] shutting down...\n'
    [[ -n $api_pgid ]] && kill -TERM "-${api_pgid}" 2>/dev/null || true
    [[ -n $web_pgid ]] && kill -TERM "-${web_pgid}" 2>/dev/null || true
    sleep 1
    [[ -n $api_pgid ]] && kill -KILL "-${api_pgid}" 2>/dev/null || true
    [[ -n $web_pgid ]] && kill -KILL "-${web_pgid}" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

prefix() {
    local label=$1
    while IFS= read -r line; do
        printf '%s %s\n' "$label" "$line"
    done
}

printf '[dev] kana-quiz\n'
printf '[dev]   api  http://localhost:8000\n'
printf '[dev]   web  http://localhost:5173\n\n'

# PYTHONUNBUFFERED keeps uvicorn's stdout flushing per line so the prefix
# pipeline doesn't appear to lag.
{ PYTHONUNBUFFERED=1 uv run uvicorn kana_quiz.main:app \
    --reload --port 8000 --app-dir backend 2>&1 | prefix '[api]'; } &
api_pgid=$!

{ ( cd frontend && npm run dev ) 2>&1 | prefix '[web]'; } &
web_pgid=$!

# Return as soon as either side exits — the EXIT trap kills whatever's left.
wait -n
