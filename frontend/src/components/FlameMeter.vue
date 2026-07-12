<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'

// Presentational "feed-the-flame" meter. Pure CSS/GPU (transform / opacity /
// filter only — this is a mobile PWA). The parent owns all game state; we just
// render it:
//   - `heat` (0..1) drives flame height + glow.
//   - `combo` warms the colour from orange (low) to white-hot (high).
//   - `event` is a one-shot {type,id} signal that replays a spark burst
//     (fast/ok) or a gutter puff (miss/burnout). The id changes every event so
//     a repeated type still re-triggers via the keyed remount.
//   - `paused` freezes the flicker (reveal dwell / intro).
const props = defineProps<{
  heat: number
  combo: number
  event: { type: string; id: number } | null
  paused?: boolean
}>()

const SPARK_COUNT = 8

// Clamp + map. The ember never fully vanishes, so floor the visual intensity a
// touch above zero.
const intensity = computed(() => Math.max(0.08, Math.min(1, props.heat)))
// Combo "temperature": the PRIMARY build-up signal. heat caps after ~3 answers
// (it's also the per-question timer), so combo is what keeps the blaze visibly
// growing across a streak — taller, wider, whiter — and it carries between
// rounds. Saturates around a combo of 12 so the fire reaches a real roar within
// the combo range a normal (short) round actually hits, not only at combo 20+.
const temp = computed(() => Math.max(0, Math.min(1, props.combo / 12)))

const flameStyle = computed(() => ({
  '--intensity': intensity.value.toFixed(3),
  '--temp': temp.value.toFixed(3),
}))

// One-shot effect overlay. `effectKey` (the event id) keys the wrapper so each
// event remounts it and restarts the CSS animation.
const effect = ref<string | null>(null)
const effectKey = ref(0)
let clearTimer: ReturnType<typeof setTimeout> | undefined

// The flame body sits inside `.flame-pop`; we run a transient "surge" on it via
// the Web Animations API so each correct answer punches the fire upward (then it
// settles to its new, higher combo baseline). WAAPI on the WRAPPER composes with
// the inner flame's variable base transform instead of clobbering it, and leaves
// the smooth heat-decay / miss-deflate transitions on `.flame` untouched.
const popEl = ref<HTMLElement | null>(null)
const reduceMotion =
  typeof window !== 'undefined' &&
  window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

function surge(type: string) {
  const el = popEl.value
  if (!el || reduceMotion) return
  // Bigger whoosh on a fast clear; momentum grows with the combo (temp).
  const big = type === 'fast'
  const peak = (big ? 0.34 : 0.16) + 0.16 * temp.value
  el.animate(
    [
      { transform: 'scaleY(1) scaleX(1)' },
      { transform: `scaleY(${1 + peak}) scaleX(${1 + peak * 0.42})`, offset: 0.26 },
      { transform: 'scaleY(1) scaleX(1)' },
    ],
    { duration: big ? 480 : 380, easing: 'cubic-bezier(0.2, 0.9, 0.3, 1)' },
  )
}

watch(
  () => props.event,
  (e) => {
    if (!e) return
    effect.value = e.type
    effectKey.value = e.id
    if (e.type === 'fast' || e.type === 'ok') surge(e.type)
    clearTimeout(clearTimer)
    clearTimer = setTimeout(() => {
      effect.value = null
    }, 950)
  },
)
onUnmounted(() => clearTimeout(clearTimer))

const isSpark = computed(() => effect.value === 'fast' || effect.value === 'ok')
const isPuff = computed(() => effect.value === 'miss' || effect.value === 'burnout')

// Fan the sparks out in an arc, with index-derived (deterministic) spread so a
// burst looks lively without per-frame randomness.
function sparkStyle(i: number) {
  const t = (i - 1) / (SPARK_COUNT - 1) // 0..1
  const angle = -72 + t * 144 // degrees, fanned around vertical
  const rad = (angle * Math.PI) / 180
  const dist = 56 + ((i * 37) % 46) // 56..101px pseudo-random
  const dx = Math.sin(rad) * dist
  const dy = -Math.cos(rad) * dist - 26
  return {
    '--dx': `${dx.toFixed(1)}px`,
    '--dy': `${dy.toFixed(1)}px`,
    '--delay': `${Math.round(t * 70)}ms`,
  }
}
</script>

