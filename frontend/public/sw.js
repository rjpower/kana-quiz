// Minimal service worker — exists so Chrome treats the page as installable.
// We deliberately don't cache HTML/JS: the app is small, the network round-trip
// is fast, and avoiding a cache means we never ship a stale shell after deploy.
// We do let the browser cache audio responses via Cache-Control headers.

self.addEventListener('install', (event) => {
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim())
})

self.addEventListener('fetch', (event) => {
  // Network passthrough. Having any fetch listener registered satisfies
  // Chrome's installability heuristic.
})
