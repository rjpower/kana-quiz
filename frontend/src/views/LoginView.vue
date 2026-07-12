<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const router = useRouter()
const route = useRoute()
const password = ref('')
const error = ref<string | null>(null)
const submitting = ref(false)

async function onSubmit() {
  error.value = null
  submitting.value = true
  try {
    const resp = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: password.value }),
    })
    if (resp.status === 401) {
      error.value = 'Wrong password.'
      return
    }
    if (!resp.ok) {
      error.value = `Login failed (${resp.status}).`
      return
    }
    const next = (route.query.next as string) || '/study'
    await router.replace(next)
  } catch (e) {
    error.value = 'Network error.'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <section class="panel login">
    <h2>Kana Quiz</h2>
    <p class="muted">Enter the password to continue.</p>
    <form @submit.prevent="onSubmit">
      <input
        v-model="password"
        type="password"
        autocomplete="current-password"
        placeholder="Password"
        autofocus
        :disabled="submitting"
      />
      <button class="btn primary" type="submit" :disabled="submitting || !password">
        {{ submitting ? 'Signing in…' : 'Sign in' }}
      </button>
    </form>
    <p v-if="error" class="error">{{ error }}</p>
  </section>
</template>

<style scoped>
.login { max-width: 360px; margin: 60px auto; }
form { display: flex; flex-direction: column; gap: 12px; margin-top: 12px; }
input {
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel-hi);
  color: inherit;
  font-size: 16px;
}
.error { color: var(--bad, #ef4444); margin-top: 10px; font-size: 14px; }
</style>
