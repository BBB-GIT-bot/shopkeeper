<script setup>
import { computed, onMounted, onBeforeUnmount, reactive, ref } from 'vue'
import { onBeforeRouteLeave, useRouter } from 'vue-router'
import { api } from '../api'
import { logout } from '../session'

const router = useRouter()
const services = ref(null), queue = ref(null), shops = ref(null)
const serviceForm = reactive({}), queueForm = reactive({}), shopDrafts = reactive({})
const keyUpdate = ref(false), newKey = ref('')
let active = true
const expired = ref(false)
const states = reactive(Object.fromEntries(['services', 'queue', 'shops'].map(name => [name, { loading: false, saving: false, error: '', message: '' }])))
const serviceFields = ['base_url', 'model', 'temperature', 'request_timeout_seconds', 'multimodal_mode']
const queueFields = [
  ['concurrency', '全局并发额度', 1], ['execution_timeout_seconds', '单次执行超时（秒）', 1],
  ['max_auto_retries', '自动重试次数（首次执行之外）', 0], ['poll_interval_seconds', '前端轮询间隔（秒）', 1],
  ['dispatch_interval_seconds', '调度间隔（秒）', 1], ['heartbeat_interval_seconds', '心跳间隔（秒）', 1],
]
const select = (value, fields) => Object.fromEntries(fields.map(key => [key, value[key]]))
const differs = (form, saved, fields) => saved && fields.some(key => form[key] !== saved[key])
const dirty = computed(() => !expired.value && Boolean(differs(serviceForm, services.value, serviceFields) || keyUpdate.value || newKey.value || differs(queueForm, queue.value, queueFields.map(row => row[0])) || shops.value?.some(shop => shopDrafts[shop.id] !== shop.authorization_status)))
function handleError(state, error) {
  state.error = [error.message, ...(error.details || []).map(item => `${item.field}：${item.message}`)].join('；')
  if (error.status === 401) {
    expired.value = true
    newKey.value = ''; keyUpdate.value = false
    services.value = null; queue.value = null; shops.value = null
    logout(); router.replace('/login')
  }
}
async function read(name) {
  const state = states[name]
  state.loading = true; state.error = ''
  try {
    if (name === 'services') {
      const saved = await api.settingsServices()
      if (!active || expired.value) return
      services.value = saved
      Object.assign(serviceForm, select(services.value, serviceFields))
    } else if (name === 'queue') {
      const saved = await api.settingsQueue()
      if (!active || expired.value) return
      queue.value = saved
      Object.assign(queueForm, select(queue.value, queueFields.map(row => row[0])))
    } else {
      const items = []
      let page = 1, result
      do { result = await api.request(`/shops?page=${page++}&page_size=200`); items.push(...result.items) } while (items.length < result.total)
      if (!active || expired.value) return
      shops.value = items
      items.forEach(shop => { shopDrafts[shop.id] = shop.authorization_status })
    }
  } catch (error) { handleError(state, error) }
  finally { state.loading = false }
}
async function save(name, shop = null) {
  const state = states[name]
  if (state.saving || expired.value) return
  state.saving = true; state.error = ''; state.message = ''
  try {
    if (name === 'services') {
      const payload = select(serviceForm, serviceFields)
      if (keyUpdate.value) payload.api_key = newKey.value
      const saved = await api.saveServices(payload)
      if (!active || expired.value) return
      services.value = saved
      newKey.value = ''; keyUpdate.value = false
      Object.assign(serviceForm, select(services.value, serviceFields))
    } else if (name === 'queue') {
      const saved = await api.saveQueue(select(queueForm, queueFields.map(row => row[0])))
      if (!active || expired.value) return
      queue.value = saved
      Object.assign(queueForm, select(queue.value, queueFields.map(row => row[0])))
    } else {
      const saved = await api.saveAuthorization(shop.id, shopDrafts[shop.id])
      if (!active || expired.value) return
      Object.assign(shop, saved); shopDrafts[shop.id] = saved.authorization_status
    }
    state.message = '已保存到后端。'
    if (name !== 'shops') {
      await read(name)
      if (state.error) state.message = '保存成功，但重新读取失败，请稍后重试。'
    }
  } catch (error) { handleError(state, error) }
  finally { state.saving = false }
}
function beforeUnload(event) { if (dirty.value) { event.preventDefault(); event.returnValue = '' } }
onBeforeRouteLeave(() => {
  if (expired.value) return true
  if (Object.values(states).some(state => state.saving)) return false
  return !dirty.value || window.confirm('有未保存修改。确定放弃修改并离开？取消后可继续编辑或保存。')
})
onMounted(() => { window.addEventListener('beforeunload', beforeUnload); Promise.allSettled(['services', 'queue', 'shops'].map(read)) })
onBeforeUnmount(() => { active = false; newKey.value = ''; window.removeEventListener('beforeunload', beforeUnload) })
</script>

