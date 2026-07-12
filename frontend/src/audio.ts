// Web Audio playback helper.
//
// Why not <audio>: in iOS standalone PWA mode, HTMLAudioElement output is
// routed through the ringer channel, which the hardware mute switch
// silences. Web Audio plays through the media channel and is unaffected.
// (Confirmed via /debug: only the Web Audio strategy produced sound.)
//
// Lifecycle: one shared AudioContext, lazily created and resumed on the
// first user gesture. Decoded buffers are cached by URL so repeated plays
// of the same word skip the decode.

let ctx: AudioContext | null = null
let currentSource: AudioBufferSourceNode | null = null
const bufferCache = new Map<string, AudioBuffer>()

function getCtx(): AudioContext | null {
  if (ctx) return ctx
  const Ctor =
    window.AudioContext ||
    (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
  if (!Ctor) return null
  ctx = new Ctor()
  return ctx
}

// Call from a click/touch handler before any awaits to satisfy autoplay
// policy. Resolves once the context is running.
export async function unlockAudio(): Promise<boolean> {
  const c = getCtx()
  if (!c) return false
  if (c.state === 'suspended') {
    try { await c.resume() } catch { /* ignore */ }
  }
  return c.state === 'running'
}

async function loadBuffer(url: string): Promise<AudioBuffer | null> {
  const cached = bufferCache.get(url)
  if (cached) return cached
  const c = getCtx()
  if (!c) return null
  const resp = await fetch(url)
  if (!resp.ok) return null
  const bytes = await resp.arrayBuffer()
  // Safari requires the callback form for older versions, but modern iOS
  // supports the promise form. Use promise; fallback if it throws.
  const buf = await new Promise<AudioBuffer>((resolve, reject) => {
    c.decodeAudioData(bytes.slice(0), resolve, reject)
  })
  bufferCache.set(url, buf)
  return buf
}

export async function playAudioUrl(url: string): Promise<void> {
  const c = getCtx()
  if (!c) throw new Error('no AudioContext')
  if (c.state === 'suspended') await c.resume()
  const buf = await loadBuffer(url)
  if (!buf) throw new Error('failed to load audio')
  if (currentSource) {
    try { currentSource.stop() } catch { /* already stopped */ }
    currentSource = null
  }
  const src = c.createBufferSource()
  src.buffer = buf
  src.connect(c.destination)
  src.onended = () => {
    if (currentSource === src) currentSource = null
  }
  src.start()
  currentSource = src
}

// Best-effort prefetch — used by the round-start prefetch loop.
export function prefetchAudioUrl(url: string): void {
  void loadBuffer(url).catch(() => { /* best-effort */ })
}

export function audioContextState(): string {
  return ctx?.state ?? 'uncreated'
}

// Audio responses are served `Cache-Control: immutable, max-age=1yr` keyed only
// by word id, so a browser that cached the OLD voice (OpenAI `nova`) keeps
// replaying it for a year even though the server now holds the new Google voice
// — and an already-cached `immutable` entry can't be revalidated away by a
// server header change; the URL itself has to change. Appending a version token
// re-keys the HTTP cache, so bumping AUDIO_VERSION forces every client to drop
// its stale MP3s and refetch the current voice. Bump on any voice/model change.
export const AUDIO_VERSION = 'aoede1'

export function versionedAudioUrl(url: string): string {
  const sep = url.includes('?') ? '&' : '?'
  return `${url}${sep}v=${AUDIO_VERSION}`
}

export function wordAudioUrl(wordId: number): string {
  return versionedAudioUrl(`/api/audio/word/${wordId}`)
}
