// Module-level cache for the deck's English gloss corpus.
//
// Used by TypeInput's ja2en autocomplete. We deduplicate concurrent
// fetches via a single in-flight promise so the first few type-ins of a
// session don't kick off N parallel requests.

let cache: string[] | null = null
let inflight: Promise<string[]> | null = null

export async function getGlosses(): Promise<string[]> {
  if (cache) return cache
  if (inflight) return inflight
  inflight = (async () => {
    try {
      const resp = await fetch('/api/glosses')
      if (!resp.ok) throw new Error(`glosses fetch failed: ${resp.status}`)
      cache = (await resp.json()) as string[]
      return cache
    } finally {
      inflight = null
    }
  })()
  return inflight
}

// Strip the leading particles English glosses commonly carry but the
// user shouldn't have to type to disambiguate. Verbs are imported with
// "to ", indefinite/definite articles surface on noun glosses, etc.
const STRIP_PREFIXES = ['to ', 'an ', 'a ', 'the ']

export function normalizeGloss(s: string): string {
  let t = s.trim().toLowerCase()
  for (const p of STRIP_PREFIXES) {
    if (t.startsWith(p)) {
      t = t.slice(p.length)
      break
    }
  }
  return t
}
