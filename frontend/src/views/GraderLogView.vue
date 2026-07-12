<script setup lang="ts">
import { onMounted, ref } from 'vue'

interface GraderEntry {
  id: number
  created_at: string
  model: string
  latency_ms: number
  word_id: number
  word_kanji: string | null
  word_kana: string | null
  word_english: string | null
  direction: string
  typed_answer: string
  cloze_sentence: string | null
  cloze_target_form: string | null
  verdict: string | null
  explanation: string | null
  alternates: string[]
  clarified_gloss: string | null
  error: string | null
  final_correct: boolean
}

const entries = ref<GraderEntry[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const limit = ref(200)

async function load() {
  loading.value = true
  error.value = null
  try {
    const resp = await fetch(`/api/gemini_log?limit=${limit.value}`)
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const body = await resp.json()
    entries.value = body.entries as GraderEntry[]
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)

function fmtTime(iso: string): string {
  try {
    const d = new Date(iso)
    return d.toLocaleString()
  } catch {
    return iso
  }
}

function verdictClass(e: GraderEntry): string {
  if (e.error) return 'v-error'
  if (e.verdict === 'correct') return 'v-correct'
  if (e.verdict === 'accept') return 'v-accept'
  if (e.verdict === 'incorrect') return 'v-incorrect'
  return 'v-unknown'
}

// True when Gemini's verdict and the user-visible outcome disagree —
// the "outcome diff" the log exists to make visible.
function isDiff(e: GraderEntry): boolean {
  if (e.error) return false
  const passed = e.verdict === 'correct' || e.verdict === 'accept'
  return passed !== e.final_correct
}
</script>

<template>
  <section class="panel">
    <div class="header">
      <h2>Grader log</h2>
      <div class="controls">
        <label class="muted">
          Limit
          <select v-model.number="limit" @change="load">
            <option :value="50">50</option>
            <option :value="200">200</option>
            <option :value="500">500</option>
            <option :value="1000">1000</option>
          </select>
        </label>
        <button class="btn ghost" type="button" @click="load">Refresh</button>
      </div>
    </div>
    <p class="muted hint">
      Each row is one call to the LLM semantic grader — the path a typed answer
      takes only after the deterministic grader rejects it. Verdict colour and
      the dimmed "→ correct/wrong" tag show what Gemini said vs. the outcome the
      user actually saw.
    </p>
  </section>

  <section v-if="error" class="panel"><p class="err">{{ error }}</p></section>
  <section v-else-if="loading" class="panel"><p class="muted">Loading…</p></section>
  <section v-else-if="entries.length === 0" class="panel">
    <p class="muted">No grader calls logged yet.</p>
  </section>

  <div v-else class="log">
    <section
      v-for="e in entries"
      :key="e.id"
      class="panel entry"
      :class="{ diff: isDiff(e) }"
    >
      <div class="row top">
        <span class="verdict" :class="verdictClass(e)">
          {{ e.error ? 'error' : (e.verdict ?? 'n/a') }}
        </span>
        <span class="outcome muted">
          → {{ e.final_correct ? 'correct' : 'wrong' }}
        </span>
        <span class="word">
          <RouterLink :to="`/word/${e.word_id}`" class="word-link">
            {{ e.word_kanji || e.word_kana || `#${e.word_id}` }}
          </RouterLink>
          <span class="muted small">
            <template v-if="e.word_kanji && e.word_kana"> ({{ e.word_kana }})</template>
            — {{ e.word_english ?? '?' }}
          </span>
        </span>
        <span class="meta muted small">
          {{ e.direction }} · {{ e.latency_ms }} ms · {{ fmtTime(e.created_at) }}
        </span>
      </div>
      <div class="row body">
        <div class="kv">
          <span class="k">typed</span>
          <span class="v code">{{ e.typed_answer }}</span>
        </div>
        <div v-if="e.cloze_target_form" class="kv">
          <span class="k">cloze</span>
          <span class="v code">{{ e.cloze_sentence }} <span class="muted">[{{ e.cloze_target_form }}]</span></span>
        </div>
        <div v-if="e.explanation" class="kv">
          <span class="k">why</span>
          <span class="v">{{ e.explanation }}</span>
        </div>
        <div v-if="e.alternates.length" class="kv">
          <span class="k">alternates</span>
          <span class="v code">{{ e.alternates.join(', ') }}</span>
        </div>
        <div v-if="e.clarified_gloss" class="kv">
          <span class="k">clarified gloss</span>
          <span class="v">{{ e.clarified_gloss }}</span>
        </div>
        <div v-if="e.error" class="kv">
          <span class="k">error</span>
          <span class="v err">{{ e.error }}</span>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.panel + .panel,
.entry + .entry { margin-top: 10px; }
.muted { color: var(--muted); }
.err { color: var(--bad); }
.small { font-size: 12px; }
.hint { margin: 6px 0 0; }

.header { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
h2 { margin: 0; }
.controls { display: flex; align-items: center; gap: 10px; }
.controls select {
  background: var(--panel-hi);
  color: inherit;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 4px 6px;
  font: inherit;
  margin-left: 6px;
}

.btn {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 6px 12px;
  font-size: 13px;
  cursor: pointer;
  background: var(--panel-hi);
  color: inherit;
}
.btn.ghost { background: transparent; color: var(--muted); }
.btn.ghost:hover { color: inherit; }

.log { display: flex; flex-direction: column; gap: 0; margin-top: 12px; }
.entry { padding: 10px 14px; }
.entry.diff { border-left: 3px solid var(--bad, #e74c3c); }

.row { display: flex; gap: 10px; flex-wrap: wrap; align-items: baseline; }
.row.top { font-size: 13px; }
.row.body { flex-direction: column; gap: 4px; margin-top: 6px; }

.verdict {
  display: inline-block;
  font-weight: 600;
  font-size: 11px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  padding: 2px 7px;
  border-radius: 4px;
  border: 1px solid var(--border);
}
.v-correct { color: #2ecc71; border-color: rgba(46, 204, 113, 0.4); }
.v-accept  { color: #f1c40f; border-color: rgba(241, 196, 15, 0.4); }
.v-incorrect { color: var(--bad, #e74c3c); border-color: rgba(231, 76, 60, 0.4); }
.v-error { color: #999; border-color: var(--border); }
.v-unknown { color: var(--muted); }

.outcome { font-size: 12px; }
.word { font-size: 14px; }
.word-link { font-weight: 600; color: inherit; text-decoration: none; border-bottom: 1px dashed var(--border); }
.word-link:hover { border-bottom-color: var(--accent); }

.meta { margin-left: auto; }

.kv { display: grid; grid-template-columns: 110px 1fr; gap: 8px; align-items: baseline; }
.kv .k { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; }
.kv .v { font-size: 13px; word-break: break-word; }
.kv .code { font-family: ui-monospace, monospace; font-size: 12px; }

@media (max-width: 560px) {
  .kv { grid-template-columns: 1fr; gap: 0; }
  .meta { margin-left: 0; }
}
</style>
