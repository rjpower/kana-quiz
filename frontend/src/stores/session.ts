import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { prefetchAudioUrl, versionedAudioUrl, wordAudioUrl } from '../audio'
import { useToastStore } from './toasts'

// Mobile networks blip constantly. Every fetch in this store goes through
// a timeout+retry wrapper so a single dropped packet can't stall the UI
// indefinitely — without it, the answer-then-prefetch flow can wedge with
// `locked=true` forever, ignoring every subsequent tap.
const FETCH_TIMEOUT_MS = 6000
const FETCH_RETRY_ATTEMPTS = 4
const FETCH_RETRY_BASE_MS = 300
// A client-perceived round trip slower than this is worth reporting even when
// it eventually succeeds — the server answers in <300ms, so the time went to
// the network or a stalled connection that server-side timing can't see.
const SLOW_FETCH_MS = 2000

// Report a fetch anomaly to the backend so client-side hangs land in the same
// log stream as the server's request timing. Best-effort and silent — telemetry
// must never break the quiz flow, and this call deliberately bypasses
// fetchWithRetry so it can't recurse.
function reportClientFetch(payload: {
  url: string
  outcome: 'slow' | 'retry' | 'timeout' | 'failed'
  duration_ms: number
  attempts: number
  detail?: string
}) {
  try {
    console.warn('[kana-quiz] fetch anomaly', payload)
    fetch('/api/client_log', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      keepalive: true,
    }).catch(() => {})
  } catch {
    /* ignore */
  }
}

// Query strings can carry per-question payloads; the path alone is enough to
// locate the slow endpoint, so strip them before logging.
const cleanUrl = (u: string) => u.split('?')[0]

async function fetchWithRetry(
  url: string,
  init: RequestInit = {},
  attempts = FETCH_RETRY_ATTEMPTS,
): Promise<Response> {
  const started = performance.now()
  let lastErr: unknown
  let timedOut = false
  for (let i = 0; i < attempts; i++) {
    const ctrl = new AbortController()
    const timer = setTimeout(() => {
      timedOut = true
      ctrl.abort()
    }, FETCH_TIMEOUT_MS)
    try {
      const resp = await fetch(url, { ...init, signal: ctrl.signal })
      clearTimeout(timer)
      // 5xx + 408/429 are worth a retry; other 4xx are terminal.
      const retriable = resp.status >= 500 || resp.status === 408 || resp.status === 429
      if (!retriable) {
        const elapsed = Math.round(performance.now() - started)
        // A success that needed a retry, or that took seconds, is the hang the
        // user felt even though the request ultimately went through.
        if (i > 0) {
          reportClientFetch({ url: cleanUrl(url), outcome: 'retry', duration_ms: elapsed, attempts: i + 1 })
        } else if (elapsed >= SLOW_FETCH_MS) {
          reportClientFetch({ url: cleanUrl(url), outcome: 'slow', duration_ms: elapsed, attempts: 1 })
        }
        return resp
      }
      lastErr = new Error(`http ${resp.status}`)
    } catch (e) {
      clearTimeout(timer)
      lastErr = e
    }
    if (i < attempts - 1) {
      await new Promise((r) => setTimeout(r, FETCH_RETRY_BASE_MS * 2 ** i))
    }
  }
  reportClientFetch({
    url: cleanUrl(url),
    outcome: timedOut ? 'timeout' : 'failed',
    duration_ms: Math.round(performance.now() - started),
    attempts,
    detail: lastErr instanceof Error ? lastErr.message : String(lastErr),
  })
  throw lastErr instanceof Error ? lastErr : new Error('fetch failed')
}

export type Direction = 'en2ja' | 'ja2en'
export type QuestionMode = 'mc' | 'type' | 'cloze' | 'cloze_choice' | 'sentence_listen'

// The answer types the user can enable/disable on the start screen. Order is
// the display order in the settings panel. `mc` and `type` are the two *core*
// recall modes — at least one must always stay on so recall can function.
export const ANSWER_TYPES = ['mc', 'type', 'cloze_choice', 'cloze', 'sentence_listen'] as const
export type AnswerType = (typeof ANSWER_TYPES)[number]
// `mc` and `type` are the core recall modes — at least one must stay on (see
// setMode); the rest are supplemental and may all be off.
const ANSWER_TYPES_KEY = 'kana-quiz:answer-types'

export type EnabledModes = Record<AnswerType, boolean>

function defaultEnabledModes(): EnabledModes {
  return { mc: true, type: true, cloze_choice: true, cloze: true, sentence_listen: true }
}

function loadEnabledModes(): EnabledModes {
  const modes = defaultEnabledModes()
  try {
    const raw = localStorage.getItem(ANSWER_TYPES_KEY)
    if (!raw) return modes
    const parsed = JSON.parse(raw) as Partial<Record<AnswerType, unknown>>
    for (const t of ANSWER_TYPES) {
      if (typeof parsed[t] === 'boolean') modes[t] = parsed[t] as boolean
    }
  } catch {
    /* corrupt/blocked storage — fall back to all-on defaults */
  }
  // Never persist (or honor) a state with both core recall modes off.
  if (!modes.mc && !modes.type) modes.mc = true
  return modes
}

// Generic localStorage helpers for scalar/boolean settings. The answer-types
// map keeps its own bespoke loader (it merges into per-key defaults); these are
// for the simpler single-value settings (quiz length, auto-reveal) so each one
// doesn't re-implement the parse/validate/try-catch dance.
function loadSetting<T>(key: string, validate: (v: unknown) => T | null, fallback: T): T {
  try {
    const raw = localStorage.getItem(key)
    if (raw === null) return fallback
    const ok = validate(JSON.parse(raw) as unknown)
    return ok === null ? fallback : ok
  } catch {
    return fallback
  }
}

function persistSetting(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch {
    /* storage blocked — the setting still applies for this session */
  }
}

export interface WordIntro {
  kana: string
  english: string
  kanji: string | null
}

export interface NextQuestion {
  word_id: number
  direction: Direction
  prompt: string
  mode: QuestionMode
  // 'word' or 'sentence' — a sentence card renders the whole line as the
  // prompt and gets a full-width answer box.
  kind?: 'word' | 'sentence'
  choices: string[]
  correct_index: number
  introduction?: WordIntro | null
  kanji?: string | null
  failure_streak?: number
  // Backend-supplied per-question countdown budget (ms). Type-in scales with
  // expected answer length; MC + drill stay at QUESTION_DURATION_MS.
  time_limit_ms?: number
  // Populated on cloze and cloze_choice cards. `cloze_template` is a
  // Japanese sentence with the target word's surface form replaced by the
  // literal token "{blank}"; ClozeInput.vue splits on it and renders an
  // inline input, ClozeChoiceCard.vue renders a static slot above the choice
  // grid. `cloze_expected` is that surface form — relayed back on the answer
  // payload so the backend grader accepts it OR the dictionary form without a
  // second cache lookup (type-in cloze only; cloze_choice grades by index).
  cloze_template?: string | null
  cloze_expected?: string | null
  // Furigana for the cloze sentence either side of the blank — the blanked
  // word (the answer) carries no reading. Each is a list of RubySegment the
  // cloze cards render as <ruby>. Absent on payloads from older backends, in
  // which case the cards fall back to splitting `cloze_template` as plain text.
  cloze_before?: RubySegment[] | null
  cloze_after?: RubySegment[] | null
  // Listening (sentence_listen) mode. `audio_url` points at the sentence
  // TTS endpoint and is auto-played on card mount by DictationCard.
  // `expected_translation` is the reference English the grader uses
  // and the reveal shows post-answer. `sentence_japanese` is the
  // Japanese sentence text — withheld visually pre-answer, surfaced on
  // the reveal so the user can read what they just heard.
  audio_url?: string | null
  expected_translation?: string | null
  sentence_japanese?: string | null
  // Furigana for `sentence_japanese`, shown as ruby on the listening reveal.
  sentence_japanese_ruby?: RubySegment[] | null
  // New-card auto-reveal: the backend ships the cached example sentence +
  // mnemonic inline on a word's first sighting so the intro card renders it
  // without a second /sentence round-trip. Null on every other card / cache miss.
  sentence?: SentencePayload | null
}

// Mirror of backend schemas.SentenceOut — the auto-reveal payload on a
// first-sighting NextQuestion. `japanese_ruby` renders as furigana.
export interface SentencePayload {
  word_id: number
  japanese: string
  english: string
  mnemonic?: string
  japanese_ruby?: RubySegment[]
  // 'generated', or the tag of the show the line was taken from.
  source?: string
  audio_url?: string
}

// One run of a furigana-annotated sentence. `t` is the surface text; `r` is
// the hiragana reading when `t` is a kanji run, omitted for plain kana / ASCII.
// Mirrors backend schemas.RubySegment. Rendered by Furigana.vue as <ruby>.
export interface RubySegment {
  t: string
  r?: string | null
}

// Mirror of srs.LEECH_FAILURE_THRESHOLD — duplicated so the UI can label
// leeches without an extra round-trip. Keep in sync if the backend changes.
export const LEECH_FAILURE_THRESHOLD = 4

export type Maturity = 'new' | 'learning' | 'young' | 'mature' | 'mastered'

export interface AnswerResult {
  correct: boolean
  outcome: 'correct' | 'incorrect' | 'timeout' | 'gave_up'
  new_due_at: string
  interval_days: number
  ease: number
  expected?: string | null
  feedback?: string | null
  // Where the card landed after this answer, and whether that's a step up.
  // Absent on the client-synthesised results the MC fast path builds before
  // the POST resolves — the toast fills them in when the response arrives.
  maturity?: Maturity
  maturity_up?: boolean
  // A sprint-deck card that just cleared its second spaced review and left
  // the rotation for good.
  archived?: boolean
}

