<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'

const props = defineProps<{ option: any }>()
const el = ref<HTMLDivElement>()
let chart: echarts.ECharts | null = null

onMounted(() => {
  chart = echarts.init(el.value!)
  chart.setOption(props.option)
  window.addEventListener('resize', resize)
})
watch(() => props.option, value => chart?.setOption(value, true), { deep: true })
function resize() { chart?.resize() }
onBeforeUnmount(() => { window.removeEventListener('resize', resize); chart?.dispose() })
</script>

<template><div ref="el" class="chart"></div></template>
