<template>
  <div class="admin-debug">
    <el-alert
      type="info"
      :closable="false"
      show-icon
      class="mb-16"
    >
      <template #title>
        调试与可观测性中心 —— 对标 Langfuse Trace Dashboard。支持追溯某次评估用了哪个 Prompt 版本、
        哪个模型档位、触发了哪些护栏规则、系统熔断器/MCP/工具的整体健康状态。
      </template>
    </el-alert>

    <el-tabs v-model="activeTab">
      <!-- Tab 1: 系统健康 -->
      <el-tab-pane label="系统健康" name="health">
        <div class="toolbar">
          <el-button type="primary" :loading="healthLoading" @click="loadHealth">
            <el-icon><RefreshLeft /></el-icon>
            刷新
          </el-button>
          <span class="field-hint">最后更新: {{ lastHealthUpdate || '—' }}</span>
        </div>

        <el-row :gutter="16" class="mb-16">
          <el-col :span="12">
            <el-card v-loading="healthLoading">
              <template #header>
                <span class="section-title">
                  <el-icon><Warning /></el-icon>
                  Circuit Breaker 状态
                </span>
              </template>
              <el-alert
                v-if="health.circuit_breakers?.error"
                type="error"
                :closable="false"
                show-icon
              >
                {{ health.circuit_breakers.error }}
              </el-alert>
              <template v-else>
                <el-table
                  :data="circuitBreakerRows"
                  size="small"
                  stripe
                  v-if="circuitBreakerRows.length"
                >
                  <el-table-column prop="name" label="熔断器名" min-width="160" />
                  <el-table-column prop="state" label="状态" width="140">
                    <template #default="{ row }">
                      <el-tag :type="circuitTagType(row.state)" size="small">
                        <el-icon class="tag-icon"><component :is="circuitIcon(row.state)" /></el-icon>
                        {{ row.state }}
                      </el-tag>
                    </template>
                  </el-table-column>
                </el-table>
                <el-empty v-else description="暂无熔断器记录 (尚未发生调用)" />
              </template>
              <div class="legend mt-16">
                <span class="legend-item">
                  <el-tag type="success" size="small">closed</el-tag> 正常放行
                </span>
                <span class="legend-item">
                  <el-tag type="warning" size="small">half_open</el-tag> 探针恢复中
                </span>
                <span class="legend-item">
                  <el-tag type="danger" size="small">open</el-tag> 熔断 fast-fail
                </span>
              </div>
            </el-card>
          </el-col>

          <el-col :span="12">
            <el-card v-loading="healthLoading">
              <template #header>
                <span class="section-title">
                  <el-icon><Link /></el-icon>
                  MCP 服务器状态
                </span>
              </template>
              <el-alert
                v-if="!health.mcp?.available"
                type="warning"
                :closable="false"
                show-icon
              >
                MCP 适配器未安装,如需 MCP 工具请安装 langchain-mcp-adapters
              </el-alert>
              <template v-else>
                <el-table
                  :data="health.mcp?.servers || []"
                  size="small"
                  stripe
                >
                  <el-table-column prop="name" label="服务器名" min-width="140" />
                  <el-table-column prop="transport" label="协议" width="120">
                    <template #default="{ row }">
                      <el-tag size="small">{{ row.transport }}</el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column prop="status" label="状态" width="100">
                    <template #default="{ row }">
                      <el-tag :type="row.connected ? 'success' : 'info'" size="small">
                        {{ row.connected ? '已连接' : '未连接' }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column prop="tools_count" label="工具数" width="90" align="center" />
                </el-table>
                <el-empty
                  v-if="!(health.mcp?.servers?.length)"
                  description="未配置 MCP 服务器"
                />
              </template>
            </el-card>
          </el-col>
        </el-row>

        <el-card v-loading="healthLoading">
          <template #header>
            <span class="section-title">
              <el-icon><Tools /></el-icon>
              LangChain / ReAct Agent 可用性
            </span>
          </template>
          <el-row :gutter="16">
            <el-col :span="8">
              <div class="status-block">
                <div class="status-label">LangChain 工具</div>
                <el-tag
                  :type="health.tools?.langchain_available ? 'success' : 'danger'"
                  size="large"
                >
                  {{ health.tools?.langchain_available ? '可用' : '未安装' }}
                </el-tag>
              </div>
            </el-col>
            <el-col :span="8">
              <div class="status-block">
                <div class="status-label">ReAct Agent</div>
                <el-tag
                  :type="health.tools?.react_agent_available ? 'success' : 'danger'"
                  size="large"
                >
                  {{ health.tools?.react_agent_available ? '可用' : '未安装' }}
                </el-tag>
              </div>
            </el-col>
            <el-col :span="8">
              <div class="status-block">
                <div class="status-label">可用工具数</div>
                <div class="status-value">
                  {{ health.tools?.available_tools?.length || 0 }} 个
                </div>
              </div>
            </el-col>
          </el-row>
          <div v-if="health.tools?.available_tools?.length" class="tools-list mt-16">
            <el-tag
              v-for="t in health.tools.available_tools"
              :key="t"
              size="small"
              class="tool-chip"
            >
              {{ t }}
            </el-tag>
          </div>
        </el-card>
      </el-tab-pane>

      <!-- Tab 2: Prompt 版本追溯 -->
      <el-tab-pane label="Prompt 版本追溯" name="prompt-version">
        <el-card>
          <template #header>
            <span class="section-title">
              <el-icon><Document /></el-icon>
              查询评估使用的 Prompt 版本
            </span>
          </template>
          <el-alert type="info" :closable="false" show-icon class="mb-16">
            从评估的 audit (AES-GCM 加密) 字段解密出 Prompt 版本信息,
            对标 Langfuse trace 详情页的 prompt_version metadata 字段。
            <br />
            prompt_source 取值:
            <strong>db</strong> (DB 加载,含 A/B / 灰度) ·
            <strong>file</strong> (文件 PromptLoader) ·
            <strong>file_fallback</strong> (DB 无此 prompt 回退文件) ·
            <strong>file_error</strong> (DB 异常回退文件)
          </el-alert>
          <el-form :inline="true" class="query-form">
            <el-form-item label="评估 ID">
              <el-input
                v-model="promptVerEvalId"
                placeholder="输入评估 ID"
                clearable
                style="width: 320px"
                @keyup.enter="loadPromptVersion"
              />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="promptVerLoading" @click="loadPromptVersion">
                <el-icon><Search /></el-icon>
                查询
              </el-button>
            </el-form-item>
          </el-form>

          <div v-if="promptVerResult" class="result-block">
            <el-alert
              v-if="promptVerResult.error"
              type="error"
              :closable="false"
              show-icon
            >
              {{ promptVerResult.error }}
              <span v-if="promptVerResult.raw_audit_type">
                (audit 类型: {{ promptVerResult.raw_audit_type }})
              </span>
            </el-alert>
            <el-descriptions v-else :column="2" border>
              <el-descriptions-item label="评估 ID">
                {{ promptVerResult.evaluation_id }}
              </el-descriptions-item>
              <el-descriptions-item label="员工 ID">
                {{ promptVerResult.employee_id || '—' }}
              </el-descriptions-item>
              <el-descriptions-item label="周期">
                {{ promptVerResult.period || '—' }}
              </el-descriptions-item>
              <el-descriptions-item label="Prompt 版本">
                <el-tag v-if="promptVerResult.prompt_version" type="primary" size="small">
                  v{{ promptVerResult.prompt_version }}
                </el-tag>
                <span v-else class="muted">—</span>
              </el-descriptions-item>
              <el-descriptions-item label="Prompt 来源">
                <el-tag :type="sourceTagType(promptVerResult.prompt_source)" size="small">
                  {{ promptVerResult.prompt_source || 'file' }}
                </el-tag>
              </el-descriptions-item>
              <el-descriptions-item label="版本 ID">
                <code v-if="promptVerResult.prompt_version_id">
                  {{ promptVerResult.prompt_version_id }}
                </code>
                <span v-else class="muted">—</span>
              </el-descriptions-item>
              <el-descriptions-item label="模型档位">
                <el-tag v-if="promptVerResult.model_tier" size="small">
                  {{ promptVerResult.model_tier }}
                </el-tag>
                <span v-else class="muted">—</span>
              </el-descriptions-item>
              <el-descriptions-item label="模型名">
                {{ promptVerResult.model_name || '—' }}
              </el-descriptions-item>
              <el-descriptions-item label="处理耗时">
                {{ promptVerResult.processing_time_ms != null
                  ? promptVerResult.processing_time_ms + ' ms' : '—' }}
              </el-descriptions-item>
              <el-descriptions-item label="置信度">
                {{ promptVerResult.confidence_score != null
                  ? promptVerResult.confidence_score.toFixed(2) : '—' }}
              </el-descriptions-item>
              <el-descriptions-item label="触发护栏规则" :span="2">
                <el-tag
                  v-for="r in promptVerResult.triggered_rules || []"
                  :key="r"
                  type="warning"
                  size="small"
                  class="rule-chip"
                >
                  {{ r }}
                </el-tag>
                <span v-if="!promptVerResult.triggered_rules?.length" class="muted">无</span>
              </el-descriptions-item>
            </el-descriptions>
            <div v-if="promptVerResult.note" class="note mt-16">
              <el-icon><InfoFilled /></el-icon>
              {{ promptVerResult.note }}
            </div>
          </div>
        </el-card>
      </el-tab-pane>

      <!-- Tab 3: 完整 Trace 链路 -->
      <el-tab-pane label="完整 Trace 链路" name="trace">
        <el-card>
          <template #header>
            <span class="section-title">
              <el-icon><Connection /></el-icon>
              查询评估的完整 Trace 链路
            </span>
          </template>
          <el-alert type="info" :closable="false" show-icon class="mb-16">
            汇总 audit + manager_view 的执行元数据,对标 Langfuse trace 详情:
            模型选型、Prompt 版本、护栏触发、风险标记、处理耗时。
          </el-alert>
          <el-form :inline="true" class="query-form">
            <el-form-item label="评估 ID">
              <el-input
                v-model="traceEvalId"
                placeholder="输入评估 ID"
                clearable
                style="width: 320px"
                @keyup.enter="loadTrace"
              />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="traceLoading" @click="loadTrace">
                <el-icon><Search /></el-icon>
                查询
              </el-button>
              <el-button v-if="traceResult" @click="copyTrace">复制 JSON</el-button>
            </el-form-item>
          </el-form>

          <div v-if="traceResult" class="result-block">
            <!-- 顶部概要 -->
            <el-descriptions :column="4" border size="small" class="mb-16">
              <el-descriptions-item label="评估 ID">
                {{ traceResult.evaluation_id }}
              </el-descriptions-item>
              <el-descriptions-item label="员工 ID">
                {{ traceResult.employee_id || '—' }}
              </el-descriptions-item>
              <el-descriptions-item label="周期">
                {{ traceResult.period || '—' }}
              </el-descriptions-item>
              <el-descriptions-item label="状态">
                <el-tag :type="evalStatusTagType(traceResult.status)" size="small">
                  {{ traceResult.status || '—' }}
                </el-tag>
              </el-descriptions-item>
              <el-descriptions-item label="总分" :span="4">
                <el-tag v-if="traceResult.overall_score != null" type="success" size="small">
                  {{ traceResult.overall_score.toFixed(1) }}
                </el-tag>
                <span v-else class="muted">—</span>
              </el-descriptions-item>
            </el-descriptions>

            <!-- Trace 详情 4 块 -->
            <el-row :gutter="16">
              <el-col :span="12">
                <el-card shadow="hover" class="trace-card">
                  <template #header>
                    <span class="trace-card-title">
                      <el-icon><Cpu /></el-icon>
                      模型选型
                    </span>
                  </template>
                  <el-descriptions :column="1" size="small" border>
                    <el-descriptions-item label="模型名">
                      {{ traceResult.trace?.model?.name || '—' }}
                    </el-descriptions-item>
                    <el-descriptions-item label="档位">
                      <el-tag v-if="traceResult.trace?.model?.tier" size="small">
                        {{ traceResult.trace.model.tier }}
                      </el-tag>
                      <span v-else class="muted">—</span>
                    </el-descriptions-item>
                  </el-descriptions>
                </el-card>
              </el-col>
              <el-col :span="12">
                <el-card shadow="hover" class="trace-card">
                  <template #header>
                    <span class="trace-card-title">
                      <el-icon><Document /></el-icon>
                      Prompt 版本
                    </span>
                  </template>
                  <el-descriptions :column="1" size="small" border>
                    <el-descriptions-item label="版本">
                      <el-tag
                        v-if="traceResult.trace?.prompt?.version != null"
                        type="primary"
                        size="small"
                      >
                        v{{ traceResult.trace.prompt.version }}
                      </el-tag>
                      <span v-else class="muted">—</span>
                    </el-descriptions-item>
                    <el-descriptions-item label="来源">
                      <el-tag
                        :type="sourceTagType(traceResult.trace?.prompt?.source)"
                        size="small"
                      >
                        {{ traceResult.trace?.prompt?.source || 'file' }}
                      </el-tag>
                    </el-descriptions-item>
                    <el-descriptions-item label="版本 ID">
                      <code v-if="traceResult.trace?.prompt?.version_id">
                        {{ traceResult.trace.prompt.version_id }}
                      </code>
                      <span v-else class="muted">—</span>
                    </el-descriptions-item>
                  </el-descriptions>
                </el-card>
              </el-col>
              <el-col :span="12">
                <el-card shadow="hover" class="trace-card">
                  <template #header>
                    <span class="trace-card-title">
                      <el-icon><Timer /></el-icon>
                      性能指标
                    </span>
                  </template>
                  <el-descriptions :column="1" size="small" border>
                    <el-descriptions-item label="处理耗时">
                      {{ traceResult.trace?.performance?.processing_time_ms != null
                        ? traceResult.trace.performance.processing_time_ms + ' ms'
                        : '—' }}
                    </el-descriptions-item>
                    <el-descriptions-item label="置信度">
                      {{ traceResult.trace?.performance?.confidence_score != null
                        ? traceResult.trace.performance.confidence_score.toFixed(2)
                        : '—' }}
                    </el-descriptions-item>
                  </el-descriptions>
                </el-card>
              </el-col>
              <el-col :span="12">
                <el-card shadow="hover" class="trace-card">
                  <template #header>
                    <span class="trace-card-title">
                      <el-icon><Warning /></el-icon>
                      护栏与风险
                    </span>
                  </template>
                  <div class="guard-section">
                    <div class="guard-label">触发护栏规则:</div>
                    <div class="guard-tags">
                      <el-tag
                        v-for="r in traceResult.trace?.guards?.triggered_rules || []"
                        :key="r"
                        type="warning"
                        size="small"
                        class="rule-chip"
                      >
                        {{ r }}
                      </el-tag>
                      <span v-if="!traceResult.trace?.guards?.triggered_rules?.length" class="muted">
                        无
                      </span>
                    </div>
                    <div class="guard-label mt-8">风险标记:</div>
                    <div class="guard-tags">
                      <el-tag
                        v-for="r in traceResult.trace?.risk?.risk_flags || []"
                        :key="r"
                        type="danger"
                        size="small"
                        class="rule-chip"
                      >
                        {{ r }}
                      </el-tag>
                      <span v-if="!traceResult.trace?.risk?.risk_flags?.length" class="muted">
                        无
                      </span>
                    </div>
                  </div>
                </el-card>
              </el-col>
            </el-row>

            <el-alert
              v-if="traceResult.langfuse_hint"
              type="success"
              :closable="false"
              show-icon
              class="mt-16"
            >
              {{ traceResult.langfuse_hint }}
            </el-alert>
          </div>
        </el-card>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { debugAdminApi } from '@/api/client'

const activeTab = ref('health')

// ====== 系统健康 ======
const healthLoading = ref(false)
const health = ref({})
const lastHealthUpdate = ref('')

async function loadHealth() {
  healthLoading.value = true
  try {
    health.value = await debugAdminApi.getSystemHealth()
    lastHealthUpdate.value = new Date().toLocaleString('zh-CN', { hour12: false })
  } catch (err) {
    ElMessage.error('加载系统健康失败: ' + err.message)
  } finally {
    healthLoading.value = false
  }
}

const circuitBreakerRows = computed(() => {
  const cb = health.value.circuit_breakers
  if (!cb || cb.error) return []
  return Object.entries(cb).map(([name, state]) => ({ name, state }))
})

function circuitTagType(state) {
  if (state === 'closed') return 'success'
  if (state === 'half_open') return 'warning'
  if (state === 'open') return 'danger'
  return 'info'
}

function circuitIcon(state) {
  if (state === 'closed') return 'CircleCheck'
  if (state === 'open') return 'CircleClose'
  return 'Loading'
}

// ====== Prompt 版本追溯 ======
const promptVerEvalId = ref('')
const promptVerLoading = ref(false)
const promptVerResult = ref(null)

async function loadPromptVersion() {
  if (!promptVerEvalId.value.trim()) {
    ElMessage.warning('请输入评估 ID')
    return
  }
  promptVerLoading.value = true
  promptVerResult.value = null
  try {
    promptVerResult.value = await debugAdminApi.getEvaluationPromptVersion(
      promptVerEvalId.value.trim(),
    )
  } catch (err) {
    ElMessage.error('查询失败: ' + err.message)
  } finally {
    promptVerLoading.value = false
  }
}

function sourceTagType(source) {
  if (source === 'db') return 'success'
  if (source === 'file') return 'info'
  if (source === 'file_fallback') return 'warning'
  if (source === 'file_error') return 'danger'
  return 'info'
}

// ====== 完整 Trace 链路 ======
const traceEvalId = ref('')
const traceLoading = ref(false)
const traceResult = ref(null)

async function loadTrace() {
  if (!traceEvalId.value.trim()) {
    ElMessage.warning('请输入评估 ID')
    return
  }
  traceLoading.value = true
  traceResult.value = null
  try {
    traceResult.value = await debugAdminApi.getEvaluationTrace(traceEvalId.value.trim())
  } catch (err) {
    ElMessage.error('查询失败: ' + err.message)
  } finally {
    traceLoading.value = false
  }
}

function evalStatusTagType(status) {
  const map = {
    approved: 'success',
    rejected: 'danger',
    ai_drafted: 'info',
    manager_review: 'warning',
    hr_audit: 'warning',
    ai_processing: '',
    error: 'danger',
  }
  return map[status] || 'info'
}

async function copyTrace() {
  if (!traceResult.value) return
  try {
    await navigator.clipboard.writeText(JSON.stringify(traceResult.value, null, 2))
    ElMessage.success('已复制到剪贴板')
  } catch {
    ElMessage.warning('剪贴板不可用,请手动复制')
  }
}

onMounted(loadHealth)
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
.toolbar {
  display: flex;
  gap: 12px;
  align-items: center;
  margin-bottom: 16px;
}
.section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
}
.field-hint {
  color: #909399;
  font-size: 12px;
}
.legend {
  display: flex;
  gap: 16px;
  font-size: 12px;
  color: #606266;
}
.legend-item {
  display: flex;
  align-items: center;
  gap: 4px;
}
.tag-icon {
  margin-right: 4px;
}
.status-block {
  text-align: center;
  padding: 16px 0;
}
.status-label {
  color: #909399;
  font-size: 12px;
  margin-bottom: 8px;
}
.status-value {
  font-size: 22px;
  font-weight: 600;
  color: #303133;
}
.tools-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.tool-chip {
  margin-bottom: 4px;
}
.query-form {
  margin-bottom: 16px;
}
.result-block {
  margin-top: 16px;
}
.muted {
  color: #909399;
}
.rule-chip {
  margin-right: 4px;
  margin-bottom: 4px;
}
.note {
  color: #606266;
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.trace-card {
  margin-bottom: 16px;
}
.trace-card-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
}
.guard-section {
  padding: 8px 0;
}
.guard-label {
  font-size: 13px;
  color: #606266;
  margin-bottom: 6px;
  font-weight: 600;
}
.guard-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: 8px;
}
</style>
