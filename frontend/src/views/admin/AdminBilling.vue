<template>
  <div class="admin-billing av-fade-in-up">
    <div class="page-header mb-16">
      <div class="page-title">
        <el-icon><Wallet /></el-icon>
        <span>计费管理</span>
      </div>
      <div class="toolbar-actions">
        <el-date-picker
          v-model="dateRange"
          type="monthrange"
          range-separator="至"
          start-placeholder="开始月份"
          end-placeholder="结束月份"
          value-format="YYYY-MM"
          @change="loadAll"
        />
        <el-button :loading="exportLoading" type="success" @click="handleExport">
          <el-icon><Download /></el-icon>导出账单
        </el-button>
      </div>
    </div>

    <!-- 账单汇总卡片 -->
    <el-row :gutter="16" class="mb-16">
      <el-col :xs="24" :sm="8" v-for="card in summaryCards" :key="card.key">
        <el-card shadow="hover" class="summary-card">
          <div class="summary-body">
            <div class="summary-icon" :style="{ background: card.color }">
              <el-icon :size="24"><component :is="card.icon" /></el-icon>
            </div>
            <div class="summary-meta">
              <div class="summary-label">{{ card.label }}</div>
              <div class="summary-value">{{ formatMoney(summary[card.key]) }}<span class="summary-unit">{{ summary.currency || 'CNY' }}</span></div>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 按月趋势 -->
    <el-card class="mb-16" v-loading="trendLoading">
      <template #header><span class="section-title"><el-icon><TrendCharts /></el-icon> 按月费用趋势</span></template>
      <div class="trend-chart">
        <div v-for="point in trend" :key="point.month" class="trend-bar-wrap">
          <div class="trend-bar" :style="{ height: barHeight(point.amount) + '%', background: 'linear-gradient(180deg, #60a5fa, #2563eb)' }"></div>
          <div class="trend-amount">{{ formatMoney(point.amount) }}</div>
          <div class="trend-month">{{ point.month }}</div>
        </div>
        <el-empty v-if="!trendLoading && trend.length === 0" description="暂无趋势数据" :image-size="60" />
      </div>
    </el-card>

    <!-- 按用户费用 -->
    <el-card class="mb-16" v-loading="byUserLoading">
      <template #header><span class="section-title"><el-icon><User /></el-icon> 按用户费用</span></template>
      <el-table :data="byUser" stripe empty-text="暂无数据">
        <el-table-column prop="user_id" label="用户 ID" min-width="160" show-overflow-tooltip />
        <el-table-column prop="user_name" label="用户名" min-width="140" show-overflow-tooltip />
        <el-table-column prop="tenant_id" label="租户" min-width="140" show-overflow-tooltip />
        <el-table-column label="请求数" width="120"><template #default="{ row }">{{ formatNum(row.request_count) }}</template></el-table-column>
        <el-table-column label="Token 用量" width="140"><template #default="{ row }">{{ formatNum(row.token_count) }}</template></el-table-column>
        <el-table-column label="费用" width="140" align="right">
          <template #default="{ row }"><span class="money-text">{{ formatMoney(row.cost) }} {{ summary.currency || 'CNY' }}</span></template>
        </el-table-column>
        <el-table-column label="占比" width="180">
          <template #default="{ row }">
            <el-progress :percentage="costPercent(row.cost)" :stroke-width="12" :text-inside="true" />
          </template>
        </el-table-column>
      </el-table>
      <div class="pagination-wrapper">
        <el-pagination v-model:current-page="userPage" v-model:page-size="userPageSize" :total="userTotal" :page-sizes="[10, 20, 50]" layout="total, sizes, prev, pager, next, jumper" @size-change="loadByUser" @current-change="loadByUser" />
      </div>
    </el-card>

    <!-- 按端点费用 -->
    <el-card v-loading="byEndpointLoading">
      <template #header><span class="section-title"><el-icon><Connection /></el-icon> 按端点费用</span></template>
      <el-table :data="byEndpoint" stripe empty-text="暂无数据">
        <el-table-column prop="endpoint" label="端点" min-width="220" show-overflow-tooltip />
        <el-table-column prop="model" label="模型" min-width="140" show-overflow-tooltip />
        <el-table-column label="请求数" width="120"><template #default="{ row }">{{ formatNum(row.request_count) }}</template></el-table-column>
        <el-table-column label="Token 用量" width="140"><template #default="{ row }">{{ formatNum(row.token_count) }}</template></el-table-column>
        <el-table-column label="平均延迟" width="120"><template #default="{ row }">{{ row.avg_latency_ms ? row.avg_latency_ms + 'ms' : '—' }}</template></el-table-column>
        <el-table-column label="费用" width="140" align="right">
          <template #default="{ row }"><span class="money-text">{{ formatMoney(row.cost) }} {{ summary.currency || 'CNY' }}</span></template>
        </el-table-column>
        <el-table-column label="占比" width="180">
          <template #default="{ row }">
            <el-progress :percentage="costPercent(row.cost)" :stroke-width="12" :text-inside="true" color="#9254de" />
          </template>
        </el-table-column>
      </el-table>
      <div class="pagination-wrapper">
        <el-pagination v-model:current-page="endpointPage" v-model:page-size="endpointPageSize" :total="endpointTotal" :page-sizes="[10, 20, 50]" layout="total, sizes, prev, pager, next, jumper" @size-change="loadByEndpoint" @current-change="loadByEndpoint" />
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { billingApi, analyticsV2Api } from '@/api/client'

const dateRange = ref([])
const exportLoading = ref(false)

