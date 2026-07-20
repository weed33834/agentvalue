<template>
  <!-- P2-1: Token/成本趋势看板,4 张 ECharts 图表 + 4 张统计卡片 -->
  <div class="admin-metrics">
    <!-- 顶部控制条:时间范围 + 粒度切换 + 刷新 -->
    <el-card class="control-bar" shadow="never">
      <div class="control-row">
        <div class="control-left">
          <el-radio-group v-model="dateRange" @change="onRangeChange">
            <el-radio-button value="7d">近 7 天</el-radio-button>
            <el-radio-button value="30d">近 30 天</el-radio-button>
            <el-radio-button value="custom">自定义</el-radio-button>
          </el-radio-group>
          <template v-if="dateRange === 'custom'">
            <el-date-picker
              v-model="customRange"
              type="datetimerange"
              range-separator="至"
              start-placeholder="开始时间"
              end-placeholder="结束时间"
              format="YYYY-MM-DD HH:mm"
              value-format="YYYY-MM-DDTHH:mm:ss"
              @change="onCustomChange"
              style="margin-left: 12px"
            />
          </template>
          <el-radio-group v-model="granularity" style="margin-left: 12px" @change="reloadAll">
            <el-radio-button value="day">按天</el-radio-button>
            <el-radio-button value="hour">按小时</el-radio-button>
          </el-radio-group>
        </div>
        <el-button :loading="loadingAny" @click="reloadAll">刷新</el-button>
      </div>
    </el-card>

    <!-- 4 张统计卡片:评估总数 / Token 总数 / 估算成本 / Provider 数 -->
    <el-row :gutter="16" class="cards-row">
      <el-col :xs="12" :sm="12" :md="6" v-for="card in cards" :key="card.key">
        <el-card shadow="hover" class="metric-card" :aria-label="`${card.label} ${card.value}`">
          <div class="metric-label">{{ card.label }}</div>
          <div class="metric-value" :title="card.value">{{ card.value }}</div>
          <div class="metric-sub">{{ card.sub }}</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- Token 用量趋势图 -->
    <el-card class="chart-card" shadow="never">
      <template #header>
        <div class="card-header">
          <span>Token 用量趋势</span>
          <span class="card-sub">prompt / completion 双线 + total 堆叠区域</span>
        </div>
      </template>
      <div v-loading="loading.token" class="chart-wrap">
        <v-chart v-if="hasTokenData" class="chart" :option="tokenOption" autoresize />
        <el-empty v-else-if="!loading.token" description="暂无 Token 用量数据" />
      </div>
    </el-card>

    <!-- 成本分布图(按 model 切片) -->
    <el-row :gutter="16">
      <el-col :xs="24" :md="12">
        <el-card class="chart-card" shadow="never">
          <template #header>
            <div class="card-header">
              <span>成本分布(按模型)</span>
              <span class="card-sub">总成本 ${{ totalCostDisplay }}</span>
            </div>
          </template>
          <div v-loading="loading.cost" class="chart-wrap">
            <v-chart v-if="hasCostData" class="chart" :option="costOption" autoresize />
            <el-empty v-else-if="!loading.cost" description="暂无成本数据" />
          </div>
        </el-card>
      </el-col>

      <!-- 评估状态分布 -->
      <el-col :xs="24" :md="12">
        <el-card class="chart-card" shadow="never">
          <template #header>
            <div class="card-header">
              <span>评估状态分布</span>
              <span class="card-sub">ai_drafted / approved / rejected / manager_review / hr_audit</span>
            </div>
          </template>
          <div v-loading="loading.eval" class="chart-wrap">
            <v-chart v-if="hasEvalData" class="chart" :option="evalStatusOption" autoresize />
            <el-empty v-else-if="!loading.eval" description="暂无评估数据" />
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- Provider 调用分布(双轴:调用次数 + Token 总数) -->
    <el-card class="chart-card" shadow="never">
      <template #header>
        <div class="card-header">
          <span>Provider 调用分布</span>
          <span class="card-sub">调用次数(左轴) + Token 总数(右轴)</span>
        </div>
      </template>
      <div v-loading="loading.provider" class="chart-wrap">
        <v-chart v-if="hasProviderData" class="chart" :option="providerOption" autoresize />
        <el-empty v-else-if="!loading.provider" description="暂无 Provider 数据" />
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, reactive } from 'vue'
import VChart from 'vue-echarts'
import '@/utils/echarts'
import { analyticsAdminApi } from '@/api/client'

// ====== 时间范围与粒度 ======
const dateRange = ref('7d')
const granularity = ref('day')
const customRange = ref([])
// start_date / end_date 最终传给后端,ISO 字符串
const dateParams = reactive({ start_date: '', end_date: '' })

function isoDaysAgo(days) {
  const d = new Date()
  d.setHours(0, 0, 0, 0)
  d.setDate(d.getDate() - days)
  return d.toISOString()
}
function isoNow() {
  return new Date().toISOString()
}

