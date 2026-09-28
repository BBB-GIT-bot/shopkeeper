<script setup>
import { ref,onMounted,onBeforeUnmount,watch } from 'vue'
import { init,use } from 'echarts/core'
import { LineChart,BarChart } from 'echarts/charts'
import { GridComponent,TooltipComponent,LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
use([LineChart,BarChart,GridComponent,TooltipComponent,LegendComponent,CanvasRenderer])
const props=defineProps({option:Object});const host=ref(null);let chart,observer
onMounted(()=>{chart=init(host.value);chart.setOption(props.option||{});observer=new ResizeObserver(()=>chart.resize());observer.observe(host.value)})
watch(()=>props.option,value=>chart?.setOption(value||{},true),{deep:true})
onBeforeUnmount(()=>{observer?.disconnect();chart?.dispose()})
</script>
<template><div ref="host" class="chart" role="img" aria-label="经营指标图表"></div></template>
