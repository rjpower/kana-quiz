<script setup lang="ts">
// Selection cloze card: shows a Japanese sentence with one word blanked out
// and four kana choices below it. The user picks which word fills the blank —
// recognition-in-context, the rung between plain multiple choice and the
// type-in cloze card (ClozeInput.vue).
//
// Grading is index-based like multiple choice, so this component is a thin
// composition: the cloze sentence frame (borrowed from ClozeInput) above the
// same ChoiceCard grid the MC question uses. The parent owns the per-choice
// reveal state and the pick handler.

import { computed } from 'vue'
import ChoiceCard from './ChoiceCard.vue'
import Furigana from './Furigana.vue'
import type { RubySegment } from '../stores/session'

const props = defineProps<{
  template: string // sentence containing the literal substring "{blank}"
  // Furigana for the sentence either side of the blank; falls back to the
  // plain `template` split when absent (older backend payloads).
  before?: RubySegment[] | null
  after?: RubySegment[] | null
  choices: string[]
  states: ('idle' | 'correct' | 'wrong' | 'reveal')[]
  disabled: boolean
}>()
defineEmits<{ (e: 'pick', index: number): void }>()

// Split the template once around the literal {blank} token — same contract as
// ClozeInput: the backend guarantees exactly one occurrence, so a missing
// token renders the whole sentence on the left rather than crashing.
const parts = computed<{ before: string; after: string }>(() => {
  const idx = props.template.indexOf('{blank}')
  if (idx === -1) return { before: props.template, after: '' }
  return {
    before: props.template.slice(0, idx),
    after: props.template.slice(idx + '{blank}'.length),
  }
})
</script>

<template>
  <div class="cloze-choice">
    <div class="cloze-sentence">
      <Furigana v-if="before" class="cloze-text" :segments="before" />
      <span v-else class="cloze-text">{{ parts.before }}</span>
      <span class="cloze-slot" aria-label="missing word">？</span>
      <Furigana v-if="after" class="cloze-text" :segments="after" />
      <span v-else class="cloze-text">{{ parts.after }}</span>
    </div>
    <div class="grid">
      <ChoiceCard
        v-for="(label, idx) in choices"
        :key="idx"
        :label="label"
        :index="idx"
        :state="states[idx] ?? 'idle'"
        :disabled="disabled"
        @pick="$emit('pick', $event)"
      />
    </div>
  </div>
</template>

<style scoped>
.cloze-choice {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
/* Mirrors ClozeInput's sentence frame so the two cloze rungs feel like one
   family — same panel, same font scale, just a static slot instead of an
   inline input. */
.cloze-sentence {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: center;
  gap: 4px 6px;
  font-size: clamp(18px, 3.4vw, 24px);
  line-height: 1.5;
  padding: 14px 12px;
  border-radius: 10px;
  background: var(--panel-hi);
  border: 1px solid var(--border);
  text-align: center;
}
.cloze-text {
  white-space: pre-wrap;
  word-break: keep-all;
}
.cloze-slot {
  display: inline-grid;
  place-items: center;
  min-width: 3ch;
  padding: 2px 10px;
  border-radius: 6px;
  border: 1px dashed var(--accent, #4c9eff);
  background: rgba(76, 158, 255, 0.08);
  color: var(--accent, #4c9eff);
  font-weight: 600;
}
.grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
@media (max-width: 480px) {
  .grid {
    grid-template-columns: 1fr;
  }
}
</style>
