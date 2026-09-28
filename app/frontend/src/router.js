import { createRouter, createWebHistory } from 'vue-router'
import { ensureSession } from './session'
import Login from './views/Login.vue'
import Dashboard from './views/Dashboard.vue'
import Workbench from './views/Workbench.vue'
import Imports from './views/Imports.vue'
import Settings from './views/Settings.vue'
const router = createRouter({ history: createWebHistory(), routes: [{ path: '/login', component: Login }, { path: '/dashboard', component: Dashboard, meta: { auth: true } }, { path: '/workbench/:productId', component: Workbench, meta: { auth: true } }, { path: '/imports', component: Imports, meta: { auth: true } }, { path: '/settings', component: Settings, meta: { auth: true, manager: true } }, { path: '/', redirect: '/dashboard' }, { path: '/:pathMatch(.*)*', redirect: '/dashboard' }] })
router.beforeEach(async (to) => { const user = await ensureSession(); if (to.meta.auth && !user) return '/login'; if (to.meta.manager && user?.role !== 'manager') return '/dashboard'; if (to.path === '/login' && user) return '/dashboard' })
export default router
