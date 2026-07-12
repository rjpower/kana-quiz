<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'

const props = defineProps<{
  durationMs: number
  resetKey: string | number
  paused?: boolean
}>()
const emit = defineEmits<{ (e: 'timeout'): void }>()

const remaining = ref(props.durationMs)
let raf = 0
let startedAt = 0

function tick(t: number) {
  if (props.paused) {
    raf = requestAnimationFrame(tick)
    return
  }
  if (!startedAt) startedAt = t
  remaining.value = Math.max(0, props.durationMs - (t - startedAt))
  if (remaining.value <= 0) {
    emit('timeout')
    return
  }
  raf = requestAnimationFrame(tick)
}

function start() {
  cancelAnimationFrame(raf)
  startedAt = 0
  remaining.value = props.durationMs
  raf = requestAnimationFrame(tick)
}

watch(() => props.resetKey, start)

onMounted(start)
onUnmounted(() => cancelAnimationFrame(raf))
</script>

<template>
  <div class="countdown">
    <div
      class="countdown-fill"
      :style="{ width: (remaining / durationMs) * 100 + '%' }"
    ></div>
    <span class="countdown-label">{{ (remaining / 1000).toFixed(1) }}s</span>
  </div>
</template>

<style scoped>
.countdown {
  position: relative;
  height: 14px;
  background: var(--panel-hi);
  border-radius: 7px;
  overflow: hidden;
  margin-bottom: 20px;
}
.countdown-fill {
  position: absolute;
  inset: 0 auto 0 0;
  background: linear-gradient(90deg, var(--accent), #7ac0ff);
  transition: none;
}
.countdown-label {
  position: absolute;
  right: 10px;
  top: 50%;
  transform: translateY(-50%);
  font-size: 11px;
  color: var(--text);
  font-variant-numeric: tabular-nums;
  mix-blend-mode: difference;
}
</style>
