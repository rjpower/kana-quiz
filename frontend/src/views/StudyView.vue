<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import ChoiceCard from '../components/ChoiceCard.vue'
import ClozeChoiceCard from '../components/ClozeChoiceCard.vue'
import ClozeInput from '../components/ClozeInput.vue'
import DictationCard from '../components/DictationCard.vue'
import FlameMeter from '../components/FlameMeter.vue'
import Furigana from '../components/Furigana.vue'
import TypeInput from '../components/TypeInput.vue'
import {
  type AnswerType,
  type RubySegment,
  type SpeedPref,
  DRILL_REPS_OPTIONS,
  HEAT_MAX,
  LEECH_FAILURE_THRESHOLD,
  NEW_CARDS_OPTIONS,
  ROUND_SIZE_OPTIONS,
  SPEED_OPTIONS,
  timeoutMsForMode,
  useSessionStore,
} from '../stores/session'

// Start-screen answer-type toggles, in display order. Labels + blurbs are
// UI-only; the keys must match the store's AnswerType union.
const ANSWER_TYPE_OPTIONS: { key: AnswerType; label: string; hint: string }[] = [
  { key: 'mc', label: 'Multiple choice', hint: 'Pick the answer from four choices.' },
  { key: 'type', label: 'Type-in', hint: 'Type the reading or meaning.' },
  { key: 'cloze_choice', label: 'Cloze — pick the word', hint: 'Choose the word that fills a sentence blank.' },
  { key: 'cloze', label: 'Cloze — type the word', hint: 'Type the word that fills a sentence blank.' },
  { key: 'sentence_listen', label: 'Sentence (listening)', hint: 'Hear a sentence and translate it.' },
]
import { playAudioUrl, unlockAudio, versionedAudioUrl, wordAudioUrl } from '../audio'

const store = useSessionStore()

const audioBlocked = ref(false)

// Landing rollup from /api/stats/overview — replaces the old per-deck table
// (that breakdown now lives on the Decks page). Shape mirrors schemas.py
// StatsOverview.
type MaturityKey = 'new' | 'learning' | 'young' | 'mature' | 'mastered'
interface MaturityTier {
  maturity: MaturityKey
  count: number
  avg_ease: number | null
}
interface Overview {
  total_words: number
  introduced: number
  new_available: number
  due: { due_now: number; next_hour: number; next_24h: number }
  maturity: MaturityTier[]
  reviews_last_7_days: number
  accuracy_last_7_days: number | null
  median_latency_ms: number | null
  learned_today: number
  learned_this_week: number
}
const overview = ref<Overview | null>(null)

// Deck-scoped session: /study?deck=<id>. The store carries the scope on
// every session fetch; this view reads the deck's own counts so the start
// screen gates on the deck rather than the whole collection.
interface DeckCounts {
  due: number
  fresh: number
  total: number
  archived: number
  profile: string
}
const route = useRoute()
const deckCounts = ref<DeckCounts | null>(null)

async function loadDeckCounts(id: number) {
  try {
    const resp = await fetch('/api/decks')
    if (!resp.ok) return
    const decks = (await resp.json()) as {
      id: number
      name: string
      profile: string
      word_count: number
      new_count: number
      due_count: number
      archived_count: number
    }[]
    const deck = decks.find((d) => d.id === id)
    if (!deck) return
    store.setDeck(id, deck.name)
    deckCounts.value = {
      due: deck.due_count,
      fresh: deck.new_count,
      total: deck.word_count,
      archived: deck.archived_count,
      profile: deck.profile,
    }
  } catch {
    // Non-fatal — the session still scopes; only the start-screen counts miss.
  }
}

watch(
  () => route.query.deck,
  (raw) => {
    const id = typeof raw === 'string' && raw !== '' ? Number(raw) : null
    const valid = id != null && Number.isFinite(id) ? id : null
    store.setDeck(valid, typeof route.query.name === 'string' ? route.query.name : '')
    deckCounts.value = null
    if (valid != null) void loadDeckCounts(valid)
  },
  { immediate: true },
)

const totalDue = computed(() =>
  store.deckId != null ? (deckCounts.value?.due ?? 0) : (overview.value?.due.due_now ?? 0),
)
const totalNew = computed(() =>
  store.deckId != null
    ? (deckCounts.value?.fresh ?? 0)
    : (overview.value?.new_available ?? 0),
)
const totalWords = computed(() => overview.value?.total_words ?? 0)

// Presentation helpers for the mastery-distribution bar.
const MATURITY_META: Record<MaturityKey, { label: string; hint: string }> = {
  new: { label: 'New', hint: 'Not started yet' },
  learning: { label: 'Learning', hint: 'Interval under a day' },
  young: { label: 'Young', hint: '1–7 day intervals' },
  mature: { label: 'Mature', hint: '1–3 week intervals' },
  mastered: { label: 'Mastered', hint: '3+ week intervals' },
}
// Only the introduced tiers make an interesting "efficiency" story; "new" is
// just the backlog. `studied` drives the distribution bar's proportions.
const studiedCount = computed(() =>
  (overview.value?.maturity ?? [])
    .filter((t) => t.maturity !== 'new')
    .reduce((a, t) => a + t.count, 0),
)
const accuracyPct = computed(() => {
  const a = overview.value?.accuracy_last_7_days
  return a == null ? null : Math.round(a * 100)
})

// Disambiguated glosses carry their distinguishing hint in a trailing
// parenthetical ("order (a command)"). Rendering the whole thing at the
// 48px prompt size wraps to three lines on a phone, so split it: the core
// keeps the display size, the hint sits under it in muted small type. Only
// a trailing "(...)" is treated this way — anything else renders as-is.
const PROMPT_HINT_RE = /^(.*\S)\s*\(([^()]+)\)$/
const promptParts = computed<{ core: string; hint: string | null }>(() => {
  const raw = store.current?.prompt ?? ''
  const m = PROMPT_HINT_RE.exec(raw)
  return m ? { core: m[1], hint: m[2] } : { core: raw, hint: null }
})
const medianSecs = computed(() => {
  const ms = overview.value?.median_latency_ms
  return ms == null ? null : (ms / 1000).toFixed(1)
})

// Compact settings-summary bits (shown on the collapsed <details> line).
const enabledModeCount = computed(
  () => ANSWER_TYPE_OPTIONS.filter((o) => store.enabledModes[o.key]).length,
)
const speedLabel = computed(
  () => SPEED_OPTIONS.find((o) => o.key === store.speedPreference)?.label ?? 'Standard',
)

async function loadOverview() {
  try {
    const resp = await fetch(`/api/stats/overview?tz_offset=${new Date().getTimezoneOffset()}`)
    if (resp.ok) overview.value = (await resp.json()) as Overview
  } catch {
    // Non-fatal — the summary is decorative; the user can still hit Start.
  }
}

interface SentenceState {
  japanese?: string
  japaneseRuby?: RubySegment[]
  english?: string
  mnemonic?: string
  source?: string
  audioUrl?: string
  loading?: boolean
  error?: string
}
// Shown under a sentence that came from a transcript rather than Gemini.
const SOURCE_LABELS: Record<string, string> = { hotspot: 'The Hot Spot' }
function sourceLabel(wordId: number): string | null {
  const source = sentences.get(wordId)?.source
  if (!source || source === 'generated') return null
  return SOURCE_LABELS[source] ?? source
}
// Per-word example-sentence cache. Sentences are cached server-side too,
// but keeping the result in memory means a re-locked card (e.g. after a
// drill round) shows it instantly without another request.
const sentences = reactive(new Map<number, SentenceState>())

// Flipped true after the first 503 from /api/words/:id/sentence — the
// backend has no GEMINI_API_KEY. Once set, the auto-fetch watchers stop
// firing and the on-demand "Show example" button is suppressed, so a
// user who's deliberately running without Gemini never sees a stub error.
const sentenceUnsupported = ref(false)

// Which missed-word rows are expanded on the round-summary screen.
// Keyed by row index (not word_id) so the same word missed twice in a
// round produces two independently-collapsible rows. The fetched sentence
// itself is still cached per-word in `sentences`.
const missExpanded = reactive(new Set<number>())
function toggleMissExpansion(rowIndex: number, wordId: number) {
  if (missExpanded.has(rowIndex)) {
    missExpanded.delete(rowIndex)
    return
  }
  missExpanded.add(rowIndex)
  void fetchSentence(wordId)
}

async function fetchSentence(wordId: number) {
  if (sentenceUnsupported.value) return
  // A sentence card is its own example; there is nothing to conjure.
  if (store.current?.word_id === wordId && store.current.kind === 'sentence') return
  const existing = sentences.get(wordId)
  if (existing && (existing.japanese || existing.loading)) return
  sentences.set(wordId, { loading: true })
  try {
    const resp = await fetch(`/api/words/${wordId}/sentence`)
    if (!resp.ok) {
      if (resp.status === 503) {
        // Gemini key isn't configured — flip the global mute so we stop
        // showing the loading/error UI for every subsequent card.
        sentenceUnsupported.value = true
        sentences.delete(wordId)
        return
      }
      sentences.set(wordId, { error: `Couldn\'t load (${resp.status}).` })
      return
    }
    const data = await resp.json()
    sentences.set(wordId, {
      japanese: data.japanese,
      japaneseRuby: data.japanese_ruby || [],
      english: data.english,
      mnemonic: data.mnemonic || '',
      source: data.source || 'generated',
      audioUrl: data.audio_url || '',
    })
  } catch (e) {
    sentences.set(wordId, { error: (e as Error).message })
  }
}

// Auto-fetch on new-word intro (before the user is quizzed) and on a
// wrong/timeout answer (right after `lastOutcome` lands). Correct answers
// keep the manual "Show example" button so easy cards stay uncluttered.
//
// We key the wrong-answer watcher on `lastOutcome` rather than `locked`
// because in type-in mode `locked` flips ahead of `lastOutcome` (the POST
// has to round-trip first), so a `locked` watcher would fire with a stale
// outcome and miss the wrong-answer trigger.
// Auto-reveal: when /session/next ships a cached sentence inline (new cards),
// seed the per-word cache from it so the intro/reveal renders instantly and the
// introOpen watcher's fetchSentence below short-circuits (no second round-trip).
watch(
  () => store.current?.word_id,
  () => {
    const c = store.current
    if (c?.sentence && !sentences.get(c.word_id)?.japanese) {
      sentences.set(c.word_id, {
        japanese: c.sentence.japanese,
        japaneseRuby: c.sentence.japanese_ruby || [],
        english: c.sentence.english,
        mnemonic: c.sentence.mnemonic || '',
        source: c.sentence.source || 'generated',
        audioUrl: c.sentence.audio_url || '',
      })
    }
  },
)
watch(
  () => store.introOpen,
  (open) => {
    if (open && store.current) void fetchSentence(store.current.word_id)
  },
)
watch(
  () => store.lastOutcome,
  (outcome) => {
    if (!outcome || !store.current) return
    if (!outcome.correct) void fetchSentence(store.current.word_id)
    // The spoken example is for a word still being learned: a miss, or a
    // word just met. A correct review stays quiet.
    if (!outcome.correct || store.current.introduction) {
      void playSentenceOnReveal(store.current.word_id)
    }
  },
)