function applyRangePreset() {
  if (dateRange.value === '7d') {
    dateParams.start_date = isoDaysAgo(7)
    dateParams.end_date = isoNow()
  } else if (dateRange.value === '30d') {
    dateParams.start_date = isoDaysAgo(30)
    dateParams.end_date = isoNow()
  } else if (customRange.value && customRange.value.length === 2) {
    dateParams.start_date = new Date(customRange.value[0]).toISOString()
    dateParams.end_date = new Date(customRange.value[1]).toISOString()
  }
}

function onRangeChange() {
  if (dateRange.value !== 'custom') {
    applyRangePreset()
    reloadAll()
  }
}
function onCustomChange() {
  if (customRange.value && customRange.value.length === 2) {
    applyRangePreset()
    reloadAll()
  }
}

// ====== 各图表独立 loading 与数据 ======
const loading = reactive({
  token: false,
  cost: false,
  provider: false,
  eval: false,
})
const loadingAny = computed(() =>
  Object.values(loading).some(Boolean)
)

const tokenData = ref({ timeline: [], series: { prompt: [], completion: [], total: [] } })
const costData = ref({ by_model: [], by_tenant: [], total_cost_usd: 0 })
const providerData = ref({ providers: [] })
const evalData = ref({ total_evaluations: 0, by_status: {}, by_period: [] })

const hasTokenData = computed(
  () => tokenData.value.timeline.length > 0 && tokenData.value.series.total.some((v) => v > 0)
)
const hasCostData = computed(() => costData.value.by_model.length > 0)
const hasProviderData = computed(() => providerData.value.providers.length > 0)
const hasEvalData = computed(() =>
  Object.values(evalData.value.by_status || {}).some((v) => v > 0)
)

// ====== 统计卡片(顶部 4 张) ======
const totalTokens = computed(() => {
  const arr = tokenData.value.series.total || []
  return arr.reduce((s, v) => s + (Number(v) || 0), 0)
})
const totalCost = computed(() => Number(costData.value.total_cost_usd || 0))
const totalCostDisplay = computed(() => totalCost.value.toFixed(4))
const providerCount = computed(() => providerData.value.providers.length)
const evalTotal = computed(() => Number(evalData.value.total_evaluations || 0))

function formatNumber(n) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K'
  return String(n)
}

const cards = computed(() => [
  {
    key: 'eval',
    label: '评估总数',
    value: formatNumber(evalTotal.value),
    sub: '时间范围内评估总量',
  },
  {
    key: 'token',
    label: 'Token 总数',
    value: formatNumber(totalTokens.value),
    sub: 'prompt + completion 累加',
  },
  {
    key: 'cost',
    label: '估算成本(USD)',
    value: '$' + totalCostDisplay.value,
    sub: '基于内置 MODEL_PRICING',
  },
  {
    key: 'provider',
    label: 'Provider 数',
    value: String(providerCount.value),
    sub: '有调用的 model 数',
  },
])

