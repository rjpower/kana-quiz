<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

interface Deck {
  id: number
  name: string
  level: number
  pick_order: 'random' | 'listed'
  created_at: string
  word_count: number
  new_count: number
  due_count: number
  ignored_count: number
}

const decks = ref<Deck[]>([])
const error = ref<string | null>(null)
const loading = ref(true)

// Cross-deck rollup — this at-a-glance total used to live on the study
// landing page; it belongs with the per-deck breakdown here.
const totals = computed(() =>
  decks.value.reduce(
    (acc, d) => ({
      words: acc.words + d.word_count,
      new_count: acc.new_count + d.new_count,
      due: acc.due + d.due_count,
      ignored: acc.ignored + d.ignored_count,
    }),
    { words: 0, new_count: 0, due: 0, ignored: 0 },
  ),
)

const newName = ref('')
const newLevel = ref(5)
const createBusy = ref(false)
const createError = ref<string | null>(null)

// Level is the new-card draw order across decks, so it is read and
// compared on this screen. Edit it here rather than making the user open
// each deck in turn.
const levelBusy = ref<Record<number, boolean>>({})
const levelError = ref<Record<number, string>>({})

async function saveLevel(deck: Deck, event: Event) {
  const input = event.target as HTMLInputElement
  const level = Number(input.value)
  if (!Number.isInteger(level) || level < 1) {
    levelError.value[deck.id] = 'Level must be a whole number of 1 or more.'
    input.value = String(deck.level)
    return
  }
  if (level === deck.level) return
  levelBusy.value[deck.id] = true
  delete levelError.value[deck.id]
  try {
    const resp = await fetch(`/api/decks/${deck.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ level }),
    })
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({ detail: resp.statusText }))
      levelError.value[deck.id] = body.detail ?? `HTTP ${resp.status}`
      input.value = String(deck.level)
      return
    }
    // Keep the row where it is: the server orders decks by level, and
    // re-sorting under the cursor would move the input the user just used.
    deck.level = ((await resp.json()) as Deck).level
  } catch (e) {
    levelError.value[deck.id] = e instanceof Error ? e.message : String(e)
    input.value = String(deck.level)
  } finally {
    levelBusy.value[deck.id] = false
  }
}

// New cards within a level are drawn at random unless the deck says
// 'listed', which follows import order; a frequency-sorted deck wants that.
const orderBusy = ref<Record<number, boolean>>({})

async function saveOrder(deck: Deck, event: Event) {
  const pick_order = (event.target as HTMLSelectElement).value as Deck['pick_order']
  if (pick_order === deck.pick_order) return
  orderBusy.value[deck.id] = true
  delete levelError.value[deck.id]
  try {
    const resp = await fetch(`/api/decks/${deck.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pick_order }),
    })
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}))
      levelError.value[deck.id] = body.detail ?? `HTTP ${resp.status}`
      return
    }
    deck.pick_order = ((await resp.json()) as Deck).pick_order
  } catch (e) {
    levelError.value[deck.id] = e instanceof Error ? e.message : String(e)
  } finally {
    orderBusy.value[deck.id] = false
  }
}

async function load() {
  loading.value = true
  error.value = null
  try {
    const resp = await fetch('/api/decks')
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    decks.value = (await resp.json()) as Deck[]
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)

async function createDeck() {
  const name = newName.value.trim()
  if (!name) {
    createError.value = 'Name required.'
    return
  }
  createBusy.value = true
  createError.value = null
  try {
    const resp = await fetch('/api/decks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, level: newLevel.value }),
    })
    if (!resp.ok) {
      const detail = await resp.json().catch(() => ({ detail: resp.statusText }))
      createError.value = detail.detail ?? `HTTP ${resp.status}`
      return
    }
    newName.value = ''
    newLevel.value = 5
    await load()
  } catch (e) {
    createError.value = e instanceof Error ? e.message : String(e)
  } finally {
    createBusy.value = false
  }
}
</script>

