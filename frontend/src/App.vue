<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'
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
  </div>
</template>
