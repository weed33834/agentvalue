<template>
  <div class="admin-alerts av-fade-in-up">
    <!-- 页面标题 -->
    <div class="page-header mb-16">
      <div class="page-title">
        <el-icon><BellFilled /></el-icon>
        <span>告警管理</span>
      </div>
      <el-button :loading="loading" @click="loadData">
        <el-icon><RefreshLeft /></el-icon>
        刷新
      </el-button>
    </div>

    <!-- 统计概览卡片 -->
    <el-row :gutter="16" class="mb-16">
      <el-col :xs="12" :sm="6" v-for="card in statCards" :key="card.key">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-card-body">
            <div class="stat-icon" :style="{ background: card.color }">
              <el-icon :size="22"><component :is="card.icon" /></el-icon>
            </div>
            <div class="stat-meta">
              <div class="stat-value">{{ stats[card.key] ?? 0 }}</div>
              <div class="stat-label">{{ card.label }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 筛选区 -->
    <div class="filter-bar mb-16">
      <el-select v-model="filters.source" placeholder="来源" clearable class="filter-select" @change="handleSearch">
        <el-option v-for="s in sourceOptions" :key="s.value" :label="s.label" :value="s.value" />
      </el-select>
      <el-select v-model="filters.severity" placeholder="严重级别" clearable class="filter-select" @change="handleSearch">
        <el-option v-for="s in severityOptions" :key="s.value" :label="s.label" :value="s.value" />
      </el-select>
      <el-select v-model="filters.status" placeholder="状态" clearable class="filter-select" @change="handleSearch">
        <el-option v-for="s in statusOptions" :key="s.value" :label="s.label" :value="s.value" />
      </el-select>
      <el-button @click="handleReset">重置</el-button>
    </div>

    <!-- 告警列表 -->
    <el-card v-loading="loading">
      <el-table :data="alerts" stripe empty-text="暂无告警" @row-click="openDetail">
        <el-table-column prop="id" label="ID" width="80" align="center" />
        <el-table-column label="来源" width="140">
          <template #default="{ row }">
            <el-tag size="small">{{ row.source || '—' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="title" label="标题" min-width="200" show-overflow-tooltip />
        <el-table-column label="严重级别" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="severityTagType(row.severity)">{{ severityLabel(row.severity) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="statusTagType(row.status)">{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="触发时间" width="180">
          <template #default="{ row }">{{ formatTime(row.triggered_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click.stop="openDetail(row)">详情</el-button>
            <el-button
              v-if="row.status === 'firing'"
              size="small" link type="warning"
              :loading="actingId === row.id"
              @click.stop="handleAcknowledge(row)"
            >
              确认
            </el-button>
            <el-button
              v-if="row.status !== 'resolved'"
              size="small" link type="success"
              :loading="actingId === row.id"
              @click.stop="handleResolve(row)"
            >
              解决
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="pagination-wrapper">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next, jumper"
          @size-change="handleSearch"
          @current-change="loadData"
        />
      </div>
    </el-card>

    <!-- 详情弹窗 -->
    <el-dialog v-model="detailVisible" title="告警详情" width="680px" @closed="detail = null">
      <el-descriptions v-if="detail" :column="2" border size="small">
        <el-descriptions-item label="ID">{{ detail.id }}</el-descriptions-item>
        <el-descriptions-item label="来源">{{ detail.source || '—' }}</el-descriptions-item>
        <el-descriptions-item label="标题" :span="2">{{ detail.title }}</el-descriptions-item>
        <el-descriptions-item label="严重级别">
          <el-tag size="small" :type="severityTagType(detail.severity)">{{ severityLabel(detail.severity) }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="状态">
          <el-tag size="small" :type="statusTagType(detail.status)">{{ statusLabel(detail.status) }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="触发时间">{{ formatTime(detail.triggered_at) }}</el-descriptions-item>
        <el-descriptions-item label="确认时间">{{ formatTime(detail.acknowledged_at) }}</el-descriptions-item>
        <el-descriptions-item label="描述" :span="2">{{ detail.description || '—' }}</el-descriptions-item>
      </el-descriptions>
      <template #footer>
        <el-button
          v-if="detail && detail.status === 'firing'"
          type="warning" :loading="actingId === detail?.id"
          @click="handleAcknowledge(detail)"
        >
          确认告警
        </el-button>
        <el-button
          v-if="detail && detail.status !== 'resolved'"
          type="success" :loading="actingId === detail?.id"
          @click="handleResolve(detail)"
        >
          解决告警
        </el-button>
        <el-button @click="detailVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { alertApi } from '@/api/client'

const loading = ref(false)
const alerts = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const stats = ref({})
const actingId = ref(null)

const filters = reactive({ source: '', severity: '', status: '' })

const sourceOptions = [
  { value: 'system', label: '系统 (system)' },
  { value: 'model', label: '模型 (model)' },
  { value: 'quota', label: '配额 (quota)' },
  { value: 'security', label: '安全 (security)' },
]
const severityOptions = [
  { value: 'critical', label: '严重 (critical)' },
  { value: 'warning', label: '警告 (warning)' },
  { value: 'info', label: '提示 (info)' },
]
const statusOptions = [
  { value: 'firing', label: '触发中 (firing)' },
  { value: 'acknowledged', label: '已确认 (acknowledged)' },
  { value: 'resolved', label: '已解决 (resolved)' },
]

const statCards = [
  { key: 'total', label: '告警总数', icon: 'BellFilled', color: '#409eff' },
  { key: 'firing', label: '触发中', icon: 'Warning', color: '#f56c6c' },
  { key: 'acknowledged', label: '已确认', icon: 'View', color: '#e6a23c' },
  { key: 'resolved', label: '已解决', icon: 'CircleCheck', color: '#67c23a' },
]

async function loadStats() {
  try {
    const data = await alertApi.stats()
    stats.value = data || {}
  } catch (err) {
    ElMessage.error('加载告警统计失败: ' + (err.message || ''))
  }
}

async function loadData() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (filters.source) params.source = filters.source
    if (filters.severity) params.severity = filters.severity
    if (filters.status) params.status = filters.status
    const data = await alertApi.list(params)
    alerts.value = data.items || []
    total.value = data.total || 0
  } catch (err) {
    ElMessage.error('加载告警列表失败: ' + (err.message || ''))
  } finally {
    loading.value = false
  }
}

function handleSearch() {
  page.value = 1
  loadData()
}

function handleReset() {
  filters.source = ''
  filters.severity = ''
  filters.status = ''
  page.value = 1
  loadData()
}

const detailVisible = ref(false)
const detail = ref(null)

async function openDetail(row) {
  detailVisible.value = true
  detail.value = null
  try {
    detail.value = await alertApi.get(row.id)
  } catch (err) {
    ElMessage.error('加载告警详情失败: ' + (err.message || ''))
    detailVisible.value = false
  }
}

async function handleAcknowledge(row) {
  actingId.value = row.id
  try {
    await alertApi.acknowledge(row.id)
    ElMessage.success('告警已确认')
    if (detail.value && detail.value.id === row.id) detail.value.status = 'acknowledged'
    await loadData()
    await loadStats()
  } catch (err) {
    ElMessage.error('确认告警失败: ' + (err.message || ''))
  } finally {
    actingId.value = null
  }
}

async function handleResolve(row) {
  actingId.value = row.id
  try {
    await alertApi.resolve(row.id)
    ElMessage.success('告警已解决')
    if (detail.value && detail.value.id === row.id) detail.value.status = 'resolved'
    await loadData()
    await loadStats()
  } catch (err) {
    ElMessage.error('解决告警失败: ' + (err.message || ''))
  } finally {
    actingId.value = null
  }
}

function severityLabel(v) {
  const item = severityOptions.find((s) => s.value === v)
  return item ? item.label : v || '—'
}
function severityTagType(v) {
  return { critical: 'danger', warning: 'warning', info: 'info' }[v] || ''
}
function statusLabel(v) {
  const item = statusOptions.find((s) => s.value === v)
  return item ? item.label : v || '—'
}
function statusTagType(v) {
  return { firing: 'danger', acknowledged: 'warning', resolved: 'success' }[v] || 'info'
}
function formatTime(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

onMounted(() => {
  loadStats()
  loadData()
})
</script>

<style scoped>
.mb-16 { margin-bottom: 16px; }
.page-header { display: flex; justify-content: space-between; align-items: center; }
.page-title { display: inline-flex; align-items: center; gap: 8px; font-size: 18px; font-weight: 600; color: var(--el-text-color-primary); }
.filter-bar { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
.filter-select { width: 180px; }
.pagination-wrapper { margin-top: 16px; display: flex; justify-content: flex-end; }
.stat-card-body { display: flex; align-items: center; gap: 14px; }
.stat-icon { width: 48px; height: 48px; border-radius: 10px; display: flex; align-items: center; justify-content: center; color: #fff; }
.stat-value { font-size: 24px; font-weight: 700; color: var(--el-text-color-primary); line-height: 1.2; }
.stat-label { font-size: 13px; color: var(--el-text-color-secondary); margin-top: 2px; }
</style>
