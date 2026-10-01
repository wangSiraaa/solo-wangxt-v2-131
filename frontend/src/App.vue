<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import EChart from './components/EChart.vue'
import { api } from './api/client'

const manifests = ref<any[]>([])
const tasks = ref<any[]>([])
const calibrations = ref<any[]>([])
const selectedManifestKey = ref('')
const selectedTaskKey = ref('')
const selectedTask = ref<any>(null)
const report = ref<any>(null)
const message = ref('')
const loading = ref(false)
const tab = ref<'waveform'|'spectrum'|'gaps'|'convention'>('waveform')
let timer: number | undefined

const selectedManifest = computed(() => manifests.value.find(m => m.manifest_key === selectedManifestKey.value))
const activeCalibration = computed(() => calibrations.value.find(c => c.status === 'active'))

async function loadAll() {
  [manifests.value, tasks.value, calibrations.value] = await Promise.all([api.manifests(), api.tasks(), api.calibrations()])
  if (!selectedManifestKey.value && manifests.value[0]) selectedManifestKey.value = manifests.value[0].manifest_key
  if (selectedTaskKey.value) await selectTask(selectedTaskKey.value)
}

async function completeSelected() {
  if (!selectedManifestKey.value) return
  const r = await api.complete(selectedManifestKey.value)
  message.value = r.already_completed ? '该清单此前已完成，重复完成幂等返回。' : `校验完成：${r.manifest.status}`
  await loadAll()
}

async function startAnalysis() {
  if (!selectedManifestKey.value) return
  try {
    const task = await api.createAnalysis({
      manifest_key: selectedManifestKey.value,
      calibration_id: activeCalibration.value?.id,
      harmonic_count: 25,
      thd_denominator: 'rms',
      window: 'integer_cycle'
    })
    selectedTaskKey.value = task.task_key
    message.value = '任务已提交，输入清单、标定版和参数已固定。'
  } catch (e: any) { message.value = e.message }
}

async function selectTask(key: string) {
  selectedTaskKey.value = key
  selectedTask.value = await api.task(key)
  try { report.value = selectedTask.value.state === 'published' ? await api.report(key) : null }
  catch { report.value = null }
}
async function cancelTask() { if (selectedTaskKey.value) { await api.cancel(selectedTaskKey.value); await loadAll() } }
async function retryTask() { if (selectedTaskKey.value) { await api.retry(selectedTaskKey.value); await loadAll() } }

const waveformOption = computed(() => {
  const series = report.value?.charts?.waveforms?.series ?? []
  return {
    title: { text: '波形（缺口以 NaN 断开）' },
    animation: false,
    tooltip: { trigger: 'axis' }, legend: { top: 24, data: series.map((s:any)=>s.channel) },
    grid: { left: 55, right: 25, top: 70, bottom: 50 },
    xAxis: { type: 'value', name: '全局采样点' },
    yAxis: { type: 'value', name: '标定后物理量' },
    dataZoom: [{ type:'inside' }, { type:'slider' }],
    series: series.map((s: any) => ({ name:s.channel, type:'line', showSymbol:false, lineStyle:{width:1}, data: s.sample.map((x:number,i:number)=>[x,s.value[i]]) }))
  }
})
const spectrumOption = computed(() => {
  const series = report.value?.charts?.spectrum?.series ?? []
  return {
    title: { text: '频谱（RMS 幅值）' }, animation:false, tooltip:{trigger:'axis'},
    legend:{top:24, data:series.map((s:any)=>s.channel)},
    grid:{left:55,right:25,top:70,bottom:50},
    xAxis:{type:'value', name:'Hz'}, yAxis:{type:'value', name:'RMS'},
    dataZoom:[{type:'inside'},{type:'slider'}],
    series: series.map((s:any)=>({name:s.channel,type:'line',showSymbol:false,data:s.frequencies_hz.map((x:number,i:number)=>[x,s.rms_amplitude[i]])}))
  }
})
const gapMarks = computed(() => report.value?.charts?.gaps ?? [])
const validationIssues = computed(() => selectedManifest.value?.validation?.issues ?? [])
const headline = computed(() => {
  const ch = report.value?.metrics?.channels ?? {}
  return Object.entries(ch).map(([name,v]:[string,any]) => ({
    channel:name, rms:v.rms, frequency:v.estimated_frequency_hz, thd:v.thd.value_percent,
    phase:v.fundamental.phase_rad, denominator:v.thd.denominator
  }))
})
const components = computed(() => Object.entries(report.value?.metrics?.symmetrical_components ?? {}).map(([kind,v]:[string,any]) => ({kind, ...v})))

onMounted(async () => {
  await loadAll()
  timer = window.setInterval(loadAll, 3000)
})
</script>

