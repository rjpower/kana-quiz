<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

const props = defineProps<{ id: string }>()
const router = useRouter()

interface DeckMeta {
  id: number
  name: string
  level: number
  profile: string
  word_count: number
  new_count: number
  due_count: number
  ignored_count: number
  archived_count: number
  created_at: string
}

interface DirectionState {
  ease: number
  repetitions: number
  introduced: boolean
}

interface DeckWord {
  id: number
  kana: string
  english: string
  kanji: string | null
  ignored: boolean
  // A sprint graduate: out of rotation like an ignored card, and named
  // cleared instead. The ignore toggle restores it.
  archived: boolean
  en2ja: DirectionState | null
  ja2en: DirectionState | null
}

// Map an SRS ease value to a visual tier for the status pill.
//
// Ease anchors:
//   1.3   — SM-2 floor; the user is leeching this card
//   2.5   — default starting ease (no signal yet, or back to baseline)
//   2.5+  — the user is bumping ease above default → strong recall
//
// We split the < 2.5 half into two tiers so a slightly-shaky card
// (2.0-2.5) stays orange, and a really-shaky one (< 2.0) goes red.
// Above 2.5 we light up green so the user gets a visible reward for
// mastering a word.
function easeTier(state: DirectionState): 'new' | 'leech' | 'shaky' | 'solid' | 'mastered' {
  if (!state.introduced || state.repetitions === 0) return 'new'
  if (state.ease < 2.0) return 'leech'
  if (state.ease < 2.5) return 'shaky'
  if (state.ease < 3.0) return 'solid'
  return 'mastered'
}

function pillTitle(direction: 'en2ja' | 'ja2en', state: DirectionState | null): string {
  const arrow = direction === 'en2ja' ? 'EN → JA' : 'JA → EN'
  if (state === null) return `${arrow}: not scheduled`
  if (!state.introduced || state.repetitions === 0) return `${arrow}: new`
  return `${arrow}: ease ${state.ease.toFixed(2)} · reps ${state.repetitions}`
}