// Display labels for the mastery buckets. Mirrors MATURITY_ORDER in srs.py;
// the server decides which bucket, this only names it.
export const MATURITY_LABEL: Record<Maturity, string> = {
  new: 'New',
  learning: 'Learning',
  young: 'Young',
  mature: 'Mature',
  mastered: 'Mastered',
}

export const QUESTION_DURATION_MS = 5000
// Recall is meaningfully slower than recognition — give type-in twice as long.
export const TYPE_QUESTION_DURATION_MS = 10000
// Selectable round lengths (questions per round). The default preserves the
// historical 25-question round.
export const ROUND_SIZE_OPTIONS = [25, 50, 75, 100] as const
export const ROUND_SIZE_DEFAULT = 25
const ROUND_SIZE_KEY = 'kana-quiz:round-size'
// Auto-reveal the mnemonic + example sentence when a brand-new card first
// appears (a teaching moment before the quiz). Defaults on.
const AUTO_REVEAL_KEY = 'kana-quiz:auto-reveal'
const SENTENCE_AUDIO_KEY = 'kana-quiz:sentence-audio'
// How many new words a focused "Learn new cards" session introduces at once.
export const NEW_CARDS_OPTIONS = [5, 10, 15, 20] as const
export const NEW_CARDS_DEFAULT = 10
const NEW_CARDS_KEY = 'kana-quiz:new-cards-per-session'
// Correct reps (with at least one fast) needed to clear a word in the burndown.
export const DRILL_REPS_OPTIONS = [1, 2, 3] as const
const DRILL_REPS_KEY = 'kana-quiz:drill-reps'
// Whether the speed-gated burndown replay runs at all after a round.
const BURNDOWN_KEY = 'kana-quiz:burndown-enabled'
const FLASH_MS_CORRECT = 200

// ---- Flame meter (gamification) -----------------------------------------
// A "feed-the-flame" vitality meter layered on the study round. The flame IS
// the per-question timer now — there's no separate countdown bar. Each question
// the flame holds steady for a grace period, then burns down, sinking to an
// ember right around the hard auto-fail. Answering fast AND correct grows the
// flame and your combo multiplier; dawdling lets it sag and a miss guts it.
// Soft stakes — a miss / timeout relights an ember and resets your combo, but
// the round continues (no lost progress).
export const HEAT_MAX = 100
const HEAT_START = 45
// Heat the flame relights to after a burnout, and the floor the per-question
// decay sinks toward — decay alone never kills the flame, only a miss does.
const EMBER_FLOOR = 10

// Per-question timing. The flame holds steady for GRACE_MS, then burns down;
// it reaches the ember floor right at the hard auto-fail (a timeout = wrong).
// Quick recognition modes (MC / selection cloze) get the short budget; typed
// recall modes (type-in / cloze / listening) get the long one.
export const DECAY_GRACE_MS = 5000
const TIMEOUT_RECOGNITION_MS = 10000
const TIMEOUT_RECALL_MS = 20000
const RECALL_MODES: QuestionMode[] = ['type', 'cloze', 'sentence_listen']
// Hard per-question time limit (ms) before the question auto-fails.
export function timeoutMsForMode(mode: QuestionMode): number {
  return RECALL_MODES.includes(mode) ? TIMEOUT_RECALL_MS : TIMEOUT_RECOGNITION_MS
}

// "Fast" answer bar — the latency under which an answer counts as fast, driving
// the flame's fast bonus AND the burndown's speed-gated graduation. It's user-
// configurable via a speed preset (a confident recognition tap is quick; typed
// recall is inherently slower, so each preset carries a pair). The UI surfaces
// the recognition figure; the recall bar rides along a notch wider.
export type SpeedPref = 'chill' | 'standard' | 'snappy'
const SPEED_PRESETS: Record<SpeedPref, { recognitionMs: number; recallMs: number }> = {
  chill: { recognitionMs: 4000, recallMs: 7000 },
  standard: { recognitionMs: 2500, recallMs: 5000 },
  snappy: { recognitionMs: 1500, recallMs: 3500 },
}
// Display metadata for the setup screen's speed control, in order.
export const SPEED_OPTIONS: { key: SpeedPref; label: string; blurb: string }[] = [
  { key: 'chill', label: 'Chill', blurb: '4.0s' },
  { key: 'standard', label: 'Standard', blurb: '2.5s' },
  { key: 'snappy', label: 'Snappy', blurb: '1.5s' },
]
const SPEED_PREF_KEY = 'kana-quiz:speed-pref'
// Latency (ms) under which an answer counts as "fast" for the given preset.
function fastMsForMode(mode: QuestionMode, pref: SpeedPref): number {
  const preset = SPEED_PRESETS[pref] ?? SPEED_PRESETS.standard
  return RECALL_MODES.includes(mode) ? preset.recallMs : preset.recognitionMs
}

const HEAT_FAST = 26 // fast + correct
const HEAT_OK = 13 // correct but slow
const HEAT_MISS = 34 // wrong / timeout / gave-up (subtracted)
const BASE_POINTS = 10
const FAST_BONUS = 1.5
const COMBO_MULTIPLIER_MAX = 6
// Persisted personal bests, shown on the start screen across reloads.
const BEST_COMBO_KEY = 'kana-quiz:best-combo'
const BEST_SCORE_KEY = 'kana-quiz:best-score'

export type FlameEventType = 'fast' | 'ok' | 'miss' | 'burnout'
// A monotonically-keyed signal the view watches to fire one-shot
// spark / gutter animations. `id` increments per event so a repeat of the
// same `type` still triggers the watcher.
export interface FlameEvent {
  type: FlameEventType
  id: number
}

// Speed-gated mastery replay: a missed word graduates only after it's answered
// correctly DRILL_REQUIRED_REPS times, of which at least DRILL_REQUIRED_FAST_REPS
// beat the speed bar — the user has to demonstrate it repeatedly *and* quickly,
// so a single lucky or slow tap doesn't clear a genuine miss. The cap is the
// escape hatch — after this many *attempts* a plain correct graduates it
// (flagged not-fast) so a user who genuinely can't hit the bar isn't trapped.
export const DRILL_REQUIRED_REPS = 2
// One fast rep proves the user can retrieve it quickly once; that's as easily a
// lucky guess as recall. Requiring two means the speed has to reproduce, which
// is the thing that distinguishes cemented knowledge from a fresh look-up.
// Clamped against the user's rep setting below — asking for two fast reps when
// only one rep is required to clear would be unsatisfiable.
export const DRILL_REQUIRED_FAST_REPS = 2
// Raised with the fast requirement: the escape hatch has to stay reachable, and
// two fast reps take more attempts to land than one.
const DRILL_ATTEMPT_CAP = 8

// Bulk prefetch: how many upcoming questions to buffer client-side, and the
// low-water mark that triggers a background refill. A single /session/batch
// round-trip fills the buffer, so answering never waits on the network — a
// mobile blip has to outlast the whole buffer before the user feels it. Kept
// small: the batch commits new-word intros this many cards ahead (same shape as
// the old one-card prefetch, just deeper), so we don't want to run miles out.
const BATCH_SIZE = 5
const BATCH_REFILL_AT = 2

// A short tail of recently-served word_ids that stays in the batch exclude set
// even after the card has left the on-screen `current` / buffer. A just-
// answered card leaves heldIds() immediately, but its SRS reschedule (the
// fire-and-forget answer POST) may not have committed yet — so a prefetch
// refill firing right after the swap can re-pick the same (word, direction)
// while the server still sees due_at <= now, and it resurfaces a few cards
// later. Carrying this tail closes that race: the client always knows what it
// just showed, even before the server processes the answer. Sized well above
// BATCH_SIZE (so the race window is always covered) yet far below the ~10 min
// relearn step measured in cards (so a genuinely missed card can still come
// back on schedule).
const RECENT_SERVE_WINDOW = 10

// Correct drill reps used to dwell here — 1200ms for a slow re-queue, 550ms for
// a fast one — purely so the inline "Too slow!" / "Nice!" panel had time to be
// read. That made being slow cost an interruption on top of the re-queue, which
// is the thing that felt punishing. Those notes are toasts now: they outlive the
// card and read fine while the next question is already up, so every correct rep
// advances at the bare FLASH_MS_CORRECT. Wrong answers still hold (no
// auto-advance) so the reveal stays in front of the user.

// One word still being drilled to mastery. `attempts` accumulates across
// re-queues so the cap can fire. `direction` is pinned to the direction the
// word was missed in, so the quick-fire re-tests the SAME recall direction it
// re-queues — never flipping E→J / J→E between attempts. `goodReps` counts the
// correct answers so far and `fastReps` how many of those beat the speed bar; a
// word clears once it has enough of both (see _drillSubmit) — one lucky tap no
// longer masters it, and neither does a run of correct-but-laboured ones.
export interface DrillItem {
  word_id: number
  prompt: string
  attempts: number
  direction: Direction
  goodReps: number
  fastReps: number
}

// A drill word that has left the queue. `cleared` = answered correctly on its
// final attempt (mastered, possibly via the cap). `fastCleared` = met the
// fast-rep requirement rather than limping out via the attempt cap. A word the
// user never got right is force-dropped at the cap with both false, so the loop
// always terminates.
export interface DrillResult {
  word_id: number
  prompt: string
  attempts: number
  cleared: boolean
  fastCleared: boolean
  direction: Direction
}