// The word clip in flight, so the example sentence can follow it instead of
// cutting it off.
let wordPlaying: Promise<void> = Promise.resolve()

async function playAudio(wordId: number) {
  if (store.audioDisabled) return
  try {
    wordPlaying = playAudioUrl(wordAudioUrl(wordId))
    await wordPlaying
    audioBlocked.value = false
  } catch (err) {
    const name = (err as Error).name
    if (name === 'NotAllowedError') audioBlocked.value = true
  }
}

function onStartRoundClick() {
  // Unlock must run synchronously inside the gesture; don't await.
  void unlockAudio()
  store.startRound('review')
}

function onLearnNewClick() {
  void unlockAudio()
  store.startNewCards()
}

function onToggleMode(key: AnswerType, e: Event) {
  store.setMode(key, (e.target as HTMLInputElement).checked)
}

function onToggleBurndown(e: Event) {
  store.setBurndownEnabled((e.target as HTMLInputElement).checked)
}

function onSetSpeed(pref: SpeedPref) {
  store.setSpeedPreference(pref)
}

function onEnableAudioClick() {
  void unlockAudio()
  if (store.current) void playAudio(store.current.word_id)
}

// Autoplay policy: for Japanese prompts the audio IS a recognition cue, so
// play immediately. For English prompts we play after the answer locks so
// the user hears the correct pronunciation as feedback. During an open
// intro card we also play immediately regardless of direction so the user
// hears the new word at the same time they read it.
watch(
  () => store.current?.word_id,
  (id) => {
    if (!id || !store.current) return
    // Listening mode owns its own audio (sentence TTS via DictationCard).
    // Skip the word-pronunciation auto-play so we don't talk over the
    // sentence the user is supposed to translate.
    if (store.current.mode === 'sentence_listen') return
    if (store.introOpen || store.current.direction === 'ja2en') void playAudio(id)
  },
)

// Spoken example: the sentence is fetched (cached server-side) and played
// after the word clip. A card that moves on before the fetch lands plays
// nothing.
async function playSentenceOnReveal(wordId: number) {
  if (!store.sentenceAudioOnReveal || store.audioDisabled) return
  await fetchSentence(wordId)
  await wordPlaying.catch(() => {})
  if (store.current?.word_id !== wordId || !store.locked) return
  const url = sentences.get(wordId)?.audioUrl
  if (!url) return
  try {
    await playAudioUrl(versionedAudioUrl(url))
  } catch (err) {
    if ((err as Error).name === 'NotAllowedError') audioBlocked.value = true
  }
}

watch(
  () => store.locked,
  (locked) => {
    if (!locked || !store.current) return
    if (store.current.mode === 'sentence_listen') return
    if (store.current.direction === 'en2ja') void playAudio(store.current.word_id)
  },
)

function onPick(index: number) {
  store.submit(index, false)
}

function onTyped(value: string) {
  store.submitTyped(value)
}

function onTimeout() {
  if (!store.locked) store.submit(null, true)
}

function onIgnoreClick() {
  if (!store.current) return
  // Confirm so a stray tap doesn't permanently shelve a word — the
  // Decks view exposes the un-ignore action but it's still extra
  // friction the user shouldn't hit by accident.
  const label = store.current.kanji || store.current.prompt
  if (!confirm(`Ignore "${label}"? You can re-enable it from the Decks page.`)) return
  void store.ignoreCurrent()
}

function handleKey(e: KeyboardEvent) {
  if (store.roundComplete) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      store.startNextRound()
    }
    return
  }
  if (!store.roundStarted && !store.deckEmpty) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      // Enter reviews when anything's due; otherwise falls through to a
      // learn-new session so the key is never a no-op when there's work.
      if (totalDue.value > 0) onStartRoundClick()
      else if (totalNew.value > 0) onLearnNewClick()
    }
    return
  }
  if (store.introOpen) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      store.dismissIntro()
    }
    return
  }
  // Locked = answer committed, post-reveal dwell. Enter/Space advances
  // early so confident users don't have to wait out the full window.
  if (store.locked) {
    if ((e.key === 'Enter' || e.key === ' ') && store.skipDwell) {
      e.preventDefault()
      store.skipDwell()
    }
    return
  }
  if (!store.current) return
  // "?" (Shift+/) gives up on the current question. Works in both MC and
  // type-in modes since we explicitly check before the type-input bail-out.
  if (e.key === '?') {
    e.preventDefault()
    store.giveUp()
    return
  }
  // Number-key shortcuts apply to the two choice-grid modes (multiple choice
  // and selection cloze); in type-in modes those keys are part of the input
  // itself (e.g. typing romaji digits).
  if (store.current.mode !== 'mc' && store.current.mode !== 'cloze_choice') return
  const digit = Number(e.key)
  if (digit >= 1 && digit <= 4) onPick(digit - 1)
}

onMounted(() => {
  store.reset()
  store.probeDeck()
  void loadOverview()
  window.addEventListener('keydown', handleKey)
})

onBeforeUnmount(() => {
  store.reset()
  window.removeEventListener('keydown', handleKey)
})

// Debounced loading indicator: only show "Loading next question…" if the
// fetch has been in flight for more than ~400ms. Without the delay every
// answer flashes a spinner and the UI feels jittery on a fast network.
const showLoadingNext = ref(false)
let loadingTimer: ReturnType<typeof setTimeout> | null = null
watch(() => store.loadingNext, (loading) => {
  if (loadingTimer) { clearTimeout(loadingTimer); loadingTimer = null }
  if (loading) {
    loadingTimer = setTimeout(() => { showLoadingNext.value = true }, 400)
  } else {
    showLoadingNext.value = false
  }
})

// Bumps on every transition to a fresh (unlocked) card so that the
// kana-input composables clear their state even when consecutive
// questions land on the same word_id — a rare picker outcome but
// observable in practice when the eligible-cards queue is thin. We
// combine `locked` and `word_id` and increment a counter on the
// false→true→false cycle so the value isn't cleared mid-reveal (it
// only flips when we're actually moving to a NEW question).
const advanceCounter = ref(0)
watch(
  () => store.locked,
  (isLocked, wasLocked) => {
    // false→true is "user just answered" (don't touch input).
    // true→false is "loading next card" — bump so a same-word
    // follow-up still reads as a new question downstream.
    if (wasLocked && !isLocked) advanceCounter.value += 1
  },
)
const resetKey = computed(
  () => (store.current?.word_id ?? 0) * 100000 + advanceCounter.value
)
// The most recently submitted typed answer for the current card. Used by
// the listening-mode reveal to show "you typed X" alongside the
// reference. Reads from the last roundAnswer pushed by the store so we
// don't have to re-thread state through the submit pipeline.
const lastTypedAnswer = computed(() => {
  const last = store.roundAnswers[store.roundAnswers.length - 1]
  if (!last || !store.current || last.word_id !== store.current.word_id) return ''
  return last.typed ?? ''
})
const directionLabel = computed(() =>
  store.current?.direction === 'en2ja' ? 'English → Japanese' : 'Japanese → English'
)
// Hard per-question auto-fail budget (ms): the flame replaces the old countdown
// bar, so the rAF loop below owns the timeout. Recognition modes get 10s, typed
// recall modes 20s.
const hardTimeoutMs = computed(() =>
  store.current ? timeoutMsForMode(store.current.mode) : timeoutMsForMode('mc'),
)

// The flame is "live" (decaying, flickering) only while a question is actually
// awaiting an answer. Frozen during the reveal dwell, the intro card, a loading
// gap, and between rounds — so reading feedback never drains the meter.
const flameLive = computed(
  () =>
    store.roundStarted &&
    !store.roundComplete &&
    !!store.current &&
    !store.locked &&
    !store.introOpen &&
    !store.loadingNext,
)
// rAF loop: the flame is both the global vitality meter and the per-question
// timer. While a question is live we bleed heat (after the grace period) and
// auto-fail once the hard budget elapses. `questionStartedAt` and the rAF
// timestamp share the performance.now() clock, so elapsed is a direct subtract.
// Mirrors the rAF pattern in Countdown.vue / MatchView.vue.
let flameRaf = 0
let flameLast = 0
function flameTick(t: number) {
  if (flameLive.value) {
    const elapsed = t - store.questionStartedAt
    if (elapsed >= hardTimeoutMs.value) {
      onTimeout()
      flameLast = 0
    } else {
      if (flameLast) store.decayFlame(t - flameLast, elapsed, hardTimeoutMs.value)
      flameLast = t
    }
  } else {
    flameLast = 0
  }
  flameRaf = requestAnimationFrame(flameTick)
}
onMounted(() => {
  flameRaf = requestAnimationFrame(flameTick)
})
onBeforeUnmount(() => cancelAnimationFrame(flameRaf))

// Whether to show the post-answer "Continue" affordance. Anything that
// auto-advances shows nothing: a pristine fast-correct answer, and now a
// correct-but-slow drill rep (it flashes "too slow" and re-queues itself).
// Only answers that genuinely hold for reading — a miss or grader feedback —
// surface the button.
const showContinue = computed(
  () =>
    store.locked &&
    !!store.skipDwell &&
    !!store.lastOutcome &&
    (!store.lastOutcome.correct || !!store.lastOutcome.feedback),
)

// Whether the reveal actually *holds* on screen for the user to read, versus a
// pristine fast-correct answer that auto-advances in ~200ms. We gate the
// post-answer reveal blocks (kanji, example sentence/mnemonic) on this so they
// don't flash in-and-out — and shove the choice grid down — on every correct
// answer. They appear only on a miss or a graded-feedback dwell, where the card
// is actually paused for reading. This is the single biggest source of the
// "flashing / jutter between responses".
const revealHeld = computed(
  () =>
    store.locked &&
    !!store.lastOutcome &&
    (!store.lastOutcome.correct || !!store.lastOutcome.feedback),
)

// One-shot "×N multiplier!" celebration: fires when the combo crosses into a
// higher multiplier tier. `id` re-keys the element so the same tier re-triggers
// the pop animation on a later combo.
const multiplierFlash = ref<{ value: number; id: number } | null>(null)
let multiplierFlashSeq = 0
let multiplierFlashTimer: ReturnType<typeof setTimeout> | null = null
watch(
  () => store.comboMultiplier,
  (mult, prev) => {
    if (mult > prev && mult > 1) {
      multiplierFlashSeq += 1
      multiplierFlash.value = { value: mult, id: multiplierFlashSeq }
      if (multiplierFlashTimer) clearTimeout(multiplierFlashTimer)
      multiplierFlashTimer = setTimeout(() => {
        multiplierFlash.value = null
      }, 1200)
    }
  },
)

