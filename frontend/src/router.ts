import { createRouter, createWebHistory } from 'vue-router'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/study' },
    { path: '/login', component: () => import('./views/LoginView.vue') },
    { path: '/study', component: () => import('./views/StudyView.vue') },
    { path: '/study/match', component: () => import('./views/MatchView.vue') },
    { path: '/import', component: () => import('./views/ImportView.vue') },
    { path: '/stats', component: () => import('./views/StatsView.vue') },
    { path: '/decks', component: () => import('./views/DecksView.vue') },
    { path: '/grader-log', component: () => import('./views/GraderLogView.vue') },
    { path: '/decks/:id', component: () => import('./views/DeckDetailView.vue'), props: true },
    { path: '/word/:id', component: () => import('./views/WordView.vue'), props: true },
    { path: '/debug', component: () => import('./views/DebugView.vue') },
  ],
})