export interface RoundAnswer {
  word_id: number
  prompt: string
  correct: boolean
  outcome: AnswerResult['outcome']
  latency_ms: number
  direction: Direction
  mode: QuestionMode
  typed?: string
  expected?: string | null
  kanji?: string | null
  failure_streak?: number
  // True when this was a brand-new card's first sighting (had an intro). A
  // focused "Learn new cards" session drills every such card in the burndown.
  was_new?: boolean
}

// ---- Continuous-match (speed round) -------------------------------------
// A timed "match the pairs" game over the recall lanes. Self-contained: it
// reuses POST /session/answer unchanged so SRS scheduling is identical to MC,
// and keeps its own state out of the round-flow reset() so the two never
// interfere.
export const MATCH_BOARD_SIZE = 6 // pairs shown at once (6 left + 6 right)
export const MATCH_GOAL = 15 // matches that win the round
export const MATCH_TIME_MS = 90_000 // round length

// Tile as served by /api/session/match-batch (mirrors schemas.MatchTile).
export interface MatchTile {
  word_id: number
  direction: Direction
  prompt: string
  answer: string
  kanji?: string | null
  failure_streak?: number
}

// One rendered half of a pairing (the prompt tile on the left, the answer tile
// on the right). A match is two opposite-side cells sharing a `pairId`.
export interface MatchCell {
  key: string
  pairId: string
  side: 'prompt' | 'answer'
  text: string
  word_id: number
  direction: Direction
  state: 'idle' | 'selected' | 'correct' | 'wrong'
  appearedAt: number
}

export interface MatchMiss {
  word_id: number
  prompt: string
}

function matchCellsForTile(tile: MatchTile, now: number): [MatchCell, MatchCell] {
  const pairId = `${tile.word_id}:${tile.direction}`
  const base = {
    pairId,
    word_id: tile.word_id,
    direction: tile.direction,
    appearedAt: now,
    state: 'idle' as const,
  }
  return [
    { ...base, key: `${pairId}:p`, side: 'prompt', text: tile.prompt },
    { ...base, key: `${pairId}:a`, side: 'answer', text: tile.answer },
  ]
}

