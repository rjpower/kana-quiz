<script setup lang="ts">
// Renders the toast queue as a fixed strip. Mounted once in App.vue so any
// route can push without wiring its own host.
//
// The whole strip is pointer-events:none. That is the load-bearing property,
// not a nicety: toasts appear mid-question and would otherwise be able to eat a
// tap meant for a choice card. Nothing in here is interactive, so there is
// nothing to lose by making it untouchable.
import { useToastStore } from '../stores/toasts'

const toasts = useToastStore()

// Fixed count so the markup stays static; positions come from --i in CSS.
const SPARKS = 6
</script>

<template>
  <div class="toast-host" aria-live="polite" aria-atomic="false">
    <TransitionGroup name="toast">
      <div
        v-for="t in toasts.toasts"
        :key="t.id"
        class="toast"
        :class="t.tone"
        role="status"
      >
        <span class="toast-icon" aria-hidden="true">{{ t.icon }}</span>
        <span class="toast-text">
          <span class="toast-main">{{ t.main }}</span>
          <span v-if="t.sub" class="toast-sub">{{ t.sub }}</span>
        </span>
        <span v-if="t.sparkle" class="sparkle" aria-hidden="true">
          <i v-for="n in SPARKS" :key="n" :style="{ '--i': n - 1 }" />
        </span>
      </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.toast-host {
  position: fixed;
  top: calc(env(safe-area-inset-top, 0px) + 10px);
  left: 50%;
  transform: translateX(-50%);
  z-index: 60;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  /* See the note in the script block — never intercept a tap. */
  pointer-events: none;
  width: max-content;
  max-width: min(92vw, 420px);
}

.toast {
  position: relative;
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 8px 14px;
  border-radius: 999px;
  border: 1px solid var(--border);
  /* Slightly translucent + blurred so it reads as an overlay rather than a
     panel that's part of the page, and so the card underneath stays legible.
     The opaque declaration first is the fallback: an unsupported color-mix
     invalidates the whole declaration, and without it the toast would render
     on a transparent background directly over the card — unreadable. */
  background: var(--panel);
  background: color-mix(in srgb, var(--panel) 88%, transparent);
  backdrop-filter: blur(6px);
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.45);
  font-size: 13px;
  line-height: 1.25;
  max-width: 100%;
}

.toast-icon {
  font-size: 15px;
  line-height: 1;
  flex: none;
}
.toast-text {
  display: flex;
  align-items: baseline;
  gap: 7px;
  min-width: 0;
}
.toast-main {
  font-weight: 650;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.toast-sub {
  color: var(--muted);
  font-size: 11px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  white-space: nowrap;
  flex: none;
}

/* Tones: a tinted border + icon rather than a saturated fill, so a correct
   answer registers peripherally without pulling the eye off the card. */
.toast.success {
  border-color: rgba(46, 204, 113, 0.45);
  color: #b6f2cf;
}
.toast.success .toast-icon { color: #4ade80; }
.toast.warn {
  border-color: rgba(255, 179, 71, 0.45);
  color: #ffd98a;
}
.toast.warn .toast-icon { color: #ffb347; }
.toast.error {
  border-color: rgba(255, 107, 107, 0.42);
  color: #ffc2c2;
}
.toast.error .toast-icon { color: #ff6b6b; }
.toast.info { color: #cdd6e6; }
.toast.info .toast-icon { color: var(--accent); }

/* ---- Sparkle: one-shot burst for an actual mastery promotion ---- */
.sparkle {
  position: absolute;
  left: 16px;
  top: 50%;
  width: 0;
  height: 0;
}
.sparkle i {
  position: absolute;
  width: 4px;
  height: 4px;
  margin: -2px 0 0 -2px;
  border-radius: 50%;
  background: #ffe27a;
  box-shadow: 0 0 6px rgba(255, 226, 122, 0.9);
  /* Each spark gets its own angle off --i; one pass, then gone. */
  transform: rotate(calc(var(--i) * 60deg)) translateX(0);
  animation: spark 620ms ease-out both;
  animation-delay: calc(var(--i) * 18ms);
}
@keyframes spark {
  0% { opacity: 0; transform: rotate(calc(var(--i) * 60deg)) translateX(0) scale(0.6); }
  25% { opacity: 1; }
  100% { opacity: 0; transform: rotate(calc(var(--i) * 60deg)) translateX(22px) scale(0.9); }
}

/* ---- Enter / leave ---- */
.toast-enter-active { transition: opacity 0.18s ease-out, transform 0.18s cubic-bezier(0.2, 1.3, 0.4, 1); }
.toast-leave-active { transition: opacity 0.22s ease-in, transform 0.22s ease-in; position: absolute; }
.toast-enter-from { opacity: 0; transform: translateY(-8px) scale(0.94); }
.toast-leave-to { opacity: 0; transform: translateY(-6px) scale(0.98); }
.toast-move { transition: transform 0.18s ease; }

@media (prefers-reduced-motion: reduce) {
  .toast-enter-active,
  .toast-leave-active,
  .toast-move { transition: opacity 0.12s linear; }
  .toast-enter-from,
  .toast-leave-to { transform: none; }
  .sparkle { display: none; }
}
</style>
