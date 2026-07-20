<template>
  <div class="playground">
    <el-alert
      type="info"
      :closable="false"
      show-icon
      class="mb-16"
    >
      <template #title>
        Prompt Playground —— 对标 Langfuse Playground。选择 Prompt 版本 + 模型 + 变量,
        实时流式预览 LLM 输出。支持 tool_calls 实时展示与 trace 追溯。
      </template>
    </el-alert>

    <el-row :gutter="16">
      <!-- 左侧配置面板 -->
      <el-col :span="9">
        <el-card shadow="never" v-loading="loading">
          <template #header>
            <span>配置</span>
          </template>

          <el-form label-position="top">
            <el-form-item label="Prompt 模板">
              <el-select
                v-model="form.prompt_name"
                filterable
                placeholder="选择模板"
                style="width: 100%"
                @change="onTemplateChange"
              >
                <el-option
                  v-for="t in templates"
                  :key="t.name"
                  :label="t.name"
                  :value="t.name"
                />
              </el-select>
            </el-form-item>

            <el-form-item label="版本">
              <el-select
                v-model="form.prompt_version"
                placeholder="选择版本"
                style="width: 100%"
                @change="onVersionChange"
              >
                <el-option
                  v-for="v in versions"
                  :key="v.version"
                  :label="`v${v.version}`"
                  :value="v.version"
                >
                  <span style="float: left">v{{ v.version }}</span>
                  <span
                    v-for="lb in versionLabelsMap[v.version] || []"
                    :key="lb"
                    style="float: right; margin-left: 6px"
                  >
                    <el-tag :type="labelTagType(lb)" size="small" effect="plain">
                      {{ lb }}
                    </el-tag>
                  </span>
                </el-option>
              </el-select>
            </el-form-item>

            <el-form-item label="模型">
              <el-select
                v-model="form.model_name"
                filterable
                placeholder="选择模型"
                style="width: 100%"
              >
                <el-option
                  v-for="m in availableModels"
                  :key="m.value"
                  :label="m.label"
                  :value="m.value"
                />
              </el-select>
            </el-form-item>

            <template v-if="variablesSchema.length > 0">
              <el-form-item label="变量">
                <div class="variables-wrap">
                  <el-form-item
                    v-for="v in variablesSchema"
                    :key="v.name"
                    :label="v.name"
                    class="var-item"
                  >
                    <el-input-number
                      v-if="v.type === 'number' || v.type === 'integer'"
                      v-model="form.variables[v.name]"
                      :placeholder="v.description"
                      style="width: 100%"
                      controls-position="right"
                    />
                    <el-input
                      v-else-if="v.type === 'text' || v.type === 'long' || v.type === 'textarea'"
                      v-model="form.variables[v.name]"
                      type="textarea"
                      :rows="3"
                      :placeholder="v.description"
                    />
                    <el-input
                      v-else
                      v-model="form.variables[v.name]"
                      :placeholder="v.description"
                    />
                  </el-form-item>
                </div>
              </el-form-item>
            </template>

            <el-row :gutter="8">
              <el-col :span="12">
                <el-form-item label="Temperature">
                  <el-input-number
                    v-model="form.temperature"
                    :min="0"
                    :max="2"
                    :step="0.1"
                    style="width: 100%"
                    controls-position="right"
                  />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="Max Tokens">
                  <el-input-number
                    v-model="form.max_tokens"
                    :min="1"
                    :max="8192"
                    style="width: 100%"
                    controls-position="right"
                  />
                </el-form-item>
              </el-col>
            </el-row>

            <el-form-item>
              <el-button
                type="primary"
                :loading="isStreaming"
                :disabled="!canRun"
                @click="streamChat"
              >
                <el-icon><VideoPlay /></el-icon>
                运行
              </el-button>
              <el-button v-if="isStreaming" type="danger" @click="stopStream">
                <el-icon><VideoPause /></el-icon>
                停止
              </el-button>
              <el-button :disabled="!form.prompt_name || !form.prompt_version" @click="loadPreview">
                <el-icon><View /></el-icon>
                预览渲染
              </el-button>
            </el-form-item>
          </el-form>
        </el-card>
      </el-col>

      <!-- 右侧输出面板 -->
      <el-col :span="15">
        <el-card shadow="never">
          <template #header>
            <div class="header-flex">
              <span>输出</span>
              <div class="status-tags">
                <el-tag v-if="traceId" type="info" size="small">
                  trace: {{ traceId.slice(0, 8) }}
                </el-tag>
                <el-tag v-if="finishReason" size="small">
                  finish: {{ finishReason }}
                </el-tag>
                <el-tag v-if="usage" type="success" size="small">
                  tokens: {{ usage.prompt_tokens }} → {{ usage.completion_tokens }}
                </el-tag>
              </div>
            </div>
          </template>

          <div class="output-container" v-loading="isStreaming">
            <pre v-if="output" class="output-text">{{ output }}</pre>
            <el-empty v-else description="点击运行开始流式输出" />

            <div v-if="toolCalls.length > 0" class="tool-calls">
              <el-divider content-position="left">Tool Calls</el-divider>
              <el-card
                v-for="tc in toolCalls"
                :key="tc.index"
                shadow="hover"
                class="tool-call-card"
              >
                <div class="tc-header">
                  <el-tag type="warning" size="small">#{{ tc.index }}</el-tag>
                  <span class="tc-name">{{ tc.name }}</span>
                </div>
                <pre class="tc-arguments">{{ formatJSON(tc.arguments) }}</pre>
              </el-card>
            </div>
          </div>
        </el-card>

        <!-- trace 信息卡 -->
        <el-card v-if="traceId" shadow="never" class="trace-card">
          <template #header>
            <span>Trace</span>
          </template>
          <div class="trace-row">
            <span class="trace-label">Trace ID:</span>
            <el-link type="primary">{{ traceId }}</el-link>
            <el-button size="small" text @click="copyTraceId">复制</el-button>
          </div>
          <div v-if="finishReason" class="trace-row">
            <span class="trace-label">Finish Reason:</span>
            <span>{{ finishReason }}</span>
          </div>
          <div v-if="usage" class="trace-row">
            <span class="trace-label">Usage:</span>
            <span>
              tokens {{ usage.total_tokens }}
              (prompt {{ usage.prompt_tokens }} + completion {{ usage.completion_tokens }})
            </span>
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { ElMessage } from 'element-plus'
import { fetchEventSource } from '@microsoft/fetch-event-source'
import { promptAdminApi, playgroundApi } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const API_BASE = import.meta.env.VITE_API_BASE_URL || '/api/v1'
const auth = useAuthStore()

