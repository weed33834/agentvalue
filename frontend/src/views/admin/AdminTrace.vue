<template>
  <div class="admin-trace">
    <!-- 顶部说明 -->
    <el-alert type="info" :closable="false" show-icon class="mb-16">
      <template #title>
        应用内 Trace 可视化 —— 对标 Langfuse Trace UI。左侧选择评估,右侧展示
        节点级 spans 树与时间线 Gantt 图,支持按 employee_id / period / status 过滤。
      </template>
    </el-alert>

    <el-row :gutter="16" class="trace-layout">
      <!-- 左侧:评估列表(可搜索) -->
      <el-col :span="7">
        <el-card class="list-card" shadow="never">
          <template #header>
            <div class="list-header">
              <span class="section-title">
                <el-icon><Collection /></el-icon>
                评估列表
              </span>
              <el-button
                size="small"
                :loading="listLoading"
                @click="loadList(1)"
              >
                <el-icon><RefreshLeft /></el-icon>
              </el-button>
            </div>
          </template>

          <!-- 过滤表单 -->
          <div class="filter-bar">
            <el-input
              v-model="filters.employee_id"
              placeholder="员工 ID"
              size="small"
              clearable
              class="filter-input"
              @keyup.enter="loadList(1)"
              @clear="loadList(1)"
            />
            <el-input
              v-model="filters.period"
              placeholder="周期"
              size="small"
              clearable
              class="filter-input"
              @keyup.enter="loadList(1)"
              @clear="loadList(1)"
            />
            <el-select
              v-model="filters.status"
              placeholder="状态"
              size="small"
              clearable
              class="filter-select"
              @change="loadList(1)"
            >
              <el-option
                v-for="s in statusOptions"
                :key="s.value"
                :label="s.label"
                :value="s.value"
              />
            </el-select>
            <el-button
              size="small"
              type="primary"
              :loading="listLoading"
              @click="loadList(1)"
            >
              <el-icon><Search /></el-icon>
            </el-button>
          </div>

          <!-- 列表 -->
          <div v-loading="listLoading" class="list-body">
            <div
              v-for="item in list"
              :key="item.evaluation_id"
              class="eval-item"
              :class="{ active: selectedId === item.evaluation_id }"
              @click="selectEvaluation(item)"
            >
              <div class="eval-row">
                <span class="eval-id" :title="item.evaluation_id">
                  {{ shortenId(item.evaluation_id) }}
                </span>
                <el-tag :type="statusTagType(item.status)" size="small">
                  {{ item.status }}
                </el-tag>
              </div>
              <div class="eval-row meta">
                <span class="meta-text">
                  <el-icon><User /></el-icon>
                  {{ item.employee_id }}
                </span>
                <span class="meta-text">
                  <el-icon><Calendar /></el-icon>
                  {{ item.period || '—' }}
                </span>
              </div>
              <div class="eval-row meta">
                <span class="meta-text">
                  总分 {{ formatScore(item.overall_score) }}
                </span>
                <span class="meta-text muted">
                  {{ formatTime(item.created_at) }}
                </span>
              </div>
            </div>

            <el-empty v-if="!listLoading && !list.length" description="无匹配评估" />
          </div>

          <el-pagination
            v-model:current-page="page"
            v-model:page-size="pageSize"
            :total="total"
            :page-sizes="[10, 20, 50]"
            layout="prev, pager, next, total"
            size="small"
            class="pagination"
            @size-change="loadList(1)"
            @current-change="loadList()"
          />
        </el-card>
      </el-col>

      <!-- 右侧:Trace 详情 -->
      <el-col :span="17">
        <el-card shadow="never" class="detail-card">
          <template #header>
            <div class="detail-header">
              <span class="section-title">
                <el-icon><Connection /></el-icon>
                Trace 详情
              </span>
              <span v-if="traceData" class="header-eval-id">
                {{ traceData.evaluation_id }}
              </span>
            </div>
          </template>

          <div v-loading="traceLoading">
            <el-empty v-if="!traceData && !traceLoading" description="请从左侧选择评估查看 Trace" />

            <div v-if="traceData">
              <!-- 顶部统计 -->
              <el-row :gutter="12" class="stat-row mb-16">
                <el-col :span="5">
                  <div class="stat-card">
                    <div class="stat-label">总耗时</div>
                    <div class="stat-value">{{ formatMs(traceData.timeline?.total_ms) }}</div>
                  </div>
                </el-col>
                <el-col :span="5">
                  <div class="stat-card">
                    <div class="stat-label">Span 数</div>
                    <div class="stat-value">{{ traceData.timeline?.span_count ?? 0 }}</div>
                  </div>
                </el-col>
                <el-col :span="4">
                  <div class="stat-card">
                    <div class="stat-label">失败 span</div>
                    <div class="stat-value" :class="{ 'text-danger': traceData.timeline?.failed_count > 0 }">
                      {{ traceData.timeline?.failed_count ?? 0 }}
                    </div>
                  </div>
                </el-col>
                <el-col :span="5">
                  <div class="stat-card">
                    <div class="stat-label">模型名</div>
                    <div class="stat-value-sm" :title="traceData.trace?.model?.name || ''">
                      {{ traceData.trace?.model?.name || '—' }}
                    </div>
                  </div>
                </el-col>
                <el-col :span="5">
                  <div class="stat-card">
                    <div class="stat-label">Prompt 版本</div>
                    <div class="stat-value-sm">
                      <el-tag
                        v-if="traceData.trace?.prompt?.version != null"
                        type="primary"
                        size="small"
                      >
                        v{{ traceData.trace.prompt.version }}
                      </el-tag>
                      <span v-else class="muted">—</span>
                    </div>
                  </div>
                </el-col>
              </el-row>

              <!-- 上半:Trace 树状视图 -->
              <el-card shadow="never" class="tree-card">
                <template #header>
                  <span class="section-title">
                    <el-icon><Files /></el-icon>
                    Span 树状视图
                  </span>
                </template>
                <el-tree
                  :data="treeData"
                  :props="treeProps"
                  default-expand-all
                  :expand-on-click-node="true"
                  node-key="key"
                >
                  <template #default="{ node, data: nodeData }">
                    <div class="tree-node">
                      <span class="tree-label" :class="{ 'node-root': nodeData.isRoot }">
                        {{ node.label }}
                      </span>
                      <el-tag
                        v-if="nodeData.duration_ms != null"
                        :type="statusTagType(nodeData.status)"
                        size="small"
                        class="tree-tag"
                      >
                        {{ formatMs(nodeData.duration_ms) }}
                      </el-tag>
                      <el-tag
                        v-if="nodeData.status && !nodeData.isRoot"
                        :type="statusTagType(nodeData.status)"
                        size="small"
                        effect="plain"
                        class="tree-tag"
                      >
                        {{ nodeData.status }}
                      </el-tag>
                      <el-collapse
                        v-if="nodeData.attributes"
                        class="attr-collapse"
                        @click.stop
                      >
                        <el-collapse-item title="attributes">
                          <pre class="attr-pre">{{ formatJson(nodeData.attributes) }}</pre>
                        </el-collapse-item>
                      </el-collapse>
                    </div>
                  </template>
                </el-tree>
              </el-card>

              <!-- 下半:时间线 Gantt 图 -->
              <el-card shadow="never" class="gantt-card mt-16">
                <template #header>
                  <span class="section-title">
                    <el-icon><DataLine /></el-icon>
                    时间线 Gantt 图
                  </span>
                </template>
                <v-chart
                  v-if="spansForChart.length"
                  class="gantt-chart"
                  :option="ganttOption"
                  autoresize
                />
                <el-empty v-else description="无 span 数据" />
                <div class="legend mt-8">
                  <span class="legend-item">
                    <span class="legend-dot dot-success"></span> success
                  </span>
                  <span class="legend-item">
                    <span class="legend-dot dot-warning"></span> warning
                  </span>
                  <span class="legend-item">
                    <span class="legend-dot dot-skipped"></span> skipped
                  </span>
                </div>
              </el-card>

              <el-alert
                v-if="traceData.langfuse_hint"
                type="success"
                :closable="false"
                show-icon
                class="mt-16"
              >
                {{ traceData.langfuse_hint }}
              </el-alert>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import VChart from 'vue-echarts'
