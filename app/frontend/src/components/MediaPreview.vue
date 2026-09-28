<script setup>
import { ref,watch,onBeforeUnmount } from 'vue'
import { download } from '../api'
const props=defineProps({material:Object});const url=ref('');const error=ref('');let generation=0
watch(()=>props.material?.file_url,async path=>{const version=++generation;if(url.value)URL.revokeObjectURL(url.value);url.value='';if(!path)return;try{const blob=await download(path.replace(/^\/api/,''));if(version===generation)url.value=URL.createObjectURL(blob)}catch(e){if(version===generation)error.value=e.message}},{immediate:true})
onBeforeUnmount(()=>{generation++;if(url.value)URL.revokeObjectURL(url.value)})
</script>
<template><div><img v-if="url&&material.material_type==='image'" :src="url" alt="教学演示图片" class="material-preview"><video v-else-if="url" :src="url" controls preload="metadata" class="material-preview"></video><p v-if="error" class="error">{{error}}</p><small>演示素材 · 不代表真实 AI 生图或生视频</small></div></template>