// ====== 配置面板状态 ======
const loading = ref(false)
const templates = ref([])
const versions = ref([])
const labels = ref([])
const versionLabelsMap = ref({}) // version -> [label...]
const variablesSchema = ref([]) // 从版本 config.variables_schema 解析
const availableModels = ref([
  { label: 'gpt-4o', value: 'gpt-4o' },
  { label: 'gpt-4o-mini', value: 'gpt-4o-mini' },
  { label: 'claude-3-5-sonnet', value: 'claude-3-5-sonnet' },
  { label: 'gemini-1.5-pro', value: 'gemini-1.5-pro' },
])

const form = reactive({
  prompt_name: '',
  prompt_version: null,
  model_name: '',
  variables: {},
  temperature: 0.3,
  max_tokens: 1024,
})

const canRun = computed(
  () => !!(form.prompt_name && form.prompt_version && form.model_name),
)

// ====== 输出面板状态 ======
const isStreaming = ref(false)
const output = ref('')
const toolCalls = ref([])
const traceId = ref('')
const finishReason = ref('')
const usage = ref(null)
const abortController = ref(null)

// ====== 数据加载 ======
async function loadTemplates() {
  loading.value = true
  try {
    const data = await promptAdminApi.listTemplates({ page: 1, page_size: 100 })
    templates.value = data.items || []
  } catch (err) {
    ElMessage.error('加载模板列表失败: ' + err.message)
  } finally {
    loading.value = false
  }
}

