<script setup lang="ts">
// Cloze quiz card: shows a Japanese sentence with one word blanked
// out, the user types the missing word in kana. The wanakana wiring
// is identical to TypeInput's en2ja branch, so we lean on the shared
// useKanaInput composable; the only thing distinctive here is the
// inline rendering of the sentence around the input.
//
// Cloze is en2ja-only in v1 — the user produces a Japanese word
// inside a Japanese sentence — so we don't expose a `direction`
// prop. The grader accepts either the dictionary form or the
// conjugated surface form (see backend grade_typed with
// cloze_expected), so a user who hasn't yet learned the conjugation
// still passes by typing the dictionary form.

import { computed } from 'vue'
import { useKanaInput } from '../composables/useKanaInput'
import Furigana from './Furigana.vue'
import type { RubySegment } from '../stores/session'

const props = defineProps<{
  template: string         // sentence with the literal substring "{blank}"
  // Furigana for the sentence either side of the blank. When present the
  // frame renders ruby; otherwise it falls back to the plain `template` split
  // (older backend payloads that don't ship segments).
  before?: RubySegment[] | null
  after?: RubySegment[] | null
  resetKey: number
  disabled: boolean
}>()
const emit = defineEmits<{
  (e: 'submit', value: string): void
}>()

const { input, value, commitKana, insertChonpu } = useKanaInput({
  enabled: () => true,            // always en2ja in v1
  resetKey: () => props.resetKey,
})

// Split the template once around the literal {blank} token. The
// backend guarantees exactly one occurrence (it called replace(..., 1)),
// so a missing token would be a bug — we render the whole sentence on
// the left and an empty right half rather than crash, then the user
// sees something obviously broken instead of a silent fail.
const parts = computed<{ before: string; after: string }>(() => {
  const idx = props.template.indexOf('{blank}')
  if (idx === -1) return { before: props.template, after: '' }
  return {
    before: props.template.slice(0, idx),
    after: props.template.slice(idx + '{blank}'.length),
  }
})

function onSubmit() {
  if (props.disabled) return
  const v = commitKana()
  if (!v) return
  emit('submit', v)
}
</script>

<template>
  <form class="cloze-form" @submit.prevent="onSubmit">
    <div class="cloze-sentence">
      <Furigana v-if="before" class="cloze-text" :segments="before" />
      <span v-else class="cloze-text">{{ parts.before }}</span>
      <input
        ref="input"
        v-model="value"
        class="cloze-input"
        type="text"
        name="kana-quiz-cloze"
        :disabled="disabled"
        placeholder="type romaji…"
        autocomplete="off"
        autocapitalize="off"
        autocorrect="off"
        spellcheck="false"
        inputmode="text"
        lang="en"
        data-1p-ignore="true"
        data-lpignore="true"
        data-form-type="other"
        aria-autocomplete="none"
      />
      <Furigana v-if="after" class="cloze-text" :segments="after" />
      <span v-else class="cloze-text">{{ parts.after }}</span>
    </div>
    <div v-if="!disabled" class="kana-chips">
      <button
        type="button"
        class="kana-chip"
        :disabled="disabled"
        :aria-label="'Insert long vowel mark ー'"
        @mousedown.prevent
        @click="insertChonpu"
      >ー</button>
    </div>
    <button
      v-if="!disabled"
      class="btn primary submit"
      type="submit"
      :disabled="!value.trim()"
    >
      Submit
    </button>
  </form>
</template>

<style scoped>
.cloze-form {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* The sentence reads as one wrapped phrase with the input slotted
   inline. Larger font than TypeInput's because the user is reading
   Japanese, and the inline input stretches to match. */
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
.cloze-input {
  font: inherit;
  min-width: 6ch;
  max-width: 14ch;
  padding: 4px 8px;
  border-radius: 6px;
  border: 1px dashed var(--accent, #4c9eff);
  background: rgba(76, 158, 255, 0.08);
  color: inherit;
  text-align: center;
  font-variant-numeric: tabular-nums;
}
.cloze-input:focus {
  outline: none;
  background: rgba(76, 158, 255, 0.16);
  border-style: solid;
  box-shadow: 0 0 0 2px rgba(76, 158, 255, 0.25);
}
.cloze-input:disabled { opacity: 0.7; }

.kana-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  justify-content: center;
}
.kana-chip {
  font-size: 22px;
  line-height: 1;
  padding: 8px 14px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--panel-hi);
  color: inherit;
  cursor: pointer;
  font-family: inherit;
  min-width: 44px;
  min-height: 40px;
}
.kana-chip:hover { border-color: var(--accent, #4c9eff); }
.kana-chip:active { transform: scale(0.96); }
.kana-chip:disabled { opacity: 0.5; cursor: not-allowed; }

.submit {
  padding: 12px;
  font-size: 15px;
}
</style>
