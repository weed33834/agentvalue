<template>
  <div class="admin-webhooks">
    <!-- 顶部说明 -->
    <el-alert type="info" :closable="false" show-icon class="mb-16">
      <template #title>
        Webhook 事件管理 —— 查看外部系统（飞书 / GitLab / 自定义）推送的事件记录、
        处理状态与错误信息，支持查看原始 payload、重试失败事件与删除记录。
      </template>
    </el-alert>

    <!-- 顶部工具栏: 标题 + 刷新 -->
    <div class="toolbar mb-16">
      <span class="section-title">
        <el-icon><Connection /></el-icon>
        Webhook 事件管理
      </span>
      <div class="toolbar-actions">
        <el-button :loading="loading" @click="loadData">
          <el-icon><RefreshLeft /></el-icon>
          刷新
        </el-button>
      </div>
    </div>

    <!-- 筛选区 -->
    <div class="filter-bar mb-16">
      <el-select
        v-model="filters.source"
        placeholder="按来源筛选"
        clearable
        class="filter-select"
        @change="handleSearch"
      >
        <el-option label="全部来源" :value="''" />
        <el-option v-for="s in sourceOptions" :key="s.value" :label="s.label" :value="s.value" />
      </el-select>
      <el-select
        v-model="filters.status"
        placeholder="按状态筛选"
        clearable
        class="filter-select"
        @change="handleSearch"
      >
        <el-option label="全部状态" :value="''" />
        <el-option v-for="s in statusOptions" :key="s.value" :label="s.label" :value="s.value" />
      </el-select>
      <el-button @click="handleReset">重置</el-button>
    </div>

    <!-- 事件列表表格 -->
    <el-card v-loading="loading" :aria-busy="loading">
      <el-table
        :data="events"
        style="width: 100%"
        stripe
        empty-text="暂无 Webhook 事件"
        @row-click="handleRowClick"
      >
        <el-table-column prop="id" label="ID" width="80" align="center" />
        <el-table-column label="来源" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="sourceTagType(row.source)">
              {{ sourceLabel(row.source) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="event_type" label="事件类型" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="mono-text">{{ row.event_type || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="statusTagType(row.status)">
              {{ statusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="接收时间" width="180">
          <template #default="{ row }">
            {{ formatTime(row.received_at) }}
          </template>
        </el-table-column>
        <el-table-column label="处理时间" width="180">
          <template #default="{ row }">
            <span v-if="row.processed_at">{{ formatTime(row.processed_at) }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click.stop="openDetail(row)">
              <el-icon><View /></el-icon>
              详情
            </el-button>
            <el-button
              v-if="row.status === 'failed'"
              size="small"
              link
              type="warning"
              :loading="retryingId === row.id"
              @click.stop="handleRetry(row)"
            >
              <el-icon><RefreshRight /></el-icon>
              重试
            </el-button>
            <el-button size="small" link type="danger" @click.stop="handleDelete(row)">
              <el-icon><Delete /></el-icon>
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- 分页 -->
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

    <!-- 事件详情 Dialog -->
    <el-dialog
      v-model="detailDialogVisible"
      title="Webhook 事件详情"
      width="760px"
      top="6vh"
      @closed="resetDetail"
    >
      <div v-loading="detailLoading">
        <el-descriptions v-if="detail" :column="2" border size="small">
          <el-descriptions-item label="ID">{{ detail.id }}</el-descriptions-item>
          <el-descriptions-item label="来源">
            <el-tag size="small" :type="sourceTagType(detail.source)">
              {{ sourceLabel(detail.source) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="事件类型">
            <span class="mono-text">{{ detail.event_type || '—' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag size="small" :type="statusTagType(detail.status)">
              {{ statusLabel(detail.status) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="租户 ID">
            <span class="mono-text">{{ detail.tenant_id || '—' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="接收时间">
            {{ formatTime(detail.received_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="处理时间">
            <span v-if="detail.processed_at">{{ formatTime(detail.processed_at) }}</span>
            <span v-else class="muted">—</span>
          </el-descriptions-item>
          <el-descriptions-item label="错误信息">
            <span v-if="detail.error_message" class="error-text">
              {{ detail.error_message }}
            </span>
            <span v-else class="muted">无</span>
          </el-descriptions-item>
        </el-descriptions>

        <el-divider content-position="left">
          <span class="payload-title">Payload (解析后)</span>
        </el-divider>
        <pre v-if="payloadJson" class="payload-pre">{{ payloadJson }}</pre>
        <el-empty v-else description="无 Payload 数据" :image-size="60" />
      </div>

      <template #footer>
        <el-button
          v-if="detail && detail.status === 'failed'"
          type="warning"
          :loading="retryingId === detail?.id"
          @click="handleRetry(detail)"
        >
          <el-icon><RefreshRight /></el-icon>
          重试此事件
        </el-button>
        <el-button @click="detailDialogVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { webhookApi } from '@/api/client'

// ====== 列表状态 ======
const loading = ref(false)
const events = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)

const filters = reactive({
  source: '',
  status: '',
})

const sourceOptions = [
  { value: 'feishu', label: '飞书 (feishu)' },
  { value: 'gitlab', label: 'GitLab (gitlab)' },
  { value: 'custom', label: '自定义 (custom)' },
]

const statusOptions = [
  { value: 'pending', label: '待处理 (pending)' },
  { value: 'processed', label: '已处理 (processed)' },
  { value: 'failed', label: '失败 (failed)' },
]

// ====== 列表加载 ======
async function loadData() {
  loading.value = true
  try {
    const params = {
      page: page.value,
      page_size: pageSize.value,
    }
    if (filters.source) params.source = filters.source
    if (filters.status) params.status = filters.status
    const data = await webhookApi.list(params)
    events.value = data.items || []
    total.value = data.total || 0
  } catch (err) {
    console.error('加载 Webhook 事件列表失败:', err)
    ElMessage.error('加载 Webhook 事件列表失败: ' + (err.message || ''))
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
  filters.status = ''
  page.value = 1
  loadData()
}

// ====== 详情 ======
const detailDialogVisible = ref(false)
const detailLoading = ref(false)
const detail = ref(null)
const retryingId = ref(null)

const payloadJson = computed(() => {
  if (!detail.value) return ''
  const payload = detail.value.parsed_payload
  if (payload == null) return ''
  try {
    return JSON.stringify(payload, null, 2)
  } catch {
    return String(payload)
  }
})

async function openDetail(row) {
  detailDialogVisible.value = true
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await webhookApi.get(row.id)
  } catch (err) {
    console.error('加载 Webhook 事件详情失败:', err)
    ElMessage.error('加载事件详情失败: ' + (err.message || ''))
    detailDialogVisible.value = false
  } finally {
    detailLoading.value = false
  }
}

function resetDetail() {
  detail.value = null
}

function handleRowClick(row) {
  openDetail(row)
}

// ====== 重试 ======
async function handleRetry(row) {
  if (row.status !== 'failed') {
    ElMessage.warning('仅失败 (failed) 状态的事件可以重试')
    return
  }
  retryingId.value = row.id
  try {
    await webhookApi.retry(row.id)
    ElMessage.success(`事件 #${row.id} 已重新加入处理队列`)
    // 同步详情态(若详情弹窗打开的是同一事件)
    if (detail.value && detail.value.id === row.id) {
      detail.value.status = 'pending'
      detail.value.error_message = null
      detail.value.processed_at = null
    }
    await loadData()
  } catch (err) {
    console.error('重试 Webhook 事件失败:', err)
    ElMessage.error('重试失败: ' + (err.message || ''))
  } finally {
    retryingId.value = null
  }
}

// ====== 删除 ======
async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(`确认删除 Webhook 事件 #${row.id}? 此操作不可恢复。`, '删除确认', {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await webhookApi.delete(row.id)
    ElMessage.success('删除成功')
    // 若删除的正是详情弹窗中展示的事件,则关闭弹窗
    if (detail.value && detail.value.id === row.id) {
      detailDialogVisible.value = false
    }
    await loadData()
  } catch (err) {
    console.error('删除 Webhook 事件失败:', err)
    ElMessage.error('删除失败: ' + (err.message || ''))
  }
}

// ====== 工具函数 ======
function sourceLabel(value) {
  const item = sourceOptions.find((s) => s.value === value)
  return item ? item.label : value || '—'
}

function sourceTagType(value) {
  const map = { feishu: 'success', gitlab: 'warning', custom: 'info' }
  return map[value] || ''
}

function statusLabel(value) {
  const item = statusOptions.find((s) => s.value === value)
  return item ? item.label : value || '—'
}

function statusTagType(value) {
  const map = { pending: 'warning', processed: 'success', failed: 'danger' }
  return map[value] || 'info'
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
  loadData()
})
</script>

<style scoped>
.admin-webhooks {
  padding: 0;
}
.mb-16 {
  margin-bottom: 16px;
}
.muted {
  color: #909399;
}
.error-text {
  color: #f56c6c;
  word-break: break-all;
}
.mono-text {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px;
  color: #303133;
}
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.toolbar-actions {
  display: flex;
  gap: 12px;
  align-items: center;
}
.section-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  font-size: 16px;
  color: #303133;
}
.filter-bar {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
}
.filter-select {
  width: 200px;
}
.pagination-wrapper {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}
.payload-title {
  font-weight: 600;
  color: #303133;
}
.payload-pre {
  background: #f5f7fa;
  padding: 12px;
  border-radius: 6px;
  font-size: 12px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  overflow-x: auto;
  max-height: 50vh;
  overflow-y: auto;
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
