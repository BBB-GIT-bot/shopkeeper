<script setup>
import { onMounted, onBeforeUnmount, watch, ref } from 'vue'
import Quill from 'quill'
import 'quill/dist/quill.snow.css'
const props=defineProps({modelValue:{type:String,default:''},delta:Object,disabled:Boolean})
const emit=defineEmits(['update:modelValue','delta'])
const host=ref(null); let editor
onMounted(()=>{editor=new Quill(host.value,{theme:'snow',modules:{toolbar:[['bold','italic'],[{list:'bullet'}],['clean']]},readOnly:props.disabled});if(props.delta)editor.setContents(props.delta);else editor.setText(props.modelValue);editor.on('text-change',(_delta,_old,source)=>{if(source==='user'){emit('update:modelValue',editor.getText().trimEnd());emit('delta',editor.getContents())}})})
watch(()=>props.delta,value=>{if(editor&&value&&JSON.stringify(value)!==JSON.stringify(editor.getContents()))editor.setContents(value)})
watch(()=>props.modelValue,value=>{if(editor && editor.getText().trimEnd()!==value)editor.setText(value||'')})
watch(()=>props.disabled,value=>editor?.enable(!value))
onBeforeUnmount(()=>{editor?.off('text-change');editor=null})
</script>
<template><div ref="host" class="rich-editor"></div></template>