// One-shot "new personal best" celebration: fires the moment the live score
// overtakes the session-start record. While `isRecordScore` holds the score
// chip wears a gold "record" marker (see template).
const recordFlash = ref(false)
let recordFlashTimer: ReturnType<typeof setTimeout> | null = null
watch(
  () => store.isRecordScore,
  (now, was) => {
    if (now && !was) {
      recordFlash.value = true
      if (recordFlashTimer) clearTimeout(recordFlashTimer)
      recordFlashTimer = setTimeout(() => {
        recordFlash.value = false
      }, 1800)
    }
  },
)
onBeforeUnmount(() => {
  if (multiplierFlashTimer) clearTimeout(multiplierFlashTimer)
  if (recordFlashTimer) clearTimeout(recordFlashTimer)
})

function choiceState(idx: number): 'idle' | 'correct' | 'wrong' | 'reveal' {
  if (!store.locked || !store.current) return 'idle'
  if (idx === store.current.correct_index) {
    return store.lastOutcome?.correct ? 'correct' : 'reveal'
  }
  if (idx === store.revealedIndex) return 'wrong'
  return 'idle'
}

const roundProgress = computed(() => (store.roundAnswered / store.activeRoundSize) * 100)
// Quick-fire progress. The label counts *words remaining* (distinct words not
// yet cleared) so it reads "14 left -> 0 left". The BAR, though, fills on every
// rep that moves a word closer, not just full clears — otherwise it sat frozen
// through the entire first pass (each word needs DRILL_REQUIRED_REPS reps, so
// nothing "clears" until the second lap) and looked stuck.
//
// A word clears on TWO counts — enough correct reps AND enough of them fast —
// so its credit is driven by whichever is further away. Crediting goodReps
// alone would let the bar march to full while a word still had zero fast reps
// and was nowhere near leaving the queue, which is the same "bar lies about the
// remaining work" bug in the other direction. Fast reps are a subset of correct
// ones, so this never over-credits.
//
// De-duped by word_id: the brief dwell where a just-cleared word is both in
// drillResults and still drillCurrent must not count twice.
const drillRemaining = computed(() =>
  Math.max(0, store.drillInitialCount - store.drillResults.length),
)
const drillProgress = computed(() => {
  const reps = store.drillReps
  const fastNeeded = store.drillFastReps
  const total = store.drillInitialCount * reps
  if (total === 0) return 0
  // Reps still owed before this word can leave, expressed on the `reps` scale.
  const owed = (goodReps: number, fastReps: number) =>
    Math.min(reps, Math.max(0, reps - goodReps, fastNeeded - fastReps))
  const counted = new Set<number>()
  let credit = 0
  for (const r of store.drillResults) {
    if (counted.has(r.word_id)) continue
    counted.add(r.word_id)
    credit += reps
  }
  for (const it of store.drillQueue) {
    if (counted.has(it.word_id)) continue
    counted.add(it.word_id)
    credit += reps - owed(it.goodReps, it.fastReps)
  }
  const cur = store.drillCurrent
  if (cur && !counted.has(cur.word_id)) {
    credit += reps - owed(cur.goodReps, cur.fastReps)
  }
  return Math.min(100, (credit / total) * 100)
})

// A brief ring-flash over the question card on every answer — a little green
// fanfare for a correct answer (there was no screen-level success feedback
// before, only the flame surge), and a subtle red for a miss. Keyed by a
// counter so the CSS animation restarts on each answer, even two in a row.
// Driven off lastOutcome so it fires in both the normal round and the drill.
const answerBurst = ref<{ id: number; tone: 'good' | 'bad' } | null>(null)
let answerBurstSeq = 0
let answerBurstTimer: ReturnType<typeof setTimeout> | null = null
watch(
  () => store.lastOutcome,
  (o) => {
    if (!o) return
    answerBurstSeq += 1
    answerBurst.value = { id: answerBurstSeq, tone: o.correct ? 'good' : 'bad' }
    if (answerBurstTimer) clearTimeout(answerBurstTimer)
    answerBurstTimer = setTimeout(() => {
      answerBurst.value = null
      answerBurstTimer = null
    }, 650)
  },
)
onBeforeUnmount(() => {
  if (answerBurstTimer) clearTimeout(answerBurstTimer)
})

// Round summary computations
const roundCorrect = computed(() =>
  store.roundAnswers.filter((a) => a.correct).length,
)
const roundAccuracy = computed(() =>
  store.roundAnswers.length === 0
    ? 0
    : roundCorrect.value / store.roundAnswers.length,
)
const avgLatencyMs = computed(() => {
  // Timeouts and wrong answers aren't useful "how fast did I recognize"
  // signal — only average over corrects.
  const corrects = store.roundAnswers.filter((a) => a.correct)
  if (corrects.length === 0) return null
  const sum = corrects.reduce((s, a) => s + a.latency_ms, 0)
  return Math.round(sum / corrects.length)
})
const bestStreakThisRound = computed(() => {
  let best = 0
  let cur = 0
  for (const a of store.roundAnswers) {
    if (a.correct) {
      cur += 1
      if (cur > best) best = cur
    } else {
      cur = 0
    }
  }
  return best
})
const missed = computed(() =>
  store.roundAnswers.filter((a) => !a.correct),
)
const drillFastCleared = computed(() =>
  store.drillResults.filter((r) => r.fastCleared).length,
)
// word_id -> its graduation record, so the summary's miss list can show how
// many tries each took to clear.
const drillByWord = computed(() => {
  const m = new Map<number, (typeof store.drillResults)[number]>()
  for (const r of store.drillResults) m.set(r.word_id, r)
  return m
})
function clearedTitle(wordId: number): string {
  const r = drillByWord.value.get(wordId)
  if (!r) return ''
  if (r.fastCleared) return 'Cleared the quick-fire at speed, repeatedly'
  if (r.cleared) return 'Cleared via the attempt cap in the quick-fire'
  return 'Still shaky — moved on after the attempt cap'
}

// Emoji fanfare tier based on accuracy.
const fanfare = computed(() => {
  const acc = roundAccuracy.value
  if (acc >= 0.96) return { title: 'Flawless!', emoji: '🏆', tone: 'gold' }
  if (acc >= 0.85) return { title: 'Great round!', emoji: '⭐', tone: 'green' }
  if (acc >= 0.7) return { title: 'Solid round', emoji: '👍', tone: 'blue' }
  if (acc >= 0.5) return { title: 'Keep going', emoji: '💪', tone: 'amber' }
  return { title: 'Shake it off', emoji: '🌱', tone: 'neutral' }
})
</script>