const deckId = computed(() => Number(props.id))
const meta = ref<DeckMeta | null>(null)
const words = ref<DeckWord[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

// Per-row toggle in-flight flag so a double-click can't fire two PATCHes.
const toggleBusy = ref<Record<number, boolean>>({})

// Sprint toggle: PATCH the deck's profile and mirror the answer locally.
const profileBusy = ref(false)
async function toggleSprint() {
  if (!meta.value || profileBusy.value) return
  profileBusy.value = true
  const next = meta.value.profile === 'sprint' ? 'standard' : 'sprint'
  try {
    const resp = await fetch(`/api/decks/${meta.value.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile: next }),
    })
    if (resp.ok) meta.value = (await resp.json()) as DeckMeta
  } finally {
    profileBusy.value = false
  }
}
// Filter chip — defaults to "all" so a freshly-opened deck shows the full
// list. The user usually lands here either to skim everything or to find
// a specific word; we don't want to hide rows until they ask.
const filter = ref<'all' | 'active' | 'ignored'>('all')
const search = ref('')

// Column sort state. ``column === null`` means "no explicit sort —
// use the server's id-ASC order (which mirrors import / introduction
// order, the most useful default for a fresh visit)". Clicking a
// header rotates: unsorted → asc → desc → unsorted.
type SortColumn = 'kana' | 'kanji' | 'english' | 'status' | 'ignored'
const sortColumn = ref<SortColumn | null>(null)
const sortDir = ref<'asc' | 'desc'>('asc')

function toggleSort(col: SortColumn) {
  if (sortColumn.value !== col) {
    sortColumn.value = col
    sortDir.value = 'asc'
    return
  }
  if (sortDir.value === 'asc') {
    sortDir.value = 'desc'
    return
  }
  // Third click on the same column clears the sort.
  sortColumn.value = null
  sortDir.value = 'asc'
}

function sortIndicator(col: SortColumn): string {
  if (sortColumn.value !== col) return ''
  return sortDir.value === 'asc' ? ' ▲' : ' ▼'
}

// For the status column, rank by the minimum ease across both
// directions — the weaker side is what gates promotion in the SRS,
// so this is the "find my leeches" sort. Cards that haven't been
// introduced in either direction get +Infinity, so ascending sort
// puts leeches first and new cards at the end (where they're a
// boring tail rather than a noisy header).
function statusRank(w: DeckWord): number {
  const es: number[] = []
  if (w.en2ja && w.en2ja.introduced && w.en2ja.repetitions > 0) es.push(w.en2ja.ease)
  if (w.ja2en && w.ja2en.introduced && w.ja2en.repetitions > 0) es.push(w.ja2en.ease)
  if (es.length === 0) return Number.POSITIVE_INFINITY
  return Math.min(...es)
}

function compareWords(a: DeckWord, b: DeckWord, col: SortColumn): number {
  switch (col) {
    case 'kana':
      return a.kana.localeCompare(b.kana, 'ja')
    case 'kanji':
      return (a.kanji ?? '').localeCompare(b.kanji ?? '', 'ja')
    case 'english':
      return a.english.localeCompare(b.english, 'en')
    case 'status':
      return statusRank(a) - statusRank(b)
    case 'ignored':
      // false < true → ascending puts active first, ignored last.
      return Number(a.ignored) - Number(b.ignored)
  }
}

async function load() {
  loading.value = true
  error.value = null
  try {
    const [metaResp, wordsResp] = await Promise.all([
      fetch('/api/decks'),
      fetch(`/api/decks/${deckId.value}/words`),
    ])
    if (!metaResp.ok) throw new Error(`decks HTTP ${metaResp.status}`)
    if (!wordsResp.ok) throw new Error(`words HTTP ${wordsResp.status}`)
    const all = (await metaResp.json()) as DeckMeta[]
    meta.value = all.find((d) => d.id === deckId.value) ?? null
    if (!meta.value) {
      error.value = 'Deck not found.'
      return
    }
    words.value = (await wordsResp.json()) as DeckWord[]
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  const rows = words.value.filter((w) => {
    if (filter.value === 'active' && w.ignored) return false
    if (filter.value === 'ignored' && !w.ignored) return false
    if (!q) return true
    return (
      w.kana.toLowerCase().includes(q)
      || (w.kanji?.toLowerCase().includes(q) ?? false)
      || w.english.toLowerCase().includes(q)
    )
  })
  if (sortColumn.value === null) return rows
  const col = sortColumn.value
  const mult = sortDir.value === 'asc' ? 1 : -1
  // Slice before sort: the source ref is reactive and sorting it in
  // place would mutate the original order, breaking the "no sort"
  // reset state.
  return rows.slice().sort((a, b) => mult * compareWords(a, b, col))
})

const counts = computed(() => {
  let active = 0
  let ignored = 0
  let cleared = 0
  for (const w of words.value) {
    if (w.archived) cleared += 1
    else if (w.ignored) ignored += 1
    else active += 1
  }
  return { all: words.value.length, active, ignored, cleared }
})

async function toggleIgnore(word: DeckWord) {
  if (toggleBusy.value[word.id]) return
  toggleBusy.value[word.id] = true
  const wasIgnored = word.ignored
  // Optimistic flip so the toggle feels instant on slow connections.
  word.ignored = !wasIgnored
  try {
    const url = `/api/words/${word.id}/ignore`
    const resp = await fetch(url, { method: wasIgnored ? 'DELETE' : 'POST' })
    if (resp.status !== 204) {
      // Roll back optimism.
      word.ignored = wasIgnored
      throw new Error(`HTTP ${resp.status}`)
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    toggleBusy.value[word.id] = false
  }
}

// Inline edit of name/level lives in the detail header now.
const editing = ref(false)
const draftName = ref('')
const draftLevel = ref(1)
const editError = ref<string | null>(null)

function startEdit() {
  if (!meta.value) return
  draftName.value = meta.value.name
  draftLevel.value = meta.value.level
  editError.value = null
  editing.value = true
}

async function saveEdit() {
  if (!meta.value) return
  const name = draftName.value.trim()
  if (!name) {
    editError.value = 'Name required.'
    return
  }
  try {
    const resp = await fetch(`/api/decks/${meta.value.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, level: draftLevel.value }),
    })
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({ detail: resp.statusText }))
      editError.value = body.detail ?? `HTTP ${resp.status}`
      return
    }
    editing.value = false
    await load()
  } catch (e) {
    editError.value = e instanceof Error ? e.message : String(e)
  }
}