import '@/utils/echarts'
import { traceAdminApi } from '@/api/client'

// ====== 左侧评估列表 ======
const list = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const listLoading = ref(false)
const filters = reactive({
  employee_id: '',
  period: '',
  status: '',
})

const statusOptions = [
  { label: 'AI 草稿', value: 'ai_drafted' },
  { label: '主管复核', value: 'manager_review' },
  { label: 'HR 复核', value: 'hr_audit' },
  { label: '已审批', value: 'approved' },
  { label: '已拒绝', value: 'rejected' },
  { label: '处理中', value: 'ai_processing' },
  { label: '错误', value: 'error' },
]

async function loadList(targetPage) {
  if (targetPage) page.value = targetPage
  listLoading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (filters.employee_id) params.employee_id = filters.employee_id
    if (filters.period) params.period = filters.period
    if (filters.status) params.status = filters.status
    const data = await traceAdminApi.listEvaluations(params)
    list.value = data.items || []
    total.value = data.total || 0
  } catch (err) {
    ElMessage.error('加载评估列表失败: ' + (err.message || ''))
  } finally {
    listLoading.value = false
  }
}

function selectEvaluation(item) {
  selectedId.value = item.evaluation_id
  loadTrace(item.evaluation_id)
}

// ====== 右侧 Trace 详情 ======
const selectedId = ref('')
const traceData = ref(null)
const traceLoading = ref(false)

