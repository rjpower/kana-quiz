<script setup lang="ts">
import type { MatchCell } from '../stores/session'

defineProps<{
  prompts: MatchCell[]
  answers: MatchCell[]
}>()
defineEmits<{ (e: 'tap', key: string): void }>()
</script>

<template>
  <div class="match-grid">
    <TransitionGroup tag="div" name="tile" class="match-col">
      <button
        v-for="c in prompts"
        :key="c.key"
        type="button"
        class="match-tile"
        :class="c.state"
        @click="$emit('tap', c.key)"
      >
        {{ c.text }}
      </button>
    </TransitionGroup>
    <TransitionGroup tag="div" name="tile" class="match-col">
      <button
        v-for="c in answers"
        :key="c.key"
        type="button"
        class="match-tile"
        :class="c.state"
        @click="$emit('tap', c.key)"
      >
        {{ c.text }}
      </button>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.match-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  align-items: start;
}
.match-col {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.match-tile {
  width: 100%;
  min-height: 56px;
  padding: 12px 14px;
  background: var(--panel-hi);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 10px;
  font-size: 19px;
  cursor: pointer;
  text-align: center;
  transition: background 0.12s, border-color 0.12s, transform 0.12s, opacity 0.2s;
}
.match-tile:hover:not(.correct):not(.wrong) { background: #2b3140; }
.match-tile.selected {
  border-color: var(--accent, #4c9eff);
  background: rgba(76, 158, 255, 0.18);
}
.match-tile.correct {
  border-color: var(--good);
  background: rgba(46, 204, 113, 0.22);
}
.match-tile.wrong {
  border-color: var(--bad);
  background: rgba(231, 76, 60, 0.22);
  animation: shake 0.3s ease;
}
@keyframes shake {
  0%, 100% { transform: translateX(0); }
  25% { transform: translateX(-4px); }
  75% { transform: translateX(4px); }
}
/* Slide new tiles in / matched tiles out. */
.tile-enter-from { opacity: 0; transform: translateY(-8px); }
.tile-leave-to { opacity: 0; transform: scale(0.9); }
.tile-leave-active { position: relative; }
</style>
