// Generic non-blocking notification strip.
//
// The quiz used to report "Too slow!" by rendering a panel inline in the card
// flow and holding the dwell open for 1.2s so it could be read. That shoved the
// choice grid down and, worse, made every slow-but-correct rep cost more than a
// wrong one — the interruption was the punishment. Toasts decouple "say
// something" from "stop and read it": the message floats above the layout,
// survives the card advancing underneath it, and never gates input.
//
// Nothing here is quiz-specific; it's a plain queue any view can push to.

import { defineStore } from 'pinia'
import { ref } from 'vue'

export type ToastTone = 'success' | 'warn' | 'error' | 'info'

export interface ToastOptions {
  tone?: ToastTone
  /** Headline. Kept short — this renders on one line on a phone. */
  main: string
  /** Optional second line, smaller and muted. */
  sub?: string | null
  /** Leading glyph. Falls back to a per-tone default. */
  icon?: string | null
  /** Emit a one-shot particle burst. Reserve it for genuine milestones. */
  sparkle?: boolean
  /** Lifetime in ms. */
  ttlMs?: number
}

export interface Toast extends Required<Omit<ToastOptions, 'ttlMs'>> {
  id: number
}

// Long enough to read a three-word headline at a glance, short enough that a
// fast answer streak doesn't build a backlog of stale messages.
const DEFAULT_TTL_MS = 2200
// Above this, the oldest is dropped. A tall stack on a phone would cover the
// choice grid, which is the exact failure the toast is meant to avoid.
const MAX_VISIBLE = 3

const DEFAULT_ICONS: Record<ToastTone, string> = {
  success: '✓',
  warn: '⏱',
  error: '✕',
  info: 'ℹ',
}

export const useToastStore = defineStore('toasts', () => {
  const toasts = ref<Toast[]>([])
  let seq = 0
  // id -> pending expiry timer, so dismiss()/update() can cancel or restart it
  // without leaking a callback that fires against a removed toast.
  const timers = new Map<number, ReturnType<typeof setTimeout>>()

  function clearTimer(id: number) {
    const t = timers.get(id)
    if (t !== undefined) {
      clearTimeout(t)
      timers.delete(id)
    }
  }

  function arm(id: number, ttlMs: number) {
    clearTimer(id)
    timers.set(
      id,
      setTimeout(() => {
        timers.delete(id)
        dismiss(id)
      }, ttlMs),
    )
  }

  function push(opts: ToastOptions): number {
    const id = ++seq
    const tone = opts.tone ?? 'info'
    toasts.value.push({
      id,
      tone,
      main: opts.main,
      sub: opts.sub ?? null,
      icon: opts.icon ?? DEFAULT_ICONS[tone],
      sparkle: opts.sparkle ?? false,
    })
    // Trim from the front — oldest goes first.
    while (toasts.value.length > MAX_VISIBLE) {
      const dropped = toasts.value.shift()
      if (dropped) clearTimer(dropped.id)
    }
    arm(id, opts.ttlMs ?? DEFAULT_TTL_MS)
    return id
  }

  /**
   * Amend a toast that's already on screen.
   *
   * The MC answer path grades client-side and doesn't await the POST, so the
   * mastery level isn't known when the toast first appears. Rather than delay
   * the feedback (or block on the network) we show the verdict immediately and
   * fold the level in when the response lands. A no-op if the toast has already
   * expired, which is the common case on a slow request — by then the user has
   * moved on and retroactively popping the level back up would be noise.
   */
  function update(id: number, patch: Partial<ToastOptions>): void {
    const t = toasts.value.find((x) => x.id === id)
    if (!t) return
    if (patch.tone !== undefined) t.tone = patch.tone
    if (patch.main !== undefined) t.main = patch.main
    if (patch.sub !== undefined) t.sub = patch.sub ?? null
    if (patch.icon !== undefined) t.icon = patch.icon ?? DEFAULT_ICONS[t.tone]
    if (patch.sparkle !== undefined) t.sparkle = patch.sparkle
    if (patch.ttlMs !== undefined) arm(id, patch.ttlMs)
  }

  function dismiss(id: number): void {
    clearTimer(id)
    const i = toasts.value.findIndex((t) => t.id === id)
    if (i >= 0) toasts.value.splice(i, 1)
  }

  /** Drop everything — used when a round ends or the view unmounts. */
  function clear(): void {
    for (const id of timers.keys()) clearTimeout(timers.get(id)!)
    timers.clear()
    toasts.value = []
  }

  return { toasts, push, update, dismiss, clear }
})
