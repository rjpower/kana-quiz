# AGENTS.md

Japanese vocabulary SRS app. FastAPI + SQLite backend, Vue 3 + Pinia
frontend. Single-user, local-first; the prod "deployment" is the dev's own
machine.

## Layout

- `backend/kana_quiz/` — FastAPI app. Routes live in `routes/`; the
  picker / SRS guts in `session.py`, `srs.py`, `task_state.py`. DB schema
  is `db.py` + incremental migrations under `migrations/`.
- `frontend/src/` — Vue. The whole quiz loop is `views/StudyView.vue`
  driven by the `stores/session.ts` Pinia store. `views/DeckDetailView.vue`
  is the per-deck card table (ignore toggle lives here).
- `data/kana_quiz.sqlite` — the real user DB. **Don't reset it.** Several
  `.backup-*` snapshots sit next to it; use them if you need a clean slate.
- `tests/` — pytest, FastAPI `TestClient`-based. `make test` runs them.

## Run

- `make dev-api` (8000) and `make dev-web` (5173, proxies /api). The
  user typically already has both running; check before starting.
- `KANA_QUIZ_DB=path/to.sqlite` overrides the DB path (used by tests).
- Gemini features (sentence generation, semantic grading, cloze
  generation) require `GEMINI_API_KEY`. Without it the app degrades:
  cloze stays off, grading falls back to exact-match.

## Data model — read before changing the picker

- `words` — vocab, one row per kana key. `ignored_at` (nullable
  timestamp) is the user-controlled "skip forever" flag.
- `task_state` — per-(word, task) SRS row. `task` ∈
  `{en2ja, ja2en, cloze, sentence_listen}`. `en2ja` + `ja2en` are the
  base recall ramp; `cloze` and `sentence_listen` are supplemental
  tracks that **unlock** once base recall ease/reps cross thresholds
  but then have their own independent SRS state.
- `introduced_at` is stamped on the first **answer**, not when the picker
  hands a card out. A `task_state` row can exist with `introduced_at IS
  NULL`: that means "dealt, never seen". Treat *introduced* as
  `introduced_at IS NOT NULL` everywhere — never as "a row exists". The
  picker runs ahead of the user (`/session/batch` deals several cards into
  a client-side buffer), so a round abandoned mid-buffer leaves rows behind
  for cards that never reached the screen; stamping at pick time used to
  send those to the user as *reviews* of material they had never met.
  Answering either recall direction stamps both, since the answer route
  only advances the one lane it was given.
- `reviews` — append-only log of every answered question. Don't
  retro-edit; downstream analytics rely on it.
- `sentence_cache` — Gemini-generated example sentences. One per
  `(word_id, model)`. Stores both the sentence and `target_form`
  (the conjugated surface form for the cloze blank).
- `audio_cache` — TTS MP3 blobs, keyed on `(text, voice, model)`.

## Gloss format — `words.english` is load-bearing

The English gloss is both the ja→en *answer* and the en→ja *prompt*, so
it has to be unique across the deck or the en→ja card is unanswerable.
The convention is `core gloss (disambiguator)`:

    命令  order (a command)
    順序  order (sequence)
    保存  preservation (of food or a file)

- **A trailing `(...)` is stripped before grading** (`PAREN_RE` in
  `grading.py`), so the user only ever has to type the core. It exists
  to disambiguate the en→ja prompt and to teach the nuance. A *mid*-string
  paren is an inline placeholder instead (`to get (something) done with`)
  and is stripped the same way.
- **Verbs must start with `to `.** `session.py:_is_verb` keys off that
  prefix to keep verb distractors away from noun cards.
- **No `,` `;` or `/` inside a gloss** — `split_meanings()` treats them as
  synonym separators at paren depth zero.
- ja→en multiple choice dedups distractors on `gloss_core()` (the
  paren-stripped core), so one card never offers two `order (…)` options.
- Rewriting a gloss? Demote the old one into `word_alternates`
  (`direction='ja2en'`, normalized) so answers that used to grade correct
  still do. `scripts/gloss_chunks.py` → LLM pass → `scripts/gloss_apply.py`
  does this end to end; `scripts/gloss_collisions.py` catches duplicates
  introduced by parallel workers. Past passes are tagged in
  `word_alternates.source`: `gloss-tighten`, `gloss-audit`,
  `gloss-rigorize`.

## Conventions

- All picker queries filter `w.ignored_at IS NULL`. New
  card-selection queries must do the same join. `/session/drill`
  intentionally does *not* — see rough edges.
- Answers are fire-and-forget from the client (`_send` in the Pinia
  store). Round-end drains the queue before showing summary. Don't
  await the POST in the hot path.
- `db.connect()` uses `isolation_level=None` — every statement
  autocommits.
- `fetchWithRetry` wraps every client fetch; use it for new endpoints.

## Known rough edges

- **Ignore feels ineffective at round end.** Ignoring mid-round does
  not splice the wrong-answer entry out of `roundAnswers`, so the
  ignored word shows up in "Words to revisit" and (because
  `/session/drill` doesn't check `ignored_at`) gets re-quizzed by
  "Drill the misses". DB-level ignore works fine; the gap is in
  `stores/session.ts` (`ignoreCurrent`, `startDrill`) and
  `routes/session.py:drill_question`.
- **Cloze target_form** can be wrong when Gemini emits a one-char form
  that collides elsewhere in the sentence; `_cloze_for_word` falls
  back to `gemini.locate_target_form` — see comments there before
  "fixing" template rendering.

## When in doubt

Inspect the DB. `sqlite3 data/kana_quiz.sqlite` is faster than
guessing what state the app is in.