async function onTemplateChange(name) {
  // 切换模板:重置版本 + 加载 versions 与 labels
  form.prompt_version = null
  versions.value = []
  labels.value = []
  versionLabelsMap.value = {}
  variablesSchema.value = []
  form.variables = {}
  if (!name) return

  loading.value = true
  try {
    const [tpl, lb] = await Promise.all([
      promptAdminApi.getTemplate(name),
      promptAdminApi.listLabels(name),
    ])
    versions.value = tpl.versions || []
    labels.value = lb || []
    // 构建 version -> [label...] 映射
    const map = {}
    for (const item of labels.value) {
      const ver = item.version
      if (!map[ver]) map[ver] = []
      map[ver].push(item.label)
    }
    versionLabelsMap.value = map
    // 默认选最新版本(最大 version 号)
    if (versions.value.length > 0) {
      const latest = versions.value.reduce((a, b) =>
        a.version > b.version ? a : b,
      )
      form.prompt_version = latest.version
      await onVersionChange(form.prompt_version)
    }
  } catch (err) {
    ElMessage.error('加载模板详情失败: ' + err.message)
  } finally {
    loading.value = false
  }
}

async function onVersionChange(version) {
  if (!form.prompt_name || version == null) return
  try {
    const ver = await promptAdminApi.getVersion(form.prompt_name, version)
    // 解析 variables_schema:兼容 config.variables_schema 数组或对象两种格式
    const schema = ver?.config?.variables_schema
    const list = []
    if (Array.isArray(schema)) {
      for (const item of schema) {
        list.push({
          name: item.name,
          type: item.type || 'string',
          description: item.description || '',
          default: item.default,
        })
      }
    } else if (schema && typeof schema === 'object') {
      for (const [k, v] of Object.entries(schema)) {
        const meta = typeof v === 'object' && v !== null ? v : {}
        list.push({
          name: k,
          type: meta.type || 'string',
          description: meta.description || '',
          default: meta.default,
        })
      }
    }
    variablesSchema.value = list
    // 初始化 variables:保留已填值,缺失的补默认值/空串
    const nextVars = { ...form.variables }
    for (const v of list) {
      if (!(v.name in nextVars)) {
        nextVars[v.name] = v.default !== undefined ? v.default : ''
      }
    }
    // 清理已不存在的变量
    for (const k of Object.keys(nextVars)) {
      if (!list.some((v) => v.name === k)) delete nextVars[k]
    }
    form.variables = nextVars
  } catch (err) {
    ElMessage.error('加载版本详情失败: ' + err.message)
  }
}

// ====== 预览渲染(本地,不调 LLM) ======
async function loadPreview() {
  if (!form.prompt_name || !form.prompt_version) return
  try {
    const data = await promptAdminApi.previewRender(form.prompt_name, {
      version: form.prompt_version,
      variables: { ...form.variables },
    })
    // 把渲染结果填入输出区,便于直接对比
    resetOutput()
    output.value = data.rendered || ''
    ElMessage.success(
      `渲染成功,变量: ${data.variables_used?.join(', ') || '无'}`,
    )
  } catch (err) {
    ElMessage.error('预览渲染失败: ' + err.message)
  }
}

// ====== SSE 流式调用 ======
function resetOutput() {
  output.value = ''
  toolCalls.value = []
  traceId.value = ''
  finishReason.value = ''
  usage.value = null
}