// ====== Token 用量趋势图(line + area 堆叠) ======
const tokenOption = computed(() => {
  const tl = tokenData.value.timeline || []
  const xData = tl.map((ts) => {
    const d = new Date(ts * 1000)
    return granularity.value === 'hour'
      ? `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:00`
      : `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  })
  return {
    tooltip: { trigger: 'axis' },
    legend: { data: ['Prompt', 'Completion', 'Total'] },
    grid: { left: 50, right: 30, top: 40, bottom: 60 },
    xAxis: { type: 'category', data: xData, boundaryGap: false },
    yAxis: { type: 'value', name: 'tokens' },
    dataZoom: [{ type: 'inside' }, { type: 'slider' }],
    series: [
      {
        name: 'Prompt',
        type: 'line',
        smooth: true,
        areaStyle: { opacity: 0.2 },
        data: tokenData.value.series.prompt || [],
      },
      {
        name: 'Completion',
        type: 'line',
        smooth: true,
        areaStyle: { opacity: 0.2 },
        data: tokenData.value.series.completion || [],
      },
      {
        name: 'Total',
        type: 'line',
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 2, type: 'dashed' },
        data: tokenData.value.series.total || [],
      },
    ],
  }
})

// ====== 成本分布图(pie, 按 model 切片) ======
const costOption = computed(() => {
  const data = (costData.value.by_model || []).map((m) => ({
    name: m.model,
    value: Number(m.cost_usd || 0),
  }))
  return {
    tooltip: { trigger: 'item', formatter: '{b}: ${c} ({d}%)' },
    legend: { type: 'scroll', orient: 'vertical', right: 10, top: 'middle' },
    series: [
      {
        name: '成本(USD)',
        type: 'pie',
        radius: ['40%', '70%'],
        center: ['40%', '50%'],
        avoidLabelOverlap: true,
        itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
        label: { show: false },
        emphasis: { label: { show: true, fontWeight: 'bold' } },
        data,
      },
    ],
  }
})

// ====== 评估状态分布(pie) ======
const evalStatusOption = computed(() => {
  const statusLabels = {
    ai_drafted: 'AI 草稿',
    approved: '已通过',
    rejected: '已驳回',
    manager_review: '主管复核',
    hr_audit: 'HR 审计',
  }
  const data = Object.entries(evalData.value.by_status || {})
    .filter(([, v]) => v > 0)
    .map(([k, v]) => ({ name: statusLabels[k] || k, value: v }))
  return {
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { type: 'scroll', orient: 'vertical', right: 10, top: 'middle' },
    series: [
      {
        name: '评估状态',
        type: 'pie',
        radius: ['40%', '70%'],
        center: ['40%', '50%'],
        itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
        label: { show: false },
        emphasis: { label: { show: true, fontWeight: 'bold' } },
        data,
      },
    ],
  }
})

// ====== Provider 调用分布(bar, 双轴) ======
const providerOption = computed(() => {
  const providers = providerData.value.providers || []
  const names = providers.map((p) => p.name)
  const calls = providers.map((p) => p.call_count)
  const tokens = providers.map((p) => p.token_total)
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { data: ['调用次数', 'Token 总数'] },
    grid: { left: 50, right: 60, top: 40, bottom: 40 },
    xAxis: { type: 'category', data: names, axisLabel: { rotate: 30 } },
    yAxis: [
      { type: 'value', name: '调用次数', position: 'left' },
      { type: 'value', name: 'Token 总数', position: 'right' },
    ],
    series: [
      {
        name: '调用次数',
        type: 'bar',
        data: calls,
        itemStyle: { color: '#409eff' },
      },
      {
        name: 'Token 总数',
        type: 'bar',
        yAxisIndex: 1,
        data: tokens,
        itemStyle: { color: '#67c23a' },
      },
    ],
  }
})

// ====== 各图表独立加载(失败显示 el-empty,不阻塞其他图表) ======
async function loadTokenUsage() {
  loading.token = true
  try {
    const params = {
      start_date: dateParams.start_date,
      end_date: dateParams.end_date,
      granularity: granularity.value,
    }
    const data = await analyticsAdminApi.tokenUsage(params)
    tokenData.value = {
      timeline: data?.timeline || [],
      series: {
        prompt: data?.series?.prompt || [],
        completion: data?.series?.completion || [],
        total: data?.series?.total || [],
      },
    }
  } catch (err) {
    console.error('加载 Token 用量趋势失败:', err)
    tokenData.value = { timeline: [], series: { prompt: [], completion: [], total: [] } }
  } finally {
    loading.token = false
  }
}

async function loadCost() {
  loading.cost = true
  try {
    const params = {
      start_date: dateParams.start_date,
      end_date: dateParams.end_date,
    }
    const data = await analyticsAdminApi.cost(params)
    costData.value = {
      by_model: data?.by_model || [],
      by_tenant: data?.by_tenant || [],
      total_cost_usd: Number(data?.total_cost_usd || 0),
    }
  } catch (err) {
    console.error('加载成本分布失败:', err)
    costData.value = { by_model: [], by_tenant: [], total_cost_usd: 0 }
  } finally {
    loading.cost = false
  }
}

async function loadProviderDistribution() {
  loading.provider = true
  try {
    const params = {
      start_date: dateParams.start_date,
      end_date: dateParams.end_date,
    }
    const data = await analyticsAdminApi.providerDistribution(params)
    providerData.value = { providers: data?.providers || [] }
  } catch (err) {
    console.error('加载 Provider 分布失败:', err)
    providerData.value = { providers: [] }
  } finally {
    loading.provider = false
  }
}

async function loadEvaluationStats() {
  loading.eval = true
  try {
    const params = {
      start_date: dateParams.start_date,
      end_date: dateParams.end_date,
    }
    const data = await analyticsAdminApi.evaluationStats(params)
    evalData.value = {
      total_evaluations: Number(data?.total_evaluations || 0),
      by_status: data?.by_status || {},
      by_period: data?.by_period || [],
    }
  } catch (err) {
    console.error('加载评估统计失败:', err)
    evalData.value = { total_evaluations: 0, by_status: {}, by_period: [] }
  } finally {
    loading.eval = false
  }
}

function reloadAll() {
  // 各图表独立加载,任何一张失败不阻塞其他
  applyRangePreset()
  loadTokenUsage()
  loadCost()
  loadProviderDistribution()
  loadEvaluationStats()
}

onMounted(reloadAll)
</script>

<style scoped>
.control-bar {
  margin-bottom: 16px;
}
.control-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.control-left {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
}
.cards-row {
  margin-bottom: 16px;
}
.metric-card {
  text-align: left;
  margin-bottom: 8px;
}
.metric-label {
  color: #606266;
  font-size: 13px;
}
.metric-value {
  margin-top: 6px;
  color: #303133;
  font-size: 28px;
  font-weight: 600;
  line-height: 1.2;
  word-break: break-all;
}
.metric-sub {
  margin-top: 6px;
  color: #909399;
  font-size: 12px;
}
.chart-card {
  margin-bottom: 16px;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.card-sub {
  color: #909399;
  font-size: 12px;
}
.chart-wrap {
  min-height: 320px;
}
.chart {
  width: 100%;
  height: 320px;
}
</style>