<template>
  <section class="page settings-page">
    <div class="eyebrow">SYSTEM CONFIGURATION</div><h2>系统与服务配置</h2>
    <p>主管维护模型、队列与模拟店铺授权。<span v-if="dirty" class="draft-label">有未保存修改</span></p>
    <div class="section-grid">
      <section class="panel">
        <h3>文字模型与素材服务</h3>
        <p v-if="states.services.loading" role="status">正在读取配置…</p>
        <p v-if="states.services.error" class="error" role="alert">{{ states.services.error }}</p>
        <button v-if="!services && !states.services.loading" class="ghost" @click="read('services')">重新读取</button>
        <form v-if="services" @submit.prevent="save('services')">
          <fieldset :disabled="states.services.saving || states.services.loading">
            <label>服务地址<input v-model="serviceForm.base_url" type="url" placeholder="https://example.invalid/v1" /></label>
            <label>模型名称<input v-model="serviceForm.model" placeholder="供应商提供的模型名称" /></label>
            <p>模型密钥：{{ services.api_key_configured ? services.api_key_masked + '（已配置）' : '未配置' }}</p>
            <label class="inline-option"><input v-model="keyUpdate" type="checkbox" @change="newKey = ''" />更新密钥</label>
            <label v-if="keyUpdate">新密钥<input v-model="newKey" type="password" autocomplete="new-password" required placeholder="仅发送后端，保存后清空" /></label>
            <div class="form-pair">
              <label>温度（0–2）<input v-model.number="serviceForm.temperature" type="number" min="0" max="2" step="0.1" required /></label>
              <label>请求超时（秒）<input v-model.number="serviceForm.request_timeout_seconds" type="number" min="1" step="1" required /></label>
            </div>
            <label>图片／视频模式<select v-model="serviceForm.multimodal_mode"><option value="simulation">simulation · 演示素材</option></select></label>
            <p>图片和视频采用教学演示素材。真实文字调用结果以业务生成结果为准。</p>
            <button class="primary" type="submit">{{ states.services.saving ? '保存中…' : '保存服务配置' }}</button>
          </fieldset>
        </form>
        <p v-if="states.services.message" role="status" class="success">{{ states.services.message }}</p>
        <small v-if="services?.updated_by">修改人 ID {{ services.updated_by }} · {{ services.updated_at }}</small>
      </section>
      <section class="panel">
        <h3>任务规则</h3>
        <p v-if="states.queue.loading" role="status">正在读取配置…</p>
        <p v-if="states.queue.error" class="error" role="alert">{{ states.queue.error }}</p>
        <button v-if="!queue && !states.queue.loading" class="ghost" @click="read('queue')">重新读取</button>
        <form v-if="queue" @submit.prevent="save('queue')">
          <fieldset :disabled="states.queue.saving || states.queue.loading">
            <label v-for="[field, label, minimum] in queueFields" :key="field">{{ label }}<input v-model.number="queueForm[field]" type="number" :min="minimum" step="1" required /></label>
            <p>重试次数可设为 0，不包含首次执行。心跳间隔须小于执行超时。</p>
            <button class="primary" type="submit">{{ states.queue.saving ? '保存中…' : '保存任务规则' }}</button>
          </fieldset>
        </form>
        <p v-if="states.queue.message" role="status" class="success">{{ states.queue.message }}</p>
        <small v-if="queue?.updated_by">修改人 ID {{ queue.updated_by }} · {{ queue.updated_at }}</small>
      </section>
      <section class="panel authorization-panel">
        <h3>模拟平台授权</h3><p>仅记录平台、接入标识和模拟状态；用于课堂演示。</p>
        <p v-if="states.shops.loading" role="status">正在读取店铺…</p>
        <p v-if="states.shops.error" class="error" role="alert">{{ states.shops.error }}</p>
        <button v-if="!shops && !states.shops.loading" class="ghost" @click="read('shops')">重新读取</button>
        <p v-if="shops?.length === 0">暂无店铺，请先由运营创建店铺。</p>
        <form v-for="shop in shops" :key="shop.id" @submit.prevent="save('shops', shop)">
          <fieldset :disabled="states.shops.saving || states.shops.loading" class="shop-authorization">
            <div><b>{{ shop.name }}</b><p>{{ shop.platform }} · {{ shop.platform_shop_id }} · {{ shop.shop_code }}</p></div>
            <label>模拟授权状态<select v-model="shopDrafts[shop.id]"><option value="not_configured">未配置</option><option value="authorized">已模拟授权</option><option value="expired">已过期</option></select></label>
            <button class="primary" type="submit">保存授权状态</button>
          </fieldset>
        </form>
        <p v-if="states.shops.message" role="status" class="success">{{ states.shops.message }}</p>
      </section>
    </div>
  </section>
</template>

<style scoped>
fieldset{border:0;padding:0;margin:0;display:grid;gap:16px;min-width:0}
label{display:grid;gap:6px;color:#45546e;font-size:13px;font-weight:600}
input,select{width:100%;border:1px solid #d8e0ec;border-radius:5px;padding:10px;font:inherit;background:#fff}
.inline-option{display:flex;align-items:center}.inline-option input{width:auto}
.form-pair{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.authorization-panel{grid-column:1/-1}.shop-authorization{grid-template-columns:1fr 220px auto;align-items:center;border-top:1px solid #edf0f5;padding:16px 0}
.success{color:#168450}.draft-label{color:#a96512}small{color:#64748b}
@media(max-width:800px){.form-pair,.shop-authorization{grid-template-columns:1fr}}
</style>
