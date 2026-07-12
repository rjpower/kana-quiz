// Wrap window.fetch so any 401 from /api/* (except the auth endpoints
// themselves) bounces the user to /login. This keeps existing call sites
// in stores/views unchanged — they don't need to know about auth.

import type { Router } from 'vue-router'

export function installAuthInterceptor(router: Router) {
  const originalFetch = window.fetch.bind(window)

  window.fetch = async (input, init) => {
    const resp = await originalFetch(input, init)
    if (resp.status !== 401) return resp

    const url = typeof input === 'string'
      ? input
      : input instanceof URL
        ? input.toString()
        : input.url
    if (!url.startsWith('/api/') || url.startsWith('/api/auth/')) {
      return resp
    }

    const current = router.currentRoute.value.fullPath
    if (router.currentRoute.value.path !== '/login') {
      void router.replace({ path: '/login', query: { next: current } })
    }
    return resp
  }
}
