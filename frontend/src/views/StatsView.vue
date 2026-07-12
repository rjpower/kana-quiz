<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

interface Stats {
  total_words: number
  introduced: number
  due_now: number
  mastered: number
  reviews_last_7_days: number
  accuracy_last_7_days: number | null
}

interface LatencyBucket {
  lower_ms: number
  upper_ms: number | null
  count: number
}

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

interface WordState {
  id: number
  kana: string
  english: string
  kanji: string | null
  deck_id: number
  deck_name: string
  directions: { en2ja: DirectionState; ja2en: DirectionState }
  maturity: Maturity
}

interface DetailedStats {
  summary: Stats
  latency_histogram: LatencyBucket[]
  median_latency_ms: number | null
  words: WordState[]
  maturity_counts: Record<Maturity, number>
}

const data = ref<DetailedStats | null>(null)
const error = ref<string | null>(null)
const filter = ref<Maturity | 'all'>('all')

async function load() {
  error.value = null
  try {
    const resp = await fetch('/api/stats/detailed')
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    data.value = (await resp.json()) as DetailedStats
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

onMounted(load)

function pctLabel(v: number | null): string {
  if (v === null) return '—'
  return (v * 100).toFixed(1) + '%'
}

const maxBucket = computed(() => {
  const hist = data.value?.latency_histogram ?? []
  return Math.max(1, ...hist.map((b) => b.count))
})

function bucketLabel(b: LatencyBucket): string {
  if (b.upper_ms === null) return `${(b.lower_ms / 1000).toFixed(1)}s+`
  return `${(b.lower_ms / 1000).toFixed(1)}–${(b.upper_ms / 1000).toFixed(1)}s`
}

const filteredWords = computed(() => {
  const words = data.value?.words ?? []
  if (filter.value === 'all') return words
  return words.filter((w) => w.maturity === filter.value)
})

const maturityOrder: Maturity[] = ['new', 'learning', 'young', 'mature', 'mastered']

function intervalLabel(w: WordState): string {
  if (w.maturity === 'new') return 'not yet shown'
  // Show the larger interval of the two directions as the "where this
  // word is" indicator on the tile.
  const d = Math.max(
    w.directions.en2ja.interval_days,
    w.directions.ja2en.interval_days,
  )
  if (d < 1) return `${Math.round(d * 24 * 60)}m`
  if (d < 7) return `${d.toFixed(1)}d`
  return `${Math.round(d)}d`
}

function maturityShort(m: Maturity): string {
  return m === 'mastered' ? 'M' : m[0].toUpperCase()
}
</script>

<template>
  <section v-if="data" class="panel">
    <h2>Progress</h2>
    <div class="grid">
      <div class="cell">
        <div class="label">Total words</div>
        <div class="value">{{ data.summary.total_words }}</div>
      </div>
      <div class="cell">
        <div class="label">Introduced</div>
        <div class="value">{{ data.summary.introduced }}</div>
      </div>
      <div class="cell">
        <div class="label">Due now</div>
        <div class="value">{{ data.summary.due_now }}</div>
      </div>
      <div class="cell">
        <div class="label">Mastered (≥21d)</div>
        <div class="value">{{ data.summary.mastered }}</div>
      </div>
      <div class="cell">
        <div class="label">Reviews (7d)</div>
        <div class="value">{{ data.summary.reviews_last_7_days }}</div>
      </div>
      <div class="cell">
        <div class="label">Accuracy (7d)</div>
        <div class="value">{{ pctLabel(data.summary.accuracy_last_7_days) }}</div>
      </div>
    </div>
  </section>

  <section v-if="data" class="panel">
    <div class="section-head">
      <h2>Response times</h2>
      <span v-if="data.median_latency_ms !== null" class="muted">
        median {{ (data.median_latency_ms / 1000).toFixed(2) }}s
      </span>
    </div>
    <div v-if="data.median_latency_ms === null" class="muted">
      No correct answers recorded yet.
    </div>
    <div v-else class="histogram">
      <div v-for="(b, i) in data.latency_histogram" :key="i" class="hist-row">
        <div class="hist-label">{{ bucketLabel(b) }}</div>
        <div class="hist-track">
          <div
            class="hist-fill"
            :style="{ width: (b.count / maxBucket) * 100 + '%' }"
          ></div>
        </div>
        <div class="hist-count">{{ b.count }}</div>
      </div>
    </div>
  </section>

  <section v-if="data" class="panel">
    <div class="section-head">
      <h2>Word states</h2>
    </div>
    <div class="maturity-legend">
      <button
        class="chip"
        :class="{ active: filter === 'all' }"
        @click="filter = 'all'"
      >
        all ({{ data.words.length }})
      </button>
      <button
        v-for="m in maturityOrder"
        :key="m"
        class="chip"
        :class="[m, { active: filter === m }]"
        @click="filter = m"
      >
        {{ m }} ({{ data.maturity_counts[m] }})
      </button>
    </div>
    <div class="word-grid">
      <RouterLink
        v-for="w in filteredWords"
        :key="w.id"
        :to="`/word/${w.id}`"
        class="word-tile"
        :class="w.maturity"
        :title="`${w.english}${w.kanji ? ' · ' + w.kanji : ''} · ${w.deck_name} · en→ja ${w.directions.en2ja.maturity} · ja→en ${w.directions.ja2en.maturity}`"
      >
        <div class="w-kana">{{ w.kana }}</div>
        <div class="w-badges">
          <span class="dir-badge" :class="w.directions.en2ja.maturity" title="English → Japanese">E{{ maturityShort(w.directions.en2ja.maturity) }}</span>
          <span class="dir-badge" :class="w.directions.ja2en.maturity" title="Japanese → English">J{{ maturityShort(w.directions.ja2en.maturity) }}</span>
        </div>
        <div class="w-meta">{{ intervalLabel(w) }}</div>
      </RouterLink>
      <div v-if="filteredWords.length === 0" class="muted empty">
        No words in this bucket.
      </div>
    </div>
  </section>

  <section class="panel" v-if="error">
    <p class="err">{{ error }}</p>
  </section>
  <section class="panel" v-else-if="!data">
    <p class="muted">Loading…</p>
  </section>
</template>

<style scoped>
.panel + .panel { margin-top: 16px; }
.section-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 12px;
}
.section-head h2 { margin: 0; }
.muted { color: var(--muted); font-size: 13px; }
.err { color: var(--bad); }

.grid {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 12px;
  margin: 16px 0 4px;
}
.cell {
  background: var(--panel-hi);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px;
}
.label { color: var(--muted); font-size: 12px; }
.value {
  font-size: 28px;
  font-weight: 600;
  margin-top: 4px;
  font-variant-numeric: tabular-nums;
}
@media (max-width: 560px) {
  .grid { grid-template-columns: 1fr 1fr; }
}

/* Histogram */
.histogram { display: flex; flex-direction: column; gap: 4px; }
.hist-row {
  display: grid;
  grid-template-columns: 72px 1fr 36px;
  align-items: center;
  gap: 10px;
}
.hist-label {
  font-size: 12px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
  text-align: right;
}
.hist-track {
  height: 14px;
  background: var(--panel-hi);
  border-radius: 4px;
  overflow: hidden;
}
.hist-fill {
  height: 100%;
  background: linear-gradient(90deg, #4c9eff, #9b6bff);
  transition: width 0.25s ease-out;
}
.hist-count {
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  text-align: left;
}

/* Maturity chips + word grid */
.maturity-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 12px;
}
.chip {
  background: var(--panel-hi);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 4px 10px;
  font-size: 12px;
  cursor: pointer;
}
.chip.active { outline: 2px solid var(--accent); }
.chip.new       { border-left: 4px solid #6c7280; }
.chip.learning  { border-left: 4px solid #f1c40f; }
.chip.young     { border-left: 4px solid #4c9eff; }
.chip.mature    { border-left: 4px solid #2ecc71; }
.chip.mastered  { border-left: 4px solid #e6b800; }

.word-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(96px, 1fr));
  gap: 8px;
}
.word-tile {
  background: var(--panel-hi);
  border: 1px solid var(--border);
  border-left-width: 4px;
  border-radius: 8px;
  padding: 8px 10px;
  min-height: 60px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  text-decoration: none;
  color: inherit;
}
.word-tile:hover { background: var(--border); }
.word-tile.new       { border-left-color: #6c7280; opacity: 0.65; }
.word-tile.learning  { border-left-color: #f1c40f; }
.word-tile.young     { border-left-color: #4c9eff; }
.word-tile.mature    { border-left-color: #2ecc71; }
.word-tile.mastered  { border-left-color: #e6b800; background: rgba(230, 184, 0, 0.08); }
.w-kana {
  font-size: 18px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.w-meta {
  font-size: 11px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.w-badges {
  display: flex;
  gap: 3px;
  margin: 2px 0;
}
.dir-badge {
  font-size: 9px;
  font-weight: 600;
  padding: 1px 4px;
  border-radius: 3px;
  background: var(--panel);
  border: 1px solid var(--border);
  letter-spacing: 0.04em;
}
.dir-badge.new       { color: #6c7280; }
.dir-badge.learning  { color: #f1c40f; border-color: rgba(241, 196, 15, 0.5); }
.dir-badge.young     { color: #4c9eff; border-color: rgba(76, 158, 255, 0.5); }
.dir-badge.mature    { color: #2ecc71; border-color: rgba(46, 204, 113, 0.5); }
.dir-badge.mastered  { color: #e6b800; border-color: rgba(230, 184, 0, 0.5); }
.empty { grid-column: 1 / -1; padding: 12px 0; }
</style>
