<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

// One term in, one card out. The route also answers the Android share
// target: a share arrives as ?text= (or ?title= / ?url=), is submitted at
// once, and the result stays on screen so a wrong reading can be caught.

interface Added {
  id: number
  kana: string
  kanji: string | null
  english: string
  deck_id: number
  deck_name: string
  created: boolean
}

const route = useRoute()
const router = useRouter()
const text = ref('')
const busy = ref(false)
const error = ref<string | null>(null)
const added = ref<Added[]>([])
const input = ref<HTMLInputElement | null>(null)

function shared(): string {
  const q = route.query
  for (const key of ['text', 'title', 'url']) {
    const v = q[key]
    const one = Array.isArray(v) ? v[0] : v
    if (typeof one === 'string' && one.trim()) return one.trim()
  }
  return ''
}

async function submit() {
  const term = text.value.trim()
  if (!term || busy.value) return
  busy.value = true
  error.value = null
  try {
    const resp = await fetch('/api/words/quick', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: term }),
    })
    if (!resp.ok) {
      const detail = await resp.json().catch(() => ({ detail: resp.statusText }))
      throw new Error(detail.detail ?? `HTTP ${resp.status}`)
    }
    added.value.unshift((await resp.json()) as Added)
    text.value = ''
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
    input.value?.focus()
  }
}

onMounted(() => {
  const term = shared()
  if (term) {
    text.value = term
    // Drop the query so a reload or a back press does not add it twice.
    void router.replace({ path: '/quick' })
    void submit()
  } else {
    input.value?.focus()
  }
})
</script>

<template>
  <section class="panel">
    <h2>Quick add</h2>
    <p class="muted small">
      Type a word in English or Japanese. The reading, the kanji and the gloss are
      filled in, and the card goes in the Quick deck.
    </p>
    <form class="row" @submit.prevent="submit">
      <input
        ref="input"
        v-model="text"
        class="term"
        type="text"
        autocomplete="off"
        autocapitalize="off"
        enterkeyhint="done"
        placeholder="dog · 犬 · いぬ"
        :disabled="busy"
      />
      <button class="btn" type="submit" :disabled="busy || !text.trim()">
        {{ busy ? 'Adding…' : 'Add' }}
      </button>
    </form>
    <div v-if="error" class="error" role="alert">
      <strong>Not added.</strong> {{ error }}
    </div>

    <ul v-if="added.length" class="added">
      <li v-for="w in added" :key="w.id" class="card" :class="{ seen: !w.created }">
        <div class="ja">
          <span class="kanji">{{ w.kanji ?? w.kana }}</span>
          <span v-if="w.kanji" class="kana">{{ w.kana }}</span>
        </div>
        <div class="en">{{ w.english }}</div>
        <div class="muted small">
          <template v-if="w.created">Added to </template>
          <template v-else>Already in </template>
          <RouterLink :to="`/decks/${w.deck_id}`">{{ w.deck_name }}</RouterLink>
          · <RouterLink :to="`/word/${w.id}`">card</RouterLink>
        </div>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.row {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}
.term {
  flex: 1;
  min-width: 0;
  font-size: 1.15rem;
  padding: 10px 12px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--panel-hi);
  color: var(--text);
}
.small { font-size: 12px; }
.error {
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: 8px;
  border: 1px solid var(--bad);
  color: var(--bad);
  overflow-wrap: anywhere;
}
.added {
  list-style: none;
  margin: 20px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.card {
  padding: 12px 14px;
  border-radius: 10px;
  background: var(--panel-hi);
  border: 1px solid var(--border);
}
.card.seen {
  opacity: 0.75;
}
.ja {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.kanji {
  font-size: 1.6rem;
}
.kana {
  color: var(--muted);
}
.en {
  margin: 4px 0 6px;
}
</style>
