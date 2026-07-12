<script setup lang="ts">
defineProps<{
  label: string
  index: number
  state: 'idle' | 'correct' | 'wrong' | 'reveal'
  disabled: boolean
}>()
defineEmits<{ (e: 'pick', index: number): void }>()
</script>

<template>
  <button
    class="choice"
    :class="state"
    :disabled="disabled"
    @click="$emit('pick', index)"
  >
    <span class="choice-key">{{ index + 1 }}</span>
    <span class="choice-label">{{ label }}</span>
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
