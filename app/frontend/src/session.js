import { reactive } from 'vue'
import { api } from './api'
export const session = reactive({ user: null, loading: false })
let promise
export function ensureSession() { if (session.user) return Promise.resolve(session.user); if (!sessionStorage.getItem('shopkeeper_token')) return Promise.resolve(null); if (!promise) { promise = api.me().then(user => { session.user = user; return user }).catch(() => { sessionStorage.removeItem('shopkeeper_token'); return null }).finally(() => { promise = null }) } return promise }
export async function login(username, password) { session.loading = true; try { const result = await api.login({ username, password }); sessionStorage.setItem('shopkeeper_token', result.access_token); session.user = result.user; return result.user } finally { session.loading = false } }
export function logout() { sessionStorage.removeItem('shopkeeper_token'); session.user = null }
