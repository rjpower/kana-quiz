import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { router } from './router'
import { installAuthInterceptor } from './auth'
import './style.css'

installAuthInterceptor(router)

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')

// Register service worker so the page is installable as a PWA.
// Skipped on localhost dev because Rsbuild's HMR + a SW cause stale-bundle
// confusion; only register when served over https in prod.
if ('serviceWorker' in navigator && location.protocol === 'https:') {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {})
  })
}

