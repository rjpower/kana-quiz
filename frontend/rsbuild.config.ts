import { defineConfig } from '@rsbuild/core'
import { pluginVue } from '@rsbuild/plugin-vue'

export default defineConfig({
  plugins: [pluginVue()],
  source: {
    entry: { index: './src/main.ts' },
  },
  html: {
    template: './index.html',
    title: 'kana-quiz',
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  output: {
    distPath: { root: 'dist' },
    // Ship source maps with the prod bundle. Runtime cost is zero
    // (separate .map files, only fetched when devtools open one) and
    // the debug payoff is huge: browser stack traces resolve to the
    // original .vue / .ts line rather than "680.d004adb9.js:1:6761".
    // scripts/smoke.py prints those resolved frames after a build.
    sourceMap: { js: 'source-map', css: true },
  },
})
