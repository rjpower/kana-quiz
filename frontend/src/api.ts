// Every call the app makes to its backend, one function per route.
//
// This is the whole contract between the pages and the server: a view or a
// store never builds a URL or reads a Response. Keeping it in one file is
// what lets the backend move without touching a page, and what the request
// recorder replays to prove the moved backend answers the same.
//
// Transport: every call goes through a timeout-and-retry wrapper. Mobile
// networks blip constantly, and without it the answer-then-prefetch flow can
// wedge with `locked=true` forever. A 5xx, 408 or 429 is retried; any other
// 4xx is terminal and surfaces as an ApiError carrying the server's `detail`.

import type { AnswerResult, Direction, MatchTile, NextQuestion } from './stores/session'

const FETCH_TIMEOUT_MS = 6000
const FETCH_RETRY_BASE_MS = 300
// A client-perceived round trip slower than this is worth reporting even when
// it eventually succeeds: the server answers in under 300ms, so the time went
// to the network or a stalled connection that server-side timing cannot see.
const SLOW_FETCH_MS = 2000

export class ApiError extends Error {
  status: number
  detail: string
  constructor(status: number, detail: string) {
    super(detail)
    this.status = status
    this.detail = detail
  }
}

type Query = Record<string, string | number | boolean | null | undefined>

interface Options {
  query?: Query
  body?: unknown
  form?: FormData
  /** How many times the transport tries before giving up. */
  attempts?: number
  keepalive?: boolean
}

// Report a fetch anomaly to the backend so client-side hangs land in the same
// log stream as the server's request timing. Best-effort and silent:
// telemetry must never break the quiz flow, and this call bypasses the retry
// wrapper so it cannot recurse.
export function reportClientFetch(payload: {
  url: string
  outcome: 'slow' | 'retry' | 'timeout' | 'failed'
  duration_ms: number
  attempts: number
  detail?: string
}) {
  try {
    console.warn('[kana-quiz] fetch anomaly', payload)
    fetch('/api/client_log', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      keepalive: true,
    }).catch(() => {})
  } catch {
    /* ignore */
  }
}

function withQuery(path: string, query?: Query): string {
  if (!query) return path
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) continue
    params.set(key, String(value))
  }
  const text = params.toString()
  return text ? `${path}?${text}` : path
}

async function fetchWithRetry(url: string, init: RequestInit, attempts: number): Promise<Response> {
  const started = performance.now()
  // Query strings can carry per-question payloads; the path alone locates the
  // slow endpoint, so strip them before logging.
  const clean = url.split('?')[0]
  let lastErr: unknown
  let timedOut = false
  for (let i = 0; i < attempts; i++) {
    const ctrl = new AbortController()
    const timer = setTimeout(() => {
      timedOut = true
      ctrl.abort()
    }, FETCH_TIMEOUT_MS)
    try {
      const resp = await fetch(url, { ...init, signal: ctrl.signal })
      clearTimeout(timer)
      const retriable = resp.status >= 500 || resp.status === 408 || resp.status === 429
      if (!retriable) {
        const elapsed = Math.round(performance.now() - started)
        if (i > 0) {
          reportClientFetch({ url: clean, outcome: 'retry', duration_ms: elapsed, attempts: i + 1 })
        } else if (elapsed >= SLOW_FETCH_MS) {
          reportClientFetch({ url: clean, outcome: 'slow', duration_ms: elapsed, attempts: 1 })
        }
        return resp
      }
      lastErr = new Error(`http ${resp.status}`)
    } catch (e) {
      clearTimeout(timer)
      lastErr = e
    }
    if (i < attempts - 1) {
      await new Promise((r) => setTimeout(r, FETCH_RETRY_BASE_MS * 2 ** i))
    }
  }
  reportClientFetch({
    url: clean,
    outcome: timedOut ? 'timeout' : 'failed',
    duration_ms: Math.round(performance.now() - started),
    attempts,
    detail: lastErr instanceof Error ? lastErr.message : String(lastErr),
  })
  throw lastErr instanceof Error ? lastErr : new Error('fetch failed')
}

// One request. Answers the parsed JSON, undefined for 204, and throws an
// ApiError for any other status, carrying the server's `detail` when it sent
// one. A single attempt unless the caller asks for more, so a page that used
// plain fetch keeps its behavior.
async function request<T>(method: string, path: string, options: Options = {}): Promise<T> {
  const init: RequestInit = { method }
  if (options.form) {
    init.body = options.form
  } else if (options.body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' }
    init.body = JSON.stringify(options.body)
  }
  if (options.keepalive) init.keepalive = true
  const resp = await fetchWithRetry(withQuery(path, options.query), init, options.attempts ?? 1)
  if (resp.status === 204) return undefined as T
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    const detail =
      body && typeof body === 'object' && typeof (body as { detail?: unknown }).detail === 'string'
        ? (body as { detail: string }).detail
        : `HTTP ${resp.status}`
    throw new ApiError(resp.status, detail)
  }
  return (await resp.json()) as T
}

// ---- shapes the pages read ------------------------------------------------