<template>
  <!-- Round summary screen -->
  <section v-if="store.roundComplete" class="panel summary" :class="fanfare.tone">
    <div class="fanfare">
      <div class="emoji">{{ fanfare.emoji }}</div>
      <h2>{{ fanfare.title }}</h2>
      <div class="muted">
        Round {{ store.roundsCompleted + 1 }} complete
      </div>
    </div>

    <div class="stat-grid">
      <div class="stat">
        <div class="label">Score</div>
        <div class="value">{{ roundCorrect }} / {{ store.roundAnswers.length }}</div>
      </div>
      <div class="stat">
        <div class="label">Accuracy</div>
        <div class="value">{{ (roundAccuracy * 100).toFixed(0) }}%</div>
      </div>
      <div class="stat">
        <div class="label">Best streak</div>
        <div class="value">{{ bestStreakThisRound }}</div>
      </div>
      <div class="stat">
        <div class="label">Avg answer</div>
        <div class="value">
          {{ avgLatencyMs === null ? '—' : (avgLatencyMs / 1000).toFixed(2) + 's' }}
        </div>
      </div>
    </div>

    <div class="game-banner">
      <div class="game-stat">
        <span class="g-icon">🔥</span>
        <span class="g-value">{{ store.gameScore }}</span>
        <span class="g-label">flame score</span>
      </div>
      <div class="game-stat">
        <span class="g-value">×{{ store.bestComboThisSession <= 0 ? 1 : Math.min(6, 1 + Math.floor(store.bestComboThisSession / 4)) }}</span>
        <span class="g-label">best multiplier</span>
      </div>
      <div v-if="store.drillResults.length" class="game-stat">
        <span class="g-value">{{ drillFastCleared }} / {{ store.drillResults.length }}</span>
        <span class="g-label">embers cleared fast</span>
      </div>
    </div>

    <div v-if="missed.length" class="missed">
      <div class="missed-title">Words to revisit</div>
      <ul>
        <li v-for="(a, i) in missed" :key="i" class="miss-row">
          <div class="miss-line" @click="toggleMissExpansion(i, a.word_id)">
            <span class="miss-prompt">
              {{ a.prompt }}
              <span v-if="a.kanji" class="miss-kanji">{{ a.kanji }}</span>
              <span
                v-if="(a.failure_streak ?? 0) >= LEECH_FAILURE_THRESHOLD"
                class="miss-leech"
                :title="`${a.failure_streak} misses in a row`"
              >leech</span>
            </span>
            <span v-if="a.mode === 'type' && a.typed" class="miss-typed">
              <span class="miss-typed-you">{{ a.typed || '—' }}</span>
              <span class="miss-typed-arrow">→</span>
              <span class="miss-typed-expected">{{ a.expected }}</span>
            </span>
            <span class="miss-tag" :class="a.outcome">{{ a.outcome }}</span>
            <span
              v-if="drillByWord.get(a.word_id)"
              class="miss-cleared"
              :class="{
                fast: drillByWord.get(a.word_id)!.fastCleared,
                unmastered: !drillByWord.get(a.word_id)!.cleared,
              }"
              :title="clearedTitle(a.word_id)"
            >{{ drillByWord.get(a.word_id)!.cleared ? '✓' : '✗' }}
              {{ drillByWord.get(a.word_id)!.attempts }}×</span>
            <span class="miss-toggle" :class="{ open: missExpanded.has(i) }">▸</span>
          </div>
          <div
            v-if="missExpanded.has(i)"
            class="miss-detail"
          >
            <div
              v-if="sentences.get(a.word_id)?.loading"
              class="muted sentence-loading"
            >
              <span class="spinner" aria-hidden="true"></span>
              <span>Loading…</span>
            </div>
            <div
              v-if="sentences.get(a.word_id)?.mnemonic"
              class="mnemonic"
            >
              <div class="mnemonic-label">Memory hook</div>
              <div class="mnemonic-body">
                {{ sentences.get(a.word_id)!.mnemonic }}
              </div>
            </div>
            <div
              v-if="sentences.get(a.word_id)?.japanese"
              class="sentence"
            >
              <div class="sentence-ja">
                <Furigana
                  v-if="sentences.get(a.word_id)?.japaneseRuby?.length"
                  :segments="sentences.get(a.word_id)!.japaneseRuby!"
                />
                <template v-else>{{ sentences.get(a.word_id)!.japanese }}</template>
              </div>
              <div class="sentence-en">
                {{ sentences.get(a.word_id)!.english }}
              </div>
            </div>
            <div v-if="sentences.get(a.word_id)?.error" class="sentence-error">
              {{ sentences.get(a.word_id)!.error }}
            </div>
          </div>
        </li>
      </ul>
    </div>

    <p v-if="store.comboMultiplier > 1 || store.combo > 0" class="carry-note">
      🔥 Your blaze carries over — starting the next round at
      <strong>×{{ store.comboMultiplier }}</strong>
      <span v-if="store.combo >= 2"> on a {{ store.combo }} combo</span>.
      Keep it alive!
    </p>
    <button class="btn primary" @click="store.startNextRound()">
      {{ store.sessionMode === 'new' ? 'Learn more new cards' : 'Start next round' }}
    </button>
    <button
      v-if="store.burndownEnabled && store.drillPendingCount > 0"
      class="btn secondary"
      @click="store.startDrill()"
    >
      {{ store.sessionMode === 'new' ? '🔥 Drill these again' : '🔥 Replay the misses again' }}
    </button>
    <button class="btn ghost back-to-setup" @click="store.backToSetup()">
      ← Back to setup
    </button>
    <p v-if="store.drillResults.length > 0" class="muted drill-recap">
      Quick-fire: {{ store.drillResults.length }} cleared · {{ drillFastCleared }} under the speed bar
    </p>
    <p class="hint">Press Enter or Space to continue.</p>
  </section>

  <!-- Pre-round setup screen -->
  <section
    v-else-if="!store.roundStarted && !store.deckEmpty"
    class="panel start"
  >
    <h2>{{ store.deckId != null ? 'Ready to study this deck?' : 'Ready to study?' }}</h2>

    <div v-if="store.deckId != null" class="deck-banner">
      <div class="deck-banner-name">{{ store.deckName || 'Deck ' + store.deckId }}</div>
      <div class="deck-banner-counts">
        <span>{{ deckCounts?.due ?? 0 }} due</span>
        <span>{{ deckCounts?.fresh ?? 0 }} new</span>
        <span>{{ deckCounts?.total ?? 0 }} active</span>
        <span v-if="deckCounts?.archived">{{ deckCounts.archived }} cleared 🏁</span>
        <span v-if="deckCounts?.profile === 'sprint'" class="deck-banner-sprint">sprint deck</span>
      </div>
      <RouterLink :to="'/decks/' + store.deckId" class="muted">Deck details →</RouterLink>
      <RouterLink to="/study" class="muted">Exit deck session</RouterLink>
    </div>

    <div v-if="overview && store.deckId == null" class="study-summary">
      <!-- Review lookahead: what's ready now and rolling in over the next day. -->
      <div class="due-schedule">
        <div class="due-cell" :class="{ on: overview.due.due_now > 0 }">
          <span class="due-num">{{ overview.due.due_now }}</span>
          <span class="due-lbl">due now</span>
        </div>
        <div class="due-cell">
          <span class="due-num">+{{ overview.due.next_hour }}</span>
          <span class="due-lbl">next hour</span>
        </div>
        <div class="due-cell">
          <span class="due-num">+{{ overview.due.next_24h }}</span>
          <span class="due-lbl">next 24h</span>
        </div>
        <div class="due-cell">
          <span class="due-num">{{ overview.new_available }}</span>
          <span class="due-lbl">new to learn</span>
        </div>
        <div class="due-cell" :class="{ on: overview.learned_today > 0 }">
          <span class="due-num">{{ overview.learned_today }}</span>
          <span class="due-lbl">learned today</span>
        </div>
        <div class="due-cell">
          <span class="due-num">{{ overview.learned_this_week }}</span>
          <span class="due-lbl">this week</span>
        </div>
      </div>

      <!-- Mastery distribution: how the studied vocabulary is progressing. -->
      <div v-if="studiedCount > 0" class="mastery">
        <div class="mastery-head">
          <span class="mastery-title">Memorization by mastery</span>
          <span class="mastery-total muted">{{ studiedCount }} of {{ totalWords }} started</span>
        </div>
        <div class="mastery-bar" role="img" aria-label="Mastery distribution">
          <template v-for="t in overview.maturity" :key="t.maturity">
            <span
              v-if="t.maturity !== 'new' && t.count > 0"
              class="mastery-seg"
              :class="t.maturity"
              :style="{ flexGrow: t.count }"
              :title="`${MATURITY_META[t.maturity].label}: ${t.count}`"
            ></span>
          </template>
        </div>
        <div class="mastery-legend">
          <template v-for="t in overview.maturity" :key="t.maturity">
            <span v-if="t.maturity !== 'new'" class="legend-item">
              <span class="legend-dot" :class="t.maturity"></span>
              <span class="legend-label">{{ MATURITY_META[t.maturity].label }}</span>
              <span class="legend-count">{{ t.count }}</span>
              <span v-if="t.avg_ease != null" class="legend-ease" :title="'Average ease — higher sticks more easily'">
                ×{{ t.avg_ease.toFixed(2) }}
              </span>
            </span>
          </template>
        </div>
      </div>

      <!-- Recent efficiency: last 7 days at a glance. -->
      <div v-if="overview.reviews_last_7_days > 0" class="efficiency">
        <div class="eff-cell">
          <span class="eff-num">{{ accuracyPct }}<small>%</small></span>
          <span class="eff-lbl">accuracy · 7d</span>
        </div>
        <div class="eff-cell">
          <span class="eff-num">{{ overview.reviews_last_7_days }}</span>
          <span class="eff-lbl">reviews · 7d</span>
        </div>
        <div v-if="medianSecs != null" class="eff-cell">
          <span class="eff-num">{{ medianSecs }}<small>s</small></span>
          <span class="eff-lbl">median answer</span>
        </div>
      </div>

      <RouterLink to="/decks" class="deck-breakdown-link muted">
        Per-deck breakdown →
      </RouterLink>
    </div>

    <!-- Primary actions: review due cards, or a focused learn-new session. -->
    <div class="actions">
      <button
        class="action-btn review"
        type="button"
        :disabled="totalDue === 0"
        @click="onStartRoundClick"
      >
        <span class="action-icon" aria-hidden="true">▶</span>
        <span class="action-text">
          <span class="action-title">Review</span>
          <span class="action-sub">
            <template v-if="totalDue > 0">{{ totalDue }} due · up to {{ store.roundSize }}</template>
            <template v-else>nothing due right now</template>
          </span>
        </span>
      </button>
      <button
        class="action-btn learn"
        type="button"
        :disabled="totalNew === 0"
        @click="onLearnNewClick"
      >
        <span class="action-icon" aria-hidden="true">✦</span>
        <span class="action-text">
          <span class="action-title">Learn new cards</span>
          <span class="action-sub">
            <template v-if="totalNew > 0">
              {{ Math.min(store.newCardsPerSession, totalNew) }} new · intro then drill
            </template>
            <template v-else>no new cards left</template>
          </span>
        </span>
      </button>
    </div>
    <p v-if="totalDue === 0 && totalNew === 0" class="hint caught-up">
      🎉 All caught up — nothing due and no new cards left.
    </p>

    <details class="settings">
      <summary>
        <span class="settings-gear" aria-hidden="true">⚙</span> Settings
        <span class="settings-summary">
          {{ enabledModeCount }} answer type{{ enabledModeCount === 1 ? '' : 's' }} ·
          {{ speedLabel }} speed · burndown {{ store.burndownEnabled ? `${store.drillReps}×` : 'off' }}
        </span>
      </summary>

      <fieldset class="setting-group">
        <legend>Answer types</legend>
        <label v-for="opt in ANSWER_TYPE_OPTIONS" :key="opt.key" class="answer-type">
          <input
            type="checkbox"
            :checked="store.enabledModes[opt.key]"
            @change="onToggleMode(opt.key, $event)"
          />
          <span class="answer-type-text">
            <span class="answer-type-label">{{ opt.label }}</span>
            <span class="answer-type-hint">{{ opt.hint }}</span>
          </span>
        </label>
        <p class="answer-type-note">
          Turning Multiple choice off promotes those cards to type-in. At least
          one of Multiple choice / Type-in stays on.
        </p>
      </fieldset>

      <fieldset class="setting-group">
        <legend>Speed</legend>
        <p class="group-hint">
          How fast an answer must be to count as “fast” — feeds the flame bonus
          and the burndown’s speed bar.
        </p>
        <div class="seg">
          <button
            v-for="opt in SPEED_OPTIONS"
            :key="opt.key"
            type="button"
            class="seg-btn"
            :class="{ active: store.speedPreference === opt.key }"
            @click="onSetSpeed(opt.key)"
          >
            {{ opt.label }}<small>under {{ opt.blurb }}</small>
          </button>
        </div>
      </fieldset>

      <fieldset class="setting-group">
        <legend>Burndown</legend>
        <label class="answer-type">
          <input type="checkbox" :checked="store.burndownEnabled" @change="onToggleBurndown" />
          <span class="answer-type-text">
            <span class="answer-type-label">Replay to fluency after a round</span>
            <span class="answer-type-hint">
              Quick-fire the round’s misses (and every card in a new-cards session)
              until you can clear each one fast — {{ store.drillFastReps }}× over,
              so the speed has to reproduce.
            </span>
          </span>
        </label>
        <div v-if="store.burndownEnabled" class="sub-setting">
          <span class="sub-label">Reps to clear a word</span>
          <div class="seg compact">
            <button
              v-for="n in DRILL_REPS_OPTIONS"
              :key="n"
              type="button"
              class="seg-btn"
              :class="{ active: store.drillReps === n }"
              @click="store.setDrillReps(n)"
            >
              {{ n }}×
            </button>
          </div>
        </div>
      </fieldset>

      <fieldset class="setting-group">
        <legend>Session sizes</legend>
        <div class="sub-setting">
          <span class="sub-label">Review round length</span>
          <div class="seg">
            <button
              v-for="opt in ROUND_SIZE_OPTIONS"
              :key="opt"
              type="button"
              class="seg-btn"
              :class="{ active: store.roundSize === opt }"
              @click="store.setRoundSize(opt)"
            >
              {{ opt }}
            </button>
          </div>
        </div>
        <div class="sub-setting">
          <span class="sub-label">New cards per learn session</span>
          <div class="seg">
            <button
              v-for="opt in NEW_CARDS_OPTIONS"
              :key="opt"
              type="button"
              class="seg-btn"
              :class="{ active: store.newCardsPerSession === opt }"
              @click="store.setNewCardsPerSession(opt)"
            >
              {{ opt }}
            </button>
          </div>
        </div>
        <label class="answer-type reveal-toggle">
          <input
            type="checkbox"
            :checked="store.autoRevealNewCards"
            @change="store.setAutoReveal(($event.target as HTMLInputElement).checked)"
          />
          <span class="answer-type-text">
            <span class="answer-type-label">Auto-reveal new cards</span>
            <span class="answer-type-hint">
              Show the mnemonic &amp; example sentence when a new word first appears.
            </span>
          </span>
        </label>
        <label class="answer-type reveal-toggle">
          <input
            type="checkbox"
            :checked="store.sentenceAudioOnReveal"
            @change="store.setSentenceAudioOnReveal(($event.target as HTMLInputElement).checked)"
          />
          <span class="answer-type-text">
            <span class="answer-type-label">Speak the example on new words and misses</span>
            <span class="answer-type-hint">
              After the answer shows on a new word or a miss, play the example sentence.
            </span>
          </span>
        </label>
      </fieldset>
    </details>

    <RouterLink to="/study/match" class="btn secondary match-link">
      ⚡ Speed round — match the pairs
    </RouterLink>

    <div v-if="store.bestScoreEver > 0" class="muted personal-best">
      🏆 Personal best · {{ store.bestScoreEver }} flame score · {{ store.bestComboEver }} combo
    </div>
    <div v-if="store.roundsCompleted > 0" class="muted rounds-so-far">
      Completed {{ store.roundsCompleted }}
      round{{ store.roundsCompleted === 1 ? '' : 's' }} this session · best streak
      {{ store.bestStreakThisSession }}
    </div>
    <p class="hint">Press Enter or Space to review.</p>
  </section>

  <!-- New-word introduction (pre-exposure before the very first quiz of a
       freshly-introduced word). -->
  <section
    v-else-if="store.introOpen && store.current?.introduction"
    class="panel intro"
  >
    <div class="intro-badge">New word</div>
    <div class="intro-kana">{{ store.current.introduction.kana }}</div>
    <div v-if="store.current.introduction.kanji" class="intro-kanji">
      {{ store.current.introduction.kanji }}
    </div>
    <div class="intro-english">{{ store.current.introduction.english }}</div>
    <button
      v-if="!store.audioDisabled"
      class="audio-btn intro-audio"
      type="button"
      aria-label="Play pronunciation"
      @click="playAudio(store.current.word_id)"
    >
      <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
        <path fill="currentColor" d="M3 10v4h4l5 5V5L7 10H3zm13.5 2a4.5 4.5 0 0 0-2.5-4.03v8.05a4.5 4.5 0 0 0 2.5-4.02zM14 3.23v2.06a7 7 0 0 1 0 13.42v2.06a9 9 0 0 0 0-17.54z"/>
      </svg>
    </button>

    <!-- Mnemonic + example sentence: prefetched in the background as soon
         as a brand-new word is picked, so by the time the user reads the
         kana/english on this card the memory hook is already present. -->
    <div
      v-if="sentences.get(store.current.word_id)?.mnemonic"
      class="mnemonic"
    >
      <div class="mnemonic-label">Memory hook</div>
      <div class="mnemonic-body">
        {{ sentences.get(store.current.word_id)!.mnemonic }}
      </div>
    </div>
    <div
      v-if="sentences.get(store.current.word_id)?.japanese"
      class="sentence intro-sentence"
    >
      <div class="sentence-ja">
        <Furigana
          v-if="sentences.get(store.current.word_id)?.japaneseRuby?.length"
          :segments="sentences.get(store.current.word_id)!.japaneseRuby!"
        />
        <template v-else>{{ sentences.get(store.current.word_id)!.japanese }}</template>
      </div>
      <div class="sentence-en">
        {{ sentences.get(store.current.word_id)!.english }}
      </div>
      <div v-if="sourceLabel(store.current.word_id)" class="sentence-source">
        From {{ sourceLabel(store.current.word_id) }}
      </div>
    </div>
    <div
      v-else-if="sentences.get(store.current.word_id)?.loading"
      class="muted sentence-loading"
    >
      <span class="spinner" aria-hidden="true"></span>
      <span>Conjuring an example sentence…</span>
    </div>

    <button class="btn primary" @click="store.dismissIntro()">Got it</button>
    <p class="hint">Press Enter or Space to start the quiz.</p>
  </section>

  <!-- Active question -->
  <section class="panel question-panel" v-else-if="store.current">
    <div
      v-if="answerBurst"
      :key="answerBurst.id"
      class="answer-burst"
      :class="answerBurst.tone"
      aria-hidden="true"
    ></div>
    <div v-if="store.deckId != null" class="deck-chip">{{ store.deckName || 'deck ' + store.deckId }}</div>
    <div v-if="!store.drillMode" class="round-bar">
      <div class="round-bar-fill" :style="{ width: roundProgress + '%' }"></div>
      <span class="round-label">
        {{ store.roundAnswered }} / {{ store.activeRoundSize }}
      </span>
    </div>
    <div v-else class="round-bar drill-bar">
      <div
        class="round-bar-fill drill-fill"
        :style="{ width: drillProgress + '%' }"
      ></div>
      <span class="round-label">
        🔥 Clear the embers · {{ drillRemaining }} left
      </span>
    </div>
    <div class="meta">
      <span class="tag">{{ directionLabel }}</span>
      <span v-if="store.drillMode" class="tag drill-tag">Quick-fire</span>
      <span class="score"
        >{{ store.score.correct }} / {{ store.score.total }}</span
      >
    </div>

    <!-- Flame meter: the *global* timer. Heat bleeds down while a question is
         live and is refuelled by answers; combo drives the multiplier. -->
    <div class="flame-hud">
      <span
        class="fchip score"
        :class="{ record: store.isRecordScore }"
        title="Flame score"
      >
        <span v-if="store.isRecordScore" class="record-pip" aria-hidden="true">🏆</span>
        {{ store.gameScore }}
      </span>
      <div class="flame-center">
        <FlameMeter
          :heat="store.heat / HEAT_MAX"
          :combo="store.combo"
          :event="store.flameEvent"
          :paused="!flameLive"
        />
        <span v-if="store.combo >= 2" class="combo-label">{{ store.combo }} combo</span>
        <transition name="mult-pop">
          <div
            v-if="multiplierFlash"
            :key="multiplierFlash.id"
            class="mult-flash"
            aria-hidden="true"
          >
            <span class="mult-flash-x">×{{ multiplierFlash.value }}</span>
            <span class="mult-flash-label">multiplier</span>
          </div>
        </transition>
      </div>
      <span
        class="fchip mult"
        :class="{ hot: store.comboMultiplier > 1 }"
        title="Score multiplier"
        >×{{ store.comboMultiplier }}</span
      >
    </div>
    <transition name="record-pop">
      <div v-if="recordFlash" class="record-flash" role="status">
        🏆 New personal best!
      </div>
    </transition>

    <div v-if="store.networkError" class="net-banner error">
      <span>Connection trouble. Your last answer is saved.</span>
      <button class="btn primary small" type="button" @click="store.recoverFromNetworkError()">
        Reconnect
      </button>
    </div>
    <div v-else-if="showLoadingNext" class="net-banner">
      <span class="spinner" aria-hidden="true"></span>
      <span class="muted">Loading next question…</span>
    </div>
    <div v-if="audioBlocked && !store.audioDisabled" class="audio-banner">
      <button class="btn primary small" type="button" @click="onEnableAudioClick">
        <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
          <path fill="currentColor" d="M3 10v4h4l5 5V5L7 10H3zm13.5 2a4.5 4.5 0 0 0-2.5-4.03v8.05a4.5 4.5 0 0 0 2.5-4.02zM14 3.23v2.06a7 7 0 0 1 0 13.42v2.06a9 9 0 0 0 0-17.54z"/>
        </svg>
        Enable audio
      </button>
      <span class="muted">Browser blocked autoplay — click once to unlock.</span>
    </div>
    <!-- Selection cloze withholds the English gloss (and the word-audio
         button) until the answer locks — otherwise the prompt would let the
         user match English→kana and ignore the sentence, making it no harder
         than plain multiple choice. After locking, the gloss + audio show as
         reveal feedback, same as the other modes. sentence_listen withholds
         its prompt the same way. -->
    <div
      v-if="(store.current.mode !== 'sentence_listen' && store.current.mode !== 'cloze_choice') || store.locked"
      class="prompt-row"
    >
      <h1
        v-if="store.current.mode === 'sentence_listen' && store.locked"
        class="prompt prompt-listen"
      >
        <Furigana
          v-if="store.current.sentence_japanese_ruby?.length"
          :segments="store.current.sentence_japanese_ruby"
        />
        <template v-else>{{ store.current.sentence_japanese }}</template>
      </h1>
      <h1 v-else class="prompt" :class="{ 'prompt-sentence': store.current.kind === 'sentence' }">
        {{ promptParts.core }}
        <span v-if="promptParts.hint" class="prompt-hint">{{ promptParts.hint }}</span>
      </h1>
      <button
        v-if="!store.audioDisabled && store.current.mode !== 'sentence_listen'"
        class="audio-btn"
        type="button"
        aria-label="Play pronunciation"
        @click="playAudio(store.current.word_id)"
      >
        <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
          <path fill="currentColor" d="M3 10v4h4l5 5V5L7 10H3zm13.5 2a4.5 4.5 0 0 0-2.5-4.03v8.05a4.5 4.5 0 0 0 2.5-4.02zM14 3.23v2.06a7 7 0 0 1 0 13.42v2.06a9 9 0 0 0 0-17.54z"/>
        </svg>
      </button>
    </div>
    <!-- Kanji form. On ja2en we show it from the start as a homonym
         disambiguator (どうじょう alone is ambiguous between 同情 and
         道場 — kanji resolves which word is being asked, without
         shortcutting the typed English answer). On en2ja we keep it
         hidden until lock since the kanji would tell the user the
         kana directly. -->
    <div
      v-if="store.current.kanji && (store.current.direction === 'ja2en' || revealHeld)"
      class="kanji-reveal"
    >{{ store.current.kanji }}</div>
    <div v-if="revealHeld && store.current" class="sentence-row">
      <button
        v-if="!sentenceUnsupported
              && !sentences.get(store.current.word_id)?.japanese
              && !sentences.get(store.current.word_id)?.loading
              && !sentences.get(store.current.word_id)?.error
              && store.lastOutcome?.correct"
        class="btn ghost small"
        type="button"
        @click="fetchSentence(store.current.word_id)"
      >
        Show example & mnemonic
      </button>
      <div
        v-if="sentences.get(store.current.word_id)?.loading"
        class="muted sentence-loading"
      >
        <span class="spinner" aria-hidden="true"></span>
        <span>Loading example…</span>
      </div>
      <div
        v-if="sentences.get(store.current.word_id)?.mnemonic"
        class="mnemonic"
      >
        <div class="mnemonic-label">Memory hook</div>
        <div class="mnemonic-body">
          {{ sentences.get(store.current.word_id)!.mnemonic }}
        </div>
      </div>
      <div
        v-if="sentences.get(store.current.word_id)?.japanese"
        class="sentence"
      >
        <div class="sentence-ja">
          <Furigana
            v-if="sentences.get(store.current.word_id)?.japaneseRuby?.length"
            :segments="sentences.get(store.current.word_id)!.japaneseRuby!"
          />
          <template v-else>{{ sentences.get(store.current.word_id)!.japanese }}</template>
        </div>
        <div class="sentence-en">
          {{ sentences.get(store.current.word_id)!.english }}
        </div>
        <div v-if="sourceLabel(store.current.word_id)" class="sentence-source">
          From {{ sourceLabel(store.current.word_id) }}
        </div>
      </div>
      <div v-if="sentences.get(store.current.word_id)?.error" class="sentence-error">
        {{ sentences.get(store.current.word_id)!.error }}
      </div>
    </div>
    <div v-if="store.current.mode === 'mc'" class="grid">
      <ChoiceCard
        v-for="(label, idx) in store.current.choices"
        :key="idx"
        :label="label"
        :index="idx"
        :state="choiceState(idx)"
        :disabled="store.locked"
        @pick="onPick"
      />
    </div>
    <ClozeChoiceCard
      v-else-if="store.current.mode === 'cloze_choice' && store.current.cloze_template"
      :template="store.current.cloze_template"
      :before="store.current.cloze_before"
      :after="store.current.cloze_after"
      :choices="store.current.choices"
      :states="store.current.choices.map((_, i) => choiceState(i))"
      :disabled="store.locked"
      @pick="onPick"
    />
    <ClozeInput
      v-else-if="store.current.mode === 'cloze' && store.current.cloze_template"
      :template="store.current.cloze_template"
      :before="store.current.cloze_before"
      :after="store.current.cloze_after"
      :reset-key="resetKey"
      :disabled="store.locked"
      @submit="onTyped"
    />
    <DictationCard
      v-else-if="store.current.mode === 'sentence_listen' && store.current.audio_url"
      :audio-url="versionedAudioUrl(store.current.audio_url)"
      :reset-key="resetKey"
      :disabled="store.locked"
      @submit="onTyped"
      @audio-blocked="audioBlocked = true"
    />
    <TypeInput
      v-else
      :direction="store.current.direction"
      :reset-key="resetKey"
      :disabled="store.locked"
      @submit="onTyped"
    />
    <div
      v-if="!store.locked && store.current && !store.introOpen"
      class="quiz-actions"
    >
      <button
        class="btn give-up"
        type="button"
        @click="store.giveUp()"
      >
        I don't know (?)
      </button>
      <button
        v-if="!store.drillMode"
        class="btn ignore"
        type="button"
        title="Skip this word for good — re-enable from the Decks page"
        @click="onIgnoreClick"
      >
        Ignore word
      </button>
      <button
        v-if="store.drillMode"
        class="btn skip-drill"
        type="button"
        title="Stop the quick-fire replay and go to the summary"
        @click="store.skipDrill()"
      >
        Skip to summary
      </button>
    </div>
    <div
      v-if="store.locked && store.current.mode === 'sentence_listen'"
      class="reveal listen-reveal"
      :class="{ ok: store.lastOutcome?.correct }"
    >
      <div class="listen-line">
        <span class="listen-label">Reference</span>
        <span class="listen-value">{{ store.current.expected_translation }}</span>
      </div>
      <div
        v-if="lastTypedAnswer && !store.lastOutcome?.correct"
        class="listen-line"
      >
        <span class="listen-label">You typed</span>
        <span class="listen-value listen-yours">{{ lastTypedAnswer }}</span>
      </div>
    </div>
    <p
      v-if="store.locked && store.lastOutcome && !store.lastOutcome.correct && (store.current.mode === 'type' || store.current.mode === 'cloze')"
      class="reveal"
    >
      Answer:
      <strong>
        {{ store.current.mode === 'cloze' && store.current.cloze_expected
           ? store.current.cloze_expected
           : store.lastOutcome.expected }}
      </strong>
      <span v-if="store.current.kanji" class="reveal-kanji">
        ({{ store.current.kanji }})
      </span>
    </p>
    <p
      v-if="store.locked && (store.current.mode === 'type' || store.current.mode === 'cloze' || store.current.mode === 'sentence_listen') && store.lastOutcome?.feedback"
      class="feedback"
    >
      {{ store.lastOutcome.feedback }}
    </p>
    <!-- Continue button shows whenever there's something for the user to
         read on the reveal screen: any non-correct outcome (incorrect,
         timeout, gave_up) OR a correct answer that still came back with
         feedback (the "accept" grader verdict — passing, but with a
         note explaining the related word the user actually typed).
         Pristine-correct answers auto-advance and don't render this. -->
    <button
      v-if="showContinue"
      class="btn primary continue-btn"
      type="button"
      @click="store.skipDwell?.()"
    >
      Continue →
    </button>
    <p
      v-if="showContinue"
      class="hint"
    >
      Press Enter or Space to advance.
    </p>
    <div
      v-if="!store.drillMode && showContinue"
      class="quiz-actions"
    >
      <button
        class="btn ignore"
        type="button"
        title="Skip this word for good — re-enable from the Decks page"
        @click="onIgnoreClick"
      >
        Ignore word
      </button>
    </div>
    <p v-else-if="store.current.mode === 'mc'" class="hint">
      Tap a card or press 1–4. Timeout counts as wrong.
    </p>
    <p v-else-if="store.current.mode === 'cloze_choice'" class="hint">
      Pick the word that fills the blank. Tap a card or press 1–4.
    </p>
    <p v-else-if="store.current.mode === 'cloze'" class="hint">
      Fill in the blank. Either the dictionary or the conjugated form works.
    </p>
    <p v-else-if="store.current.mode === 'sentence_listen'" class="hint">
      Listen to the sentence and translate it into English. Tap Replay to hear it again.
    </p>
    <p v-else class="hint">
      Type the answer and press Enter. Typos are forgiven.
    </p>
  </section>

  <section class="panel" v-else-if="store.deckEmpty">
    <h2>No words yet</h2>
    <p class="muted">
      Upload a vocabulary CSV via <RouterLink to="/import">Import</RouterLink>
      to start studying.
    </p>
  </section>

  <section class="panel" v-else>
    <p class="muted">Loading…</p>
  </section>
