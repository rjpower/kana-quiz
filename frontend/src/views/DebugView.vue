<script setup lang="ts">
// Diagnostics page for figuring out why audio doesn't play in installed-PWA
// mode even though it works in a regular browser tab.
//
// Background — what changes between tab and PWA on iOS / Android:
//
// 1. Display mode is `standalone`: `window.matchMedia('(display-mode:
//    standalone)').matches` is true (and on iOS, `navigator.standalone`).
//    Browsers sometimes apply stricter autoplay rules to standalone PWAs.
//
// 2. iOS Safari standalone has historically required that audio playback
//    be initiated DURING a user gesture (touchend/click). A `new Audio()`
//    element constructed AFTER `await fetch(...)` has already lost the
//    gesture token and `.play()` will reject with NotAllowedError — even
//    if a prior unlock was performed on a *different* Audio element.
//    Workaround: reuse one persistent Audio element across plays so the
//    initial gesture-unlock keeps applying when its `src` is swapped.
//
// 3. iOS hardware mute switch silences <audio> in standalone mode. The
//    Web Audio API is NOT muted by the switch. There's no JS API to read
//    the mute switch state, so it can look like "audio is broken" when
//    really the phone is on silent.
//
// 4. Service workers / cookies / storage all behave like the browser tab,
//    but cookies are scoped per the start_url's origin/path — if your PWA
//    was installed before a cookie-auth migration, it may have stale or
//    no auth cookies and 401s from /api/audio look like silent failures.

import { onMounted, ref } from 'vue'

const log = ref<string[]>([])
function logLine(msg: string) {
  const t = new Date().toISOString().slice(11, 23)
  log.value.unshift(`[${t}] ${msg}`)
  if (log.value.length > 200) log.value.length = 200
}

interface KV { k: string; v: string }
const env = ref<KV[]>([])
const audioCaps = ref<KV[]>([])
const swInfo = ref<KV[]>([])
const permissions = ref<KV[]>([])
const storage = ref<KV[]>([])
const apiProbe = ref<KV[]>([])