// 汇总
const summary = ref({})
const summaryLoading = ref(false)
const summaryCards = [
  { key: 'total_cost', label: '总费用', icon: 'Wallet', color: '#2563eb' },
  { key: 'token_cost', label: 'Token 费用', icon: 'Coin', color: '#e6a23c' },
  { key: 'request_cost', label: '请求费用', icon: 'Histogram', color: '#67c23a' },
]

// 趋势
const trend = ref([])
const trendLoading = ref(false)
const maxTrend = computed(() => Math.max(1, ...trend.value.map((p) => Number(p.amount) || 0)))
function barHeight(amount) {
  const v = Number(amount) || 0
  return Math.max(2, Math.round((v / maxTrend.value) * 100))
}

// 按用户
const byUser = ref([])
const byUserLoading = ref(false)
const userTotal = ref(0)
const userPage = ref(1)
const userPageSize = ref(20)

// 按端点
const byEndpoint = ref([])
const byEndpointLoading = ref(false)
const endpointTotal = ref(0)
const endpointPage = ref(1)
const endpointPageSize = ref(20)

const totalCost = computed(() => Number(summary.value.total_cost) || 0)
function costPercent(cost) {
  const t = totalCost.value
  if (t <= 0) return 0
  return Math.min(100, Math.round((Number(cost) || 0) / t * 100))
}

function dateParams() {
  const params = {}
  if (dateRange.value && dateRange.value.length === 2) {
    params.start = dateRange.value[0]
    params.end = dateRange.value[1]
  }
  return params
}

async function loadSummary() {
  summaryLoading.value = true
  try { summary.value = await billingApi.summary(dateParams()) || {} }
  catch (err) { ElMessage.error('加载账单汇总失败: ' + (err.message || '')) } finally { summaryLoading.value = false }
}

async function loadTrend() {
  trendLoading.value = true
  try {
    const data = await analyticsV2Api.cost({ ...dateParams() })
    trend.value = data.items || data.points || []
  } catch (err) {
    ElMessage.error('加载趋势失败: ' + (err.message || ''))
  } finally { trendLoading.value = false }
}

async function loadByUser() {
  byUserLoading.value = true
  try {
    const data = await billingApi.byUser({ ...dateParams(), page: userPage.value, page_size: userPageSize.value })
    byUser.value = data.items || []
    userTotal.value = data.total || 0
  } catch (err) {
    ElMessage.error('加载按用户费用失败: ' + (err.message || ''))
  } finally { byUserLoading.value = false }
}

async function loadByEndpoint() {
  byEndpointLoading.value = true
  try {
    const data = await billingApi.byEndpoint({ ...dateParams(), page: endpointPage.value, page_size: endpointPageSize.value })
    byEndpoint.value = data.items || []
    endpointTotal.value = data.total || 0
  } catch (err) {
    ElMessage.error('加载按端点费用失败: ' + (err.message || ''))
  } finally { byEndpointLoading.value = false }
}

function loadAll() {
  loadSummary()
  loadTrend()
  loadByUser()
  loadByEndpoint()
}

async function handleExport() {
  exportLoading.value = true
  try {
    const blob = await billingApi.export(dateParams())
    const url = window.URL.createObjectURL(new Blob([blob]))
    const a = document.createElement('a')
    a.href = url
    a.download = `billing-${Date.now()}.csv`
    a.click()
    window.URL.revokeObjectURL(url)
    ElMessage.success('账单导出成功')
  } catch (err) {
    ElMessage.error('导出失败: ' + (err.message || ''))
  } finally {
    exportLoading.value = false
  }
}

function formatMoney(n) {
  if (n == null) return '0.00'
  return Number(n).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}
function formatNum(n) {
  if (n == null) return '0'
  return Number(n).toLocaleString('zh-CN')
}

onMounted(() => { loadAll() })
</script>

<style scoped>
.mb-16 { margin-bottom: 16px; }
.page-header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }
.page-title { display: inline-flex; align-items: center; gap: 8px; font-size: 18px; font-weight: 600; color: var(--el-text-color-primary); }
.toolbar-actions { display: flex; gap: 12px; align-items: center; }
.section-title { display: inline-flex; align-items: center; gap: 6px; font-weight: 600; color: var(--el-text-color-primary); }
.summary-card { height: 100%; }
.summary-body { display: flex; align-items: center; gap: 16px; }
.summary-icon { width: 56px; height: 56px; border-radius: 12px; display: flex; align-items: center; justify-content: center; color: #fff; flex-shrink: 0; }
.summary-label { font-size: 13px; color: var(--el-text-color-secondary); }
.summary-value { font-size: 26px; font-weight: 700; color: var(--el-text-color-primary); line-height: 1.3; }
.summary-unit { font-size: 13px; color: var(--el-text-color-secondary); margin-left: 6px; font-weight: 400; }
.trend-chart { display: flex; align-items: flex-end; gap: 16px; height: 200px; padding: 16px 8px 0; overflow-x: auto; }
.trend-bar-wrap { display: flex; flex-direction: column; align-items: center; min-width: 60px; height: 100%; justify-content: flex-end; }
.trend-bar { width: 36px; border-radius: 6px 6px 0 0; transition: height 0.4s ease; min-height: 4px; }
.trend-amount { font-size: 11px; color: var(--el-text-color-regular); margin-top: 4px; }
.trend-month { font-size: 12px; color: var(--el-text-color-secondary); margin-top: 2px; }
.pagination-wrapper { margin-top: 16px; display: flex; justify-content: flex-end; }
.money-text { font-weight: 600; color: #2563eb; }
</style>
