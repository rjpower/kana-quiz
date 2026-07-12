<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

interface ImportReport {
  inserted: number
  updated: number
  skipped: number
}

interface Deck {
  id: number
  name: string
  level: number
  created_at: string
  word_count: number
  new_count: number
  due_count: number
}

const file = ref<File | null>(null)
const busy = ref(false)
const report = ref<ImportReport | null>(null)
const error = ref<string | null>(null)
const dragOver = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

const decks = ref<Deck[]>([])
const decksLoading = ref(true)
// '' = "create new"; otherwise stringified deck id.
const selectedDeck = ref<string>('')
const newDeckName = ref('')
const newDeckLevel = ref(5)

async function loadDecks() {
  decksLoading.value = true
  try {
    const resp = await fetch('/api/decks')
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    decks.value = (await resp.json()) as Deck[]
    if (decks.value.length > 0 && !selectedDeck.value) {
      selectedDeck.value = String(decks.value[0].id)
    }
  } catch (e) {
    error.value = `Couldn't load decks: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    decksLoading.value = false
  }
}

onMounted(loadDecks)

const creatingNewDeck = computed(() => selectedDeck.value === '__new__')

const canSubmit = computed(() => {
  if (!file.value || busy.value) return false
  if (creatingNewDeck.value) return newDeckName.value.trim().length > 0
  return selectedDeck.value !== ''
})

function accept(f: File | null) {
  if (!f) return
  if (!/\.csv$/i.test(f.name) && f.type && !f.type.includes('csv')) {
    error.value = `"${f.name}" doesn't look like a CSV.`
    return
  }
  file.value = f
  report.value = null
  error.value = null
}

function onFileChange(e: Event) {
  const target = e.target as HTMLInputElement
  accept(target.files?.[0] ?? null)
}

function onDrop(e: DragEvent) {
  e.preventDefault()
  dragOver.value = false
  accept(e.dataTransfer?.files?.[0] ?? null)
}

function onDragOver(e: DragEvent) {
  e.preventDefault()
  dragOver.value = true
}

function onDragLeave() {
  dragOver.value = false
}

function openPicker() {
  fileInput.value?.click()
}

function clearFile() {
  file.value = null
  report.value = null
  error.value = null
  if (fileInput.value) fileInput.value.value = ''
}

const sizeLabel = computed(() => {
  if (!file.value) return ''
  const kb = file.value.size / 1024
  if (kb < 1024) return `${kb.toFixed(1)} KB`
  return `${(kb / 1024).toFixed(2)} MB`
})