async function deleteDeck() {
  if (!meta.value) return
  if (!confirm(`Delete deck "${meta.value.name}"? This cannot be undone.`)) return
  try {
    const resp = await fetch(`/api/decks/${meta.value.id}`, { method: 'DELETE' })
    if (resp.status === 204) {
      await router.push('/decks')
      return
    }
    const body = await resp.json().catch(() => ({ detail: resp.statusText }))
    editError.value = body.detail ?? `HTTP ${resp.status}`
  } catch (e) {
    editError.value = e instanceof Error ? e.message : String(e)
  }
}
</script>

<template>
  <section class="panel">
    <RouterLink to="/decks" class="back-link">← All decks</RouterLink>

    <div v-if="loading" class="muted">Loading…</div>
    <div v-else-if="error" class="err">{{ error }}</div>
    <template v-else-if="meta">
      <div v-if="!editing" class="header-row">
        <div>
          <h2 class="deck-title">{{ meta.name }}</h2>
          <div class="muted">
            level {{ meta.level }} · {{ counts.all }} words
            <span v-if="counts.ignored > 0">
              · {{ counts.ignored }} ignored
            </span>
            <span v-if="counts.cleared > 0">
              · {{ counts.cleared }} cleared 🏁
            </span>
            <span v-if="meta.profile === 'sprint'" class="sprint-tag">sprint</span>
          </div>
        </div>
        <div class="header-actions">
          <RouterLink
            class="btn primary"
            :to="{ path: '/study', query: { deck: String(meta.id), name: meta.name } }"
          >Study this deck</RouterLink>
          <button class="btn ghost" type="button" :disabled="profileBusy" @click="toggleSprint">
            {{ meta.profile === 'sprint' ? 'Make standard' : 'Make sprint' }}
          </button>
          <button class="btn ghost" type="button" @click="startEdit">Rename</button>
          <button class="btn danger" type="button" @click="deleteDeck">Delete</button>
        </div>
      </div>
      <div v-else class="edit-form">
        <input
          v-model="draftName"
          class="input"
          type="text"
          placeholder="Deck name"
        />
        <label class="level">
          Level
          <input
            v-model.number="draftLevel"
            class="input small"
            type="number"
            min="1"
            max="20"
          />
        </label>
        <button class="btn primary" type="button" @click="saveEdit">Save</button>
        <button
          class="btn ghost"
          type="button"
          @click="editing = false"
        >Cancel</button>
      </div>
      <div v-if="editError" class="row-error">{{ editError }}</div>
    </template>
  </section>

  <section v-if="meta" class="panel">
    <div class="toolbar">
      <input
        v-model="search"
        class="input"
        type="search"
        placeholder="Filter by kana, kanji, or meaning…"
        autocomplete="off"
      />
      <div class="filter-chips">
        <button
          type="button"
          class="chip"
          :class="{ active: filter === 'all' }"
          @click="filter = 'all'"
        >All ({{ counts.all }})</button>
        <button
          type="button"
          class="chip"
          :class="{ active: filter === 'active' }"
          @click="filter = 'active'"
        >Active ({{ counts.active }})</button>
        <button
          type="button"
          class="chip"
          :class="{ active: filter === 'ignored' }"
          @click="filter = 'ignored'"
        >Ignored ({{ counts.ignored }})</button>
      </div>
    </div>

    <p v-if="filtered.length === 0" class="muted">No cards match.</p>
    <table v-else class="words-table">
      <thead>
        <tr>
          <th
            class="col-kana sortable"
            :class="{ active: sortColumn === 'kana' }"
            @click="toggleSort('kana')"
          >Kana<span class="sort-arrow">{{ sortIndicator('kana') }}</span></th>
          <th
            class="col-kanji sortable"
            :class="{ active: sortColumn === 'kanji' }"
            @click="toggleSort('kanji')"
          >Kanji<span class="sort-arrow">{{ sortIndicator('kanji') }}</span></th>
          <th
            class="col-english sortable"
            :class="{ active: sortColumn === 'english' }"
            @click="toggleSort('english')"
          >English<span class="sort-arrow">{{ sortIndicator('english') }}</span></th>
          <th
            class="col-state sortable"
            :class="{ active: sortColumn === 'status' }"
            :title="'Sort by weakest ease (leeches first)'"
            @click="toggleSort('status')"
          >Status<span class="sort-arrow">{{ sortIndicator('status') }}</span></th>
          <th
            class="col-status sortable"
            :class="{ active: sortColumn === 'ignored' }"
            @click="toggleSort('ignored')"
          >Ignored<span class="sort-arrow">{{ sortIndicator('ignored') }}</span></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="w in filtered" :key="w.id" :class="{ ignored: w.ignored }">
          <td class="col-kana">{{ w.kana }}</td>
          <td class="col-kanji">{{ w.kanji || '—' }}</td>
          <td class="col-english">{{ w.english }}</td>
          <td class="col-state">
            <span class="pill-row">
              <span
                v-if="w.en2ja"
                class="pill"
                :class="['tier-' + easeTier(w.en2ja)]"
                :title="pillTitle('en2ja', w.en2ja)"
              >
                <span class="pill-dir">EN→JA</span>
                <span class="pill-val">{{
                  easeTier(w.en2ja) === 'new' ? '·' : w.en2ja.ease.toFixed(1)
                }}</span>
              </span>
              <span v-else class="pill tier-absent" :title="pillTitle('en2ja', null)">
                <span class="pill-dir">EN→JA</span>
                <span class="pill-val">—</span>
              </span>
              <span
                v-if="w.ja2en"
                class="pill"
                :class="['tier-' + easeTier(w.ja2en)]"
                :title="pillTitle('ja2en', w.ja2en)"
              >
                <span class="pill-dir">JA→EN</span>
                <span class="pill-val">{{
                  easeTier(w.ja2en) === 'new' ? '·' : w.ja2en.ease.toFixed(1)
                }}</span>
              </span>
              <span v-else class="pill tier-absent" :title="pillTitle('ja2en', null)">
                <span class="pill-dir">JA→EN</span>
                <span class="pill-val">—</span>
              </span>
            </span>
          </td>
          <td class="col-status">
            <button
              type="button"
              class="toggle"
              :class="{ on: w.ignored }"
              :disabled="toggleBusy[w.id]"
              :aria-pressed="w.ignored"
              :aria-label="w.ignored ? 'Re-enable word' : 'Ignore word'"
              :title="w.ignored ? 'Re-enable' : 'Ignore'"
              @click="toggleIgnore(w)"
            >
              <span class="knob" aria-hidden="true"></span>
            </button>
          </td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<style scoped>