async function loadTrace(evaluationId) {
  traceLoading.value = true
  traceData.value = null
  try {
    traceData.value = await traceAdminApi.getTrace(evaluationId)
  } catch (err) {
    ElMessage.error('加载 Trace 失败: ' + (err.message || ''))
  } finally {
    traceLoading.value = false
  }
}

// 树状视图数据:根节点 evaluation_id + 子节点 7 个 span
const treeProps = { children: 'children', label: 'label' }
const treeData = computed(() => {
  if (!traceData.value) return []
  const root = {
    key: 'root',
    label: `evaluation: ${traceData.value.evaluation_id}`,
    isRoot: true,
    children: (traceData.value.spans || []).map((s, idx) => ({
      key: `span-${idx}-${s.name}`,
      label: s.name,
      duration_ms: s.duration_ms,
      status: s.status,
      attributes: s.attributes,
    })),
  }
  return [root]
})

// 时间线 Gantt 数据
const spansForChart = computed(() => traceData.value?.spans || [])

const statusColorMap = {
  success: '#67c23a',
  warning: '#e6a23c',
  skipped: '#909399',
}

const ganttOption = computed(() => {
  const spans = spansForChart.value
  if (!spans.length) return {}
  // 倒序展示让第一个 span 在最上方(横向条形图自下而上)
  const reversed = [...spans].reverse()
  const categories = reversed.map((s) => s.name)
  // 堆叠条形图:base 系列(start_ms,透明) + duration 系列(duration_ms,按 status 着色)
  const baseData = reversed.map((s) => s.start_ms || 0)
  const durationData = reversed.map((s, idx) => ({
    value: s.duration_ms || 0,
    itemStyle: { color: statusColorMap[s.status] || '#909399' },
  }))
  // x 轴最大值: total_ms 或最后一个 span 的结束时间
  const maxTime = traceData.value?.timeline?.total_ms || 0

  return {
    tooltip: {
      trigger: 'item',
      formatter: (p) => {
        const idx = p.dataIndex
        const span = reversed[idx]
        if (!span) return p.name
        return `${span.name}<br/>起: ${(span.start_ms || 0).toFixed(1)} ms<br/>止: ${((span.start_ms || 0) + (span.duration_ms || 0)).toFixed(1)} ms<br/>耗时: ${span.duration_ms || 0} ms<br/>状态: ${span.status}`
      },
    },
    grid: { left: 130, right: 30, top: 20, bottom: 40 },
    xAxis: {
      type: 'value',
      name: '时间 (ms)',
      min: 0,
      max: maxTime > 0 ? Math.ceil(maxTime * 1.05) : undefined,
    },
    yAxis: {
      type: 'category',
      data: categories,
      inverse: false,
    },
    series: [
      // 透明 base 系列: 占据 start_ms 的偏移,让 duration 条对齐正确位置
      {
        name: 'start',
        type: 'bar',
        stack: 'gantt',
        data: baseData,
        barWidth: '60%',
        itemStyle: { color: 'transparent' },
        silent: true,
      },
      // 实际耗时条,按 status 着色
      {
        name: 'duration',
        type: 'bar',
        stack: 'gantt',
        data: durationData,
        barWidth: '60%',
        label: {
          show: true,
          position: 'right',
          formatter: (p) => `${p.value} ms`,
          fontSize: 11,
          color: '#606266',
        },
      },
    ],
  }
})

