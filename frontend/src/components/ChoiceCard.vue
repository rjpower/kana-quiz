<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  label: string
  index: number
  state: 'idle' | 'correct' | 'wrong' | 'reveal'
  disabled: boolean
}>()
defineEmits<{ (e: 'pick', index: number): void }>()

// English glosses carry their disambiguating hint in a trailing
// parenthetical ("order (a command)"). Under a countdown the user scans the
// core first, so keep it at full size and demote the hint to a second line.
const PARTS_RE = /^(.*\S)\s*\(([^()]+)\)$/
const parts = computed<{ core: string; hint: string | null }>(() => {
  const m = PARTS_RE.exec(props.label)
  return m ? { core: m[1], hint: m[2] } : { core: props.label, hint: null }
})
</script>

<template>
  <button
    class="choice"
    :class="state"
    :disabled="disabled"
    @click="$emit('pick', index)"
  >
    <span class="choice-key">{{ index + 1 }}</span>
    <span class="choice-label">
      {{ parts.core }}
      <span v-if="parts.hint" class="choice-hint">{{ parts.hint }}</span>
    </span>
  </button>
</template>

<style scoped>
.choice {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  background: var(--panel-hi);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 14px 16px;
  border-radius: 10px;
  font-size: 20px;
  cursor: pointer;
  text-align: left;
  transition: background 0.12s, border-color 0.12s;
}
.choice:hover:not(:disabled) { background: #2b3140; }
.choice:disabled { cursor: default; }
.choice-label { min-width: 0; }
.choice-hint {
  display: block;
  font-size: 13px;
  line-height: 1.3;
  color: var(--muted);
  margin-top: 2px;
}
.choice-key {
  display: inline-grid;
  place-items: center;
  width: 24px;
  height: 24px;
  font-size: 12px;
  background: rgba(255, 255, 255, 0.06);
  border-radius: 6px;
  color: var(--muted);
}
.choice.correct { border-color: var(--good); background: rgba(46, 204, 113, 0.18); }
.choice.wrong   { border-color: var(--bad);  background: rgba(231, 76, 60, 0.18); }
.choice.reveal  { border-color: var(--good); background: rgba(46, 204, 113, 0.10); }
</style>