<template>
  <!-- --intensity / --temp live on the stage so the colour vars (--core/--rim,
       defined on .flame-stage from --temp) resolve, and the flame + blobs +
       ember all inherit them. Setting them on .flame instead leaves the stage's
       --core/--rim invalid → the blobs paint transparent (flame body vanishes,
       only the fallback-coloured sparks show). -->
  <div class="flame-stage" :class="{ paused }" :style="flameStyle">
    <div ref="popEl" class="flame-pop">
      <div class="flame">
        <div class="blob blob-outer"></div>
        <div class="blob blob-mid"></div>
        <div class="blob blob-inner"></div>
        <div class="blob blob-core"></div>
      </div>
    </div>
    <div class="ember-base"></div>

    <!-- One-shot feedback overlay, remounted per event id -->
    <div class="fx" :key="effectKey">
      <template v-if="isSpark">
        <span
          v-for="i in SPARK_COUNT"
          :key="i"
          class="spark"
          :class="effect"
          :style="sparkStyle(i)"
        ></span>
      </template>
      <span v-else-if="isPuff" class="puff" :class="effect"></span>
    </div>
  </div>
</template>

<style scoped>
.flame-stage {
  position: relative;
  height: 190px;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  overflow: visible;
  /* Warm palette; combo (--temp) mixes from warm -> white-hot. */
  --c-warm: #ff5a1f;
  --c-mid: #ffae2b;
  --c-hot: #ffe49a;
  --c-white: #fff7e0;
  --core: color-mix(in srgb, var(--c-white) calc(var(--temp) * 100%), var(--c-mid));
  --rim: color-mix(in srgb, var(--c-hot) calc(var(--temp) * 100%), var(--c-warm));
}

/* Surge wrapper: WAAPI punches a transient scale-pop here on each correct answer
   (see surge() in the script). It composes multiplicatively with the flame's own
   variable transform below, so the pop never clobbers the combo/heat base size. */
.flame-pop {
  position: relative;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  transform-origin: bottom center;
  will-change: transform;
}

/* Flame size is COMBO-led: heat (--intensity) gives a solid, always-visible base
   flame + flicker, but the big growth comes from --temp (combo), which climbs the
   whole streak and carries across rounds. Even at combo 0 this is a real fire
   (not a dim ember); by a roar of a combo it fills the meter. */
.flame {
  position: relative;
  width: 96px;
  height: 112px;
  transform-origin: bottom center;
  transform: scaleY(calc(0.55 + 0.35 * var(--intensity) + 0.78 * var(--temp)))
    scaleX(calc(0.7 + 0.2 * var(--intensity) + 0.34 * var(--temp)));
  opacity: calc(0.7 + 0.3 * var(--intensity));
  transition: transform 0.34s ease, opacity 0.34s ease;
  filter: drop-shadow(
    0 0 calc(16px + 30px * var(--temp))
      color-mix(in srgb, var(--rim) calc(60% + 30% * var(--temp)), transparent)
  );
}

.blob {
  position: absolute;
  left: 50%;
  bottom: 0;
  border-radius: 50% 50% 48% 48% / 64% 64% 36% 36%;
  transform: translateX(-50%);
  will-change: transform;
}
.blob-outer {
  width: 92px;
  height: 110px;
  background: radial-gradient(
    ellipse 60% 70% at 50% 78%,
    color-mix(in srgb, var(--rim) 92%, transparent) 0%,
    color-mix(in srgb, var(--rim) 44%, transparent) 58%,
    transparent 76%
  );
  filter: blur(5px);
  animation: flicker-a 1.5s ease-in-out infinite;
}
.blob-mid {
  width: 68px;
  height: 92px;
  background: radial-gradient(
    ellipse 60% 70% at 50% 80%,
    color-mix(in srgb, var(--core) 75%, var(--rim)) 0%,
    color-mix(in srgb, var(--rim) 82%, transparent) 62%,
    transparent 80%
  );
  filter: blur(3px);
  animation: flicker-b 1.1s ease-in-out infinite;
}
.blob-inner {
  width: 44px;
  height: 70px;
  background: radial-gradient(
    ellipse 60% 70% at 50% 82%,
    var(--core) 0%,
    color-mix(in srgb, var(--core) 60%, var(--rim)) 58%,
    transparent 82%
  );
  filter: blur(2px);
  animation: flicker-a 0.85s ease-in-out infinite;
}
.blob-core {
  width: 24px;
  height: 46px;
  bottom: 8px;
  background: radial-gradient(
    ellipse 60% 70% at 50% 80%,
    #fffdf5 0%,
    var(--core) 62%,
    transparent 82%
  );
  filter: blur(1px);
  animation: flicker-b 0.7s ease-in-out infinite;
}