// ====== 工具函数 ======
function shortenId(id) {
  if (!id) return '—'
  return id.length > 14 ? `${id.slice(0, 8)}…${id.slice(-4)}` : id
}

function formatScore(score) {
  if (score == null) return '—'
  return Number(score).toFixed(1)
}

function formatTime(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

function formatMs(ms) {
  if (ms == null) return '—'
  return `${ms} ms`
}

function formatJson(obj) {
  if (obj == null) return '—'
  try {
    return JSON.stringify(obj, null, 2)
  } catch {
    return String(obj)
  }
}

function statusTagType(status) {
  if (status === 'success' || status === 'approved') return 'success'
  if (status === 'warning' || status === 'manager_review' || status === 'hr_audit') return 'warning'
  if (status === 'skipped' || status === 'ai_drafted') return 'info'
  if (status === 'rejected' || status === 'error') return 'danger'
  return 'info'
}

// 选中变化时不重载 trace(已在 selectEvaluation 中处理)

onMounted(() => {
  loadList(1)
})
</script>

<style scoped>
.mb-16 {
  margin-bottom: 16px;
}
.mt-16 {
  margin-top: 16px;
}
.mt-8 {
  margin-top: 8px;
}
.muted {
  color: #909399;
}
.text-danger {
  color: #f56c6c;
}
.admin-trace {
  min-height: 100%;
}
.trace-layout {
  align-items: stretch;
}
.section-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
}
.list-card,
.detail-card {
  height: 100%;
}
.list-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.detail-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}
.header-eval-id {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: #606266;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 60%;
}
.filter-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 12px;
}
.filter-input {
  width: 100%;
}
.filter-bar .el-input.filter-input {
  width: calc(50% - 4px);
}
.filter-select {
  width: 100%;
}
.list-body {
  max-height: calc(100vh - 360px);
  overflow-y: auto;
}
.eval-item {
  padding: 10px 12px;
  border-radius: 4px;
  cursor: pointer;
  border: 1px solid transparent;
  transition: background 0.15s, border-color 0.15s;
  margin-bottom: 6px;
}
.eval-item:hover {
  background: #f5f7fa;
}
.eval-item.active {
  background: #ecf5ff;
  border-color: #409eff;
}
.eval-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}
.eval-row.meta {
  font-size: 12px;
  color: #606266;
  margin-top: 4px;
}
.eval-row.meta .meta-text {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.eval-id {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px;
  color: #303133;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pagination {
  margin-top: 12px;
  display: flex;
  justify-content: center;
}
.stat-row {
  margin-bottom: 4px;
}
.stat-card {
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  padding: 12px;
  height: 100%;
}
.stat-label {
  color: #909399;
  font-size: 12px;
  margin-bottom: 6px;
}
.stat-value {
  color: #303133;
  font-size: 20px;
  font-weight: 600;
  line-height: 1.2;
}
.stat-value-sm {
  color: #303133;
  font-size: 14px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tree-card,
.gantt-card {
  border: 1px solid #ebeef5;
}
.tree-node {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  width: 100%;
}
.tree-label {
  font-size: 13px;
  color: #303133;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.node-root {
  font-weight: 600;
  color: #2563eb;
}
.tree-tag {
  margin-right: 4px;
}
.attr-collapse {
  display: inline-block;
  width: 100%;
  margin-top: 4px;
}
.attr-collapse :deep(.el-collapse-item__header) {
  height: 28px;
  line-height: 28px;
  font-size: 12px;
  color: #606266;
  background: transparent;
  border-bottom: none;
}
.attr-collapse :deep(.el-collapse-item__content) {
  padding-bottom: 8px;
}
.attr-pre {
  background: #f5f7fa;
  padding: 8px;
  border-radius: 4px;
  font-size: 12px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  overflow-x: auto;
  margin: 0;
}
.gantt-chart {
  width: 100%;
  height: 320px;
}
.legend {
  display: flex;
  gap: 16px;
  font-size: 12px;
  color: #606266;
}
.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.legend-dot {
  width: 12px;
  height: 12px;
  border-radius: 2px;
  display: inline-block;
}
.dot-success {
  background: #67c23a;
}
.dot-warning {
  background: #e6a23c;
}
.dot-skipped {
  background: #909399;
}
</style>
