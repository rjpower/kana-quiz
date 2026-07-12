// Shared wanakana wiring for any input that wants romaji-→-kana
// conversion under the same lifecycle as the type-in component.
//
// Responsibilities:
//   * Bind / unbind wanakana to the input element while `enabled` is
//     true, using watchEffect so the unbind closure captures the exact
//     element we bound — independent of whatever prop value is current
//     at teardown time.
//   * Clear and refocus the input whenever `resetKey` changes (one
//     question → next).
//   * Provide `commitKana()`: read the DOM value, force a final
//     `toKana()` pass for any straggling romaji, write the result back
//     into both v-model state and the DOM, and return it for emit.
//   * Provide `insertChonpu()`: insert the ー mark at the caret, since
//     Android keyboards bury '-' two taps deep and wanakana only emits
//     ー when the user types a literal '-'.
//
// `enabled` and `resetKey` are getter functions so callers can compute
// them off any reactive source (props, store fields, computed refs)
// without forcing a particular wrapper shape.

import { ref, watch, watchEffect, type Ref } from 'vue'
import { bind, toKana, unbind } from 'wanakana'

export interface KanaInputOptions {
  enabled: () => boolean
  resetKey: () => number
}

export interface KanaInputHandle {
  input: Ref<HTMLInputElement | null>
  value: Ref<string>
  commitKana: () => string
  insertChonpu: () => void
}

export function useKanaInput(opts: KanaInputOptions): KanaInputHandle {
  const input = ref<HTMLInputElement | null>(null)
  const value = ref('')

  // Bind WITHOUT IMEMode — with IMEMode set, wanakana intercepts the
  // first Enter as a "commit pending romaji" gesture and the form
  // submit never fires (forces a second Enter). Plain bind() converts
  // on every input event without that commit gate; commitKana() below
  // handles whatever romaji was left dangling at submit time.
  watchEffect((onCleanup) => {
    const el = input.value
    if (!el || !opts.enabled()) return
    bind(el)
    onCleanup(() => {
      try { unbind(el) } catch { /* already unbound — fine */ }
    })
  })

  watch(opts.resetKey, () => {
    value.value = ''
    requestAnimationFrame(() => input.value?.focus())
  }, { immediate: true })

  function commitKana(): string {
    let v = (input.value?.value ?? value.value).trim()
    if (opts.enabled()) {
      // toKana() plain (no IMEMode) commits any trailing ambiguous
      // syllable — "konban" → "こんばん" not "こんばn", "ohayoug" →
      // "おはようg" stays since "g" alone is meaningless but the next
      // keystroke will resolve it. Idempotent for kana-only strings.
      v = toKana(v)
      value.value = v
      if (input.value) input.value.value = v
    }
    return v
  }

  function insertChonpu() {
    const el = input.value
    if (!el) return
    const start = el.selectionStart ?? el.value.length
    const end = el.selectionEnd ?? el.value.length
    const before = el.value.slice(0, start)
    const after = el.value.slice(end)
    const next = before + 'ー' + after
    el.value = next
    value.value = next
    const caret = start + 1
    el.focus()
    try {
      el.setSelectionRange(caret, caret)
    } catch {
      /* some input types throw on setSelectionRange; focus is enough */
    }
  }

  return { input, value, commitKana, insertChonpu }
}