interface PrefetchStatus {
  total_words?: number
  cached?: number
  missing?: number
  key_configured?: boolean
  worker_pending_wake?: boolean
  worker_busy?: boolean
  worker_current?: string | null   // legacy field; tts still emits it
  worker_inflight?: string[]       // gemini emits this — concurrent workers
  worker_batch_done?: number
  worker_batch_total?: number
  worker_concurrency?: number
  worker_last_error?: string | null
  model?: string
  voice?: string
}
interface ServerDebug {
  db?: { path: string; size_bytes: number }
  tables?: Record<string, number>
  audio?: PrefetchStatus
  sentences?: PrefetchStatus
  sentence_cache_by_model?: { model: string; count: number }[]
}
const serverDebug = ref<ServerDebug | null>(null)
const serverDebugError = ref<string | null>(null)

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`
}

function pct(part: number, total: number): string {
  if (!total) return '—'
  return `${((part / total) * 100).toFixed(0)}%`
}

function workerLabel(s: PrefetchStatus | undefined): string {
  if (!s) return '—'
  if (!s.key_configured) return 'disabled (no API key)'
  if (s.worker_busy) {
    const total = s.worker_batch_total ?? 0
    const done = s.worker_batch_done ?? 0
    const progress = total > 0 ? ` (${done}/${total})` : ''
    const inflight = s.worker_inflight ?? []
    if (inflight.length > 0) {
      const sample = inflight.slice(0, 5).join(', ')
      const more = inflight.length > 5 ? `, +${inflight.length - 5} more` : ''
      const max = s.worker_concurrency ? `/${s.worker_concurrency}` : ''
      return `busy${progress} · ${inflight.length}${max} in flight: ${sample}${more}`
    }
    if (s.worker_current) return `busy${progress} · ${s.worker_current}`
    return `busy${progress}`
  }
  if (s.worker_pending_wake) return 'wake queued'
  return (s.missing ?? 0) > 0 ? 'idle (waiting on wakeup)' : 'idle (cache full)'
}

function detectMode(): string {
  const standalone =
    window.matchMedia?.('(display-mode: standalone)').matches ||
    (navigator as unknown as { standalone?: boolean }).standalone === true
  return standalone ? 'standalone (PWA)' : 'browser tab'
}

async function gatherEnv() {
  const m = window.matchMedia
  env.value = [
    { k: 'display mode', v: detectMode() },
    { k: 'matchMedia(standalone)', v: String(m?.('(display-mode: standalone)').matches ?? '?') },
    { k: 'matchMedia(fullscreen)', v: String(m?.('(display-mode: fullscreen)').matches ?? '?') },
    { k: 'matchMedia(minimal-ui)', v: String(m?.('(display-mode: minimal-ui)').matches ?? '?') },
    { k: 'navigator.standalone', v: String((navigator as unknown as { standalone?: boolean }).standalone ?? 'undefined') },
    { k: 'userAgent', v: navigator.userAgent },
    { k: 'platform', v: navigator.platform || '?' },
    { k: 'language', v: navigator.language },
    { k: 'online', v: String(navigator.onLine) },
    { k: 'cookieEnabled', v: String(navigator.cookieEnabled) },
    { k: 'document.cookie', v: document.cookie || '(empty)' },
    { k: 'location', v: location.href },
    { k: 'visibility', v: document.visibilityState },
    { k: 'devicePixelRatio', v: String(window.devicePixelRatio) },
    { k: 'viewport', v: `${window.innerWidth}×${window.innerHeight}` },
  ]
}

function gatherAudioCaps() {
  const a = document.createElement('audio')
  audioCaps.value = [
    { k: 'canPlayType audio/mpeg', v: a.canPlayType('audio/mpeg') || '(no)' },
    { k: 'canPlayType audio/mp4', v: a.canPlayType('audio/mp4') || '(no)' },
    { k: 'canPlayType audio/wav', v: a.canPlayType('audio/wav') || '(no)' },
    { k: 'canPlayType audio/ogg', v: a.canPlayType('audio/ogg') || '(no)' },
    { k: 'AudioContext', v: typeof (window.AudioContext || (window as unknown as { webkitAudioContext?: unknown }).webkitAudioContext) === 'function' ? 'yes' : 'no' },
    { k: 'MediaSession', v: 'mediaSession' in navigator ? 'yes' : 'no' },
  ]
  // AudioContext state — only created lazily because some browsers count
  // `new AudioContext()` against autoplay heuristics.
  try {
    const Ctor = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
    if (Ctor) {
      const ctx = new Ctor()
      audioCaps.value.push({ k: 'AudioContext.state', v: ctx.state })
      audioCaps.value.push({ k: 'AudioContext.sampleRate', v: String(ctx.sampleRate) })
      void ctx.close()
    }
  } catch (e) {
    audioCaps.value.push({ k: 'AudioContext error', v: (e as Error).message })
  }
}

async function gatherSW() {
  if (!('serviceWorker' in navigator)) {
    swInfo.value = [{ k: 'serviceWorker', v: 'unsupported' }]
    return
  }
  const regs = await navigator.serviceWorker.getRegistrations()
  swInfo.value = [
    { k: 'controller', v: navigator.serviceWorker.controller ? navigator.serviceWorker.controller.scriptURL : '(none)' },
    { k: 'registrations', v: String(regs.length) },
  ]
  regs.forEach((r, i) => {
    swInfo.value.push({
      k: `reg[${i}] scope`,
      v: r.scope,
    })
    swInfo.value.push({
      k: `reg[${i}] active`,
      v: r.active ? `${r.active.scriptURL} (${r.active.state})` : '(none)',
    })
  })
}

async function gatherPermissions() {
  if (!('permissions' in navigator)) {
    permissions.value = [{ k: 'permissions API', v: 'unsupported' }]
    return
  }
  const names = ['microphone', 'notifications', 'persistent-storage', 'background-sync'] as const
  const out: KV[] = []
  for (const name of names) {
    try {
      const status = await navigator.permissions.query({ name: name as PermissionName })
      out.push({ k: name, v: status.state })
    } catch (e) {
      out.push({ k: name, v: `error: ${(e as Error).message}` })
    }
  }
  permissions.value = out
}

async function gatherStorage() {
  const out: KV[] = []
  try {
    out.push({ k: 'localStorage keys', v: String(localStorage.length) })
  } catch {
    out.push({ k: 'localStorage', v: '(blocked)' })
  }
  if ('storage' in navigator && 'estimate' in navigator.storage) {
    try {
      const est = await navigator.storage.estimate()
      out.push({ k: 'storage.usage', v: `${est.usage} / ${est.quota}` })
    } catch (e) {
      out.push({ k: 'storage.estimate', v: `error: ${(e as Error).message}` })
    }
  }
  if ('storage' in navigator && 'persisted' in navigator.storage) {
    try {
      out.push({ k: 'storage.persisted', v: String(await navigator.storage.persisted()) })
    } catch { /* ignore */ }
  }
  storage.value = out
}

async function probeApi() {
  const out: KV[] = []
  try {
    const r = await fetch('/api/session/upcoming?n=1', { credentials: 'include' })
    out.push({ k: 'GET /api/session/upcoming', v: `${r.status} ${r.statusText}` })
  } catch (e) {
    out.push({ k: 'GET /api/session/upcoming', v: `error: ${(e as Error).message}` })
  }
  try {
    const r = await fetch('/api/stats', { credentials: 'include' })
    out.push({ k: 'GET /api/stats', v: `${r.status} ${r.statusText}` })
  } catch (e) {
    out.push({ k: 'GET /api/stats', v: `error: ${(e as Error).message}` })
  }
  apiProbe.value = out
}

// ---- audio playback strategies ----

let sharedAudio: HTMLAudioElement | null = null
const unlocked = ref(false)
const SILENT_WAV =
  'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA='

function ensureShared(): HTMLAudioElement {
  if (!sharedAudio) {
    sharedAudio = new Audio()
    sharedAudio.preload = 'auto'
    for (const ev of ['play', 'playing', 'ended', 'pause', 'stalled', 'suspend', 'error', 'abort']) {
      sharedAudio.addEventListener(ev, () => {
        const code = sharedAudio?.error?.code
        logLine(`shared event: ${ev}${code ? ` errCode=${code}` : ''}`)
      })
    }
  }
  return sharedAudio
}

async function unlock() {
  const a = ensureShared()
  a.src = SILENT_WAV
  try {
    await a.play()
    a.pause()
    a.currentTime = 0
    unlocked.value = true
    logLine('unlock: silent primer played OK')
  } catch (e) {
    logLine(`unlock FAILED: ${(e as Error).name}: ${(e as Error).message}`)
  }
}

async function fetchFirstWordId(): Promise<number | null> {
  const r = await fetch('/api/session/upcoming?n=1', { credentials: 'include' })
  if (!r.ok) {
    logLine(`upcoming returned ${r.status}`)
    return null
  }
  const j = await r.json()
  return (j.word_ids?.[0] as number | undefined) ?? null
}

async function testShared() {
  logLine('--- test: shared Audio element ---')
  const id = await fetchFirstWordId()
  if (!id) return
  const a = ensureShared()
  try {
    const resp = await fetch(`/api/audio/word/${id}`)
    logLine(`fetched audio status=${resp.status}`)
    if (!resp.ok) return
    const blob = await resp.blob()
    logLine(`blob size=${blob.size} type=${blob.type}`)
    a.src = URL.createObjectURL(blob)
    await a.play()
    logLine('shared play() resolved')
  } catch (e) {
    logLine(`shared play FAILED: ${(e as Error).name}: ${(e as Error).message}`)
  }
}

async function testFresh() {
  logLine('--- test: new Audio() per play ---')
  const id = await fetchFirstWordId()
  if (!id) return
  try {
    const resp = await fetch(`/api/audio/word/${id}`)
    if (!resp.ok) return
    const blob = await resp.blob()
    const audio = new Audio(URL.createObjectURL(blob))
    await audio.play()
    logLine('fresh play() resolved')
  } catch (e) {
    logLine(`fresh play FAILED: ${(e as Error).name}: ${(e as Error).message}`)
  }
}

async function testWebAudio() {
  logLine('--- test: Web Audio API (bypasses iOS mute switch in some cases) ---')
  const id = await fetchFirstWordId()
  if (!id) return
  try {
    const Ctor = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
    if (!Ctor) {
      logLine('no AudioContext')
      return
    }
    const ctx = new Ctor()
    if (ctx.state === 'suspended') {
      await ctx.resume()
      logLine(`resumed ctx, state=${ctx.state}`)
    }
    const resp = await fetch(`/api/audio/word/${id}`)
    if (!resp.ok) return
    const buf = await resp.arrayBuffer()
    const decoded = await ctx.decodeAudioData(buf)
    const src = ctx.createBufferSource()
    src.buffer = decoded
    src.connect(ctx.destination)
    src.start()
    logLine(`webaudio start, ctx.state=${ctx.state}, dur=${decoded.duration.toFixed(2)}s`)
  } catch (e) {
    logLine(`webaudio FAILED: ${(e as Error).name}: ${(e as Error).message}`)
  }
}

async function gatherServerDebug() {
  serverDebugError.value = null
  try {
    const r = await fetch('/api/stats/debug', { credentials: 'include' })
    if (!r.ok) {
      serverDebugError.value = `HTTP ${r.status} ${r.statusText}`
      serverDebug.value = null
      return
    }
    serverDebug.value = await r.json()
  } catch (e) {
    serverDebugError.value = (e as Error).message
    serverDebug.value = null
  }
}

async function refreshAll() {
  await Promise.all([
    gatherEnv(), gatherAudioCaps(), gatherSW(), gatherPermissions(),
    gatherStorage(), probeApi(), gatherServerDebug(),
  ])
  logLine('refreshed diagnostics')
}

async function unregisterSW() {
  if (!('serviceWorker' in navigator)) return
  const regs = await navigator.serviceWorker.getRegistrations()
  for (const r of regs) {
    const ok = await r.unregister()
    logLine(`unregister ${r.scope}: ${ok}`)
  }
  await gatherSW()
}

onMounted(() => {
  logLine(`mode: ${detectMode()}`)
  void refreshAll()
})
</script>

<template>
  <section class="panel">
    <h2>Debug</h2>
    <p class="muted">
      Audio + PWA diagnostics. Run from inside the installed app to compare
      against a regular browser tab.
    </p>
    <div class="actions">
      <button class="btn" @click="refreshAll">Refresh all</button>
      <button class="btn" @click="log = []">Clear log</button>
      <button class="btn danger" @click="unregisterSW">Unregister service workers</button>
    </div>
  </section>

  <section class="panel">
    <h3>Server: database & prefetch workers</h3>
    <p v-if="serverDebugError" class="error">Error: {{ serverDebugError }}</p>
    <div v-if="serverDebug">
      <h4 class="sub">SQLite</h4>
      <table><tbody>
        <tr><td>path</td><td>{{ serverDebug.db?.path }}</td></tr>
        <tr><td>size on disk</td><td>{{ formatBytes(serverDebug.db?.size_bytes ?? 0) }}</td></tr>
        <tr v-for="(n, k) in serverDebug.tables" :key="k"><td>rows: {{ k }}</td><td>{{ n }}</td></tr>
      </tbody></table>

      <h4 class="sub">Audio (TTS) prefetch</h4>
      <table v-if="serverDebug.audio"><tbody>
        <tr><td>model</td><td>{{ serverDebug.audio.model }} / voice {{ serverDebug.audio.voice }}</td></tr>
        <tr><td>API key</td>
          <td :class="serverDebug.audio.key_configured ? 'ok' : 'bad'">
            {{ serverDebug.audio.key_configured ? 'configured' : 'missing' }}
          </td>
        </tr>
        <tr><td>cached / total</td>
          <td>
            {{ serverDebug.audio.cached }} / {{ serverDebug.audio.total_words }}
            ({{ pct(serverDebug.audio.cached ?? 0, serverDebug.audio.total_words ?? 0) }})
          </td>
        </tr>
        <tr><td>missing</td>
          <td :class="(serverDebug.audio.missing ?? 0) > 0 ? 'warn' : 'ok'">
            {{ serverDebug.audio.missing }}
          </td>
        </tr>
        <tr><td>worker</td>
          <td :class="serverDebug.audio.worker_busy ? 'warn' : 'ok'">
            {{ workerLabel(serverDebug.audio) }}
          </td>
        </tr>
        <tr v-if="serverDebug.audio.worker_last_error">
          <td>last error</td>
          <td class="bad">{{ serverDebug.audio.worker_last_error }}</td>
        </tr>
      </tbody></table>

      <h4 class="sub">Sentences (Gemini) prefetch</h4>
      <table v-if="serverDebug.sentences"><tbody>
        <tr><td>model</td><td>{{ serverDebug.sentences.model }}</td></tr>
        <tr><td>API key</td>
          <td :class="serverDebug.sentences.key_configured ? 'ok' : 'bad'">
            {{ serverDebug.sentences.key_configured ? 'configured' : 'missing' }}
          </td>
        </tr>
        <tr><td>cached / total</td>
          <td>
            {{ serverDebug.sentences.cached }} / {{ serverDebug.sentences.total_words }}
            ({{ pct(serverDebug.sentences.cached ?? 0, serverDebug.sentences.total_words ?? 0) }})
          </td>
        </tr>
        <tr><td>missing</td>
          <td :class="(serverDebug.sentences.missing ?? 0) > 0 ? 'warn' : 'ok'">
            {{ serverDebug.sentences.missing }}
          </td>
        </tr>
        <tr><td>worker</td>
          <td :class="serverDebug.sentences.worker_busy ? 'warn' : 'ok'">
            {{ workerLabel(serverDebug.sentences) }}
          </td>
        </tr>
        <tr v-if="serverDebug.sentences.worker_last_error">
          <td>last error</td>
          <td class="bad">{{ serverDebug.sentences.worker_last_error }}</td>
        </tr>
      </tbody></table>

      <h4
        v-if="serverDebug.sentence_cache_by_model && serverDebug.sentence_cache_by_model.length > 1"
        class="sub"
      >
        Sentence rows by model (legacy carry-over)
      </h4>
      <table v-if="serverDebug.sentence_cache_by_model && serverDebug.sentence_cache_by_model.length > 1">
        <tbody>
          <tr v-for="row in serverDebug.sentence_cache_by_model" :key="row.model">
            <td>{{ row.model }}</td><td>{{ row.count }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>

  <section class="panel">
    <h3>Audio playback test</h3>
    <p class="muted">
      Unlocked: <strong>{{ unlocked ? 'yes' : 'no' }}</strong>
    </p>
    <div class="actions">
      <button class="btn primary" @click="unlock">1. Unlock (silent primer)</button>
      <button class="btn" @click="testShared">2. Play (shared element)</button>
      <button class="btn" @click="testFresh">3. Play (fresh element)</button>
      <button class="btn" @click="testWebAudio">4. Play (Web Audio)</button>
    </div>
    <p class="hint">
      Order matters: tap <em>Unlock</em> first inside the same gesture tree.
      iOS PWA mode often allows (2) but blocks (3) with NotAllowedError —
      that's the bug your StudyView would hit.
    </p>
  </section>

  <section class="panel">
    <h3>Environment</h3>
    <table><tbody>
      <tr v-for="r in env" :key="r.k"><td>{{ r.k }}</td><td>{{ r.v }}</td></tr>
    </tbody></table>
  </section>

  <section class="panel">
    <h3>Audio capabilities</h3>
    <table><tbody>
      <tr v-for="r in audioCaps" :key="r.k"><td>{{ r.k }}</td><td>{{ r.v }}</td></tr>
    </tbody></table>
  </section>

  <section class="panel">
    <h3>Service worker</h3>
    <table><tbody>
      <tr v-for="r in swInfo" :key="r.k"><td>{{ r.k }}</td><td>{{ r.v }}</td></tr>
    </tbody></table>
  </section>

  <section class="panel">
    <h3>Permissions</h3>
    <table><tbody>
      <tr v-for="r in permissions" :key="r.k"><td>{{ r.k }}</td><td>{{ r.v }}</td></tr>
    </tbody></table>
  </section>

  <section class="panel">
    <h3>Storage</h3>
    <table><tbody>
      <tr v-for="r in storage" :key="r.k"><td>{{ r.k }}</td><td>{{ r.v }}</td></tr>
    </tbody></table>
  </section>

  <section class="panel">
    <h3>API probe</h3>
    <table><tbody>
      <tr v-for="r in apiProbe" :key="r.k"><td>{{ r.k }}</td><td>{{ r.v }}</td></tr>
    </tbody></table>
  </section>

  <section class="panel">
    <h3>Log</h3>
    <pre class="log"><code v-for="(l, i) in log" :key="i">{{ l }}
</code></pre>
  </section>
</template>

<style scoped>
.actions { display: flex; gap: 8px; flex-wrap: wrap; margin: 8px 0; }
.hint { font-size: 12px; color: var(--muted); margin-top: 8px; }
.btn.danger { color: var(--bad); }
.error { color: var(--bad); }
.sub {
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted);
  margin: 14px 0 4px;
}
td.ok { color: var(--good); }
td.warn { color: #d88a3a; }
td.bad { color: var(--bad); }

table { width: 100%; border-collapse: collapse; font-size: 13px; }
td {
  padding: 4px 6px;
  border-bottom: 1px dashed var(--border);
  vertical-align: top;
  word-break: break-all;
  font-family: ui-monospace, SFMono-Regular, monospace;
}
td:first-child { color: var(--muted); width: 38%; }

.log {
  background: var(--panel-hi);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px;
  font-size: 12px;
  max-height: 320px;
  overflow: auto;
  white-space: pre-wrap;
  margin: 0;
  font-family: ui-monospace, SFMono-Regular, monospace;
}
.log code { display: block; }
</style>
