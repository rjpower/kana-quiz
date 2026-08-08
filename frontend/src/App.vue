<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'
import ToastHost from './components/ToastHost.vue'
</script>

<template>
  <div class="app-shell">
    <nav class="nav">
      <RouterLink to="/study">Study</RouterLink>
      <RouterLink to="/import">Import</RouterLink>
      <RouterLink to="/decks">Decks</RouterLink>
      <RouterLink to="/stats">Stats</RouterLink>
      <RouterLink to="/debug">Debug</RouterLink>
    </nav>
    <RouterView />
    <!-- Fixed overlay, so it sits outside the shell's flow and any route can
         push to it without laying out space for a message that may never come. -->
    <ToastHost />
  </div>
</template>
