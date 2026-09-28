<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { session, logout } from './session'
const route = useRoute(); const router = useRouter(); const inApp = computed(() => Boolean(session.user && route.path !== '/login'))
function signOut() { logout(); router.push('/login') }
</script>
<template><div class="app-shell"><aside v-if="inApp" class="sidebar"><h1>运营助手</h1><nav><RouterLink to="/dashboard">运营大盘</RouterLink><RouterLink to="/imports">导入中心</RouterLink><RouterLink v-if="session.user?.role === 'manager'" to="/settings">系统配置</RouterLink></nav><button class="link-button" @click="signOut">退出登录</button></aside><main :class="{content: inApp}"><header v-if="inApp" class="topbar"><span>{{ session.user.username }} · {{ session.user.role === 'manager' ? '主管' : '运营' }}</span></header><RouterView /></main></div></template>
