<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { playAudioUrl, unlockAudio, wordAudioUrl } from '../audio'

const props = defineProps<{ id: string }>()

type Maturity = 'new' | 'learning' | 'young' | 'mature' | 'mastered'

interface DirectionState {
  ease: number
  interval_days: number
  repetitions: number
  due_at: string | null
  introduced_at: string | null
  maturity: Maturity
  failure_streak: number
  leech: boolean
}

interface WordDetail {
  id: number
  kana: string
  english: string
  kanji: string | null
  deck_id: number
  deck_name: string
  directions: { en2ja: DirectionState; ja2en: DirectionState }
  maturity: Maturity
}

interface SentencePayload {
  japanese: string
  english: string
  mnemonic: string
}

const word = ref<WordDetail | null>(null)
const error = ref<string | null>(null)
const status = ref<string>('')
const sentence = ref<SentencePayload | null>(null)
const sentenceLoading = ref(false)
const sentenceError = ref<string | null>(null)

async function loadSentence() {
  if (!word.value) return
  sentenceLoading.value = true
  sentenceError.value = null
  try {
    const resp = await fetch(`/api/words/${word.value.id}/sentence`)
    if (!resp.ok) {
      sentenceError.value = resp.status === 503
        ? 'Sentence generation isn\'t configured.'
        : `Couldn\'t load (${resp.status}).`
      return
    }
    const data = await resp.json()
    sentence.value = {
      japanese: data.japanese,
      english: data.english,
      mnemonic: data.mnemonic || '',
    }
  } catch (e) {
    sentenceError.value = (e as Error).message
  } finally {
    sentenceLoading.value = false
  }
}