</template>

<style scoped>
.meta {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  color: var(--muted);
  margin-bottom: 10px;
}
.tag {
  background: var(--panel-hi);
  padding: 3px 8px;
  border-radius: 6px;
}
.score { font-variant-numeric: tabular-nums; }
.prompt-row {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  margin: 16px 0 24px;
}
.prompt {
  font-size: clamp(32px, 6vw, 48px);
  margin: 0;
  text-align: center;
  font-weight: 600;
}
.prompt.prompt-sentence {
  font-size: clamp(20px, 3.4vw, 30px);
  font-weight: 500;
  line-height: 1.7;
}
.deck-chip {
  align-self: center;
  font-size: 12px;
  letter-spacing: 0.04em;
  color: var(--muted);
  border: 1px solid var(--border, #444);
  border-radius: 999px;
  padding: 2px 10px;
  margin-bottom: 6px;
}
.deck-banner {
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: center;
  border: 1px solid var(--border, #444);
  border-radius: 12px;
  padding: 14px 18px;
  margin-bottom: 8px;
}
.deck-banner-name {
  font-size: 20px;
  font-weight: 600;
}
.deck-banner-counts {
  display: flex;
  gap: 14px;
  color: var(--muted);
  font-size: 14px;
}
.deck-banner-sprint {
  color: var(--accent, #e8a33d);
}
.prompt-hint {
  display: block;
  font-size: clamp(13px, 2.2vw, 16px);
  font-weight: 400;
  line-height: 1.3;
  color: var(--muted);
  margin-top: 6px;
  max-width: 22em;
  margin-inline: auto;
}
.audio-btn {
  background: var(--panel-hi);
  border: 1px solid var(--border);
  border-radius: 50%;
  width: 36px;
  height: 36px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  color: var(--muted);
  transition: transform 0.1s ease, background 0.1s ease, color 0.1s ease;
}
.audio-btn:hover { color: inherit; }
.audio-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  margin-bottom: 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel-hi);
  font-size: 13px;
}
.audio-banner .btn.small {
  padding: 6px 10px;
  font-size: 13px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.audio-btn:hover { background: var(--border); }
.audio-btn:active { transform: scale(0.94); }
.grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.hint {
  color: var(--muted);
  font-size: 12px;
  margin-top: 14px;
  text-align: center;
}
.muted { color: var(--muted); }
@media (max-width: 480px) {
  .grid { grid-template-columns: 1fr; }
}

.round-bar {
  position: relative;
  height: 6px;
  background: var(--panel-hi);
  border-radius: 3px;
  overflow: hidden;
  margin-bottom: 10px;
}
.round-bar-fill {
  position: absolute;
  inset: 0 auto 0 0;
  background: linear-gradient(90deg, #4c9eff, #9b6bff);
  transition: width 0.25s ease-out;
}
.round-label {
  position: absolute;
  right: 0;
  top: 10px;
  font-size: 11px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.streak { margin-left: 6px; color: #ffb347; }

/* ---- Flame HUD (global meter) ---- */
.flame-hud {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 10px;
  margin: 4px 0 14px;
  min-height: 190px;
}
.flame-center {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  flex: 1;
}
.combo-label {
  font-size: 12px;
  font-weight: 600;
  color: #ffb347;
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.02em;
  text-shadow: 0 0 8px rgba(255, 130, 40, 0.5);
}
.fchip {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 44px;
  padding: 5px 10px;
  border-radius: 8px;
  background: var(--panel-hi);
  border: 1px solid var(--border);
  font-size: 14px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.fchip.score {
  color: var(--muted);
  gap: 4px;
  transition: color 0.25s ease, border-color 0.25s ease, box-shadow 0.25s ease,
    background 0.25s ease;
}
.fchip.score.record {
  color: #ffe9a8;
  border-color: rgba(241, 196, 15, 0.7);
  background: linear-gradient(160deg, rgba(241, 196, 15, 0.26), rgba(255, 138, 40, 0.16));
  box-shadow: 0 0 12px rgba(241, 196, 15, 0.4);
}
.record-pip {
  font-size: 11px;
  line-height: 1;
  animation: record-pip-bob 1.6s ease-in-out infinite;
}
@keyframes record-pip-bob {
  0%, 100% { transform: translateY(0) rotate(-6deg); }
  50% { transform: translateY(-2px) rotate(6deg); }
}
.fchip.mult {
  color: var(--muted);
  transition: color 0.25s ease, border-color 0.25s ease, box-shadow 0.25s ease,
    background 0.25s ease;
}
.fchip.mult.hot {
  color: #fff;
  border-color: rgba(255, 138, 40, 0.7);
  background: linear-gradient(160deg, rgba(255, 138, 40, 0.28), rgba(255, 90, 31, 0.18));
  box-shadow: 0 0 12px rgba(255, 120, 40, 0.45);
}

.skip-drill {
  padding: 8px 12px;
  font-size: 12px;
  background: transparent;
  border: 1px dashed var(--border);
  color: var(--muted);
  border-radius: 6px;
  cursor: pointer;
}
.skip-drill:hover { color: inherit; border-style: solid; }

/* Drill re-queue nudge — a bold callout that pops in on the reveal, then the
   card auto-advances (no Continue tap). Two tones: `.good` (fast, green — one
   more to lock it in) and `.slow` (amber — clear it faster, with a single
   shake). Replaces the old quiet speed-note paragraph. */
/* Answer feedback ring-flash — a quick glow around the question card. Green
   celebrates a correct answer; a subtler red marks a miss. Absolutely
   positioned over the card (pointer-events off) so it never blocks a tap or
   shifts layout; a fresh :key restarts the animation on every answer. */
.question-panel { position: relative; }
.answer-burst {
  position: absolute;
  inset: 0;
  border-radius: inherit;
  pointer-events: none;
  z-index: 4;
}
.answer-burst.good { animation: answer-burst-good 0.6s ease-out both; }
.answer-burst.bad { animation: answer-burst-bad 0.45s ease-out both; }
@keyframes answer-burst-good {
  0% {
    box-shadow: inset 0 0 0 2px rgba(46, 204, 113, 0.9), 0 0 26px rgba(46, 204, 113, 0.4);
    background: rgba(46, 204, 113, 0.1);
  }
  100% {
    box-shadow: inset 0 0 0 2px rgba(46, 204, 113, 0), 0 0 26px rgba(46, 204, 113, 0);
    background: rgba(46, 204, 113, 0);
  }
}
@keyframes answer-burst-bad {
  0% { box-shadow: inset 0 0 0 2px rgba(231, 76, 60, 0.7); }
  100% { box-shadow: inset 0 0 0 2px rgba(231, 76, 60, 0); }
}

/* "×N multiplier!" tier-up burst, overlaid on the flame. */
.mult-flash {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  display: flex;
  flex-direction: column;
  align-items: center;
  pointer-events: none;
  z-index: 3;
  text-shadow: 0 0 14px rgba(255, 150, 40, 0.7);
}
.mult-flash-x {
  font-size: 40px;
  font-weight: 800;
  line-height: 1;
  font-variant-numeric: tabular-nums;
  color: #fff7e0;
}
.mult-flash-label {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: #ffce8a;
}
.mult-pop-enter-active { animation: mult-pop-in 0.42s cubic-bezier(0.2, 1.5, 0.4, 1); }
.mult-pop-leave-active { animation: mult-pop-out 0.5s ease-in forwards; }
@keyframes mult-pop-in {
  0% { transform: translate(-50%, -50%) scale(0.3); opacity: 0; }
  60% { transform: translate(-50%, -64%) scale(1.15); opacity: 1; }
  100% { transform: translate(-50%, -58%) scale(1); opacity: 1; }
}
@keyframes mult-pop-out {
  0% { transform: translate(-50%, -58%) scale(1); opacity: 1; }
  100% { transform: translate(-50%, -100%) scale(0.85); opacity: 0; }
}

/* "New personal best" toast — a fixed gold banner that drops in from the top. */
.record-flash {
  position: fixed;
  top: 18px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 50;
  padding: 10px 18px;
  border-radius: 999px;
  font-size: 14px;
  font-weight: 700;
  letter-spacing: 0.02em;
  color: #1a1206;
  background: linear-gradient(135deg, #ffe9a8, #ffb347);
  box-shadow: 0 8px 28px rgba(241, 196, 15, 0.45), 0 0 0 1px rgba(255, 220, 120, 0.6);
  white-space: nowrap;
}
.record-pop-enter-active { animation: record-drop-in 0.5s cubic-bezier(0.2, 1.4, 0.4, 1); }
.record-pop-leave-active { animation: record-drop-out 0.45s ease-in forwards; }
@keyframes record-drop-in {
  0% { transform: translateX(-50%) translateY(-40px) scale(0.85); opacity: 0; }
  100% { transform: translateX(-50%) translateY(0) scale(1); opacity: 1; }
}
@keyframes record-drop-out {
  0% { transform: translateX(-50%) translateY(0); opacity: 1; }
  100% { transform: translateX(-50%) translateY(-24px); opacity: 0; }
}

@media (prefers-reduced-motion: reduce) {
  .answer-burst.good,
  .answer-burst.bad,
  .mult-pop-enter-active,
  .mult-pop-leave-active,
  .record-pop-enter-active,
  .record-pop-leave-active,
  .record-pip { animation: none !important; }
}

/* ---- Game banner on the summary ---- */
.game-banner {
  display: flex;
  justify-content: space-around;
  gap: 10px;
  margin: 0 0 16px;
  padding: 12px 10px;
  border: 1px solid rgba(255, 138, 40, 0.35);
  border-radius: 10px;
  background: linear-gradient(160deg, rgba(255, 138, 40, 0.1), rgba(255, 90, 31, 0.05));
}
.game-stat {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
}
.game-stat .g-icon { font-size: 16px; line-height: 1; }
.game-stat .g-value {
  font-size: 20px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: #ffce8a;
}
.game-stat .g-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--muted);
}

.miss-cleared {
  font-size: 10px;
  font-weight: 600;
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 4px;
  color: var(--muted);
  background: rgba(127, 127, 127, 0.15);
  border: 1px solid var(--border);
  font-variant-numeric: tabular-nums;
}
.miss-cleared.fast {
  color: var(--good);
  background: rgba(46, 204, 113, 0.15);
  border-color: rgba(46, 204, 113, 0.4);
}
.miss-cleared.unmastered {
  color: var(--bad);
  background: rgba(231, 76, 60, 0.14);
  border-color: rgba(231, 76, 60, 0.4);
}

.flame-blurb { font-size: 13px; margin-top: 8px; line-height: 1.45; }
.flame-blurb em { font-style: normal; color: #ffb347; font-weight: 600; }
.personal-best { margin: 12px 0 0; font-size: 13px; color: #ffce8a; }

/* Round summary */
.summary {
  text-align: center;
  animation: pop-in 0.35s ease-out;
}
@keyframes pop-in {
  from { transform: scale(0.96); opacity: 0; }
  to   { transform: scale(1);    opacity: 1; }
}
.fanfare .emoji {
  font-size: 72px;
  line-height: 1;
  margin-bottom: 4px;
  animation: bounce 0.6s ease-in-out;
}
@keyframes bounce {
  0%   { transform: translateY(0); }
  30%  { transform: translateY(-18px); }
  60%  { transform: translateY(0); }
  80%  { transform: translateY(-6px); }
  100% { transform: translateY(0); }
}
.fanfare h2 { margin: 4px 0 2px; font-size: 28px; }

.summary.gold    { border-color: #f1c40f; box-shadow: 0 0 0 1px rgba(241, 196, 15, 0.4); }
.summary.green   { border-color: var(--good); }
.summary.blue    { border-color: var(--accent); }
.summary.amber   { border-color: #d88a3a; }
.summary.neutral { }

.stat-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  margin: 20px 0;
}
.stat {
  background: var(--panel-hi);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px 6px;
}
.stat .label { font-size: 11px; color: var(--muted); }
.stat .value {
  font-size: 22px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  margin-top: 2px;
}
@media (max-width: 480px) {
  .stat-grid { grid-template-columns: repeat(2, 1fr); }
}

.missed {
  text-align: left;
  background: var(--panel-hi);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px 14px;
  margin-bottom: 16px;
}
.missed-title {
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 6px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.missed ul { list-style: none; margin: 0; padding: 0; }
.miss-row {
  border-bottom: 1px dashed var(--border);
  padding: 4px 0;
}
.miss-row:last-child { border-bottom: none; }
.miss-line {
  display: flex;
  justify-content: space-between;
  align-items: center;
  cursor: pointer;
  user-select: none;
}
.miss-line:hover .miss-toggle { color: inherit; }
.miss-toggle {
  font-size: 11px;
  color: var(--muted);
  margin-left: 8px;
  transition: transform 0.15s ease;
  display: inline-block;
}
.miss-toggle.open { transform: rotate(90deg); }
.miss-detail {
  padding: 8px 0 4px;
  text-align: left;
}
.miss-prompt { font-size: 16px; }
.miss-tag {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 4px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.miss-tag.incorrect { background: rgba(231, 76, 60, 0.2); color: var(--bad); }
.miss-tag.timeout   { background: rgba(241, 196, 15, 0.2); color: #f1c40f; }
.miss-tag.gave_up   { background: rgba(155, 107, 255, 0.2); color: #c9b3ff; }
.miss-typed {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  font-size: 13px;
  color: var(--muted);
  margin-left: 8px;
}
.miss-typed-you { text-decoration: line-through; color: var(--bad); }
.miss-typed-arrow { color: var(--muted); }
.miss-typed-expected { color: var(--good); font-weight: 500; }
.reveal {
  text-align: center;
  margin-top: 14px;
  padding: 10px 12px;
  background: rgba(231, 76, 60, 0.12);
  border: 1px solid rgba(231, 76, 60, 0.35);
  border-radius: 8px;
  font-size: 15px;
}
.reveal strong {
  font-size: clamp(20px, 3vw, 26px);
  margin-left: 6px;
}
.prompt-listen {
  font-size: clamp(20px, 4vw, 28px);
  line-height: 1.4;
  font-weight: 500;
  margin: 0;
  text-align: center;
}
.listen-reveal {
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: stretch;
  text-align: left;
  font-size: 14px;
}
.listen-reveal.ok {
  background: rgba(46, 204, 113, 0.12);
  border-color: rgba(46, 204, 113, 0.35);
}
.listen-line {
  display: flex;
  gap: 10px;
  align-items: baseline;
}
.listen-label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted);
  min-width: 78px;
}
.listen-value { flex: 1; }
.listen-yours { color: var(--bad); text-decoration: line-through; }
.feedback {
  margin-top: 8px;
  margin-left: auto;
  margin-right: auto;
  font-style: italic;
  color: var(--muted, #888);
  font-size: 14px;
  max-width: 480px;
  text-align: center;
}

.btn.primary {
  width: 100%;
  padding: 14px;
  font-size: 16px;
  font-weight: 600;
}
.btn.secondary {
  width: 100%;
  padding: 12px;
  margin-top: 8px;
  font-size: 14px;
  font-weight: 500;
  background: var(--panel-hi);
  color: inherit;
  border: 1px solid var(--border);
  cursor: pointer;
  border-radius: 8px;
}
.btn.secondary:hover { background: var(--border); }
.drill-recap { margin-top: 10px; font-size: 12px; }
.carry-note {
  margin: 4px 0 10px;
  font-size: 13px;
  color: #ffce8a;
  line-height: 1.4;
}
.carry-note strong {
  color: #fff;
  font-variant-numeric: tabular-nums;
}
.drill-tag { background: rgba(155, 107, 255, 0.25); color: #c9b3ff; }
.drill-fill { background: linear-gradient(90deg, #9b6bff, #ff79c6); }

.intro { text-align: center; }
.intro-badge {
  display: inline-block;
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #ffb347;
  background: rgba(255, 179, 71, 0.15);
  border: 1px solid rgba(255, 179, 71, 0.4);
  border-radius: 6px;
  padding: 3px 8px;
  margin-bottom: 18px;
}
.intro-kana {
  font-size: clamp(40px, 8vw, 64px);
  font-weight: 600;
  margin: 4px 0;
}
.intro-kanji {
  font-size: clamp(24px, 4vw, 32px);
  color: var(--muted);
  margin-bottom: 6px;
}
.intro-english {
  font-size: 20px;
  color: var(--text);
  margin-bottom: 20px;
}
.intro-audio { margin: 0 auto 20px; display: flex; }

.kanji-reveal {
  text-align: center;
  font-size: clamp(20px, 3vw, 28px);
  color: var(--muted);
  margin: -12px 0 18px;
  font-weight: 500;
}
.reveal-kanji {
  margin-left: 8px;
  color: var(--muted);
  font-size: 16px;
}
.reveal-dict-form {
  display: block;
  margin-top: 4px;
  color: var(--muted);
  font-size: 12px;
  font-style: italic;
}
.miss-kanji {
  margin-left: 6px;
  color: var(--muted);
  font-size: 13px;
}
.sentence-row {
  margin: 4px 0 16px;
  text-align: center;
}
.btn.ghost {
  background: transparent;
  border: 1px dashed var(--border);
  color: var(--muted);
  border-radius: 6px;
  cursor: pointer;
  padding: 6px 10px;
}
.btn.ghost:hover { color: inherit; border-style: solid; }
.btn.ghost.small { font-size: 12px; }
.btn.ghost:disabled { opacity: 0.6; cursor: progress; }
.sentence {
  margin-top: 6px;
  padding: 12px 14px;
  background: var(--panel-hi);
  border: 1px solid var(--border);
  border-radius: 8px;
  text-align: left;
}
.sentence-source {
  margin-top: 4px;
  font-size: 11px;
  color: var(--muted);
  letter-spacing: 0.02em;
}
.sentence-ja {
  font-size: 17px;
  margin-bottom: 4px;
}
.sentence-en {
  font-size: 13px;
  color: var(--muted);
}
.sentence-error {
  margin-top: 6px;
  font-size: 12px;
  color: var(--bad);
}
.mnemonic {
  margin: 6px 0;
  padding: 12px 14px;
  background: rgba(255, 179, 71, 0.08);
  border: 1px solid rgba(255, 179, 71, 0.35);
  border-left-width: 3px;
  border-radius: 8px;
  text-align: left;
}
.mnemonic-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: #ffb347;
  margin-bottom: 4px;
}
.mnemonic-body {
  font-size: 14px;
  line-height: 1.45;
  color: var(--text);
}
.intro-sentence { margin-top: 10px; }
.continue-btn {
  margin-top: 14px;
  padding: 10px;
  font-size: 14px;
  width: 100%;
}
.quiz-actions {
  margin-top: 12px;
  display: flex;
  justify-content: center;
  gap: 8px;
  flex-wrap: wrap;
}
.give-up,
.ignore {
  padding: 8px 12px;
  font-size: 12px;
  background: transparent;
  border: 1px dashed var(--border);
  color: var(--muted);
  border-radius: 6px;
  cursor: pointer;
}
.give-up:hover,
.ignore:hover { color: inherit; border-style: solid; }
.ignore:hover { color: var(--bad, #e74c3c); border-color: rgba(231, 76, 60, 0.5); }
.sentence-loading {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  margin: 6px 0;
}
.miss-leech {
  margin-left: 6px;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #d88a3a;
  background: rgba(216, 138, 58, 0.15);
  border: 1px solid rgba(216, 138, 58, 0.4);
  padding: 1px 6px;
  border-radius: 4px;
  vertical-align: middle;
}

.net-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  margin-bottom: 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel-hi);
  font-size: 13px;
}
.net-banner.error {
  border-color: rgba(231, 76, 60, 0.6);
  background: rgba(231, 76, 60, 0.12);
  justify-content: space-between;
}
.net-banner .btn.small {
  padding: 6px 10px;
  font-size: 13px;
  width: auto;
}
.spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

.start { text-align: center; }
.start h2 { margin: 4px 0 8px; font-size: 28px; }
.start .muted { font-size: 14px; }
.rounds-so-far { margin: 12px 0 4px; }
.start .btn.primary { margin-top: 20px; }

.answer-types,
.setting-group {
  margin: 18px auto 4px;
  max-width: 480px;
  text-align: left;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px 14px 14px;
}
.answer-types legend,
.setting-group legend {
  padding: 0 6px;
  font-size: 13px;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.answer-type {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 7px 4px;
  cursor: pointer;
}
.answer-type input {
  margin-top: 3px;
  width: 18px;
  height: 18px;
  flex: none;
  accent-color: var(--accent, #4c9eff);
  cursor: pointer;
}
.answer-type-text {
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.answer-type-label { font-size: 15px; }
.answer-type-hint { font-size: 12px; color: var(--muted); }
.answer-type-note {
  margin: 8px 4px 0;
  font-size: 12px;
  color: var(--muted);
  line-height: 1.4;
}

.quiz-length .length-options {
  display: flex;
  gap: 8px;
  margin: 4px 0 10px;
}
.length-option {
  flex: 1;
  padding: 10px 0;
  font-size: 16px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  background: var(--panel-hi);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.12s, border-color 0.12s, color 0.12s;
}
.length-option:hover { background: var(--border); }
.length-option.active {
  border-color: var(--accent, #4c9eff);
  background: rgba(76, 158, 255, 0.18);
  color: var(--text);
}
.reveal-toggle { padding-top: 4px; }

/* Primary actions: the two big start-a-session buttons. */
.actions {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-width: 480px;
  margin: 20px auto 6px;
}
.action-btn {
  display: flex;
  align-items: center;
  gap: 14px;
  width: 100%;
  padding: 16px 18px;
  text-align: left;
  border-radius: 12px;
  border: 1px solid var(--border);
  background: var(--panel-hi);
  color: var(--text);
  cursor: pointer;
  transition: border-color 0.12s, background 0.12s, transform 0.06s;
}
.action-btn:hover:not(:disabled) { border-color: var(--accent, #4c9eff); }
.action-btn:active:not(:disabled) { transform: translateY(1px); }
.action-btn:disabled { opacity: 0.45; cursor: not-allowed; }
.action-btn.review:not(:disabled) {
  border-color: var(--accent, #4c9eff);
  background: rgba(76, 158, 255, 0.14);
}
.action-btn.learn:not(:disabled) {
  border-color: #f0a13a;
  background: rgba(240, 161, 58, 0.12);
}
.action-icon {
  font-size: 22px;
  line-height: 1;
  flex: none;
  width: 30px;
  text-align: center;
}
.action-btn.learn .action-icon { color: #f0a13a; }
.action-text { display: flex; flex-direction: column; gap: 2px; }
.action-title { font-size: 17px; font-weight: 700; }
.action-sub { font-size: 12.5px; color: var(--muted); }
.caught-up { margin-top: 10px; }

/* Collapsible settings disclosure. */
.settings {
  max-width: 480px;
  margin: 16px auto 4px;
  text-align: left;
}
.settings > summary {
  list-style: none;
  cursor: pointer;
  padding: 11px 14px;
  border: 1px solid var(--border);
  border-radius: 10px;
  font-size: 14px;
  font-weight: 600;
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}
.settings > summary::-webkit-details-marker { display: none; }
.settings[open] > summary { border-color: var(--accent, #4c9eff); }
.settings-gear { font-size: 15px; }
.settings-summary { font-weight: 400; font-size: 12px; color: var(--muted); }

.group-hint {
  margin: 0 4px 10px;
  font-size: 12px;
  color: var(--muted);
  line-height: 1.4;
}
.sub-setting { margin: 10px 4px 4px; }
.sub-label {
  display: block;
  font-size: 12.5px;
  color: var(--muted);
  margin-bottom: 6px;
}

/* Segmented pill control (speed / lengths / reps). */
.seg {
  display: flex;
  gap: 8px;
}
.seg-btn {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  padding: 9px 0;
  font-size: 15px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  background: var(--panel-hi);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.12s, border-color 0.12s;
}
.seg-btn small { font-size: 10.5px; font-weight: 400; color: var(--muted); }
.seg-btn:hover { background: var(--border); }
.seg-btn.active {
  border-color: var(--accent, #4c9eff);
  background: rgba(76, 158, 255, 0.18);
}
.seg-btn.active small { color: var(--text); }
.seg.compact { max-width: 220px; }

.back-to-setup { margin-top: 4px; }
.match-link {
  display: block;
  text-align: center;
  text-decoration: none;
  margin: 14px auto 0;
  max-width: 480px;
}

.study-summary {
  margin: 18px auto 4px;
  max-width: 480px;
  text-align: left;
}

/* --- Due lookahead --- */
.due-schedule {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
  margin-bottom: 16px;
}
.due-cell {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  padding: 10px 4px;
  border-radius: 10px;
  background: rgba(127, 127, 127, 0.08);
}
.due-cell.on { background: rgba(74, 144, 226, 0.14); }
.due-num {
  font-size: 22px;
  font-weight: 700;
  line-height: 1;
  font-variant-numeric: tabular-nums;
}
.due-cell.on .due-num { color: var(--accent, #4a90e2); }
.due-lbl {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--muted, #888);
  text-align: center;
}

/* --- Mastery distribution --- */
.mastery { margin-bottom: 16px; }
.mastery-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 6px;
}
.mastery-title { font-size: 13px; font-weight: 600; }
.mastery-total { font-size: 11px; }
.mastery-bar {
  display: flex;
  height: 12px;
  border-radius: 6px;
  overflow: hidden;
  background: rgba(127, 127, 127, 0.12);
}
.mastery-seg { min-width: 3px; }
.mastery-seg.learning { background: #f1c40f; }
.mastery-seg.young    { background: #4c9eff; }
.mastery-seg.mature   { background: #2ecc71; }
.mastery-seg.mastered { background: #e6b800; }
.mastery-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  margin-top: 8px;
}
.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
}
.legend-dot {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  flex: 0 0 auto;
}
.legend-dot.learning { background: #f1c40f; }
.legend-dot.young    { background: #4c9eff; }
.legend-dot.mature   { background: #2ecc71; }
.legend-dot.mastered { background: #e6b800; }
.legend-label { color: var(--muted, #888); }
.legend-count { font-weight: 600; font-variant-numeric: tabular-nums; }
.legend-ease {
  font-size: 10px;
  color: var(--muted, #888);
  font-variant-numeric: tabular-nums;
}

/* --- Recent efficiency --- */
.efficiency {
  display: flex;
  gap: 20px;
  padding: 12px 14px;
  border-radius: 10px;
  background: rgba(127, 127, 127, 0.06);
  margin-bottom: 12px;
}
.eff-cell { display: flex; flex-direction: column; gap: 2px; }
.eff-num {
  font-size: 20px;
  font-weight: 700;
  line-height: 1;
  font-variant-numeric: tabular-nums;
}
.eff-num small { font-size: 12px; font-weight: 500; color: var(--muted, #888); }
.eff-lbl {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--muted, #888);
}

.deck-breakdown-link {
  display: inline-block;
  font-size: 12px;
  text-decoration: none;
}
.deck-breakdown-link:hover { color: inherit; }
</style>