<template>
  <div class="app">
    <div class="header">
      <div>
        <h1>电能质量离线复核平台</h1>
        <div class="muted">不可变原始块 · 固定标定快照 · 缺口显式建模 · 报告只发布一次</div>
      </div>
      <span class="muted">FastAPI / Celery / NumPy / SciPy / ECharts</span>
    </div>
    <div class="grid">
      <div>
        <div class="panel">
          <strong>录波清单</strong>
          <div v-for="m in manifests" :key="m.manifest_key" class="item" :class="{active:m.manifest_key===selectedManifestKey}" @click="selectedManifestKey=m.manifest_key">
            <div class="row"><b>{{m.name}}</b><span class="badge" :class="m.status">{{m.status}}</span></div>
            <div class="muted">{{m.manifest_key}} · {{m.expected_blocks}} 块</div>
            <div class="muted">digest: {{m.raw_manifest_digest?.slice(0,12)}}...</div>
          </div>
          <div class="row"><button @click="completeSelected">核对并完成上传</button><button class="secondary" @click="startAnalysis">启动分析</button></div>
        </div>
        <div class="panel" style="margin-top:14px">
          <strong>异步任务</strong>
          <div v-for="t in tasks" :key="t.task_key" class="item" :class="{active:t.task_key===selectedTaskKey}" @click="selectTask(t.task_key)">
            <div class="row"><span class="badge" :class="t.state">{{t.state}}</span><span class="muted">attempt {{t.attempts}}</span></div>
            <div class="muted">{{t.task_key.slice(0,12)}} · stage: {{t.stage ?? '-'}}</div>
          </div>
        </div>
      </div>
      <div class="panel">
        <div v-if="message" class="badge current">{{message}}</div>
        <div v-if="selectedManifest">
          <h2>{{selectedManifest.name}}</h2>
          <p class="muted">通道：{{selectedManifest.channel_order.join(', ')}}；标称频率 {{selectedManifest.nominal_frequency}} Hz</p>
          <h3>上传核对问题（排序不会吞掉重叠/缺口）</h3>
          <table><thead><tr><th>级别</th><th>代码</th><th>说明</th><th>详情</th></tr></thead>
          <tbody><tr v-for="(q,i) in validationIssues" :key="i"><td><span class="badge" :class="q.severity">{{q.severity}}</span></td><td>{{q.code}}</td><td>{{q.message}}</td><td><pre>{{JSON.stringify(q.detail)}}</pre></td></tr></tbody></table>
        </div>
        <div v-if="selectedTask" style="margin-top:20px">
          <div class="row"><h2>任务 {{selectedTask.task_key.slice(0,8)}}</h2><span class="badge" :class="selectedTask.state">{{selectedTask.state}}</span>
            <button @click="cancelTask">取消</button><button class="secondary" @click="retryTask">重试</button></div>
          <div class="muted">固定标定 ID：{{selectedTask.calibration_id}}；清单 digest：{{selectedTask.manifest_snapshot.raw_manifest_digest?.slice(0,12)}}；阶段：{{selectedTask.stage}}</div>
          <div v-if="selectedTask.error" class="badge fatal">阶段错误已保留</div>
        </div>
        <div v-if="report" style="margin-top:18px">
          <div class="row"><h2>报告</h2><span class="badge" :class="report.status">{{report.status}}</span><span class="badge" :class="report.review_state">{{report.review_state}}</span>
            <span v-if="report.superseded_reason" class="badge warning">标定已更新，需复核</span></div>
          <div class="tabs">
            <button :class="{secondary:tab!=='waveform'}" @click="tab='waveform'">波形</button>
            <button :class="{secondary:tab!=='spectrum'}" @click="tab='spectrum'">频谱</button>
            <button :class="{secondary:tab!=='gaps'}" @click="tab='gaps'">缺口/质量</button>
            <button :class="{secondary:tab!=='convention'}" @click="tab='convention'">口径</button>
          </div>
          <EChart v-if="tab==='waveform'" :option="waveformOption" />
          <EChart v-if="tab==='spectrum'" :option="spectrumOption" />
          <div v-if="tab==='gaps'">
            <h3>缺测区间</h3>
            <table><thead><tr><th>起始样本</th><th>结束样本</th><th>缺测数</th><th>相邻块</th></tr></thead><tbody>
              <tr v-for="(g,i) in gapMarks" :key="i"><td>{{g.start_sample}}</td><td>{{g.end_sample}}</td><td>{{g.sample_count}}</td><td>{{g.after_block}} → {{g.before_block}}</td></tr>
            </tbody></table>
            <h3>质量状态</h3>
            <table><thead><tr><th>级别</th><th>代码</th><th>通道</th><th>说明</th></tr></thead><tbody>
              <tr v-for="(q,i) in report.quality_flags" :key="i"><td><span class="badge" :class="q.severity">{{q.severity}}</span></td><td>{{q.code}}</td><td>{{q.channel ?? '-'}}</td><td>{{q.message}}</td></tr>
            </tbody></table>
          </div>
          <div v-if="tab==='convention'">
            <h3>通道数值</h3>
            <table><thead><tr><th>通道</th><th>RMS</th><th>频率</th><th>基波相位(rad)</th><th>THD %</th><th>分母</th></tr></thead><tbody>
              <tr v-for="x in headline" :key="x.channel"><td>{{x.channel}}</td><td>{{x.rms?.toFixed(5)}}</td><td>{{x.frequency?.toFixed(3)}}</td><td>{{x.phase?.toFixed(4)}}</td><td>{{x.thd?.toFixed(4)}}</td><td>{{x.denominator}}</td></tr>
            </tbody></table>
            <h3>对称分量</h3>
            <table><thead><tr><th>类型</th><th>正序RMS</th><th>负序RMS</th><th>负序不平衡%</th><th>零序RMS</th><th>零序不平衡%</th></tr></thead><tbody>
              <tr v-for="c in components" :key="c.kind"><td>{{c.kind}}</td><td>{{c.positive.rms.toFixed(5)}}</td><td>{{c.negative.rms.toFixed(5)}}</td><td>{{c.negative_unbalance_percent?.toFixed(3)}}</td><td>{{c.zero.rms.toFixed(5)}}</td><td>{{c.zero_unbalance_percent?.toFixed(3)}}</td></tr>
            </tbody></table>
            <pre>{{JSON.stringify(report.convention,null,2)}}</pre>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