/* The glowing coal bed under the flame; always present so a dead meter still
   reads as a dim ember rather than nothing. */
.ember-base {
  position: absolute;
  bottom: 2px;
  left: 50%;
  transform: translateX(-50%);
  width: calc(48px + 24px * var(--intensity) + 28px * var(--temp));
  height: 15px;
  border-radius: 50%;
  background: radial-gradient(
    ellipse at center,
    color-mix(in srgb, var(--rim) 85%, #ff3b00) 0%,
    color-mix(in srgb, #ff3b00 50%, transparent) 60%,
    transparent 75%
  );
  filter: blur(3px);
  opacity: calc(0.5 + 0.5 * var(--intensity));
  transition: width 0.35s ease, opacity 0.35s ease;
  animation: ember-pulse 1.3s ease-in-out infinite;
}

.paused .blob,
.paused .ember-base {
  animation-play-state: paused;
}

@keyframes flicker-a {
  0%, 100% { transform: translateX(-50%) scaleX(1) scaleY(1); }
  35% { transform: translateX(-54%) scaleX(0.94) scaleY(1.05); }
  70% { transform: translateX(-47%) scaleX(1.05) scaleY(0.97); }
}
@keyframes flicker-b {
  0%, 100% { transform: translateX(-50%) scaleX(1) scaleY(1); }
  40% { transform: translateX(-46%) scaleX(1.06) scaleY(0.95); }
  75% { transform: translateX(-53%) scaleX(0.95) scaleY(1.06); }
}
@keyframes ember-pulse {
  0%, 100% { opacity: calc(0.45 + 0.45 * var(--intensity)); }
  50% { opacity: calc(0.65 + 0.35 * var(--intensity)); }
}

/* One-shot feedback overlay */
.fx {
  position: absolute;
  bottom: 24px;
  left: 50%;
  width: 0;
  height: 0;
  pointer-events: none;
}
.spark {
  position: absolute;
  left: 0;
  bottom: 0;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--core, #ffe49a);
  box-shadow: 0 0 6px var(--rim, #ff5a1f);
  opacity: 0;
  animation: spark-fly 0.85s ease-out var(--delay, 0ms) forwards;
}
.spark.ok {
  width: 5px;
  height: 5px;
  opacity: 0;
}
@keyframes spark-fly {
  0% { transform: translate(0, 0) scale(1); opacity: 1; }
  100% { transform: translate(var(--dx), var(--dy)) scale(0.2); opacity: 0; }
}

/* Gutter puff on a miss / burnout: a desaturated smoke ring that rises + fades. */
.puff {
  position: absolute;
  left: -16px;
  bottom: -8px;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: radial-gradient(
    ellipse at center,
    rgba(140, 140, 150, 0.5) 0%,
    rgba(120, 120, 130, 0.2) 55%,
    transparent 72%
  );
  filter: blur(3px);
  opacity: 0;
  animation: puff-rise 0.9s ease-out forwards;
}
.puff.burnout {
  background: radial-gradient(
    ellipse at center,
    rgba(160, 160, 170, 0.65) 0%,
    rgba(120, 120, 130, 0.25) 55%,
    transparent 72%
  );
}
@keyframes puff-rise {
  0% { transform: translateY(0) scale(0.6); opacity: 0.75; }
  100% { transform: translateY(-48px) scale(1.6); opacity: 0; }
}

@media (prefers-reduced-motion: reduce) {
  .blob,
  .ember-base,
  .spark,
  .puff {
    animation: none !important;
  }
}
</style>
