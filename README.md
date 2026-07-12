# kana-quiz

A local-first Japanese vocabulary trainer: Duolingo-style timed questions on top
of an Anki-style spaced-repetition scheduler. Words display primarily in kana,
with optional kanji. You bring your own vocabulary as a CSV — there's no bundled
dictionary and no third-party network dependency required to study.

- **Backend:** FastAPI + SQLite, an SM-2 variant scheduler.
- **Frontend:** Vue 3 + Pinia + Rsbuild, installable as a PWA.
- **Deploy:** single-user, self-hosted via Docker Compose.

## Features

- **Spaced repetition** with independent per-direction scheduling (recognition
  `ja→en` and recall `en→ja` are tracked and scheduled separately).
- **Multiple question modes**, mixed adaptively: multiple choice, type-in,
  cloze (fill-the-blank, choose or type), and sentence listening/dictation.
- **Adaptive distractors** — multiple-choice options are drawn from
  phonetically- and semantically-confusable words so the choices actually test
  discrimination.
- **Focused learn sessions** for new cards (intro → quiz → drill to fluency) kept
  separate from due-card reviews.
- **Speed-gated "burndown"** drills that replay a round's misses until you can
  clear each one quickly.
- **Due-time jitter** so a big study session doesn't dump as one wall of reviews
  at the same hour the next day.
- **Landing dashboard**: review lookahead (due now / next hour / next 24h) plus a
  mastery distribution and recent-accuracy summary.
- **Optional LLM free-text grading** (Gemini) for typed answers, so "to wake up"
  and "wake up" both count without maintaining a synonym list by hand.
- **Optional text-to-speech** (Google Cloud TTS) for pronunciation and listening
  questions.

Every LLM/TTS integration is optional — with no keys configured the app runs
fully offline; those features simply hide themselves.

## Quick start (local dev)

```bash
# One-time
make sync          # backend deps (uv)
make web-install   # frontend deps (npm)

# Two terminals
make dev-api       # http://localhost:8000 — FastAPI + SQLite at data/kana_quiz.sqlite
make dev-web       # http://localhost:5173 — Rsbuild dev, proxies /api -> :8000
```

Open `http://localhost:5173/import`, upload a CSV, then head to `/study`.

## Deploy (Docker)

```bash
cp .env.example .env   # optional — see Configuration
docker compose up -d --build
```

The container serves the built frontend and API on port 8000 and persists the
SQLite database to `./data`. The bundled `docker-compose.yml` attaches to an
external Docker network named `web` (intended to sit behind a reverse proxy);
drop that block, or `docker network create web`, if you don't use one.

## Configuration

All configuration is via environment variables (put them in `.env`); no secrets
are ever committed. Everything here is optional.

| variable | purpose |
|----------|---------|
| `KANA_AUTH_PASSWORD` | If set, gates `/api/*` behind a signed cookie. Unset = open (fine on localhost/LAN). |
| `KANA_AUTH_SECRET` | HMAC secret for the auth cookie (defaults to the password if unset). |
| `GEMINI_API_KEY` | Enables LLM grading of typed free-text answers. |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to a Google Cloud service-account key for TTS. |
| `KANA_TTS_GCP_PROJECT` | GCP project id for Cloud Text-to-Speech. |
| `KANA_TTS_DISABLED` | Set to `1` to hard-disable TTS (used in tests/CI). |
| `KANA_QUIZ_DB` | Path to the SQLite file (defaults to `data/kana_quiz.sqlite`). |

A few scheduler/interleave knobs (`KANA_REVIEW_JITTER_*`,
`KANA_*_INTERLEAVE_PROBABILITY`, `KANA_NEW_WORD_BACKLOG_LIMIT`,
`KANA_PAIR_SNOOZE_SECONDS`) have sensible defaults; see the source for details.

## CSV format

Header row required. Columns:

| column          | required | notes                                            |
|-----------------|----------|--------------------------------------------------|
| kana            | yes      | primary display form (unique key)                |
| english         | yes      | English gloss                                    |
| kanji           | no       | optional secondary display                       |
| tags            | no       | comma-separated; used to group distractors       |
| interval_days   | no       | seed SRS interval (e.g. from an Anki export)     |
| ease            | no       | seed SRS ease factor                             |
| repetitions     | no       | seed consecutive-correct count                   |
| due_at          | no       | ISO-8601; defaults to now when seeding interval  |
| introduced_at   | no       | ISO-8601; auto-filled when seeding interval > 0  |

Re-uploading the same CSV updates existing rows by `kana` (idempotent). SRS
columns are only overwritten when the incoming CSV actually supplies them, so a
second upload of just `kana,english` won't trample live scheduler state. See
`sample_vocab.csv` for a minimal example.

## Tests

```bash
make test
```

## License

MIT — see [LICENSE](LICENSE).