<template>
  <section class="panel">
    <div class="decks-header">
      <h2>Decks</h2>
      <RouterLink to="/grader-log" class="btn ghost">Grader log →</RouterLink>
    </div>

    <div class="new-deck">
      <input
        v-model="newName"
        class="input"
        type="text"
        placeholder="New deck name"
        @keydown.enter="createDeck"
      />
      <label class="level">
        Level
        <input
          v-model.number="newLevel"
          class="input small"
          type="number"
          min="1"
          max="20"
        />
      </label>
      <button
        class="btn primary"
        type="button"
        :disabled="createBusy || !newName.trim()"
        @click="createDeck"
      >
        {{ createBusy ? 'Creating…' : 'Create' }}
      </button>
    </div>
    <div v-if="createError" class="row-error">{{ createError }}</div>
  </section>

  <section v-if="error" class="panel">
    <p class="err">{{ error }}</p>
  </section>

  <section v-else-if="loading" class="panel">
    <p class="muted">Loading…</p>
  </section>

  <section v-else-if="decks.length === 0" class="panel">
    <p class="muted">No decks yet. Create one above or via Import.</p>
  </section>

  <section v-else-if="decks.length > 1" class="panel deck-totals">
    <div class="deck-totals-label muted">All decks</div>
    <div class="deck-stats">
      <div class="stat">
        <div class="lbl">Words</div>
        <div class="val">{{ totals.words }}</div>
      </div>
      <div class="stat">
        <div class="lbl">New</div>
        <div class="val" :class="{ on: totals.new_count > 0 }">{{ totals.new_count }}</div>
      </div>
      <div class="stat">
        <div class="lbl">Due</div>
        <div class="val" :class="{ on: totals.due > 0 }">{{ totals.due }}</div>
      </div>
      <div v-if="totals.ignored > 0" class="stat">
        <div class="lbl">Ignored</div>
        <div class="val muted">{{ totals.ignored }}</div>
      </div>
    </div>
  </section>

  <div v-if="decks.length > 0" class="deck-list">
    <section v-for="d in decks" :key="d.id" class="panel deck-card">
      <div class="deck-row">
        <div class="deck-main">
          <div class="deck-name">{{ d.name }}</div>
          <label class="deck-sub level">
            Level
            <input
              class="input small"
              type="number"
              min="1"
              max="20"
              :value="d.level"
              :disabled="levelBusy[d.id]"
              @change="saveLevel(d, $event)"
            />
          </label>
          <label class="deck-sub level">
            New cards
            <select
              class="input small"
              :value="d.pick_order"
              :disabled="orderBusy[d.id]"
              @change="saveOrder(d, $event)"
            >
              <option value="random">random</option>
              <option value="listed">in order</option>
            </select>
          </label>
          <div v-if="levelError[d.id]" class="row-error">{{ levelError[d.id] }}</div>
        </div>
        <div class="deck-stats">
          <div class="stat">
            <div class="lbl">Words</div>
            <div class="val">{{ d.word_count }}</div>
          </div>
          <div class="stat">
            <div class="lbl">New</div>
            <div class="val">{{ d.new_count }}</div>
          </div>
          <div class="stat">
            <div class="lbl">Due</div>
            <div class="val">{{ d.due_count }}</div>
          </div>
          <div v-if="d.ignored_count > 0" class="stat">
            <div class="lbl">Ignored</div>
            <div class="val muted">{{ d.ignored_count }}</div>
          </div>
        </div>
        <div class="deck-actions">
          <RouterLink :to="`/decks/${d.id}`" class="btn ghost">Manage</RouterLink>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.panel + .panel { margin-top: 12px; }
.muted { color: var(--muted); }
.err { color: var(--bad); }

h2 { margin: 0 0 14px; }

.decks-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.decks-header h2 { margin: 0; }

.new-deck {
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
.input.small { width: 80px; flex: 0 0 80px; min-width: 0; padding: 6px 8px; font-size: 13px; }
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
.btn.primary {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}
.btn.primary:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.btn.ghost { background: transparent; color: var(--muted); }
.btn.ghost:hover { color: inherit; }
.btn.danger {
  color: var(--bad);
  border-color: rgba(231, 76, 60, 0.4);
  background: transparent;
}
.btn.danger:hover { background: rgba(231, 76, 60, 0.12); }

.deck-totals {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  padding: 12px 16px;
  margin-top: 12px;
}
.deck-totals-label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-weight: 600;
}
.stat .val.on { color: var(--accent); }

.deck-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 12px;
}
.deck-card { padding: 14px 16px; }
.deck-row {
  display: grid;
  grid-template-columns: 1fr auto auto;
  align-items: center;
  gap: 16px;
}
@media (max-width: 560px) {
  .deck-row { grid-template-columns: 1fr; }
  .deck-stats { justify-self: start; }
  .deck-actions { justify-self: start; }
}
.deck-main { min-width: 0; }
.deck-name { font-size: 16px; font-weight: 600; }
.deck-sub { font-size: 12px; }
.deck-stats {
  display: flex;
  gap: 16px;
}
.stat .lbl {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted);
}
.stat .val {
  font-size: 18px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.deck-actions { display: flex; gap: 6px; }
.deck-actions .btn.ghost {
  display: inline-flex;
  align-items: center;
  text-decoration: none;
}

.stat .val.muted { color: var(--muted); }

.row-error {
  margin-top: 8px;
  font-size: 13px;
  color: var(--bad);
  background: rgba(231, 76, 60, 0.08);
  border: 1px solid rgba(231, 76, 60, 0.3);
  border-radius: 6px;
  padding: 6px 10px;
}
</style>
