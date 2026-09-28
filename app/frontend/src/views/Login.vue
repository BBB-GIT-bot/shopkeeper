<script setup>
import { ref } from 'vue'; import { useRouter } from 'vue-router'; import { login } from '../session'
const router=useRouter(); const username=ref('operator'); const password=ref('operator123'); const error=ref(''); const loading=ref(false)
async function submit(){ error.value=''; loading.value=true; try { await login(username.value,password.value); router.push('/dashboard') } catch(e){ error.value=e.message } finally { loading.value=false } }
</script>
<template><section class="login-page"><div class="login-card"><div class="eyebrow">SHOPKEEPER / WORKSPACE</div><h1>电商运营助手</h1><p>从商品建档到复盘的单品运营闭环</p><form @submit.prevent="submit"><label>账号<input v-model="username" autocomplete="username" required /></label><label>密码<input v-model="password" type="password" autocomplete="current-password" required /></label><p v-if="error" class="error">{{ error }}</p><button class="primary" :disabled="loading">{{ loading ? '登录中…' : '登录工作空间' }}</button></form><small>演示账号：operator / operator123；主管：manager / manager123</small></div></section></template>