async function streamChat() {
  if (!canRun.value) return
  resetOutput()
  isStreaming.value = true

  const ctrl = new AbortController()
  abortController.value = ctrl

  try {
    await fetchEventSource(`${API_BASE}${playgroundApi.runUrl}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${auth.token}`,
      },
      body: JSON.stringify({
        prompt_name: form.prompt_name,
        prompt_version: form.prompt_version,
        model_name: form.model_name,
        variables: { ...form.variables },
        temperature: form.temperature,
        max_tokens: form.max_tokens,
      }),
      signal: ctrl.signal,
      openWhenHidden: true,

      onopen: async (resp) => {
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      },
      onmessage: (ev) => {
        if (ev.event === 'token') {
          const data = JSON.parse(ev.data)
          output.value += data.content
        } else if (ev.event === 'tool_call_start') {
          const data = JSON.parse(ev.data)
          toolCalls.value.push({
            index: data.index,
            name: data.name,
            id: data.id,
            arguments: '',
          })
        } else if (ev.event === 'tool_call_delta') {
          const data = JSON.parse(ev.data)
          const tc = toolCalls.value.find((t) => t.index === data.index)
          if (tc) tc.arguments += data.arguments
        } else if (ev.event === 'tool_call_end') {
          const data = JSON.parse(ev.data)
          const tc = toolCalls.value.find((t) => t.index === data.index)
          if (tc) tc.arguments = data.arguments // 完整覆盖
        } else if (ev.event === 'trace') {
          const data = JSON.parse(ev.data)
          traceId.value = data.trace_id
        } else if (ev.event === 'done') {
          const data = JSON.parse(ev.data)
          finishReason.value = data.finish_reason
          usage.value = data.usage
        } else if (ev.event === 'error') {
          const data = JSON.parse(ev.data)
          ElMessage.error(data.message)
        }
        // ping 忽略
      },
      onerror: (err) => {
        // 抛出以终止重连,否则 fetch-event-source 默认重试
        throw err
      },
      onclose: () => {
        isStreaming.value = false
      },
    })
  } catch (e) {
    if (e?.name !== 'AbortError') {
      ElMessage.error(e?.message || 'Playground 运行失败')
    }
  } finally {
    isStreaming.value = false
  }
}

function stopStream() {
  if (abortController.value) {
    abortController.value.abort()
  }
  isStreaming.value = false
}

// ====== helpers ======
function formatJSON(str) {
  if (!str) return ''
  try {
    return JSON.stringify(JSON.parse(str), null, 2)
  } catch {
    return str
  }
}

async function copyTraceId() {
  try {
    await navigator.clipboard.writeText(traceId.value)
    ElMessage.success('Trace ID 已复制')
  } catch {
    ElMessage.error('复制失败,请手动选取')
  }
}

function labelTagType(label) {
  if (label === 'production') return 'danger'
  if (label === 'latest') return 'info'
  if (label?.startsWith('prod-')) return 'success'
  if (label?.startsWith('canary-')) return 'warning'
  if (label === 'staging') return ''
  return 'info'
}

onMounted(loadTemplates)

onUnmounted(() => {
  stopStream()
})
</script>

<style scoped>
.playground {
  padding: 20px;
}

.mb-16 {
  margin-bottom: 16px;
}

.header-flex {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.status-tags {
  display: flex;
  gap: 8px;
}

.variables-wrap {
  width: 100%;
}

.var-item {
  margin-bottom: 12px;
}

.output-container {
  min-height: 400px;
  max-height: 600px;
  overflow-y: auto;
  background: #f5f7fa;
  padding: 16px;
  border-radius: 4px;
}

.output-text {
  font-family: 'Menlo', 'Monaco', monospace;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
}

.tool-call-card {
  margin-top: 12px;
}

.tc-header {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}

.tc-name {
  font-family: monospace;
  font-weight: 600;
}

.tc-arguments {
  font-family: monospace;
  font-size: 12px;
  background: #fff;
  padding: 8px;
  border-radius: 3px;
  margin: 0;
  white-space: pre-wrap;
}

.trace-card {
  margin-top: 16px;
}

.trace-row {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
  font-size: 13px;
}

.trace-label {
  color: #606266;
  min-width: 100px;
}
</style>
