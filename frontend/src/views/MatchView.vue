<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import MatchGrid from '../components/MatchGrid.vue'
import { MATCH_GOAL, MATCH_TIME_MS, useSessionStore } from '../stores/session'

const store = useSessionStore()
const router = useRouter()

// The match round owns its own always-running countdown (Countdown.vue pauses
// on locked/intro, which don't apply here). rAF so it stays smooth.
const remainingMs = ref(MATCH_TIME_MS)
let raf = 0
let startedAt = 0

function tick(t: number) {
  if (!startedAt) startedAt = t
  remainingMs.value = Math.max(0, MATCH_TIME_MS - (t - startedAt))
  if (remainingMs.value <= 0) {
    void store.endMatch()
    return
  }
  if (store.matchActive) raf = requestAnimationFrame(tick)
}

function begin() {
  cancelAnimationFrame(raf)
  startedAt = 0
  remainingMs.value = MATCH_TIME_MS
  void store.startMatch().then(() => {
    if (store.matchActive) raf = requestAnimationFrame(tick)
  })
}

onMounted(begin)
onBeforeUnmount(() => {
  cancelAnimationFrame(raf)
  store.matchReset()
})

const seconds = computed(() => Math.ceil(remainingMs.value / 1000))
const timePct = computed(() => (remainingMs.value / MATCH_TIME_MS) * 100)
const goalPct = computed(() => Math.min(100, (store.matchMatched / MATCH_GOAL) * 100))
const accuracy = computed(() => {
  const total = store.matchMatched + store.matchMisses.length
  return total === 0 ? 0 : store.matchMatched / total
})
const reachedGoal = computed(() => store.matchMatched >= MATCH_GOAL)
const loading = computed(
  () => store.matchActive && store.matchPrompts.length === 0,
)
</script>

<template>
  <!-- Summary -->
  <section v-if="store.matchComplete" class="panel match-summary">
    <div class="emoji">{{ reachedGoal ? '⚡' : '⏱️' }}</div>
    <h2>{{ reachedGoal ? 'Goal smashed!' : "Time!" }}</h2>
    <div class="stat-grid">
      <div class="stat">
        <div class="label">Matched</div>
        <div class="value">{{ store.matchMatched }}</div>
      </div>
      <div class="stat">
        <div class="label">Best combo</div>
        <div class="value">{{ store.matchBestStreak }}</div>
      </div>
      <div class="stat">
        <div class="label">Points</div>
        <div class="value">{{ store.matchScore }}</div>
      </div>
      <div class="stat">
        <div class="label">Accuracy</div>
        <div class="value">{{ (accuracy * 100).toFixed(0) }}%</div>
      </div>
    </div>

    <div v-if="store.matchMisses.length" class="missed">
      <div class="missed-title">Words to revisit</div>
      <ul>
        <li v-for="m in store.matchMisses" :key="m.word_id">{{ m.prompt }}</li>
      </ul>
    </div>

    <button class="btn primary" type="button" @click="begin">Play again</button>
    <button class="btn secondary" type="button" @click="router.push('/study')">
      Back to study
    </button>
  </section>

  <!-- Active board -->
  <section v-else class="panel match-play">
    <div class="match-hud">
      <span class="hud-chip">⏱ {{ seconds }}s</span>
      <span class="hud-chip">{{ store.matchMatched }} / {{ MATCH_GOAL }}</span>
      <span class="hud-chip">{{ store.matchScore }} pts</span>
      <span v-if="store.matchStreak >= 2" class="hud-chip streak">🔥 {{ store.matchStreak }}</span>
    </div>
    <div class="bar time-bar"><div class="bar-fill time-fill" :style="{ width: timePct + '%' }"></div></div>
    <div class="bar goal-bar"><div class="bar-fill goal-fill" :style="{ width: goalPct + '%' }"></div></div>

    <p class="match-instructions">Tap a prompt, then its match. Beat the clock!</p>

    <div v-if="loading" class="muted match-loading">
      <span class="spinner" aria-hidden="true"></span>
      <span>Dealing the board…</span>
    </div>
    <MatchGrid
      v-else
      :prompts="store.matchPrompts"
      :answers="store.matchAnswers"
      @tap="store.onMatchTap"
    />

    <button class="btn ghost end-btn" type="button" @click="store.endMatch()">
      End round
    </button>
  </section>
</template>

<style scoped>
.match-play { text-align: center; }
.match-hud {
  display: flex;
  justify-content: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.hud-chip {
  background: var(--panel-hi);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 4px 10px;
  font-size: 14px;
  font-variant-numeric: tabular-nums;
}
.hud-chip.streak { color: #ffb347; border-color: rgba(255, 179, 71, 0.4); }
.bar {
  position: relative;
  height: 8px;
  background: var(--panel-hi);
  border-radius: 4px;
  overflow: hidden;
  margin-bottom: 6px;
}
.bar-fill { position: absolute; inset: 0 auto 0 0; transition: width 0.2s linear; }
.time-fill { background: linear-gradient(90deg, #4c9eff, #7ac0ff); }
.goal-fill { background: linear-gradient(90deg, #2ecc71, #9b6bff); transition: width 0.25s ease-out; }
.match-instructions {
  color: var(--muted);
  font-size: 13px;
  margin: 10px 0 14px;
}
.match-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 40px 0;
}
.spinner {
  display: inline-block;
  width: 16px;
  height: 16px;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.end-btn { margin-top: 18px; }

.match-summary { text-align: center; animation: pop-in 0.35s ease-out; }
@keyframes pop-in {
  from { transform: scale(0.96); opacity: 0; }
  to { transform: scale(1); opacity: 1; }
}
.match-summary .emoji { font-size: 64px; line-height: 1; }
.match-summary h2 { margin: 6px 0 2px; font-size: 26px; }
.stat-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  margin: 20px 0;
}
.stat {
  background: var(--panel-hi);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px 6px;
}
.stat .label { font-size: 11px; color: var(--muted); }
.stat .value {
  font-size: 22px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  margin-top: 2px;
}
@media (max-width: 480px) {
  .stat-grid { grid-template-columns: repeat(2, 1fr); }
}
.missed {
  text-align: left;
  background: var(--panel-hi);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px 14px;
  margin-bottom: 16px;
}
.missed-title {
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 6px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.missed ul { list-style: none; margin: 0; padding: 0; }
.missed li { padding: 3px 0; border-bottom: 1px dashed var(--border); }
.missed li:last-child { border-bottom: none; }
.btn.primary { width: 100%; padding: 14px; font-size: 16px; font-weight: 600; }
.btn.secondary {
  width: 100%;
  padding: 12px;
  margin-top: 8px;
  font-size: 14px;
  font-weight: 500;
  background: var(--panel-hi);
  color: inherit;
  border: 1px solid var(--border);
  cursor: pointer;
  border-radius: 8px;
}
.btn.secondary:hover { background: var(--border); }
.btn.ghost {
  background: transparent;
  border: 1px dashed var(--border);
  color: var(--muted);
  border-radius: 6px;
  cursor: pointer;
  padding: 8px 14px;
  font-size: 13px;
}
.btn.ghost:hover { color: inherit; border-style: solid; }
.muted { color: var(--muted); }
</style>