async function load() {
  error.value = null
  try {
    const resp = await fetch('/api/stats/detailed')
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const data = await resp.json()
    const wantId = Number(props.id)
    const found = (data.words ?? []).find((w: WordDetail) => w.id === wantId)
    if (!found) throw new Error('word not found')
    word.value = found
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

async function play() {
  if (!word.value) return
  // Run unlock + play synchronously inside the click. Web Audio context
  // resume must happen in the gesture; the actual decode/play can await.
  void unlockAudio()
  status.value = `loading ${word.value.kana}…`
  try {
    await playAudioUrl(wordAudioUrl(word.value.id))
    status.value = `played ${word.value.kana}`
  } catch (e) {
    status.value = `failed: ${(e as Error).name}: ${(e as Error).message}`
  }
}

function intervalLabel(d: DirectionState): string {
  if (d.maturity === 'new') return 'not yet shown'
  const days = d.interval_days
  if (days < 1) return `${Math.round(days * 24 * 60)}m`
  if (days < 7) return `${days.toFixed(1)}d`
  return `${Math.round(days)}d`
}

function anyLeech(w: WordDetail): boolean {
  return w.directions.en2ja.leech || w.directions.ja2en.leech
}

function maxFailureStreak(w: WordDetail): number {
  return Math.max(
    w.directions.en2ja.failure_streak,
    w.directions.ja2en.failure_streak,
  )
}

function fmtDate(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString()
}

onMounted(load)
</script>

<template>
  <section v-if="error" class="panel error">{{ error }}</section>

  <section v-else-if="word" class="panel">
    <RouterLink to="/stats" class="back">← Stats</RouterLink>
    <div class="deck-line muted">Deck: {{ word.deck_name }}</div>
    <div class="card-head" :class="word.maturity">
      <div v-if="anyLeech(word)" class="leech-badge">
        leech · {{ maxFailureStreak(word) }} misses in a row
      </div>
      <div class="kana">{{ word.kana }}</div>
      <div v-if="word.kanji" class="kanji">{{ word.kanji }}</div>
      <div class="english">{{ word.english }}</div>
      <button class="play" type="button" aria-label="Play pronunciation" @click="play">
        <svg viewBox="0 0 24 24" width="28" height="28" aria-hidden="true">
          <path
            fill="currentColor"
            d="M3 10v4h4l5 5V5L7 10H3zm13.5 2a4.5 4.5 0 0 0-2.5-4.03v8.05a4.5 4.5 0 0 0 2.5-4.02zM14 3.23v2.06a7 7 0 0 1 0 13.42v2.06a9 9 0 0 0 0-17.54z"
          />
        </svg>
        Play
      </button>
      <p v-if="status" class="status">{{ status }}</p>
    </div>

    <div class="sentence-block">
      <button
        v-if="!sentence && !sentenceLoading && !sentenceError"
        class="btn ghost small"
        type="button"
        @click="loadSentence"
      >Show example & mnemonic</button>
      <p v-if="sentenceLoading" class="muted">Loading…</p>
      <p v-if="sentenceError" class="error">{{ sentenceError }}</p>
      <div v-if="sentence?.mnemonic" class="mnemonic">
        <div class="mnemonic-label">Memory hook</div>
        <div class="mnemonic-body">{{ sentence.mnemonic }}</div>
      </div>
      <div v-if="sentence?.japanese" class="sentence">
        <div class="sentence-ja">{{ sentence.japanese }}</div>
        <div class="sentence-en">{{ sentence.english }}</div>
      </div>
    </div>

    <div class="dir-grid">
      <div class="dir-col">
        <h3 class="dir-head">English → Japanese</h3>
        <dl class="srs">
          <div><dt>Maturity</dt><dd>{{ word.directions.en2ja.maturity }}</dd></div>
          <div><dt>Interval</dt><dd>{{ intervalLabel(word.directions.en2ja) }}</dd></div>
          <div><dt>Ease</dt><dd>{{ word.directions.en2ja.ease.toFixed(2) }}</dd></div>
          <div><dt>Reps</dt><dd>{{ word.directions.en2ja.repetitions }}</dd></div>
          <div><dt>Due</dt><dd>{{ fmtDate(word.directions.en2ja.due_at) }}</dd></div>
          <div><dt>Introduced</dt><dd>{{ fmtDate(word.directions.en2ja.introduced_at) }}</dd></div>
          <div><dt>Failure streak</dt><dd>{{ word.directions.en2ja.failure_streak }}</dd></div>
          <div><dt>Leech</dt><dd>{{ word.directions.en2ja.leech ? 'yes' : 'no' }}</dd></div>
        </dl>
      </div>
      <div class="dir-col">
        <h3 class="dir-head">Japanese → English</h3>
        <dl class="srs">
          <div><dt>Maturity</dt><dd>{{ word.directions.ja2en.maturity }}</dd></div>
          <div><dt>Interval</dt><dd>{{ intervalLabel(word.directions.ja2en) }}</dd></div>
          <div><dt>Ease</dt><dd>{{ word.directions.ja2en.ease.toFixed(2) }}</dd></div>
          <div><dt>Reps</dt><dd>{{ word.directions.ja2en.repetitions }}</dd></div>
          <div><dt>Due</dt><dd>{{ fmtDate(word.directions.ja2en.due_at) }}</dd></div>
          <div><dt>Introduced</dt><dd>{{ fmtDate(word.directions.ja2en.introduced_at) }}</dd></div>
          <div><dt>Failure streak</dt><dd>{{ word.directions.ja2en.failure_streak }}</dd></div>
          <div><dt>Leech</dt><dd>{{ word.directions.ja2en.leech ? 'yes' : 'no' }}</dd></div>
        </dl>
      </div>
    </div>
  </section>

  <section v-else class="panel"><p class="muted">Loading…</p></section>
</template>

<style scoped>
.error { color: var(--bad); }
.muted { color: var(--muted); }
.back {
  display: inline-block;
  font-size: 13px;
  color: var(--muted);
  text-decoration: none;
  margin-bottom: 12px;
}
.back:hover { color: inherit; }

.card-head {
  text-align: center;
  padding: 24px 12px;
  border: 1px solid var(--border);
  border-left-width: 4px;
  border-radius: 12px;
  background: var(--panel-hi);
}
.card-head.new       { border-left-color: #6c7280; opacity: 0.85; }
.card-head.learning  { border-left-color: #f1c40f; }
.card-head.young     { border-left-color: #4c9eff; }
.card-head.mature    { border-left-color: #2ecc71; }
.card-head.mastered  { border-left-color: #e6b800; }

.leech-badge {
  display: inline-block;
  margin-bottom: 12px;
  padding: 4px 10px;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #d88a3a;
  background: rgba(216, 138, 58, 0.15);
  border: 1px solid rgba(216, 138, 58, 0.4);
  border-radius: 6px;
}
.kana { font-size: clamp(40px, 9vw, 64px); font-weight: 700; }
.kanji { font-size: 22px; color: var(--muted); margin-top: 4px; }
.english { font-size: 18px; margin-top: 8px; }

.play {
  margin-top: 18px;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 12px 20px;
  font-size: 16px;
  font-weight: 600;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--panel);
  color: inherit;
  cursor: pointer;
}
.play:active { transform: scale(0.97); }

.status {
  margin: 10px 0 0;
  font-size: 12px;
  color: var(--muted);
  font-family: ui-monospace, monospace;
}

.deck-line {
  font-size: 13px;
  margin-bottom: 10px;
}
.dir-grid {
  margin-top: 16px;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
@media (max-width: 560px) {
  .dir-grid { grid-template-columns: 1fr; }
}
.dir-head {
  margin: 0 0 6px;
  font-size: 13px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted);
  font-weight: 500;
}
.srs {
  margin: 0;
  display: grid;
  grid-template-columns: 1fr;
  gap: 4px 12px;
}
.srs > div {
  display: flex;
  justify-content: space-between;
  padding: 6px 0;
  border-bottom: 1px dashed var(--border);
  font-size: 14px;
}
.srs dt { color: var(--muted); margin: 0; }
.srs dd { margin: 0; font-variant-numeric: tabular-nums; }

.sentence-block { margin: 16px 0 0; }
.sentence-block .btn.ghost {
  background: transparent;
  border: 1px dashed var(--border);
  color: var(--muted);
  border-radius: 6px;
  cursor: pointer;
  padding: 6px 10px;
  font-size: 12px;
}
.sentence-block .btn.ghost:hover { color: inherit; border-style: solid; }
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
.mnemonic-body { font-size: 14px; line-height: 1.45; }
.sentence {
  margin-top: 6px;
  padding: 12px 14px;
  background: var(--panel-hi);
  border: 1px solid var(--border);
  border-radius: 8px;
  text-align: left;
}
.sentence-ja { font-size: 17px; margin-bottom: 4px; }
.sentence-en { font-size: 13px; color: var(--muted); }
</style>