export const useSessionStore = defineStore('session', () => {
  const current = ref<NextQuestion | null>(null)
  // Bulk-prefetch buffer: a small queue of upcoming questions pulled in one
  // /session/batch round-trip. fetchNext shifts the head so a swap after an
  // answer is instant; when it drains below BATCH_REFILL_AT we top it back up
  // in the background. `refillPromise` guards against overlapping refills.
  const prefetchBuffer = ref<NextQuestion[]>([])
  let refillPromise: Promise<void> | null = null
  // In-flight POST /session/answer promises. We don't block card swaps on
  // them (perceived latency goes from ~POST + FETCH to ~0), but we do
  // wait for the queue to drain before showing the round summary so the
  // server-side SRS state is consistent with what the user sees.
  const pendingAnswers: Promise<unknown>[] = []
  // FIFO of the last RECENT_SERVE_WINDOW word_ids put on screen. Feeds the
  // batch exclude set (see excludeIds) so a refill can't re-serve a card whose
  // answer POST is still in flight. Cleared on round start / reset so a fresh
  // round never inherits stale exclusions that could starve a small due pile.
  const recentServed: number[] = []
  function rememberServed(wordId: number) {
    recentServed.push(wordId)
    while (recentServed.length > RECENT_SERVE_WINDOW) recentServed.shift()
  }
  const deckEmpty = ref(false)
  const flash = ref<'green' | 'red' | null>(null)
  const lastOutcome = ref<AnswerResult | null>(null)
  const revealedIndex = ref<number | null>(null)
  const locked = ref(false)
  const score = ref({ correct: 0, total: 0 })
  const questionStartedAt = ref<number>(0)

  // Round state: we chunk the stream into rounds of ROUND_SIZE so the user
  // gets a breather and some feedback between bursts.
  const roundAnswers = ref<RoundAnswer[]>([])
  const roundComplete = ref(false)
  const roundStarted = ref(false)
  const roundsCompleted = ref(0)
  const streak = ref(0)
  const bestStreakThisSession = ref(0)

  // ---- Flame meter state ----
  // `heat` is the global fuel (0..HEAT_MAX). `combo` is consecutive correct
  // answers that haven't been broken by a miss OR a burnout; it drives the
  // score multiplier. `gameScore` accumulates across rounds within a session.
  const heat = ref(HEAT_START)
  const combo = ref(0)
  const gameScore = ref(0)
  const bestComboThisSession = ref(0)
  const flameEvent = ref<FlameEvent | null>(null)
  let flameEventSeq = 0
  const comboMultiplier = computed(() =>
    Math.min(COMBO_MULTIPLIER_MAX, 1 + Math.floor(combo.value / 4)),
  )
  // All-time bests (persisted) for the start screen.
  const bestComboEver = ref<number>(
    loadSetting(BEST_COMBO_KEY, (v) => (typeof v === 'number' ? v : null), 0),
  )
  const bestScoreEver = ref<number>(
    loadSetting(BEST_SCORE_KEY, (v) => (typeof v === 'number' ? v : null), 0),
  )
  // Frozen snapshot of the all-time best score at session start. Once the live
  // score overtakes the record, recordGameBests() bumps bestScoreEver to match
  // it, so bestScoreEver can no longer be used to detect "am I beating my best
  // right now" — this baseline can. The view watches `isRecordScore` to fire a
  // one-shot celebration and keep a gold marker on the score while it holds.
  const sessionStartBestScore = ref(bestScoreEver.value)
  const isRecordScore = computed(
    () => sessionStartBestScore.value > 0 && gameScore.value > sessionStartBestScore.value,
  )

  const roundAnswered = computed(() => roundAnswers.value.length)

  // Set once we see a 503 from /api/audio — kills further prefetch + UI.
  const audioDisabled = ref(false)

  // True when the current question carries an `introduction` payload that
  // hasn't been dismissed yet. While true the countdown is paused (treated
  // as locked) and the user sees the "meet this word" card instead of the
  // quiz UI. Flipped false by `dismissIntro()` from the intro-card button.
  const introOpen = ref(false)

  // Drill-the-misses mode. Independent of the SRS-bearing round flow:
  // questions come from a fixed queue (the misses), grading is local-only,
  // nothing is POSTed to /answer and nothing touches roundAnswers/score.
  const drillMode = ref(false)
  const drillQueue = ref<DrillItem[]>([])
  const drillResults = ref<DrillResult[]>([])
  // Number of DISTINCT words the burndown started with, snapshotted at startDrill
  // so the progress bar has a stable denominator (0 / 14 -> 14 / 14). A word
  // only counts once it leaves the queue (a slow re-queue is not progress).
  const drillInitialCount = ref(0)
  // Every word_id the burndown is drilling — passed to /session/drill as the
  // confuser pool so each question's distractors lean on the OTHER words the
  // user missed this round (a discrimination drill on the confusable set),
  // topped up by the adaptive pools server-side when it's thin.
  const drillPoolIds = ref<number[]>([])
  // The word currently on screen during the drill (shifted off the queue);
  // carries its running `attempts` count so a re-queue preserves it.
  const drillCurrent = ref<DrillItem | null>(null)
  // Non-blocking feedback strip. Answer verdicts, mastery promotions and the
  // drill's "too slow" nudge all go here instead of into the card flow.
  const toasts = useToastStore()

  // Set during the post-answer dwell window. The keyboard handler in
  // StudyView calls this to advance to the next question early when the
  // user has finished reading the reveal. Cleared automatically when the
  // dwell timer fires or when the user invokes it.
  const skipDwell = ref<(() => void) | null>(null)

  // True between an answered question and the next question being on
  // screen. StudyView shows a small "Loading next question…" indicator
  // when this stays true for more than a beat — without it, a stalled
  // prefetch on a flaky mobile network looks like the app is dead.
  const loadingNext = ref(false)
  // Set when the retrying fetch wrapper has exhausted its attempts.
  // StudyView surfaces a "Reconnect" button so the user can recover
  // manually instead of staring at a frozen card.
  const networkError = ref(false)

  // Which answer types the user has enabled (start-screen toggles). Persisted
  // to localStorage and relayed to /session/next as the `modes` allow-list so
  // the backend only serves enabled types. The store owns this because it's
  // what builds the fetch URL; StudyView just binds checkboxes to it.
  const enabledModes = ref<EnabledModes>(loadEnabledModes())

  // Quiz length (questions per round). `roundSize` is the user's setting;
  // `activeRoundSize` is snapshotted at round start so changing the setting
  // mid-round can't resize the live round under the user.
  const roundSize = ref<number>(
    loadSetting(
      ROUND_SIZE_KEY,
      (v) => ((ROUND_SIZE_OPTIONS as readonly number[]).includes(v as number) ? (v as number) : null),
      ROUND_SIZE_DEFAULT,
    ),
  )
  const activeRoundSize = ref<number>(roundSize.value)

  function setRoundSize(n: number) {
    if (!(ROUND_SIZE_OPTIONS as readonly number[]).includes(n)) return
    roundSize.value = n
    persistSetting(ROUND_SIZE_KEY, n)
  }

  // Auto-reveal the mnemonic + example sentence on a new card's first sighting.
  const autoRevealNewCards = ref<boolean>(
    loadSetting(AUTO_REVEAL_KEY, (v) => (typeof v === 'boolean' ? v : null), true),
  )

  function setAutoReveal(on: boolean) {
    autoRevealNewCards.value = on
    persistSetting(AUTO_REVEAL_KEY, on)
  }

  // Speak the example sentence once the answer is shown, so the word is
  // heard in context on every card and not only on the intro.
  const sentenceAudioOnReveal = ref<boolean>(
    loadSetting(SENTENCE_AUDIO_KEY, (v) => (typeof v === 'boolean' ? v : null), true),
  )

  function setSentenceAudioOnReveal(on: boolean) {
    sentenceAudioOnReveal.value = on
    persistSetting(SENTENCE_AUDIO_KEY, on)
  }

  // Speed preset — drives the "fast" bar for the flame + burndown graduation.
  const speedPreference = ref<SpeedPref>(
    loadSetting(
      SPEED_PREF_KEY,
      (v) => (v === 'chill' || v === 'standard' || v === 'snappy' ? v : null),
      'standard',
    ),
  )
  function setSpeedPreference(pref: SpeedPref) {
    speedPreference.value = pref
    persistSetting(SPEED_PREF_KEY, pref)
  }

  // Burndown: whether the replay runs, and how many good reps clear a word.
  const burndownEnabled = ref<boolean>(
    loadSetting(BURNDOWN_KEY, (v) => (typeof v === 'boolean' ? v : null), true),
  )
  function setBurndownEnabled(on: boolean) {
    burndownEnabled.value = on
    persistSetting(BURNDOWN_KEY, on)
  }
  const drillReps = ref<number>(
    loadSetting(
      DRILL_REPS_KEY,
      (v) => ((DRILL_REPS_OPTIONS as readonly number[]).includes(v as number) ? (v as number) : null),
      DRILL_REQUIRED_REPS,
    ),
  )
  function setDrillReps(n: number) {
    if (!(DRILL_REPS_OPTIONS as readonly number[]).includes(n)) return
    drillReps.value = n
    persistSetting(DRILL_REPS_KEY, n)
  }
  // Fast reps needed to clear, clamped to the total reps required. Without the
  // clamp the "1×" setting could never be satisfied: it would ask for one
  // correct rep but two fast ones, and the word would grind to the attempt cap
  // every time.
  const drillFastReps = computed(() =>
    Math.min(DRILL_REQUIRED_FAST_REPS, drillReps.value),
  )

  // How many new words a focused "Learn new cards" session pulls in.
  const newCardsPerSession = ref<number>(
    loadSetting(
      NEW_CARDS_KEY,
      (v) => ((NEW_CARDS_OPTIONS as readonly number[]).includes(v as number) ? (v as number) : null),
      NEW_CARDS_DEFAULT,
    ),
  )
  function setNewCardsPerSession(n: number) {
    if (!(NEW_CARDS_OPTIONS as readonly number[]).includes(n)) return
    newCardsPerSession.value = n
    persistSetting(NEW_CARDS_KEY, n)
  }

  // Which flavour of session is live: 'review' (due cards only) or 'new' (a
  // focused learn session that introduces + drills fresh words). Threaded to the
  // picker as the `pool` param and read by the burndown builder (a new session
  // drills every card to fluency; a review session drills only the misses).
  const sessionMode = ref<'review' | 'new'>('review')

  // Deck scope. When set, every /session/next and /session/batch call carries
  // `deck=<id>` so the picker serves that deck alone — the podcast-episode
  // study flow. Set from StudyView's route query; null is the normal
  // whole-collection session.
  const deckId = ref<number | null>(null)
  const deckName = ref('')

  function setDeck(id: number | null, name = '') {
    if (deckId.value === id) {
      deckName.value = name || deckName.value
      return
    }
    deckId.value = id
    deckName.value = name
    // A different scope means every buffered question is from the wrong
    // pool; drop the hand so the next fetch refills in scope.
    prefetchBuffer.value = []
  }

  function setMode(mode: AnswerType, on: boolean) {
    const next: EnabledModes = { ...enabledModes.value, [mode]: on }
    // Keep at least one core recall mode on. If the user turns off the last
    // one, flip the other on so we never strand recall with nothing to render.
    if (!next.mc && !next.type) {
      next[mode === 'mc' ? 'type' : 'mc'] = true
    }
    enabledModes.value = next
    try {
      localStorage.setItem(ANSWER_TYPES_KEY, JSON.stringify(next))
    } catch {
      /* storage blocked — toggles still apply for this session */
    }
  }

  function modesParam(): string {
    return ANSWER_TYPES.filter((m) => enabledModes.value[m]).join(',')
  }

  // Decode-warm the audio a question will auto-play, so swapping to it never
  // stalls on a cold fetch+decode. The worst offender on a slow connection is
  // the multi-second listening-sentence clip, which we now warm a card ahead.
  // Best-effort and silent — `prefetchAudioUrl` swallows its own errors.
  function warmQuestionAudio(q: NextQuestion | null) {
    if (!q || audioDisabled.value) return
    prefetchAudioUrl(wordAudioUrl(q.word_id))
    if (q.mode === 'sentence_listen' && q.audio_url) {
      prefetchAudioUrl(versionedAudioUrl(q.audio_url))
    }
  }

  async function prefetchUpcomingAudio() {
    if (audioDisabled.value) return
    try {
      const resp = await fetchWithRetry(`/api/session/upcoming?n=${activeRoundSize.value}`, {}, 2)
      if (!resp.ok) return
      const ids: number[] = (await resp.json()).word_ids ?? []
      // Fire-and-forget: warm the Web Audio decoded-buffer cache so
      // playAudioUrl is instant on the first hit.
      for (const id of ids) {
        prefetchAudioUrl(wordAudioUrl(id))
      }
    } catch {
      /* best-effort warmup */
    }
  }

  function nextUrl(excludeId?: number): string {
    const params = new URLSearchParams()
    if (excludeId) params.set('exclude', String(excludeId))
    params.set('modes', modesParam())
    params.set('reveal', autoRevealNewCards.value ? '1' : '0')
    params.set('pool', sessionMode.value)
    if (deckId.value != null) params.set('deck', String(deckId.value))
    return `/api/session/next?${params.toString()}`
  }

  async function fetchQuestion(excludeId?: number): Promise<NextQuestion | null> {
    const resp = await fetchWithRetry(nextUrl(excludeId))
    if (resp.status === 204) return null
    if (!resp.ok) throw new Error(`next failed: ${resp.status}`)
    return (await resp.json()) as NextQuestion
  }

  // Word_ids the client already holds — the on-screen card plus everything
  // buffered — so a refill never re-serves a word we're already about to show.
  // Note this forgets a card the instant it's answered (it leaves `current`);
  // preventing an in-flight-reschedule re-serve is excludeIds/recentServed's
  // job, not this function's.
  function heldIds(): number[] {
    const ids: number[] = []
    if (current.value) ids.push(current.value.word_id)
    for (const q of prefetchBuffer.value) ids.push(q.word_id)
    return ids
  }

  // The full batch exclude set: everything in hand plus the recently-served
  // tail. Using this (rather than bare heldIds) is what keeps a refill from
  // re-serving a card whose reschedule hasn't landed yet — see recentServed.
  function excludeIds(): number[] {
    return Array.from(new Set([...heldIds(), ...recentServed]))
  }

  function batchUrl(excludeIds: number[], n: number): string {
    const params = new URLSearchParams()
    params.set('n', String(n))
    if (excludeIds.length) params.set('exclude', excludeIds.join(','))
    params.set('modes', modesParam())
    params.set('reveal', autoRevealNewCards.value ? '1' : '0')
    params.set('pool', sessionMode.value)
    if (deckId.value != null) params.set('deck', String(deckId.value))
    return `/api/session/batch?${params.toString()}`
  }

  // Top the buffer back up to BATCH_SIZE in the background. Best-effort and
  // single-flight: if it fails or the round moves on, fetchNext falls back to a
  // cold fetch. Appends are re-checked against the *current* hand so a slow
  // refill landing after the user has advanced can't introduce a duplicate.
  function refillBuffer() {
    if (refillPromise) return
    const need = BATCH_SIZE - prefetchBuffer.value.length
    if (need <= 0) return
    refillPromise = (async () => {
      try {
        const resp = await fetchWithRetry(batchUrl(excludeIds(), need), {}, 2)
        if (resp.status === 204 || !resp.ok) return
        const batch = (await resp.json()) as { questions: NextQuestion[] }
        // A refill that lands after the round ended / entered the drill must not
        // seed the buffer for a session that's over.
        if (!roundStarted.value || roundComplete.value || drillMode.value) return
        const held = new Set(excludeIds())
        for (const q of batch.questions) {
          if (held.has(q.word_id)) continue
          prefetchBuffer.value.push(q)
          held.add(q.word_id)
        }
        // Warm the next card's audio while the current one is on screen.
        warmQuestionAudio(prefetchBuffer.value[0] ?? null)
      } catch {
        /* refill is best-effort */
      } finally {
        refillPromise = null
      }
    })()
  }

  async function fetchNext() {
    if (roundComplete.value || !roundStarted.value) return
    flash.value = null
    lastOutcome.value = null
    loadingNext.value = true
    networkError.value = false
    // Keep `locked` true across any await below: if we cleared it now and the
    // network blipped, the countdown would resume against the *old* question's
    // startedAt and instantly emit a spurious timeout — locking us into a
    // "every question times out" loop until the fetch finally lands.

    // Buffer empty but a refill is in flight: race it against a short deadline.
    // Holding for it saves a request when it lands quickly, but on a stalled
    // network we'd rather fall through to a fresh cold fetch (own retry budget)
    // than wait out its full timeout.
    if (prefetchBuffer.value.length === 0 && refillPromise) {
      const deadline = new Promise((r) => setTimeout(r, 1500))
      try {
        await Promise.race([refillPromise, deadline])
      } catch { /* ignore */ }
    }
    try {
      if (prefetchBuffer.value.length === 0) {
        // Cold: pull a fresh batch synchronously before we can show anything.
        const resp = await fetchWithRetry(batchUrl(excludeIds(), BATCH_SIZE))
        if (resp.status !== 204 && resp.ok) {
          const batch = (await resp.json()) as { questions: NextQuestion[] }
          const held = new Set(excludeIds())
          for (const q of batch.questions) {
            if (held.has(q.word_id)) continue
            prefetchBuffer.value.push(q)
            held.add(q.word_id)
          }
        }
        // Degenerate deck (only the just-answered card is due): the batch
        // excludes it and comes back empty, so re-serve the soonest card the
        // old single-fetch way rather than falsely reporting an empty deck.
        if (prefetchBuffer.value.length === 0) {
          const q = await fetchQuestion()
          if (q !== null) prefetchBuffer.value.push(q)
        }
      }
      if (prefetchBuffer.value.length === 0) {
        // Nothing left to serve. If we've already worked at least one card this
        // session, the deck simply drained (a review round cleared its due pile,
        // or a new-cards session ran out of unseen words) — wrap up the round
        // (burndown or summary) instead of falsely flashing the empty-deck panel.
        if (roundAnswers.value.length > 0) {
          locked.value = false
          revealedIndex.value = null
          loadingNext.value = false
          await concludeRound()
          return
        }
        current.value = null
        deckEmpty.value = true
        locked.value = false
        revealedIndex.value = null
        loadingNext.value = false
        return
      }
      current.value = prefetchBuffer.value.shift()!
      rememberServed(current.value.word_id)
      deckEmpty.value = false
    } catch {
      // All retries exhausted — surface a recoverable error rather than
      // wedging the round. The user can hit "Reconnect" to retry.
      networkError.value = true
      loadingNext.value = false
      return
    }
    // New question is on screen — release the lock so the (now reset)
    // countdown can begin against the new word's startedAt.
    locked.value = false
    revealedIndex.value = null
    loadingNext.value = false
    // Hold the timer if this is a first-sighting word — the intro card needs
    // user dismissal before the quiz starts. Otherwise begin counting now.
    if (current.value?.introduction) {
      introOpen.value = true
    } else {
      introOpen.value = false
      questionStartedAt.value = performance.now()
    }
    // Warm the now-next card and refill the buffer if it's running low.
    warmQuestionAudio(prefetchBuffer.value[0] ?? null)
    if (prefetchBuffer.value.length <= BATCH_REFILL_AT) refillBuffer()
  }

  async function recoverFromNetworkError() {
    if (!networkError.value) return
    await fetchNext()
  }

  function dismissIntro() {
    if (!introOpen.value) return
    introOpen.value = false
    questionStartedAt.value = performance.now()
  }

  // ---- Flame meter logic ----
  function emitFlame(type: FlameEventType) {
    flameEventSeq += 1
    flameEvent.value = { type, id: flameEventSeq }
  }

  // The flame now persists ACROSS rounds within a session: a hot streak carries
  // its heat AND combo (and therefore the score multiplier) straight into the
  // next round, so a strong run keeps building a taller, whiter blaze instead of
  // resetting to a cold ember every 25 questions — that reset was why the
  // build-up "didn't work" between rounds. Heat/combo are cleared only by
  // reset() (a brand-new session). Here we just drop the one-shot spark/puff
  // signal so the previous round's last animation can't replay on the new card.
  function startRoundFlame() {
    flameEvent.value = null
  }

  function recordGameBests() {
    if (combo.value > bestComboThisSession.value) bestComboThisSession.value = combo.value
    if (bestComboThisSession.value > bestComboEver.value) {
      bestComboEver.value = bestComboThisSession.value
      persistSetting(BEST_COMBO_KEY, bestComboEver.value)
    }
    if (gameScore.value > bestScoreEver.value) {
      bestScoreEver.value = gameScore.value
      persistSetting(BEST_SCORE_KEY, bestScoreEver.value)
    }
  }

  // Apply one answer's outcome to the flame, combo and score. `fastMs` is the
  // per-mode latency bar for the speed bonus. Called from both the main answer
  // path (_send) and the drill (_drillSubmit).
  function applyFlameOutcome(correct: boolean, latencyMs: number, fastMs: number) {
    const fast = correct && latencyMs <= fastMs
    if (correct) {
      heat.value = Math.min(HEAT_MAX, heat.value + (fast ? HEAT_FAST : HEAT_OK))
      combo.value += 1
      gameScore.value += Math.round(BASE_POINTS * comboMultiplier.value * (fast ? FAST_BONUS : 1))
      recordGameBests()
      emitFlame(fast ? 'fast' : 'ok')
    } else {
      // A miss costs heat and breaks the combo. If it drains the flame, relight
      // to an ember (soft stakes — the round continues).
      heat.value = Math.max(0, heat.value - HEAT_MISS)
      combo.value = 0
      if (heat.value <= 0) {
        heat.value = EMBER_FLOOR
        emitFlame('burnout')
      } else {
        emitFlame('miss')
      }
    }
  }

  // Per-question decay driven by the view's rAF loop while a question is live.
  // `elapsedMs` is time since the question appeared; `timeoutMs` is its hard
  // auto-fail budget. The flame holds for a grace period, then burns down at a
  // constant rate that would sink a full flame from max to ember over the
  // remaining window — so a roaring flame embers right at the auto-fail and a
  // lower one embers sooner. Decay never kills the flame (it bottoms out at the
  // ember floor); only a real miss / timeout breaks the combo.
  function decayFlame(dtMs: number, elapsedMs: number, timeoutMs: number) {
    if (elapsedMs <= DECAY_GRACE_MS) return
    if (heat.value <= EMBER_FLOOR) return
    const span = Math.max(1, timeoutMs - DECAY_GRACE_MS)
    const rate = (HEAT_MAX - EMBER_FLOOR) / span
    heat.value = Math.max(EMBER_FLOOR, heat.value - dtMs * rate)
  }

  async function _send(body: Record<string, unknown>, typed?: string) {
    if (!current.value || locked.value) return
    locked.value = true
    const q = current.value
    const latency = Math.round(performance.now() - questionStartedAt.value)

    // Fire the answer POST without awaiting it. We track the promise so
    // round-end can drain the queue before showing the summary. Retries
    // matter here: a dropped POST silently loses an SRS update and the
    // user re-sees the word later thinking it never moved.
    const post = fetchWithRetry('/api/session/answer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        word_id: q.word_id,
        direction: q.direction,
        latency_ms: latency,
        ...body,
      }),
    }).then(async (resp) => {
      if (!resp.ok) throw new Error(`answer failed: ${resp.status}`)
      return (await resp.json()) as AnswerResult
    })
    pendingAnswers.push(post)
    void post.catch(() => {}).finally(() => {
      const i = pendingAnswers.indexOf(post)
      if (i >= 0) pendingAnswers.splice(i, 1)
    })

    // Multiple-choice grading is trivially client-side; typed grading is
    // server-side fuzzy matching, so for type-mode we have to await the
    // POST to know correct/wrong + the expected reveal text. Selection cloze
    // is index-based too, so it takes the same instant client-side path.
    let result: AnswerResult
    if (q.mode === 'mc' || q.mode === 'cloze_choice') {
      const chosen = body.chosen_index as number | null
      const correctIdx = body.correct_index as number | null
      const timedOut = body.timed_out as boolean
      const gaveUp = body.gave_up === true
      const correct = !timedOut && !gaveUp && chosen !== null && chosen === correctIdx
      const outcome: AnswerResult['outcome'] = gaveUp
        ? 'gave_up'
        : timedOut
          ? 'timeout'
          : correct
            ? 'correct'
            : 'incorrect'
      result = {
        correct,
        outcome,
        new_due_at: '',
        interval_days: 0,
        ease: 0,
        expected: null,
        feedback: null,
      }
    } else {
      try {
        result = await post
      } catch {
        // Network blip — treat as wrong so the user isn't silently stuck.
        result = {
          correct: false, outcome: 'incorrect',
          new_due_at: '', interval_days: 0, ease: 0, expected: null, feedback: null,
        }
      }
    }

    lastOutcome.value = result
    flash.value = result.correct ? 'green' : 'red'
    score.value.total += 1
    if (result.correct) {
      score.value.correct += 1
      streak.value += 1
      if (streak.value > bestStreakThisSession.value) {
        bestStreakThisSession.value = streak.value
      }
    } else {
      streak.value = 0
    }

    // Feed the flame: grows on a fast/correct answer, guts on a miss. The fast
    // bar scales per-mode (recognition < 2s, recall < 5s).
    applyFlameOutcome(result.correct, latency, fastMsForMode(q.mode, speedPreference.value))

    // Verdict toast. MC and cloze_choice are graded client-side and don't await
    // the POST, so the mastery bucket isn't known yet — show the verdict now
    // (feedback has to be immediate to feel connected to the tap) and fold the
    // level in when the response lands. `toastAnswer` handles both by taking
    // whatever it has; the async patch is a no-op once the toast has expired.
    const toastId = toastAnswer(result)
    if (result.maturity === undefined) {
      void post
        .then((r) => applyMaturityToast(toastId, r))
        .catch(() => {
          /* Network trouble already surfaces via networkError; nothing to add. */
        })
    }

    // The streak reported by the server is *pre-answer*, so add one if the
    // user just got it wrong (matches what the next /next call would return).
    const newStreak = result.correct ? 0 : (q.failure_streak ?? 0) + 1
    roundAnswers.value.push({
      word_id: q.word_id,
      prompt: q.prompt,
      correct: result.correct,
      outcome: result.outcome,
      latency_ms: latency,
      direction: q.direction,
      mode: q.mode,
      typed,
      expected: result.expected,
      kanji: q.kanji ?? null,
      failure_streak: newStreak,
      was_new: !!q.introduction,
    })

    // Auto-advance only when the answer was correct AND there's no
    // feedback to read. The "accept" grader verdict comes back as
    // correct=true with feedback populated (the user typed a related
    // but distinct word — e.g. 役立つ vs 役に立つ — and the explanation
    // names both forms); flashing past that defeats the point. Any
    // wrong / timeout / gave_up answer also waits, since the reveal
    // carries the mnemonic + example sentence and the feedback (when
    // Gemini supplied it) belongs in front of the user's eyes.
    const hasFeedback = !!result.feedback
    await new Promise<void>((resolve) => {
      if (result.correct && !hasFeedback) {
        const timer = setTimeout(() => {
          skipDwell.value = null
          resolve()
        }, FLASH_MS_CORRECT)
        skipDwell.value = () => {
          clearTimeout(timer)
          skipDwell.value = null
          resolve()
        }
      } else {
        skipDwell.value = () => {
          skipDwell.value = null
          resolve()
        }
      }
    })

    if (roundAnswers.value.length >= activeRoundSize.value) {
      await concludeRound()
      return
    }
    await fetchNext()
  }

  // Wrap up the round: drain in-flight answer POSTs, then either launch the
  // speed-gated burndown or flip straight to the summary. Called both when the
  // round hits its length and when a session drains early (deck ran out of due
  // reviews / unseen new words mid-round).
  async function concludeRound() {
    if (pendingAnswers.length > 0) {
      // Drain so the server's SRS state matches what we're about to show.
      try {
        await Promise.all(pendingAnswers.map((p) => p.catch(() => {})))
      } catch { /* ignore */ }
    }
    current.value = null
    prefetchBuffer.value = []
    // Burndown drills the round's misses (review) or every card (new session).
    // Skip it entirely when the user has burndown off or there's nothing to
    // drill — either way, straight to the summary.
    if (burndownEnabled.value && buildDrillItems().length > 0) {
      await startDrill()
    } else {
      roundComplete.value = true
    }
  }

  // Verdict + mastery level as a single discreet toast. Deliberately terse: it
  // fires on every answer, so anything longer than a couple of words would read
  // as noise by the tenth card. The level is the sub-line because it's the part
  // worth glancing at, not the part worth reading.
  function toastAnswer(result: AnswerResult): number {
    const level = result.maturity ? MATURITY_LABEL[result.maturity] : null
    if (!result.correct) {
      const missed =
        result.outcome === 'timeout'
          ? { tone: 'warn' as const, icon: '⏱', main: "Time's up" }
          : result.outcome === 'gave_up'
            ? { tone: 'info' as const, icon: '↷', main: 'Skipped' }
            : { tone: 'error' as const, icon: '✕', main: 'Missed' }
      return toasts.push({ ...missed, sub: level })
    }
    // A sprint card that just cleared outranks a level-up: the card is done
    // for good, and that is the news.
    if (result.archived) {
      return toasts.push({
        tone: 'success',
        icon: '🏁',
        main: 'Cleared',
        sub: 'Archived from this deck',
        sparkle: true,
      })
    }
    // A promotion leads with the new level and earns the sparkle; an ordinary
    // correct answer just confirms and shows where the card sits.
    return toasts.push({
      tone: 'success',
      icon: result.maturity_up ? '✨' : '✓',
      main: result.maturity_up && level ? level : 'Correct',
      sub: result.maturity_up ? 'Levelled up' : level,
      sparkle: !!result.maturity_up,
    })
  }

  // Late-arriving level for the client-graded fast path (see the call site).
  function applyMaturityToast(id: number, r: AnswerResult | undefined): void {
    if (!r?.maturity) return
    const level = MATURITY_LABEL[r.maturity]
    if (r.correct && r.archived) {
      toasts.update(id, {
        icon: '🏁',
        main: 'Cleared',
        sub: 'Archived from this deck',
        sparkle: true,
      })
    } else if (r.correct && r.maturity_up) {
      toasts.update(id, { icon: '✨', main: level, sub: 'Levelled up', sparkle: true })
    } else {
      toasts.update(id, { sub: level })
    }
  }

  async function submit(chosen: number | null, timedOut: boolean) {
    if (!current.value) return
    if (drillMode.value) {
      await _drillSubmit(chosen, timedOut)
      return
    }
    revealedIndex.value = chosen
    await _send({
      chosen_index: chosen,
      correct_index: current.value.correct_index,
      timed_out: timedOut,
      // Relay the mode so the backend records the answer on the right task
      // lane (mc -> recall, cloze_choice -> the selection cloze lane).
      mode: current.value.mode,
    })
  }

  async function submitTyped(value: string) {
    // Relay mode + cloze_expected back so the backend grader applies
    // the right candidate set without a second sentence_cache lookup.
    // The values come straight from the question payload we got from
    // /session/next; the trust boundary is "us to us" so we just ship
    // them. Older clients omit both and get default type-in grading.
    const q = current.value
    await _send(
      {
        chosen_index: null,
        correct_index: null,
        timed_out: false,
        typed_answer: value,
        mode: q?.mode,
        cloze_expected: q?.mode === 'cloze' ? q.cloze_expected : null,
      },
      value,
    )
  }

  async function giveUp() {
    if (!current.value || locked.value) return
    if (drillMode.value) {
      // Drill mode is local-only — treat give-up as a wrong drill answer
      // so the reveal flow runs.
      await _drillSubmit(null, false)
      return
    }
    await _send({
      chosen_index: null,
      correct_index: current.value.correct_index,
      timed_out: false,
      typed_answer: null,
      gave_up: true,
      // Route the give-up to the card's own task lane. Without this, giving up
      // on a cloze / cloze_choice / listening card would land on the base
      // recall lane instead of the supplemental one the user was actually on.
      mode: current.value.mode,
    })
  }

  async function ignoreCurrent() {
    // Fire-and-forget the ignore POST and immediately advance — the user
    // doesn't care whether the row update has landed; the next picker call
    // will notice and skip the word regardless.
    if (!current.value) return
    if (drillMode.value) return
    const id = current.value.word_id
    void fetchWithRetry(`/api/words/${id}/ignore`, { method: 'POST' }, 2).catch(() => {})
    // Drop any buffered question that points at the same word so the ignored
    // word can't resurface from the prefetch queue.
    prefetchBuffer.value = prefetchBuffer.value.filter((q) => q.word_id !== id)
    // Also drop it from this round's answers so a missed-then-ignored word can't
    // seed the burndown (startDrill builds the quick-fire queue from the round's
    // misses). The backend drill endpoint 404s ignored words too, as a backstop.
    roundAnswers.value = roundAnswers.value.filter((a) => a.word_id !== id)
    if (locked.value) {
      // Post-answer: the review row is already recorded. Resolve the dwell
      // promise the same way the Continue button does so round-size and
      // round-complete bookkeeping stay intact.
      skipDwell.value?.()
      return
    }
    // Pre-answer: skip the answer pipeline entirely — no review row, no SRS update.
    flash.value = null
    lastOutcome.value = null
    await fetchNext()
  }

  async function _fetchDrillQuestion(
    wordId: number,
    direction: Direction,
  ): Promise<NextQuestion | null> {
    // Feed the OTHER misses as the confuser pool (server tops up if it's thin).
    const pool = drillPoolIds.value.filter((id) => id !== wordId)
    const poolParam = pool.length ? `&pool_ids=${pool.join(',')}` : ''
    try {
      const resp = await fetchWithRetry(
        `/api/session/drill?word_id=${wordId}&direction=${direction}${poolParam}`,
      )
      if (!resp.ok) return null
      return (await resp.json()) as NextQuestion
    } catch {
      return null
    }
  }

  async function _advanceDrill() {
    // Stay `locked` across the fetch await below. If we unlocked now, the rAF
    // auto-fail loop (which only checks `!locked`) would see the *previous*
    // question still on screen with a stale `questionStartedAt` and fire a
    // spurious timeout mid-fetch — re-entering _drillSubmit and shifting a
    // second item off the queue, so the burndown drains twice as fast and ends
    // early (worst on slow cards, whose stale elapsed already exceeds the 10s
    // budget). We only unlock once the next question + a fresh start time are
    // in place. (Same "keep locked across awaits" rule as fetchNext.)
    locked.value = true
    revealedIndex.value = null
    flash.value = null
    lastOutcome.value = null
    // Deliberately NOT clearing toasts here: this runs on every advance, and
    // the whole point of moving the "too slow" nudge out of the card flow is
    // that it survives the next question coming up. It expires on its own.
    if (drillQueue.value.length === 0) {
      drillMode.value = false
      drillCurrent.value = null
      current.value = null
      locked.value = false
      // Every miss cleared — fall through to the round summary.
      roundComplete.value = true
      return
    }
    const item = drillQueue.value.shift()!
    const q = await _fetchDrillQuestion(item.word_id, item.direction)
    if (q === null) {
      // Word vanished or backend hiccup — skip it and keep going.
      await _advanceDrill()
      return
    }
    drillCurrent.value = item
    current.value = q
    // Set the clock, THEN unlock — no await between, so the timeout loop never
    // sees an unlocked question with a stale start time.
    questionStartedAt.value = performance.now()
    locked.value = false
  }

  // Fisher-Yates, in place. Used on the initial drill queue so a burndown isn't
  // replayed in the order the misses happened — with a short queue that order is
  // still fresh in the user's head, and answering in it tests sequence recall
  // rather than the word.
  function shuffleInPlace<T>(xs: T[]): T[] {
    for (let i = xs.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1))
      ;[xs[i], xs[j]] = [xs[j]!, xs[i]!]
    }
    return xs
  }

  // Put a word back for another rep at a random position rather than the back.
  //
  // Always-to-the-back turned a small queue into a strict rotation: with three
  // words left it's A-B-C-A-B-C forever, and the user starts riding the rhythm
  // instead of recalling. A random slot keeps the gap unpredictable, so a fast
  // answer has to come from actually knowing the word.
  //
  // Never index 0 when something else is queued: that would ask the same word
  // twice in a row, and the interleaving gap — recalling something else before
  // coming back — is what makes the second rep worth anything.
  function requeueDrillItem(item: DrillItem) {
    const q = drillQueue.value
    const idx = q.length === 0 ? 0 : 1 + Math.floor(Math.random() * q.length)
    q.splice(idx, 0, item)
  }

  async function _drillSubmit(chosen: number | null, timedOut: boolean) {
    if (!current.value || locked.value) return
    locked.value = true
    revealedIndex.value = chosen
    const q = current.value
    const item = drillCurrent.value
    const latency = Math.round(performance.now() - questionStartedAt.value)
    const correct = !timedOut && chosen !== null && chosen === q.correct_index
    const fastMs = fastMsForMode(q.mode, speedPreference.value)
    const fast = correct && latency <= fastMs
    const attempts = (item?.attempts ?? 0) + 1
    const prompt = item?.prompt ?? q.prompt
    const direction = item?.direction ?? q.direction
    const goodReps = (item?.goodReps ?? 0) + (correct ? 1 : 0)
    const fastReps = (item?.fastReps ?? 0) + (fast ? 1 : 0)

    const result: AnswerResult = {
      correct,
      outcome: timedOut ? 'timeout' : correct ? 'correct' : 'incorrect',
      new_due_at: '',
      interval_days: 0,
      ease: 0,
      expected: null,
      feedback: null,
    }
    lastOutcome.value = result
    flash.value = correct ? 'green' : 'red'
    // The drill feeds the flame too — keeps the meter alive through the
    // quick-fire phase and rewards clearing embers fast.
    applyFlameOutcome(correct, latency, fastMs)

    // A word leaves the queue when it's MASTERED (>= DRILL_REQUIRED_REPS correct
    // reps, of which >= drillFastReps beat the speed bar) OR it hits the attempt
    // cap — at the cap it's force-dropped whatever the answer, so a word the user
    // can't get right can't loop forever. Anything else re-queues, carrying its
    // rep tally.
    const reachedCap = attempts >= DRILL_ATTEMPT_CAP
    const enoughFast = fastReps >= drillFastReps.value
    const mastered = goodReps >= drillReps.value && enoughFast
    const cleared = mastered || (correct && reachedCap)
    const leave = mastered || reachedCap
    if (leave) {
      drillResults.value.push({
        word_id: q.word_id, prompt, attempts, cleared, fastCleared: enoughFast, direction,
      })
    } else {
      requeueDrillItem({ word_id: q.word_id, prompt, attempts, direction, goodReps, fastReps })
      // Nudge toward whatever's still missing. A fast rep earns a green "Nice";
      // a slow one gets the amber "Too slow". (Only on a re-queue — a
      // mastered/capped clear just advances with the success flash.)
      if (correct) {
        const fastLeft = Math.max(0, drillFastReps.value - fastReps)
        toasts.push(
          fast
            ? {
                tone: 'success',
                icon: '✨',
                main: 'Nice!',
                sub: fastLeft > 1 ? `${fastLeft} more fast` : 'Once more to lock it in',
              }
            : { tone: 'warn', icon: '⏱', main: 'Too slow', sub: 'Re-queued' },
        )
      }
    }

    // Auto-advance every *correct* drill rep so the quick-fire stays in flow —
    // slow or not, it advances at the same brisk pace and the toast carries the
    // nudge. Only a genuine miss (wrong / timeout / gave-up) holds, keeping the
    // answer reveal in front of the user.
    const autoAdvanceMs = correct ? FLASH_MS_CORRECT : null
    await new Promise<void>((resolve) => {
      if (autoAdvanceMs !== null) {
        const timer = setTimeout(() => {
          skipDwell.value = null
          resolve()
        }, autoAdvanceMs)
        skipDwell.value = () => {
          clearTimeout(timer)
          skipDwell.value = null
          resolve()
        }
      } else {
        skipDwell.value = () => {
          skipDwell.value = null
          resolve()
        }
      }
    })
    await _advanceDrill()
  }

  // The words the burndown will drill, deduped by word in first-seen order.
  // Review sessions drill only the misses; a focused new-cards session drills
  // EVERY card (the point is to hammer the fresh words to fluency). Each item is
  // pinned to the direction it was answered in — the quick-fire re-tests that
  // same recall direction instead of coin-flipping E→J / J→E on every re-fetch.
  function buildDrillItems(): DrillItem[] {
    const seen = new Set<number>()
    const items: DrillItem[] = []
    for (const a of roundAnswers.value) {
      const include = sessionMode.value === 'new' ? true : !a.correct
      if (!include || seen.has(a.word_id)) continue
      seen.add(a.word_id)
      items.push({
        word_id: a.word_id, prompt: a.prompt, attempts: 0,
        direction: a.direction, goodReps: 0, fastReps: 0,
      })
    }
    return items
  }
  // How many words the burndown would drill right now (drives the summary's
  // "run it again" affordance).
  const drillPendingCount = computed(() => buildDrillItems().length)

  async function startDrill() {
    const items = buildDrillItems()
    if (items.length === 0) {
      roundComplete.value = true
      return
    }
    // Shuffled here rather than in buildDrillItems(): that one also backs the
    // `drillPendingCount` computed, and a computed that reorders its result on
    // every read is a trap waiting to be stepped in.
    drillQueue.value = shuffleInPlace(items)
    drillResults.value = []
    drillInitialCount.value = items.length
    drillPoolIds.value = items.map((i) => i.word_id)
    drillCurrent.value = null
    toasts.clear()
    drillMode.value = true
    roundComplete.value = false
    prefetchBuffer.value = []
    await _advanceDrill()
  }

  // Bail out of the quick-fire replay straight to the summary (the "Skip"
  // affordance). Misses are already SRS-recorded by the round, so skipping only
  // forgoes the extra fluency reps.
  function skipDrill() {
    drillMode.value = false
    drillCurrent.value = null
    drillQueue.value = []
    drillPoolIds.value = []
    toasts.clear()
    current.value = null
    locked.value = false
    roundComplete.value = true
  }

  function startRound(mode: 'review' | 'new' = 'review') {
    sessionMode.value = mode
    // Snapshot the length so a mid-round settings change can't resize it. A
    // review round runs the user's quiz length; a new-cards session runs the
    // (smaller) new-cards-per-session count.
    activeRoundSize.value = mode === 'new' ? newCardsPerSession.value : roundSize.value
    roundAnswers.value = []
    roundComplete.value = false
    roundStarted.value = true
    current.value = null
    prefetchBuffer.value = []
    recentServed.length = 0
    startRoundFlame()
    void prefetchUpcomingAudio()
    void fetchNext()
  }

  // Kick off a focused "Learn new cards" session (intro → quiz → drill-all).
  function startNewCards() {
    startRound('new')
  }

  function startNextRound() {
    roundsCompleted.value += 1
    startRound(sessionMode.value)
  }

  // Leave the summary and return to the pre-round setup screen (clean flow so
  // the user can switch between reviews / new / settings between rounds).
  function backToSetup() {
    roundComplete.value = false
    roundStarted.value = false
    drillMode.value = false
    current.value = null
  }

  async function probeDeck() {
    // "No words imported yet" detection for the setup screen. Uses the deck
    // summary (a pure read) rather than /session/next: the picker MUTATES — in
    // mixed mode it can introduce a brand-new word as a side effect — and merely
    // loading the setup screen must never silently pull a new card into
    // rotation now that new words are introduced only via the explicit button.
    try {
      const resp = await fetchWithRetry('/api/decks', {}, 2)
      if (!resp.ok) return
      const summary = (await resp.json()) as Array<{ word_count?: number }>
      deckEmpty.value = summary.reduce((a, d) => a + (d.word_count ?? 0), 0) === 0
    } catch {
      // If we can't even probe, leave deckEmpty as-is and let the user try.
    }
  }

  // ---- Continuous-match slice -------------------------------------------
  // Deliberately NOT cleared by reset() so the speed round and the study round
  // can't stomp each other's state. matchReset() owns its lifecycle.
  const matchActive = ref(false)
  const matchComplete = ref(false)
  const matchPrompts = ref<MatchCell[]>([])
  const matchAnswers = ref<MatchCell[]>([])
  const matchBuffer = ref<MatchTile[]>([])
  const matchScore = ref(0)
  const matchMatched = ref(0)
  const matchStreak = ref(0)
  const matchBestStreak = ref(0)
  const matchMisses = ref<MatchMiss[]>([])
  const matchSelectedKey = ref<string | null>(null)
  // Per-pair mismatch bookkeeping. We forgive the first mis-tap of a tile and
  // only record an `incorrect` review on the second, so a fat-finger slip
  // doesn't reset a known card's SRS state.
  const matchMismatch = new Map<string, number>()
  const matchRecordedIncorrect = new Set<string>()
  let matchRefilling = false

  function matchFindCell(key: string): MatchCell | undefined {
    return (
      matchPrompts.value.find((c) => c.key === key) ||
      matchAnswers.value.find((c) => c.key === key)
    )
  }

  function matchActiveWordIds(): number[] {
    return [
      ...matchPrompts.value.map((c) => c.word_id),
      ...matchBuffer.value.map((t) => t.word_id),
    ]
  }

  async function fetchMatchBatch(n: number, excludeIds: number[]): Promise<MatchTile[]> {
    const params = new URLSearchParams()
    params.set('n', String(n))
    if (excludeIds.length) params.set('exclude_ids', excludeIds.join(','))
    try {
      const resp = await fetchWithRetry(`/api/session/match-batch?${params.toString()}`, {}, 3)
      if (!resp.ok) return []
      return ((await resp.json()).tiles ?? []) as MatchTile[]
    } catch {
      return []
    }
  }

  async function refillMatchBuffer() {
    if (matchRefilling) return
    matchRefilling = true
    try {
      const have = matchActiveWordIds()
      const tiles = await fetchMatchBatch(MATCH_BOARD_SIZE * 2, have)
      const seen = new Set(have)
      for (const t of tiles) {
        if (!seen.has(t.word_id)) {
          matchBuffer.value.push(t)
          seen.add(t.word_id)
        }
      }
    } finally {
      matchRefilling = false
    }
  }

  function addMatchPairFromBuffer(): boolean {
    const tile = matchBuffer.value.shift()
    if (matchBuffer.value.length < MATCH_BOARD_SIZE) void refillMatchBuffer()
    if (!tile) return false
    const [p, a] = matchCellsForTile(tile, performance.now())
    matchPrompts.value.push(p)
    // Shuffle the answer column so row N never lines up with its prompt.
    const idx = Math.floor(Math.random() * (matchAnswers.value.length + 1))
    matchAnswers.value.splice(idx, 0, a)
    return true
  }

  function postMatchReview(
    wordId: number,
    direction: Direction,
    correct: boolean,
    latencyMs: number | null,
  ) {
    // Reuse the unchanged answer path: an MC-shaped payload so the backend
    // advances SRS exactly as a real multiple-choice answer would.
    const post = fetchWithRetry('/api/session/answer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        word_id: wordId,
        direction,
        mode: 'mc',
        chosen_index: correct ? 0 : 1,
        correct_index: 0,
        timed_out: false,
        latency_ms: latencyMs,
      }),
    }).then((r) => {
      if (!r.ok) throw new Error(`answer failed: ${r.status}`)
      return r.json()
    })
    pendingAnswers.push(post)
    void post.catch(() => {}).finally(() => {
      const i = pendingAnswers.indexOf(post)
      if (i >= 0) pendingAnswers.splice(i, 1)
    })
  }

  function addMatchMiss(cell: MatchCell) {
    if (matchMisses.value.some((m) => m.word_id === cell.word_id)) return
    matchMisses.value.push({ word_id: cell.word_id, prompt: cell.text })
  }

  function removeMatchPair(pairId: string) {
    matchPrompts.value = matchPrompts.value.filter((c) => c.pairId !== pairId)
    matchAnswers.value = matchAnswers.value.filter((c) => c.pairId !== pairId)
    if (!matchActive.value) return
    const added = addMatchPairFromBuffer()
    // Out of cards and the board has drained — the player cleared the pool.
    if (!added && matchPrompts.value.length === 0) void endMatch()
  }

  function evaluateMatch(a: MatchCell, b: MatchCell) {
    const prompt = a.side === 'prompt' ? a : b
    const answer = a.side === 'answer' ? a : b
    if (prompt.pairId === answer.pairId) {
      prompt.state = 'correct'
      answer.state = 'correct'
      const latency = Math.max(
        0,
        Math.min(Math.round(performance.now() - prompt.appearedAt), QUESTION_DURATION_MS),
      )
      postMatchReview(prompt.word_id, prompt.direction, true, latency)
      matchMatched.value += 1
      matchStreak.value += 1
      if (matchStreak.value > matchBestStreak.value) matchBestStreak.value = matchStreak.value
      // Base 10 points + a small escalating combo bonus (capped).
      matchScore.value += 10 + Math.min(matchStreak.value - 1, 10) * 2
      const reachedGoal = matchMatched.value >= MATCH_GOAL
      setTimeout(() => removeMatchPair(prompt.pairId), 220)
      if (reachedGoal) void endMatch()
    } else {
      prompt.state = 'wrong'
      answer.state = 'wrong'
      matchStreak.value = 0
      const count = (matchMismatch.get(prompt.pairId) ?? 0) + 1
      matchMismatch.set(prompt.pairId, count)
      // Forgive the first slip; record an incorrect review on the second.
      if (count >= 2 && !matchRecordedIncorrect.has(prompt.pairId)) {
        matchRecordedIncorrect.add(prompt.pairId)
        postMatchReview(prompt.word_id, prompt.direction, false, null)
        addMatchMiss(prompt)
      }
      const pk = prompt.key
      const ak = answer.key
      setTimeout(() => {
        const p = matchFindCell(pk)
        const an = matchFindCell(ak)
        if (p && p.state === 'wrong') p.state = 'idle'
        if (an && an.state === 'wrong') an.state = 'idle'
      }, 450)
    }
  }

  function onMatchTap(key: string) {
    if (!matchActive.value) return
    const cell = matchFindCell(key)
    if (!cell || cell.state === 'correct' || cell.state === 'wrong') return
    const selKey = matchSelectedKey.value
    if (selKey === null) {
      cell.state = 'selected'
      matchSelectedKey.value = key
      return
    }
    if (selKey === key) {
      cell.state = 'idle'
      matchSelectedKey.value = null
      return
    }
    const sel = matchFindCell(selKey)
    if (!sel) {
      cell.state = 'selected'
      matchSelectedKey.value = key
      return
    }
    if (sel.side === cell.side) {
      sel.state = 'idle'
      cell.state = 'selected'
      matchSelectedKey.value = key
      return
    }
    matchSelectedKey.value = null
    evaluateMatch(sel, cell)
  }

  function matchReset() {
    matchActive.value = false
    matchComplete.value = false
    matchPrompts.value = []
    matchAnswers.value = []
    matchBuffer.value = []
    matchScore.value = 0
    matchMatched.value = 0
    matchStreak.value = 0
    matchBestStreak.value = 0
    matchMisses.value = []
    matchSelectedKey.value = null
    matchMismatch.clear()
    matchRecordedIncorrect.clear()
  }

  async function startMatch() {
    matchReset()
    matchActive.value = true
    matchBuffer.value = await fetchMatchBatch(MATCH_BOARD_SIZE * 3, [])
    for (let i = 0; i < MATCH_BOARD_SIZE; i++) {
      if (!addMatchPairFromBuffer()) break
    }
    // Nothing to play (no introduced cards / empty deck) — end immediately so
    // the view shows the "caught up" summary instead of a blank board.
    if (matchPrompts.value.length === 0) void endMatch()
  }

  async function endMatch() {
    if (!matchActive.value) return
    matchActive.value = false
    if (pendingAnswers.length > 0) {
      try {
        await Promise.all(pendingAnswers.map((p) => p.catch(() => {})))
      } catch {
        /* ignore */
      }
    }
    matchComplete.value = true
  }

  function reset() {
    current.value = null
    prefetchBuffer.value = []
    recentServed.length = 0
    deckEmpty.value = false
    flash.value = null
    lastOutcome.value = null
    revealedIndex.value = null
    locked.value = false
    score.value = { correct: 0, total: 0 }
    roundAnswers.value = []
    roundComplete.value = false
    roundStarted.value = false
    roundsCompleted.value = 0
    streak.value = 0
    bestStreakThisSession.value = 0
    drillMode.value = false
    drillQueue.value = []
    drillResults.value = []
    drillPoolIds.value = []
    drillCurrent.value = null
    toasts.clear()
    // Flame + session game state (bests-ever are persisted; left untouched).
    heat.value = HEAT_START
    combo.value = 0
    gameScore.value = 0
    bestComboThisSession.value = 0
    sessionStartBestScore.value = bestScoreEver.value
    flameEvent.value = null
    introOpen.value = false
    loadingNext.value = false
    networkError.value = false
  }

  return {
    current,
    deckEmpty,
    flash,
    lastOutcome,
    revealedIndex,
    locked,
    score,
    roundAnswers,
    roundAnswered,
    roundComplete,
    roundStarted,
    roundsCompleted,
    streak,
    bestStreakThisSession,
    // Flame meter
    heat,
    combo,
    comboMultiplier,
    questionStartedAt,
    gameScore,
    bestComboThisSession,
    bestComboEver,
    bestScoreEver,
    isRecordScore,
    flameEvent,
    decayFlame,
    audioDisabled,
    introOpen,
    drillMode,
    drillQueue,
    drillResults,
    drillInitialCount,
    drillCurrent,
    skipDwell,
    loadingNext,
    networkError,
    enabledModes,
    setMode,
    roundSize,
    activeRoundSize,
    setRoundSize,
    autoRevealNewCards,
    setAutoReveal,
    sentenceAudioOnReveal,
    setSentenceAudioOnReveal,
    speedPreference,
    setSpeedPreference,
    burndownEnabled,
    setBurndownEnabled,
    drillReps,
    setDrillReps,
    drillFastReps,
    newCardsPerSession,
    setNewCardsPerSession,
    sessionMode,
    deckId,
    deckName,
    setDeck,
    drillPendingCount,
    fetchNext,
    submit,
    submitTyped,
    giveUp,
    ignoreCurrent,
    startRound,
    startNewCards,
    startNextRound,
    backToSetup,
    startDrill,
    skipDrill,
    dismissIntro,
    probeDeck,
    recoverFromNetworkError,
    reset,
    // Continuous-match slice
    matchActive,
    matchComplete,
    matchPrompts,
    matchAnswers,
    matchScore,
    matchMatched,
    matchStreak,
    matchBestStreak,
    matchMisses,
    matchSelectedKey,
    startMatch,
    onMatchTap,
    endMatch,
    matchReset,
  }
})