async function submit() {
  if (!file.value) return
  if (!canSubmit.value) return
  busy.value = true
  error.value = null
  report.value = null
  try {
    const body = new FormData()
    body.append('file', file.value)
    if (creatingNewDeck.value) {
      body.append('new_deck_name', newDeckName.value.trim())
      body.append('new_deck_level', String(newDeckLevel.value))
    } else {
      body.append('deck_id', selectedDeck.value)
    }
    const resp = await fetch('/api/import', { method: 'POST', body })
    if (!resp.ok) {
      const detail = await resp.json().catch(() => ({ detail: resp.statusText }))
      throw new Error(detail.detail ?? `HTTP ${resp.status}`)
    }
    report.value = (await resp.json()) as ImportReport
    // Refresh deck stats so word_count reflects the import.
    void loadDecks()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="panel">
    <h2>Import vocabulary</h2>

    <div class="deck-picker">
      <label class="dp-label">Deck</label>
      <div v-if="decksLoading" class="muted small">Loading decks…</div>
      <select v-else v-model="selectedDeck" class="dp-select">
        <option v-for="d in decks" :key="d.id" :value="String(d.id)">
          {{ d.name }} (level {{ d.level }}) · {{ d.word_count }} words
        </option>
        <option value="__new__">+ Add new deck…</option>
      </select>
      <div v-if="creatingNewDeck" class="dp-new">
        <input
          v-model="newDeckName"
          class="dp-input"
          type="text"
          placeholder="Deck name"
        />
        <label class="dp-level">
          Level
          <input
            v-model.number="newDeckLevel"
            class="dp-input small"
            type="number"
            min="1"
            max="20"
          />
        </label>
      </div>
    </div>

    <div
      class="dropzone"
      :class="{ over: dragOver, filled: !!file, busy }"
      role="button"
      tabindex="0"
      @click="openPicker"
      @keydown.enter.prevent="openPicker"
      @keydown.space.prevent="openPicker"
      @dragover="onDragOver"
      @dragenter="onDragOver"
      @dragleave="onDragLeave"
      @drop="onDrop"
    >
      <input
        ref="fileInput"
        class="file-input"
        type="file"
        accept=".csv,text/csv"
        @change="onFileChange"
      />
      <div v-if="!file" class="dz-idle">
        <div class="dz-icon">📄</div>
        <div class="dz-title">
          Drop a CSV here
          <span class="muted">or click to choose</span>
        </div>
        <div class="dz-hint">.csv · UTF-8</div>
      </div>
      <div v-else class="dz-file">
        <div class="dz-icon ok">✓</div>
        <div class="dz-file-meta">
          <div class="dz-file-name">{{ file.name }}</div>
          <div class="dz-file-sub">{{ sizeLabel }}</div>
        </div>
        <button class="dz-clear" type="button" @click.stop="clearFile">
          Clear
        </button>
      </div>
    </div>

    <div class="actions">
      <button class="btn primary" :disabled="!canSubmit" @click="submit">
        {{ busy ? 'Uploading…' : file ? 'Upload' : 'Choose a file' }}
      </button>
    </div>

    <div v-if="report" class="result ok">
      <div class="result-head">Import complete</div>
      <div class="result-stats">
        <span><strong>{{ report.inserted }}</strong> inserted</span>
        <span><strong>{{ report.updated }}</strong> updated</span>
        <span><strong>{{ report.skipped }}</strong> skipped</span>
      </div>
    </div>
    <div v-if="error" class="result err">{{ error }}</div>

    <details class="csv-help">
      <summary>CSV format</summary>
      <p>
        Pick an existing deck above or choose "Add new deck…" to create one
        in the same step. Header row required. <code>kana</code> and
        <code>english</code> are the only required columns; rows missing
        either are skipped. Re-uploading into the same deck updates existing
        rows keyed by <code>kana</code>.
      </p>
      <table class="cols">
        <thead>
          <tr><th>column</th><th>required</th><th>notes</th></tr>
        </thead>
        <tbody>
          <tr><td><code>kana</code></td><td>yes</td><td>primary display form (unique key)</td></tr>
          <tr><td><code>english</code></td><td>yes</td><td>English gloss</td></tr>
          <tr><td><code>kanji</code></td><td>no</td><td>optional secondary display</td></tr>
          <tr><td><code>tags</code></td><td>no</td><td>comma-separated; used to group distractors</td></tr>
          <tr><td><code>interval_days</code></td><td>no</td><td>seed SRS interval (from Anki export)</td></tr>
          <tr><td><code>ease</code></td><td>no</td><td>seed SRS ease factor</td></tr>
          <tr><td><code>repetitions</code></td><td>no</td><td>seed consecutive-correct count</td></tr>
          <tr><td><code>due_at</code></td><td>no</td><td>ISO-8601; auto-stamped when seeding interval</td></tr>
          <tr><td><code>introduced_at</code></td><td>no</td><td>ISO-8601; auto-stamped when interval &gt; 0</td></tr>
        </tbody>
      </table>
    </details>
  </section>
</template>

<style scoped>
h2 { margin: 0 0 14px; }
.muted { color: var(--muted); }
.small { font-size: 12px; }

.deck-picker {
  margin-bottom: 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.dp-label {
  font-size: 12px;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.dp-select,
.dp-input {
  background: var(--panel-hi);
  color: inherit;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 14px;
  font-family: inherit;
}
.dp-select { width: 100%; }
.dp-input.small { width: 80px; padding: 6px 8px; font-size: 13px; }
.dp-new {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-top: 4px;
}
.dp-new .dp-input { flex: 1; }
.dp-level {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--muted);
}

.dropzone {
  position: relative;
  border: 2px dashed var(--border);
  border-radius: 14px;
  background: var(--panel-hi);
  padding: 28px 20px;
  text-align: center;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s, transform 0.1s;
  outline: none;
}
.dropzone:hover,
.dropzone:focus-visible {
  border-color: var(--accent);
  background: rgba(76, 158, 255, 0.06);
}
.dropzone.over {
  border-color: var(--accent);
  background: rgba(76, 158, 255, 0.12);
  transform: scale(1.01);
}
.dropzone.filled {
  border-style: solid;
  border-color: var(--good);
  background: rgba(46, 204, 113, 0.08);
}
.dropzone.busy { cursor: progress; opacity: 0.8; }

.file-input {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  border: 0;
}

.dz-idle { display: flex; flex-direction: column; gap: 4px; }
.dz-icon {
  font-size: 40px;
  line-height: 1;
  margin-bottom: 4px;
}
.dz-icon.ok {
  color: var(--good);
  font-size: 28px;
}
.dz-title { font-size: 16px; }
.dz-title .muted { margin-left: 4px; font-size: 14px; }
.dz-hint { font-size: 12px; color: var(--muted); margin-top: 2px; }

.dz-file {
  display: flex;
  align-items: center;
  gap: 14px;
  text-align: left;
}
.dz-file-meta { flex: 1; min-width: 0; }
.dz-file-name {
  font-size: 15px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dz-file-sub { font-size: 12px; color: var(--muted); }
.dz-clear {
  background: transparent;
  color: var(--muted);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 12px;
  cursor: pointer;
}
.dz-clear:hover { color: var(--text); border-color: var(--muted); }

.actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 14px;
}
.btn.primary {
  padding: 10px 20px;
  font-weight: 600;
}

.result {
  margin-top: 14px;
  padding: 12px 14px;
  border-radius: 10px;
  font-size: 14px;
}
.result.ok {
  background: rgba(46, 204, 113, 0.12);
  color: var(--good);
  border: 1px solid rgba(46, 204, 113, 0.3);
}
.result.err {
  background: rgba(231, 76, 60, 0.12);
  color: var(--bad);
  border: 1px solid rgba(231, 76, 60, 0.3);
}
.result-head { font-weight: 600; margin-bottom: 4px; }
.result-stats {
  display: flex;
  gap: 18px;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.result-stats strong { font-weight: 700; }

.csv-help {
  margin-top: 20px;
  border-top: 1px solid var(--border);
  padding-top: 12px;
}
.csv-help summary {
  cursor: pointer;
  color: var(--muted);
  font-size: 13px;
  user-select: none;
}
.csv-help summary:hover { color: var(--text); }
.csv-help p {
  margin: 10px 0;
  font-size: 13px;
  color: var(--muted);
}
.cols {
  width: 100%;
  font-size: 12px;
  border-collapse: collapse;
  margin-top: 8px;
}
.cols th, .cols td {
  text-align: left;
  padding: 5px 8px;
  border-bottom: 1px solid var(--border);
}
.cols th { color: var(--muted); font-weight: 500; }
.cols td:nth-child(2) { color: var(--muted); }
code {
  background: rgba(255, 255, 255, 0.06);
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 12px;
}
</style>
