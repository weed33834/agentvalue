<template>
  <div class="admin-multi-agent">
    <!-- 顶部说明 -->
    <el-alert type="info" :closable="false" show-icon class="mb-16">
      <template #title>
        多 Agent 协作 —— 对标 Coze Multi-Agent。Supervisor 模式调度
        data_analyst / code_reviewer / risk_assessor / report_writer,
        支持 interrupt_at 暂停与人工 resume,各 expert 失败不影响其他 Agent。
      </template>
    </el-alert>

    <div class="layout-row">
      <!-- 左侧:任务列表 + 新建按钮 -->
      <el-card class="left-panel" v-loading="listLoading">
        <template #header>
          <div class="panel-header">
            <span class="panel-title">任务列表</span>
            <div class="panel-actions">
              <el-button
                size="small"
                :loading="listLoading"
                circle
                @click="loadThreads"
              >
                <el-icon><Refresh /></el-icon>
              </el-button>
              <el-button size="small" type="primary" @click="openCreateDialog">
                <el-icon><Plus /></el-icon>
                新建任务
              </el-button>
            </div>
          </div>
        </template>

        <el-select
          v-model="filterStatus"
          placeholder="按状态过滤"
          clearable
          size="small"
          class="filter-select"
          @change="loadThreads"
        >
          <el-option label="全部状态" :value="''" />
          <el-option
            v-for="s in STATUS_OPTIONS"
            :key="s.value"
            :label="s.label"
            :value="s.value"
          />
        </el-select>

        <div class="thread-list">
          <div
            v-for="t in threads"
            :key="t.thread_id"
            class="thread-item"
            :class="{ active: selectedThreadId === t.thread_id }"
            @click="selectThread(t.thread_id)"
          >
            <div class="thread-item-title">{{ t.task || '(无任务名)' }}</div>
            <div class="thread-item-meta">
              <el-tag size="small" :type="statusTagType(t.status)">
                {{ statusLabel(t.status) }}
              </el-tag>
              <span class="thread-item-id">{{ t.thread_id }}</span>
            </div>
            <div class="thread-item-ts">{{ formatTime(t.created_at) }}</div>
          </div>
          <el-empty
            v-if="!threads.length"
            description="暂无任务"
            :image-size="60"
          />
        </div>
      </el-card>

      <!-- 右侧:任务详情 -->
      <div class="right-panel">
        <el-empty
          v-if="!selectedThreadId"
          description="请从左侧选择任务,或新建任务"
        />

        <template v-else>
          <!-- 详情头部:状态 + 操作按钮 -->
          <el-card class="mb-16" v-loading="detailLoading">
            <div class="detail-header">
              <div class="detail-title-block">
                <div class="detail-title">{{ currentTask || '(无任务名)' }}</div>
                <div class="detail-sub">
                  <el-tag size="small" :type="statusTagType(currentStatus)">
                    {{ statusLabel(currentStatus) }}
                  </el-tag>
                  <span class="detail-thread-id">{{ selectedThreadId }}</span>
                  <span v-if="currentInterruptNode" class="detail-interrupt">
                    暂停于: {{ currentInterruptNode }}
                  </span>
                </div>
              </div>
              <div class="detail-actions">
                <el-button
                  size="small"
                  :loading="detailLoading"
                  @click="refreshCurrent"
                >
                  <el-icon><Refresh /></el-icon>
                  刷新
                </el-button>
                <el-button
                  v-if="currentStatus === 'waiting'"
                  size="small"
                  type="primary"
                  @click="openResumeDialog"
                >
                  <el-icon><VideoPlay /></el-icon>
                  恢复执行
                </el-button>
                <el-button
                  size="small"
                  type="warning"
                  @click="openTestDialog"
                >
                  <el-icon><Cpu /></el-icon>
                  测试配置
                </el-button>
              </div>
            </div>
          </el-card>

          <!-- 状态时间线 -->
          <el-card class="mb-16">
            <template #header>
              <span class="panel-title">状态时间线</span>
            </template>
            <el-empty
              v-if="!timeline.length"
              description="暂无时间线"
              :image-size="60"
            />
            <el-timeline v-else>
              <el-timeline-item
                v-for="(item, idx) in timeline"
                :key="idx"
                :timestamp="formatTime(item.ts)"
                :type="timelineTagType(item.status)"
                placement="top"
              >
                <div class="timeline-content">
                  <span class="timeline-node">{{ item.node }}</span>
                  <el-tag size="small" :type="timelineTagType(item.status)">
                    {{ item.status }}
                  </el-tag>
                  <span v-if="item.iteration" class="timeline-iter">
                    迭代 #{{ item.iteration }}
                  </span>
                </div>
                <div v-if="item.extra" class="timeline-extra">
                  <pre>{{ formatExtra(item.extra) }}</pre>
                </div>
              </el-timeline-item>
            </el-timeline>
          </el-card>

          <!-- Artifacts 卡片网格 -->
          <el-card class="mb-16">
            <template #header>
              <span class="panel-title">各 Agent 产出 (Artifacts)</span>
            </template>
            <el-empty
              v-if="!artifactKeys.length"
              description="暂无产出"
              :image-size="60"
            />
            <div v-else class="artifact-grid">
              <el-card
                v-for="key in artifactKeys"
                :key="key"
                class="artifact-card"
                :class="{ 'artifact-error': isArtifactError(artifacts[key]) }"
                shadow="hover"
              >
                <template #header>
                  <div class="artifact-header">
                    <el-icon><User /></el-icon>
                    <span class="artifact-name">{{ agentLabel(key) }}</span>
                    <el-tag
                      v-if="isArtifactError(artifacts[key])"
                      size="small"
                      type="danger"
                    >
                      失败
                    </el-tag>
                    <el-tag v-else size="small" type="success">成功</el-tag>
                  </div>
                </template>
                <div class="artifact-body">
                  <pre>{{ formatArtifact(artifacts[key]) }}</pre>
                </div>
              </el-card>
            </div>
          </el-card>

          <!-- 最终报告区 -->
          <el-card>
            <template #header>
              <div class="report-header">
                <span class="panel-title">最终报告</span>
                <el-button
                  v-if="currentFinalReport"
                  size="small"
                  link
                  @click="copyReport"
                >
                  <el-icon><CopyDocument /></el-icon>
                  复制
                </el-button>
              </div>
            </template>
            <el-empty
              v-if="!currentFinalReport"
              description="暂无最终报告 (任务尚未完成或 report_writer 未执行)"
              :image-size="60"
            />
            <div v-else class="final-report" v-html="renderedReport"></div>
          </el-card>
        </template>
      </div>
    </div>

    <!-- 新建任务 Dialog -->
    <el-dialog
      v-model="createDialogVisible"
      title="新建多 Agent 任务"
      width="640px"
      @closed="resetCreateForm"
    >
      <el-form
        ref="createFormRef"
        :model="createForm"
        :rules="createFormRules"
        label-position="top"
        v-loading="createSubmitting"
      >
        <el-form-item label="任务描述" prop="task">
          <el-input
            v-model="createForm.task"
            type="textarea"
            :rows="3"
            placeholder="如: 分析员工 E1001 在 2026-W28 的表现,综合日报、代码贡献和风险"
          />
        </el-form-item>
        <el-form-item label="上下文 (JSON)">
          <el-input
            v-model="createContextInput"
            type="textarea"
            :rows="3"
            placeholder='{"employee_id": "E1001", "period": "2026-W28"}'
          />
          <span class="form-hint">共享给所有 Agent 的上下文,如 employee_id / period</span>
        </el-form-item>
        <el-form-item :label="`最大迭代次数: ${createForm.max_iterations}`">
          <el-slider
            v-model="createForm.max_iterations"
            :min="1"
            :max="50"
            :step="1"
            show-input
          />
          <span class="form-hint">默认 10, 硬上限 50 (防失控)</span>
        </el-form-item>
        <el-form-item label="暂停节点 (可选)">
          <el-select
            v-model="createForm.interrupt_at"
            placeholder="不暂停"
            clearable
            style="width: 100%"
          >
            <el-option label="不暂停" :value="''" />
            <el-option
              v-for="n in INTERRUPT_NODES"
              :key="n.value"
              :label="n.label"
              :value="n.value"
            />
          </el-select>
          <span class="form-hint">执行到此节点时暂停等人工确认 (Human-in-the-loop)</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createDialogVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="createSubmitting"
          @click="handleCreate"
        >运行</el-button>
      </template>
    </el-dialog>

    <!-- 恢复执行 Dialog -->
    <el-dialog
      v-model="resumeDialogVisible"
      title="恢复执行"
      width="540px"
    >
      <el-form label-position="top">
        <el-form-item label="决策">
          <el-input
            v-model="resumeForm.decision"
            placeholder="如 approve / reject / 修改后继续"
          />
        </el-form-item>
        <el-form-item label="备注">
          <el-input
            v-model="resumeForm.comment"
            type="textarea"
            :rows="3"
            placeholder="人工备注 (可选)"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="resumeDialogVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="resumeSubmitting"
          @click="handleResume"
        >恢复</el-button>
      </template>
    </el-dialog>

    <!-- 测试配置 Dialog (同步执行) -->
    <el-dialog
      v-model="testDialogVisible"
      title="同步测试多 Agent 配置"
      width="640px"
      @closed="resetTestForm"
    >
      <el-alert type="warning" :closable="false" show-icon class="mb-16">
        <template #title>
          测试模式同步执行 (不进队列),适合快速验证 Agent 路由与产出是否正确。
        </template>
      </el-alert>
      <el-form
        ref="testFormRef"
        :model="testForm"
        :rules="createFormRules"
        label-position="top"
        v-loading="testSubmitting"
      >
        <el-form-item label="任务描述" prop="task">
          <el-input
            v-model="testForm.task"
            type="textarea"
            :rows="3"
            placeholder="测试任务描述"
          />
        </el-form-item>
        <el-form-item label="上下文 (JSON)">
          <el-input
            v-model="testContextInput"
            type="textarea"
            :rows="3"
            placeholder='{"employee_id": "E1001"}'
          />
        </el-form-item>
        <el-form-item :label="`最大迭代次数: ${testForm.max_iterations}`">
          <el-slider
            v-model="testForm.max_iterations"
            :min="1"
            :max="50"
            :step="1"
            show-input
          />
        </el-form-item>
        <el-form-item label="暂停节点 (可选)">
          <el-select
            v-model="testForm.interrupt_at"
            placeholder="不暂停"
            clearable
            style="width: 100%"
          >
            <el-option label="不暂停" :value="''" />
            <el-option
              v-for="n in INTERRUPT_NODES"
              :key="n.value"
              :label="n.label"
              :value="n.value"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <div v-if="testResult" class="test-result">
        <el-divider content-position="left">测试结果</el-divider>
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="状态">
            <el-tag :type="statusTagType(testResult.status)">
              {{ statusLabel(testResult.status) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item v-if="testResult.interrupt_node" label="暂停节点">
            {{ testResult.interrupt_node }}
          </el-descriptions-item>
          <el-descriptions-item v-if="testResult.error" label="错误">
            <span class="error-text">{{ testResult.error }}</span>
          </el-descriptions-item>
          <el-descriptions-item v-if="testResult.iteration !== undefined" label="迭代次数">
            {{ testResult.iteration }}
          </el-descriptions-item>
        </el-descriptions>
      </div>
      <template #footer>
        <el-button @click="testDialogVisible = false">关闭</el-button>
        <el-button
          type="primary"
          :loading="testSubmitting"
          @click="handleTest"
        >执行测试</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { multiAgentAdminApi } from '@/api/client'

// 状态枚举 (与后端 thread_store 对齐)
const STATUS_OPTIONS = [
  { value: 'running', label: '运行中' },
  { value: 'waiting', label: '等待人工' },
  { value: 'completed', label: '已完成' },
  { value: 'failed', label: '失败' },
]

// 可中断节点 (与后端 ALL_ROUTABLE 对齐)
const INTERRUPT_NODES = [
  { value: 'data_analyst', label: '数据分析师 (data_analyst)' },
  { value: 'code_reviewer', label: '代码评审 (code_reviewer)' },
  { value: 'risk_assessor', label: '风险评估 (risk_assessor)' },
  { value: 'report_writer', label: '报告生成 (report_writer)' },
]

// Agent 中文名映射 (用于 artifacts 卡片标题)
const AGENT_LABELS = {
  data_analyst: '数据分析师',
  code_reviewer: '代码评审',
  risk_assessor: '风险评估',
  report_writer: '报告生成',
}

// ============================================================
// 左侧任务列表
// ============================================================

const listLoading = ref(false)
const threads = ref([])
const filterStatus = ref('')
const selectedThreadId = ref('')

async function loadThreads() {
  listLoading.value = true
  try {
    const params = {}
    if (filterStatus.value) params.status = filterStatus.value
    const data = await multiAgentAdminApi.listThreads(params)
    threads.value = data.items || []
    // 如果当前选中的 thread 不在列表中, 清空选择
    if (
      selectedThreadId.value &&
      !threads.value.find((t) => t.thread_id === selectedThreadId.value)
    ) {
      selectedThreadId.value = ''
    }
  } catch (err) {
    ElMessage.error(err.message || '加载任务列表失败')
  } finally {
    listLoading.value = false
  }
}

function selectThread(threadId) {
  selectedThreadId.value = threadId
  refreshCurrent()
}

// ============================================================
// 右侧详情
// ============================================================

const detailLoading = ref(false)
const currentMeta = ref(null)
const currentValues = ref({})

const currentTask = computed(() => currentMeta.value?.task || '')
const currentStatus = computed(() => currentMeta.value?.status || '')
const currentInterruptNode = computed(
  () => currentMeta.value?.interrupt_node || '',
)
const currentFinalReport = computed(
  () => currentMeta.value?.final_report || currentValues.value?.final_report || '',
)

const timeline = computed(() => {
  const t = currentValues.value?.timeline || currentMeta.value?.timeline || []
  return Array.isArray(t) ? t : []
})

const artifacts = computed(() => {
  const a =
    currentValues.value?.artifacts || currentMeta.value?.artifacts || {}
  return a || {}
})

const artifactKeys = computed(() => Object.keys(artifacts.value))

// 简易 markdown 渲染 (避免引入新依赖, 仅处理标题/列表/段落)
const renderedReport = computed(() => renderMarkdown(currentFinalReport.value))

async function refreshCurrent() {
  if (!selectedThreadId.value) return
  detailLoading.value = true
  try {
    const data = await multiAgentAdminApi.getState(selectedThreadId.value)
    currentMeta.value = data.meta || {}
    currentValues.value = data.values || {}
  } catch (err) {
    ElMessage.error(err.message || '加载任务详情失败')
  } finally {
    detailLoading.value = false
  }
}

// ============================================================
// 新建任务 Dialog
// ============================================================

const createDialogVisible = ref(false)
const createSubmitting = ref(false)
const createFormRef = ref(null)

const createForm = reactive({
  task: '',
  max_iterations: 10,
  interrupt_at: '',
})

const createContextInput = ref('')

const createFormRules = {
  task: [
    { required: true, message: '请输入任务描述', trigger: 'blur' },
    { min: 1, max: 2000, message: '长度 1-2000 字符', trigger: 'blur' },
  ],
}

function openCreateDialog() {
  resetCreateForm()
  createDialogVisible.value = true
}

function resetCreateForm() {
  createForm.task = ''
  createForm.max_iterations = 10
  createForm.interrupt_at = ''
  createContextInput.value = ''
  createFormRef.value?.clearValidate?.()
}

async function handleCreate() {
  if (!createFormRef.value) return
  try {
    await createFormRef.value.validate()
  } catch {
    return
  }
  // 解析 context JSON
  let context = {}
  if (createContextInput.value.trim()) {
    try {
      context = JSON.parse(createContextInput.value)
    } catch {
      ElMessage.error('上下文 JSON 解析失败, 请检查格式')
      return
    }
  }
  createSubmitting.value = true
  const payload = {
    task: createForm.task,
    context,
    max_iterations: createForm.max_iterations,
    interrupt_at: createForm.interrupt_at || null,
  }
  try {
    const data = await multiAgentAdminApi.run(payload)
    ElMessage.success(`任务已创建: ${data.thread_id}`)
    createDialogVisible.value = false
    selectedThreadId.value = data.thread_id
    await loadThreads()
    await refreshCurrent()
  } catch (err) {
    ElMessage.error(err.message || '创建任务失败')
  } finally {
    createSubmitting.value = false
  }
}

// ============================================================
// 恢复执行 Dialog
// ============================================================

const resumeDialogVisible = ref(false)
const resumeSubmitting = ref(false)
const resumeForm = reactive({
  decision: 'approve',
  comment: '',
})

function openResumeDialog() {
  resumeForm.decision = 'approve'
  resumeForm.comment = ''
  resumeDialogVisible.value = true
}

async function handleResume() {
  if (!selectedThreadId.value) return
  resumeSubmitting.value = true
  try {
    await multiAgentAdminApi.resume(selectedThreadId.value, {
      decision: resumeForm.decision || 'approve',
      comment: resumeForm.comment || '',
    })
    ElMessage.success('已发送恢复指令')
    resumeDialogVisible.value = false
    await refreshCurrent()
  } catch (err) {
    ElMessage.error(err.message || '恢复失败')
  } finally {
    resumeSubmitting.value = false
  }
}

// ============================================================
// 测试配置 Dialog (同步执行)
// ============================================================

const testDialogVisible = ref(false)
const testSubmitting = ref(false)
const testFormRef = ref(null)
const testResult = ref(null)

const testForm = reactive({
  task: '',
  max_iterations: 10,
  interrupt_at: '',
})

const testContextInput = ref('')

function openTestDialog() {
  resetTestForm()
  testDialogVisible.value = true
}

function resetTestForm() {
  testForm.task = ''
  testForm.max_iterations = 10
  testForm.interrupt_at = ''
  testContextInput.value = ''
  testResult.value = null
  testFormRef.value?.clearValidate?.()
}

async function handleTest() {
  if (!testFormRef.value) return
  try {
    await testFormRef.value.validate()
  } catch {
    return
  }
  let context = {}
  if (testContextInput.value.trim()) {
    try {
      context = JSON.parse(testContextInput.value)
    } catch {
      ElMessage.error('上下文 JSON 解析失败')
      return
    }
  }
  testSubmitting.value = true
  const payload = {
    task: testForm.task,
    context,
    max_iterations: testForm.max_iterations,
    interrupt_at: testForm.interrupt_at || null,
  }
  try {
    const data = await multiAgentAdminApi.test(payload)
    testResult.value = data
    if (data.status === 'completed') {
      ElMessage.success('测试完成')
    } else if (data.status === 'waiting') {
      ElMessage.info(`测试暂停于: ${data.interrupt_node}`)
    } else if (data.status === 'failed') {
      ElMessage.error('测试失败: ' + (data.error || ''))
    }
    // 测试完成后刷新左侧列表 (可能创建了 thread)
    await loadThreads()
  } catch (err) {
    ElMessage.error(err.message || '测试失败')
  } finally {
    testSubmitting.value = false
  }
}

// ============================================================
// 辅助函数
// ============================================================

function statusLabel(s) {
  const item = STATUS_OPTIONS.find((x) => x.value === s)
  return item ? item.label : s || '未知'
}

function statusTagType(s) {
  const map = {
    running: 'primary',
    waiting: 'warning',
    completed: 'success',
    failed: 'danger',
  }
  return map[s] || 'info'
}

function timelineTagType(s) {
  const map = {
    ok: 'success',
    resumed: 'primary',
    error: 'danger',
    llm_error: 'danger',
    max_iter_exceeded: 'warning',
  }
  return map[s] || 'info'
}

function agentLabel(name) {
  return AGENT_LABELS[name] || name
}

function isArtifactError(artifact) {
  return (
    artifact &&
    typeof artifact === 'object' &&
    Object.prototype.hasOwnProperty.call(artifact, 'error')
  )
}

function formatArtifact(artifact) {
  if (!artifact) return ''
  try {
    return JSON.stringify(artifact, null, 2)
  } catch {
    return String(artifact)
  }
}

function formatExtra(extra) {
  if (!extra) return ''
  if (typeof extra === 'string') return extra
  try {
    return JSON.stringify(extra, null, 2)
  } catch {
    return String(extra)
  }
}

function formatTime(ts) {
  if (!ts) return ''
  // ISO 时间字符串 或 unix 时间戳
  if (typeof ts === 'number') {
    return new Date(ts * (ts < 1e12 ? 1000 : 1)).toLocaleString()
  }
  try {
    return new Date(ts).toLocaleString()
  } catch {
    return String(ts)
  }
}

function copyReport() {
  if (!currentFinalReport.value) return
  navigator.clipboard
    .writeText(currentFinalReport.value)
    .then(() => ElMessage.success('已复制'))
    .catch(() => ElMessage.error('复制失败'))
}

// 简易 markdown 渲染 (避免引入新依赖)
function renderMarkdown(md) {
  if (!md) return ''
  // 转义 HTML
  let html = md
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
  // 标题
  html = html.replace(/^###### (.*)$/gm, '<h6>$1</h6>')
  html = html.replace(/^##### (.*)$/gm, '<h5>$1</h5>')
  html = html.replace(/^#### (.*)$/gm, '<h4>$1</h4>')
  html = html.replace(/^### (.*)$/gm, '<h3>$1</h3>')
  html = html.replace(/^## (.*)$/gm, '<h2>$1</h2>')
  html = html.replace(/^# (.*)$/gm, '<h1>$1</h1>')
  // 粗体 / 斜体
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>')
  // 行内代码
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>')
  // 无序列表
  html = html.replace(/^- (.*)$/gm, '<li>$1</li>')
  // 段落 (空行分隔)
  html = html
    .split(/\n\n+/)
    .map((block) => {
      if (
        block.startsWith('<h') ||
        block.startsWith('<li')
      ) {
        return block
      }
      return `<p>${block.replace(/\n/g, '<br>')}</p>`
    })
    .join('')
  // 包裹连续 <li>
  html = html.replace(/(<li>.*?<\/li>(?:\s*<li>.*?<\/li>)*)/gs, '<ul>$1</ul>')
  return html
}

// ============================================================
// 轮询: 选中任务运行中时定时刷新
// ============================================================

let pollTimer = null

function startPolling() {
  stopPolling()
  pollTimer = setInterval(() => {
    if (
      selectedThreadId.value &&
      (currentStatus.value === 'running' || currentStatus.value === 'waiting')
    ) {
      refreshCurrent()
    }
  }, 2000)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

onMounted(() => {
  loadThreads()
  startPolling()
})

onUnmounted(() => {
  stopPolling()
})
</script>

<style scoped>
.admin-multi-agent {
  padding: 0;
}
.mb-16 {
  margin-bottom: 16px;
}
.layout-row {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}
.left-panel {
  width: 320px;
  flex-shrink: 0;
}
.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.panel-title {
  font-weight: 600;
}
.panel-actions {
  display: flex;
  gap: 8px;
}
.filter-select {
  width: 100%;
  margin-bottom: 12px;
}
.thread-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.thread-item {
  padding: 10px;
  border: 1px solid #e4e7ed;
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.2s;
}
.thread-item:hover {
  border-color: #409eff;
  background-color: #f0f7ff;
}
.thread-item.active {
  border-color: #409eff;
  background-color: #ecf5ff;
}
.thread-item-title {
  font-size: 14px;
  font-weight: 500;
  margin-bottom: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.thread-item-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 2px;
}
.thread-item-id {
  font-size: 12px;
  color: #909399;
  font-family: monospace;
}
.thread-item-ts {
  font-size: 11px;
  color: #c0c4cc;
}
.right-panel {
  flex: 1;
  min-width: 0;
}
.detail-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.detail-title {
  font-size: 16px;
  font-weight: 600;
  margin-bottom: 6px;
  word-break: break-word;
}
.detail-sub {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 12px;
  color: #909399;
}
.detail-thread-id {
  font-family: monospace;
}
.detail-interrupt {
  color: #e6a23c;
}
.detail-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}
.timeline-content {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.timeline-node {
  font-weight: 600;
}
.timeline-iter {
  font-size: 12px;
  color: #909399;
}
.timeline-extra {
  margin-top: 4px;
  font-size: 12px;
}
.timeline-extra pre {
  margin: 0;
  padding: 6px;
  background: #f5f7fa;
  border-radius: 3px;
  font-family: monospace;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 120px;
  overflow-y: auto;
}
.artifact-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
}
.artifact-card {
  border: 1px solid #e4e7ed;
}
.artifact-card.artifact-error {
  border-color: #f56c6c;
}
.artifact-header {
  display: flex;
  align-items: center;
  gap: 6px;
}
.artifact-name {
  font-weight: 600;
  flex: 1;
}
.artifact-body {
  max-height: 240px;
  overflow-y: auto;
}
.artifact-body pre {
  margin: 0;
  font-family: monospace;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
}
.report-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.final-report {
  line-height: 1.7;
  word-break: break-word;
}
.final-report :deep(h1) {
  font-size: 1.4em;
  margin: 0.6em 0 0.4em;
  border-bottom: 1px solid #eee;
}
.final-report :deep(h2) {
  font-size: 1.25em;
  margin: 0.6em 0 0.4em;
}
.final-report :deep(h3) {
  font-size: 1.1em;
  margin: 0.5em 0 0.3em;
}
.final-report :deep(ul) {
  margin: 0.4em 0;
  padding-left: 1.6em;
}
.final-report :deep(li) {
  margin: 0.2em 0;
}
.final-report :deep(code) {
  background: #f5f7fa;
  padding: 1px 4px;
  border-radius: 3px;
  font-family: monospace;
  font-size: 0.9em;
}
.form-hint {
  display: block;
  margin-top: 4px;
  font-size: 12px;
  color: #909399;
}
.test-result {
  margin-top: 8px;
}
.error-text {
  color: #f56c6c;
  word-break: break-word;
}

@media (max-width: 1024px) {
  .layout-row {
    flex-direction: column;
  }
  .left-panel {
    width: 100%;
  }
}
</style>
