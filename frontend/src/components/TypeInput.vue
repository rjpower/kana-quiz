<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useKanaInput } from '../composables/useKanaInput'
import { getGlosses, normalizeGloss } from '../glossCache'

const props = defineProps<{
  direction: 'en2ja' | 'ja2en'
  resetKey: number
  disabled: boolean
}>()
const emit = defineEmits<{
  (e: 'submit', value: string): void
}>()

const { input, value, commitKana, insertChonpu } = useKanaInput({
  enabled: () => props.direction === 'en2ja',
  resetKey: () => props.resetKey,
})

const chipRefs = ref<HTMLButtonElement[]>([])

function focusChip(i: number) {
  const el = chipRefs.value[i]
  if (el) el.focus()
}

function onInputArrowDown(e: KeyboardEvent) {
  if (suggestions.value.length === 0) return
  e.preventDefault()
  focusChip(0)
}

function onChipKey(e: KeyboardEvent, i: number) {
  // Horizontal strip: Left/Right move within chips, Up returns to input,
  // Down is a no-op (we're already at the bottom of the answer block).
  // Esc bails back to the input so the user can keep typing.
  switch (e.key) {
    case 'ArrowRight':
      e.preventDefault()
      focusChip(Math.min(i + 1, chipRefs.value.length - 1))
      break
    case 'ArrowLeft':
      e.preventDefault()
      if (i === 0) input.value?.focus()
      else focusChip(i - 1)
      break
    case 'ArrowUp':
      e.preventDefault()
      input.value?.focus()
      break
    case 'Escape':
      e.preventDefault()
      input.value?.focus()
      break
  }
}

// Deck gloss corpus, lazy-loaded for the ja2en autocomplete chip strip.
// Empty until the first ja2en mount completes the fetch — until then the
// suggestions computed property simply returns an empty list.
const glosses = ref<string[]>([])
onMounted(() => {
  if (props.direction !== 'ja2en') return
  void getGlosses().then((g) => { glosses.value = g }).catch(() => {})
})

// Suggestion gating: never show until the user has typed at least 3
// chars, never show more than 5 chips at once. Typing 3 letters of the
// target is most of the recall work already done, so the cap is just a
// readability guard — when many tokens share a common prefix (e.g.
// "imm" has 6 hits in our deck: immature, immediate, immediately,
// immediately after, immediately before, to immigrate), we truncate
// alphabetically rather than hide the strip entirely. Hiding made the
// strip silently disappear at exactly the prefixes where it would have
// been most useful.
const SUGGEST_MIN_CHARS = 3
const SUGGEST_MAX_RESULTS = 5

const suggestions = computed<string[]>(() => {
  if (props.direction !== 'ja2en') return []
  if (glosses.value.length === 0) return []
  const q = normalizeGloss(value.value)
  if (q.length < SUGGEST_MIN_CHARS) return []
  const matches: string[] = []
  for (const g of glosses.value) {
    const ng = normalizeGloss(g)
    // Skip exact matches — the user has already typed the answer (modulo
    // a leading "to "/"a "), no point suggesting it back.
    if (ng === q) continue
    if (ng.startsWith(q)) {
      matches.push(g)
      if (matches.length >= SUGGEST_MAX_RESULTS) break
    }
  }
  return matches
})

// Reset the chip element ref array whenever the suggestion set changes, so
// stale entries from a longer prior list can't be focused after the strip
// shrinks. The :ref callback repopulates it during the next render.
watch(() => suggestions.value.length, () => { chipRefs.value = [] })

function pickSuggestion(s: string) {
  // Tapping a chip is an explicit commit gesture — auto-submit so the
  // user doesn't have to chip-then-Enter on mobile. We still set the
  // input value first so the locked / reveal screen shows what was
  // typed (existing wrong-answer flow reads `typed` from the submitted
  // string, but the input itself remains the visible record).
  value.value = s
  if (input.value) {
    input.value.value = s
  }
  emit('submit', s)
}

// Wanakana lifecycle + commit + ー chip are all owned by useKanaInput.
// Nothing else to wire here.

function onSubmit() {
  if (props.disabled) return
  const v = commitKana()
  if (!v) return
  emit('submit', v)
}
</script>

<template>
  <form class="type-form" @submit.prevent="onSubmit">
    <input
      ref="input"
      v-model="value"
      class="type-input"
      type="text"
      name="kana-quiz-answer"
      :disabled="disabled"
      :placeholder="direction === 'en2ja' ? 'type romaji…' : 'type meaning…'"
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
      @keydown.down="onInputArrowDown"
    />
    <div v-if="direction === 'en2ja' && !disabled" class="kana-chips">
      <button
        type="button"
        class="kana-chip"
        :disabled="disabled"
        :aria-label="'Insert long vowel mark ー'"
        @mousedown.prevent
        @click="insertChonpu"
      >ー</button>
    </div>
    <div v-if="suggestions.length > 0" class="gloss-chips" role="listbox">
      <button
        v-for="(s, i) in suggestions"
        :key="s"
        :ref="(el) => { if (el) chipRefs[i] = el as HTMLButtonElement }"
        type="button"
        class="gloss-chip"
        role="option"
        :disabled="disabled"
        @mousedown.prevent
        @click="pickSuggestion(s)"
        @keydown="onChipKey($event, i)"
      >{{ s }}</button>
    </div>
    <!-- Once locked, StudyView renders its own Continue button as the
         primary action; keeping a disabled Submit alongside it is just
         visual noise. The form's Enter-submit handler still fires while
         unlocked, which is the only state Submit is meaningful in. -->
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
.type-form {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.type-input {
  width: 100%;
  font-size: clamp(22px, 4vw, 32px);
  padding: 14px 16px;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--panel-hi);
  color: inherit;
  text-align: center;
  font-variant-numeric: tabular-nums;
}
.type-input:focus {
  outline: none;
  border-color: var(--accent, #4c9eff);
  box-shadow: 0 0 0 2px rgba(76, 158, 255, 0.25);
}
.type-input:disabled {
  opacity: 0.7;
}
.submit {
  padding: 12px;
  font-size: 15px;
}
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
.gloss-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  justify-content: center;
}
.gloss-chip {
  font-size: 14px;
  line-height: 1.3;
  padding: 6px 12px;
  border-radius: 16px;
  border: 1px solid var(--border);
  background: var(--panel-hi);
  color: inherit;
  cursor: pointer;
  font-family: inherit;
  min-height: 32px;
}
.gloss-chip:hover { border-color: var(--accent, #4c9eff); color: inherit; }
.gloss-chip:focus-visible {
  outline: none;
  border-color: var(--accent, #4c9eff);
  box-shadow: 0 0 0 2px rgba(76, 158, 255, 0.35);
}
.gloss-chip:active { transform: scale(0.97); }
.gloss-chip:disabled { opacity: 0.5; cursor: not-allowed; }
</style>