export interface DeckOut {
  id: number
  name: string
  level: number
  created_at: string
  profile: string
  pick_order: 'random' | 'listed'
  word_count: number
  new_count: number
  due_count: number
  archived_count: number
}

export interface DeckPatch {
  name?: string
  level?: number
  profile?: string
  pick_order?: string
}

export interface QuickAddOut {
  id: number
  kana: string
  kanji: string | null
  english: string
  deck_id: number
  deck_name: string
  created: boolean
}

export interface ImportReportOut {
  inserted: number
  updated: number
  skipped: number
  deck_id: number
  deck_name: string
}

export interface SentenceOut {
  word_id: number
  japanese: string
  english: string
  mnemonic: string
  japanese_ruby: { t: string; r?: string | null }[]
  source: string
  audio_url: string
}

export interface QuestionQuery {
  modes: string
  reveal: '0' | '1'
  pool: string
  deck?: number
}

export interface AnswerIn {
  word_id: number
  direction: Direction
  latency_ms: number | null
  [key: string]: unknown
}

// ---- the routes --------------------------------------------------------------

export const api = {
  auth: {
    /** Answers the status: 200 signed in, 401 wrong password. */
    async login(password: string): Promise<number> {
      const resp = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      })
      return resp.status
    },
  },

  session: {
    /** The next question, or null when the round is out. */
    async next(query: QuestionQuery & { exclude?: number }, attempts = 4): Promise<NextQuestion | null> {
      const got = await request<NextQuestion | undefined>('GET', '/api/session/next', { query: { ...query }, attempts })
      return got ?? null
    },
    async batch(query: QuestionQuery & { n: number; exclude?: string }, attempts = 2): Promise<NextQuestion[]> {
      const got = await request<{ questions: NextQuestion[] } | undefined>('GET', '/api/session/batch', { query: { ...query }, attempts })
      return got?.questions ?? []
    },
    drill(query: { word_id: number; direction: Direction; pool_ids?: string }, attempts = 4): Promise<NextQuestion> {
      return request('GET', '/api/session/drill', { query, attempts })
    },
    async upcoming(n: number, attempts = 2): Promise<number[]> {
      const got = await request<{ word_ids?: number[] }>('GET', '/api/session/upcoming', { query: { n }, attempts })
      return got.word_ids ?? []
    },
    async matchBatch(n: number, excludeIds: number[], attempts = 3): Promise<MatchTile[]> {
      const query: Query = { n }
      if (excludeIds.length) query.exclude_ids = excludeIds.join(',')
      const got = await request<{ tiles?: MatchTile[] }>('GET', '/api/session/match-batch', { query, attempts })
      return got.tiles ?? []
    },
    answer(body: AnswerIn, attempts = 4): Promise<AnswerResult> {
      return request('POST', '/api/session/answer', { body, attempts })
    },
  },

  words: {
    quick(text: string): Promise<QuickAddOut> {
      return request('POST', '/api/words/quick', { body: { text } })
    },
    ignore(id: number, ignored: boolean, attempts = 1): Promise<void> {
      return request(ignored ? 'POST' : 'DELETE', `/api/words/${id}/ignore`, { attempts })
    },
    sentence(id: number): Promise<SentenceOut> {
      return request('GET', `/api/words/${id}/sentence`)
    },
    glosses(): Promise<string[]> {
      return request('GET', '/api/glosses')
    },
  },

  decks: {
    list<T = DeckOut>(attempts = 1): Promise<T[]> {
      return request('GET', '/api/decks', { attempts })
    },
    create<T = DeckOut>(body: { name: string; level: number }): Promise<T> {
      return request('POST', '/api/decks', { body })
    },
    update<T = DeckOut>(id: number, patch: DeckPatch): Promise<T> {
      return request('PATCH', `/api/decks/${id}`, { body: patch })
    },
    delete(id: number, force: boolean): Promise<void> {
      return request('DELETE', `/api/decks/${id}`, { query: force ? { force: 'true' } : undefined })
    },
    words<T>(id: number): Promise<T[]> {
      return request('GET', `/api/decks/${id}/words`)
    },
    ignored<T>(id: number): Promise<T[]> {
      return request('GET', `/api/decks/${id}/ignored`)
    },
  },

  stats: {
    summary<T>(): Promise<T> {
      return request('GET', '/api/stats')
    },
    overview<T>(tzOffset: number): Promise<T> {
      return request('GET', '/api/stats/overview', { query: { tz_offset: tzOffset } })
    },
    detailed<T>(): Promise<T> {
      return request('GET', '/api/stats/detailed')
    },
    debug<T>(): Promise<T> {
      return request('GET', '/api/stats/debug')
    },
  },

  async graderLog<T>(limit: number): Promise<T[]> {
    const got = await request<{ entries: T[] }>('GET', '/api/gemini_log', { query: { limit } })
    return got.entries
  },

  importCsv(form: FormData): Promise<ImportReportOut> {
    return request('POST', '/api/import', { form })
  },

  audio: {
    wordUrl(id: number): string {
      return `/api/audio/word/${id}`
    },
    sentenceUrl(id: number): string {
      return `/api/audio/sentence/${id}`
    },
  },
}
