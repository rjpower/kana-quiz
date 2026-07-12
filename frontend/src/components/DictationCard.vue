<script setup lang="ts">
// Listening / sentence-translation card.
//
// The user hears the cached Gemini sentence (auto-played on mount), then
// types an English translation in a plain text input — no kana
// conversion since the answer language is English, not Japanese.
//
// Audio gets up to three plays per card: the auto-play on mount uses
// the first, and the visible Replay button can be tapped twice more
// before locking. The plays-remaining counter resets on `reset_key`
// change so a fresh card starts with a full budget.

import { onMounted, ref, watch } from 'vue'
import { playAudioUrl, unlockAudio } from '../audio'

const props = defineProps<{
  audioUrl: string
  resetKey: number
  disabled: boolean
}>()
const emit = defineEmits<{
  (e: 'submit', value: string): void
  (e: 'audio-blocked'): void
}>()

const value = ref('')
const input = ref<HTMLInputElement | null>(null)
// Total budget is 3 plays. The auto-play on mount uses #1, so the
// visible Replay counter starts at 2 — that's what the user sees.
const TOTAL_PLAYS = 3
const playsRemaining = ref(TOTAL_PLAYS)

async function playAudio() {
  if (playsRemaining.value <= 0) return
  playsRemaining.value -= 1
  try {
    await playAudioUrl(props.audioUrl)
  } catch (err) {
    // NotAllowedError = browser autoplay block. Surface to parent so
    // StudyView can show the "Enable audio" banner the same way it
    // does for word-pronunciation playback.
    if ((err as Error).name === 'NotAllowedError') {
      emit('audio-blocked')
      // Refund the play — the user didn't actually hear anything.
      playsRemaining.value += 1
    }
  }
}

async function onMountedPlay() {
  // The card may have been mounted by an auto-advance — the audio
  // context could still be suspended on browsers that demand a fresh
  // gesture after a tab switch. Try to unlock; if it stays suspended
  // the playAudioUrl below will throw NotAllowedError and we'll surface
  // the banner.
  await unlockAudio()
  await playAudio()
}

onMounted(() => {
  void onMountedPlay()
  input.value?.focus()
})

// Reset state + re-play on a card change. The reset_key is the new
// question's word_id, so it changes only on a genuine new card.
watch(
  () => props.resetKey,
  () => {
    value.value = ''
    playsRemaining.value = TOTAL_PLAYS
    void onMountedPlay()
    input.value?.focus()
  },
)

function onSubmit() {
  if (props.disabled) return
  const v = value.value.trim()
  if (!v) return
  emit('submit', v)
}
</script>

<template>
  <form class="dictation-form" @submit.prevent="onSubmit">
    <div v-if="!disabled" class="audio-controls">
      <button
        class="replay-btn"
        type="button"
        :disabled="playsRemaining <= 0"
        :aria-label="`Replay audio (${playsRemaining} left)`"
        @click="playAudio"
      >
        <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
          <path
            fill="currentColor"
            d="M3 10v4h4l5 5V5L7 10H3zm13.5 2a4.5 4.5 0 0 0-2.5-4.03v8.05a4.5 4.5 0 0 0 2.5-4.02zM14 3.23v2.06a7 7 0 0 1 0 13.42v2.06a9 9 0 0 0 0-17.54z"
          />
        </svg>
        <span class="replay-label">
          Replay
          <span class="plays-left">({{ playsRemaining }} left)</span>
        </span>
      </button>
    </div>
    <input
      v-if="!disabled"
      ref="input"
      v-model="value"
      class="dictation-input"
      type="text"
      name="kana-quiz-dictation"
      placeholder="type the English translation…"
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
.dictation-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
  align-items: stretch;
}
.audio-controls {
  display: flex;
  justify-content: center;
  padding: 8px 0 4px;
}
.replay-btn {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  padding: 12px 22px;
  font: inherit;
  font-size: 15px;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--panel-hi);
  color: inherit;
  cursor: pointer;
}
.replay-btn:hover:not(:disabled) {
  border-color: var(--accent, #4c9eff);
}
.replay-btn:active:not(:disabled) {
  transform: scale(0.97);
}
.replay-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.replay-label {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
}
.plays-left {
  font-size: 12px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.dictation-input {
  width: 100%;
  font-size: clamp(20px, 3.6vw, 26px);
  padding: 14px 16px;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--panel-hi);
  color: inherit;
  text-align: center;
}
.dictation-input:focus {
  outline: none;
  border-color: var(--accent, #4c9eff);
  box-shadow: 0 0 0 2px rgba(76, 158, 255, 0.25);
}
.submit {
  padding: 12px;
  font-size: 15px;
}
</style>