.panel + .panel { margin-top: 12px; }
.muted { color: var(--muted); }
.err { color: var(--bad); }

.back-link {
  display: inline-block;
  font-size: 13px;
  color: var(--muted);
  text-decoration: none;
  margin-bottom: 10px;
}
.back-link:hover { color: inherit; }

.header-row {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  flex-wrap: wrap;
}
.sprint-tag {
  margin-left: 8px;
  padding: 1px 8px;
  border-radius: 999px;
  border: 1px solid var(--accent, #e8a33d);
  color: var(--accent, #e8a33d);
  font-size: 12px;
}
.deck-title { margin: 0 0 4px; font-size: 22px; }
.header-actions { display: flex; gap: 6px; }

.edit-form {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}
.input {
  background: var(--panel-hi);
  color: inherit;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 14px;
  font-family: inherit;
  flex: 1;
  min-width: 160px;
}
.input.small { width: 80px; flex: 0 0 80px; padding: 6px 8px; font-size: 13px; }
.level {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--muted);
}
.btn {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 14px;
  font-size: 14px;
  cursor: pointer;
  font-weight: 500;
  background: var(--panel-hi);
  color: inherit;
}
.btn.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
.btn.ghost { background: transparent; color: var(--muted); }
.btn.ghost:hover { color: inherit; }
.btn.danger {
  color: var(--bad);
  border-color: rgba(231, 76, 60, 0.4);
  background: transparent;
}
.btn.danger:hover { background: rgba(231, 76, 60, 0.12); }

.row-error {
  margin-top: 8px;
  font-size: 13px;
  color: var(--bad);
  background: rgba(231, 76, 60, 0.08);
  border: 1px solid rgba(231, 76, 60, 0.3);
  border-radius: 6px;
  padding: 6px 10px;
}

.toolbar {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.filter-chips { display: flex; gap: 6px; }
.chip {
  background: var(--panel-hi);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 5px 12px;
  font-size: 12px;
  cursor: pointer;
  color: var(--muted);
  font-family: inherit;
}
.chip:hover { color: inherit; }
.chip.active {
  color: inherit;
  border-color: var(--accent);
  background: rgba(76, 158, 255, 0.12);
}

.words-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
}
.words-table th, .words-table td {
  text-align: left;
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
}
.words-table th {
  font-weight: 500;
  color: var(--muted);
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.words-table th.sortable {
  cursor: pointer;
  user-select: none;
}
.words-table th.sortable:hover { color: inherit; }
.words-table th.active { color: inherit; }
.sort-arrow {
  display: inline-block;
  min-width: 1ch;
  font-size: 10px;
  opacity: 0.85;
}
.col-kana { font-size: 16px; }
.col-kanji { color: var(--muted); font-size: 15px; width: 18%; }
.col-english { width: 30%; }
.col-state { width: 170px; }
.col-status { width: 80px; text-align: center; }

.pill-row {
  display: inline-flex;
  gap: 6px;
  align-items: center;
}
.pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  border: 1px solid transparent;
  line-height: 1.2;
}
.pill-dir {
  font-size: 9px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  opacity: 0.85;
}
.pill-val { font-weight: 600; }

/* Tier colors. Tuned for the existing dark theme; backgrounds are
   translucent so they pick up panel-hi underneath, and borders sit
   one step brighter than fill so the chip reads as a distinct token
   instead of a bg-color smear. */
.pill.tier-new {
  background: rgba(76, 158, 255, 0.14);
  border-color: rgba(76, 158, 255, 0.45);
  color: #8ec2ff;
}
.pill.tier-leech {
  background: rgba(231, 76, 60, 0.18);
  border-color: rgba(231, 76, 60, 0.55);
  color: #ff8b7c;
}
.pill.tier-shaky {
  background: rgba(241, 142, 60, 0.16);
  border-color: rgba(241, 142, 60, 0.55);
  color: #ffb074;
}
.pill.tier-solid {
  background: rgba(102, 187, 106, 0.14);
  border-color: rgba(102, 187, 106, 0.5);
  color: #94d999;
}
.pill.tier-mastered {
  background: rgba(241, 196, 15, 0.16);
  border-color: rgba(241, 196, 15, 0.6);
  color: #ffd966;
}
.pill.tier-absent {
  background: transparent;
  border-color: var(--border);
  color: var(--muted);
  opacity: 0.6;
}
.words-table tr.ignored .pill { opacity: 0.4; }
.words-table tr.ignored .col-kana,
.words-table tr.ignored .col-english,
.words-table tr.ignored .col-kanji {
  opacity: 0.45;
}

/* Toggle switch — flips to accent-blue when the card is ignored.
   Disabled state uses opacity rather than removing the click target so
   the user sees the in-flight feedback when toggling. */
.toggle {
  background: var(--panel-hi);
  border: 1px solid var(--border);
  border-radius: 12px;
  width: 42px;
  height: 22px;
  padding: 2px;
  cursor: pointer;
  position: relative;
  transition: background 0.15s ease, border-color 0.15s ease;
}
.toggle .knob {
  display: block;
  width: 16px;
  height: 16px;
  background: var(--muted);
  border-radius: 50%;
  transition: transform 0.15s ease, background 0.15s ease;
}
.toggle.on { background: var(--accent); border-color: var(--accent); }
.toggle.on .knob { background: #fff; transform: translateX(20px); }
.toggle:disabled { opacity: 0.6; cursor: progress; }
</style>
