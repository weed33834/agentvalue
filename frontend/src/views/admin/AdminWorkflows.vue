<template>
  <div class="admin-workflows">
    <!-- 顶部说明 -->
    <el-alert type="info" :closable="false" show-icon class="mb-16">
      <template #title>
        工作流可视化编排 —— 对标 Dify Workflow / Coze Bot 编排。拖拽节点 + 连线构建 DAG,
        后端解释执行引擎按拓扑排序顺序执行,支持 LLM / HTTP / 条件分支 / 代码 / 知识库 / 输出 7
        种节点类型。
      </template>
    </el-alert>

    <!-- 顶部工具栏 -->
    <div class="toolbar mb-16">
      <el-select
        v-model="selectedWorkflowId"
        placeholder="选择工作流"
        clearable
        style="width: 280px"
        @change="handleSelectWorkflow"
      >
        <el-option
          v-for="wf in workflows"
          :key="wf.id"
          :label="`${wf.name}${wf.enabled ? '' : ' (已禁用)'}`"
          :value="wf.id"
        />
      </el-select>
      <el-button :loading="loading" @click="loadWorkflows">
        <el-icon><RefreshLeft /></el-icon>
        刷新
      </el-button>
      <el-button type="primary" @click="openCreateDialog">
        <el-icon><Plus /></el-icon>
        新建工作流
      </el-button>
      <div class="toolbar-right" v-if="selectedWorkflowId">
        <el-button :loading="validating" @click="handleValidate">
          <el-icon><CircleCheck /></el-icon>
          验证
        </el-button>
        <el-button :loading="saving" type="success" @click="handleSave">
          <el-icon><Check /></el-icon>
          保存
        </el-button>
        <el-button :loading="running" type="warning" @click="openRunDialog">
          <el-icon><VideoPlay /></el-icon>
          运行
        </el-button>
        <el-button @click="openRunHistoryDialog">
          <el-icon><Histogram /></el-icon>
          运行历史
        </el-button>
        <el-button
          :type="currentWorkflow?.enabled ? 'danger' : 'success'"
          link
          @click="handleToggle"
        >
          {{ currentWorkflow?.enabled ? '禁用' : '启用' }}
        </el-button>
        <el-button type="danger" link @click="handleDelete">删除</el-button>
      </div>
    </div>

    <!-- 主区域: 节点面板 + Vue Flow 画布 + 属性面板 -->
    <div class="main-area">
      <!-- 左侧: 节点面板 (拖拽源) -->
      <el-card class="node-palette">
        <template #header>
          <span class="panel-title">节点类型</span>
        </template>
        <div class="palette-list">
          <div
            v-for="(meta, type) in NODE_TYPES"
            :key="type"
            class="palette-item"
            :style="{ borderLeftColor: meta.color }"
            draggable="true"
            @dragstart="handleDragStart($event, type)"
          >
            <el-icon :style="{ color: meta.color }">
              <component :is="meta.icon" />
            </el-icon>
            <span>{{ meta.label }}</span>
            <span class="palette-meta"> in{{ meta.inputs }}/out{{ meta.outputs }} </span>
          </div>
        </div>
        <el-divider />
        <div class="palette-tip">
          <el-icon><InfoFilled /></el-icon>
          <span>拖拽节点到画布,选中节点后右侧编辑属性</span>
        </div>
      </el-card>

      <!-- 中间: Vue Flow 画布 -->
      <el-card class="canvas-card" body-class="canvas-body">
        <template #header>
          <div class="canvas-header">
            <span class="panel-title">
              {{ currentWorkflow?.name || '请选择工作流' }}
              <el-tag v-if="currentWorkflow" size="small" class="ml-8">
                v{{ currentWorkflow.version }}
              </el-tag>
            </span>
            <span class="canvas-tip" v-if="!selectedWorkflowId">
              ← 选择已有工作流或点击"新建工作流"
            </span>
          </div>
        </template>
        <div class="flow-container" @drop="handleDrop" @dragover.prevent>
          <VueFlow
            v-model:nodes="nodes"
            v-model:edges="edges"
            :node-types="customNodeTypes"
            :default-viewport="{ zoom: 1 }"
            fit-view-on-init
            @nodes-change="onNodesChange"
            @edges-change="onEdgesChange"
            @connect="onConnect"
            @node-click="onNodeClick"
            @pane-click="onPaneClick"
          >
            <Background :gap="20" :size="1" pattern-color="#aab" />
            <Controls />
            <MiniMap />
          </VueFlow>
        </div>
      </el-card>

      <!-- 右侧: 属性面板 -->
      <el-card class="property-panel">
        <template #header>
          <span class="panel-title">
            {{
              selectedNode ? `属性 - ${selectedNode.data?.label || selectedNode.id}` : '属性面板'
            }}
          </span>
        </template>
        <div v-if="!selectedNode" class="empty-property">
          <el-empty description="选中节点后编辑属性" :image-size="60" />
        </div>
        <el-form v-else label-position="top" size="small">
          <el-form-item label="节点 ID">
            <el-input v-model="selectedNode.id" disabled />
          </el-form-item>
          <el-form-item label="节点类型">
            <el-input :value="NODE_TYPES[selectedNode.type]?.label" disabled />
          </el-form-item>
          <el-form-item label="显示名称">
            <el-input v-model="selectedNode.data.label" placeholder="节点显示名称" />
          </el-form-item>

          <!-- 根据 type 显示不同 config 表单 -->
          <template v-if="selectedNode.type === 'llm'">
            <el-form-item label="模型">
              <el-input
                v-model="selectedNode.data.config.model"
                placeholder="如 gpt-4 / qwen-plus"
              />
            </el-form-item>
            <el-form-item label="Prompt 模板">
              <el-input
                v-model="selectedNode.data.config.prompt_template"
                type="textarea"
                :rows="4"
                placeholder="支持 {{var}} 替换, 如 {{inputs.user_input}}"
              />
            </el-form-item>
            <el-form-item label="Temperature">
              <el-slider
                v-model="selectedNode.data.config.temperature"
                :min="0"
                :max="2"
                :step="0.1"
                show-input
              />
            </el-form-item>
            <el-form-item label="Max Tokens">
              <el-input-number v-model="selectedNode.data.config.max_tokens" :min="1" :max="8192" />
            </el-form-item>
          </template>

          <template v-else-if="selectedNode.type === 'http'">
            <el-form-item label="Method">
              <el-select v-model="selectedNode.data.config.method">
                <el-option label="GET" value="GET" />
                <el-option label="POST" value="POST" />
                <el-option label="PUT" value="PUT" />
                <el-option label="DELETE" value="DELETE" />
              </el-select>
            </el-form-item>
            <el-form-item label="URL">
              <el-input
                v-model="selectedNode.data.config.url"
                placeholder="https://api.example.com/users"
              />
            </el-form-item>
            <el-form-item label="Headers (JSON)">
              <el-input
                v-model="headersInput"
                type="textarea"
                :rows="3"
                placeholder='{"X-User": "{{inputs.user}}"}'
              />
            </el-form-item>
            <el-form-item label="Body 模板">
              <el-input
                v-model="selectedNode.data.config.body_template"
                type="textarea"
                :rows="4"
                placeholder='{"name": "{{inputs.user}}"}'
              />
            </el-form-item>
            <el-form-item label="超时 (秒)">
              <el-input-number v-model="selectedNode.data.config.timeout" :min="1" :max="300" />
            </el-form-item>
          </template>

          <template v-else-if="selectedNode.type === 'condition'">
            <el-form-item label="条件表达式">
              <el-input
                v-model="selectedNode.data.config.expression"
                type="textarea"
                :rows="3"
                placeholder="如 score > 60 (支持 and / or / 比较)"
              />
            </el-form-item>
            <el-alert type="info" :closable="false" show-icon class="mt-8">
              <template #title> 条件节点有 true/false 两个输出端口, 从对应 handle 连线 </template>
            </el-alert>
          </template>

          <template v-else-if="selectedNode.type === 'code'">
            <el-form-item label="Python 代码">
              <el-input
                v-model="selectedNode.data.config.source"
                type="textarea"
                :rows="8"
                placeholder="result = inputs.get('x', 0) * 2"
              />
            </el-form-item>
            <el-alert type="warning" :closable="false" show-icon class="mt-8">
              <template #title>
                Sandbox 禁用 builtins, 仅可访问 inputs / context / result 与白名单函数
              </template>
            </el-alert>
          </template>

          <template v-else-if="selectedNode.type === 'knowledge'">
            <el-form-item label="查询模板">
              <el-input
                v-model="selectedNode.data.config.query_template"
                type="textarea"
                :rows="3"
                placeholder="search for {{inputs.topic}}"
              />
            </el-form-item>
            <el-form-item label="Top K">
              <el-input-number v-model="selectedNode.data.config.top_k" :min="1" :max="50" />
            </el-form-item>
          </template>

          <el-divider />
          <el-button type="danger" link @click="handleDeleteNode">
            <el-icon><Delete /></el-icon>
            删除节点
          </el-button>
        </el-form>
      </el-card>
    </div>

    <!-- 创建/编辑 Dialog -->
    <el-dialog
      v-model="createDialogVisible"
      title="新建工作流"
      width="540px"
      @closed="resetCreateForm"
    >
      <el-form
        ref="createFormRef"
        :model="createForm"
        :rules="createFormRules"
        label-position="top"
      >
        <el-form-item label="名称" prop="name">
          <el-input v-model="createForm.name" placeholder="如 daily_evaluation" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="createForm.description"
            type="textarea"
            :rows="2"
            placeholder="工作流用途描述"
          />
        </el-form-item>
        <el-form-item label="输入变量 Schema (JSON)">
          <el-input
            v-model="inputSchemaInput"
            type="textarea"
            :rows="5"
            placeholder='{"variables": [{"name": "x", "type": "int", "default": 0}]}'
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="handleCreate"> 创建 </el-button>
      </template>
    </el-dialog>

    <!-- 运行 Dialog -->
    <el-dialog v-model="runDialogVisible" title="运行工作流" width="640px" @closed="resetRunForm">
      <el-form label-position="top">
        <el-form-item label="输入变量 (JSON)">
          <el-input
            v-model="runInputsInput"
            type="textarea"
            :rows="6"
            placeholder='{"x": 21, "user": "alice"}'
          />
        </el-form-item>
      </el-form>
      <el-divider />
      <div v-if="runResult" class="run-result">
        <div class="run-status">
          <el-tag :type="runStatusTagType(runResult.status)">
            {{ runResult.status }}
          </el-tag>
          <span class="run-meta">
            run_id: {{ runResult.run_id }} | thread_id: {{ runResult.thread_id }}
          </span>
        </div>
        <el-tabs v-model="activeResultTab">
          <el-tab-pane label="节点状态" name="nodes">
            <el-table :data="nodeStatesList" size="small" max-height="280">
              <el-table-column prop="id" label="节点" min-width="100" />
              <el-table-column label="状态" width="100">
                <template #default="{ row }">
                  <el-tag size="small" :type="nodeStatusTagType(row.status)">
                    {{ row.status }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="started_at" label="开始" min-width="160" />
              <el-table-column prop="completed_at" label="完成" min-width="160" />
              <el-table-column label="输出" min-width="200">
                <template #default="{ row }">
                  <pre class="output-pre">{{ formatJson(row.output || row.error) }}</pre>
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>
          <el-tab-pane label="最终输出" name="outputs">
            <pre class="output-pre">{{ formatJson(runResult.outputs) }}</pre>
          </el-tab-pane>
        </el-tabs>
      </div>
      <template #footer>
        <el-button @click="runDialogVisible = false">关闭</el-button>
        <el-button type="primary" :loading="running" @click="handleRun"> 执行 </el-button>
      </template>
    </el-dialog>

    <!-- 运行历史 Dialog -->
    <el-dialog v-model="runHistoryDialogVisible" title="运行历史" width="800px">
      <el-table :data="runHistory" v-loading="historyLoading" max-height="500">
        <el-table-column prop="id" label="Run ID" min-width="180" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="runStatusTagType(row.status)">
              {{ row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="thread_id" label="Thread ID" min-width="180" />
        <el-table-column prop="created_at" label="创建时间" min-width="180" />
        <el-table-column prop="completed_at" label="完成时间" min-width="180" />
      </el-table>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, markRaw, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { VueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'
import {
  ChatDotRound,
  Check,
  CircleCheck,
  CircleClose,
  Collection,
  Delete,
  Document,
  Histogram,
  InfoFilled,
  Link,
  Plus,
  RefreshLeft,
  Share,
  Switch,
  VideoPlay,
} from '@element-plus/icons-vue'
import { h } from 'vue'
import { workflowAdminApi } from '@/api/client'

// ============================================================
// 节点类型定义 (与后端 WorkflowEngine.NODE_TYPES 对齐)
// ============================================================
const NODE_TYPES = {
  start: {
    label: '开始',
    icon: markRaw(Share),
    color: '#67c23a',
    inputs: 0,
    outputs: 1,
  },
  llm: {
    label: 'LLM',
    icon: markRaw(ChatDotRound),
    color: '#409eff',
    inputs: 1,
    outputs: 1,
  },
  http: {
    label: 'HTTP',
    icon: markRaw(Link),
    color: '#e6a23c',
    inputs: 1,
    outputs: 1,
  },
  condition: {
    label: '条件',
    icon: markRaw(Switch),
    color: '#f56c6c',
    inputs: 1,
    outputs: 2,
  },
  code: {
    label: '代码',
    icon: markRaw(Document),
    color: '#909399',
    inputs: 1,
    outputs: 1,
  },
  knowledge: {
    label: '知识库',
    icon: markRaw(Collection),
    color: '#9c27b0',
    inputs: 1,
    outputs: 1,
  },
  end: {
    label: '结束',
    icon: markRaw(CircleClose),
    color: '#67c23a',
    inputs: 1,
    outputs: 0,
  },
}

// ============================================================
// 状态
// ============================================================
const loading = ref(false)
const validating = ref(false)
const saving = ref(false)
const running = ref(false)
const creating = ref(false)
const historyLoading = ref(false)

const workflows = ref([])
const selectedWorkflowId = ref('')
const currentWorkflow = ref(null)

// Vue Flow 数据
const nodes = ref([])
const edges = ref([])
const selectedNode = ref(null)

// Dialog 状态
const createDialogVisible = ref(false)
const runDialogVisible = ref(false)
const runHistoryDialogVisible = ref(false)

const createFormRef = ref()
const createForm = reactive({
  name: '',
  description: '',
})
const createFormRules = {
  name: [{ required: true, message: '请输入工作流名', trigger: 'blur' }],
}
const inputSchemaInput = ref('{"variables": []}')

const runInputsInput = ref('{}')
const runResult = ref(null)
const activeResultTab = ref('nodes')

const runHistory = ref([])

const headersInput = ref('{}')

// ============================================================
// 自定义节点组件 (用 h 渲染, 避免 inline component 复杂性)
// ============================================================
function renderCustomNode(props) {
  // Vue Flow 传 { id, type, data, selected, ... }
  const meta = NODE_TYPES[props.type] || { label: props.type, color: '#999' }
  const label = props.data?.label || meta.label
  const isSelected = props.selected
  return h(
    'div',
    {
      class: ['wf-node', `wf-node-${props.type}`, { selected: isSelected }],
      style: {
        borderColor: meta.color,
        boxShadow: isSelected ? `0 0 0 2px ${meta.color}` : 'none',
      },
    },
    [
      h('div', { class: 'wf-node-header', style: { background: meta.color } }, [
        h('span', { class: 'wf-node-icon' }, label.charAt(0)),
        h('span', { class: 'wf-node-title' }, label),
      ]),
      h('div', { class: 'wf-node-body' }, props.type),
      // 输入/输出 Handle 由 Vue Flow 自动渲染 (无 type 时使用默认)
    ],
  )
}

const customNodeTypes = {
  start: markRaw(renderCustomNode),
  llm: markRaw(renderCustomNode),
  http: markRaw(renderCustomNode),
  condition: markRaw(renderCustomNode),
  code: markRaw(renderCustomNode),
  knowledge: markRaw(renderCustomNode),
  end: markRaw(renderCustomNode),
}

// ============================================================
// 生命周期
// ============================================================
onMounted(() => {
  loadWorkflows()
})

// ============================================================
// 工作流列表
// ============================================================
async function loadWorkflows() {
  loading.value = true
  try {
    const data = await workflowAdminApi.list()
    workflows.value = data.items || []
  } catch (e) {
    ElMessage.error(`加载工作流列表失败: ${e.message}`)
  } finally {
    loading.value = false
  }
}

async function handleSelectWorkflow(workflowId) {
  if (!workflowId) {
    currentWorkflow.value = null
    nodes.value = []
    edges.value = []
    return
  }
  try {
    const data = await workflowAdminApi.get(workflowId)
    currentWorkflow.value = data
    // 把 graph 还原到画布
    const graph = data.graph || { nodes: [], edges: [] }
    nodes.value = (graph.nodes || []).map((n) => ({
      id: n.id,
      type: n.type,
      position: n.position || { x: 0, y: 0 },
      data: {
        label: n.data?.label || NODE_TYPES[n.type]?.label || n.id,
        config: n.data?.config || defaultConfig(n.type),
      },
    }))
    edges.value = (graph.edges || []).map((e) => ({
      id: `e-${e.source}-${e.target}-${e.source_handle || ''}`,
      source: e.source,
      target: e.target,
      sourceHandle: e.source_handle || undefined,
      targetHandle: undefined,
      label: e.source_handle || undefined,
      animated: false,
    }))
    selectedNode.value = null
  } catch (e) {
    ElMessage.error(`加载工作流详情失败: ${e.message}`)
  }
}

function defaultConfig(type) {
  switch (type) {
    case 'llm':
      return {
        model: 'gpt-4',
        prompt_template: '',
        temperature: 0.1,
        max_tokens: 1024,
      }
    case 'http':
      return {
        method: 'GET',
        url: '',
        headers: {},
        body_template: '',
        timeout: 30,
      }
    case 'condition':
      return { expression: '' }
    case 'code':
      return { source: 'result = None' }
    case 'knowledge':
      return { query_template: '', top_k: 5 }
    default:
      return {}
  }
}

// ============================================================
// 拖拽创建节点
// ============================================================
function handleDragStart(event, type) {
  event.dataTransfer.setData('application/wf-node-type', type)
  event.dataTransfer.effectAllowed = 'move'
}

function handleDrop(event) {
  const type = event.dataTransfer.getData('application/wf-node-type')
  if (!type || !NODE_TYPES[type]) return
  // 计算落点位置 (画布相对坐标, 简化为 viewport 偏移)
  const rect = event.currentTarget.getBoundingClientRect()
  const position = {
    x: event.clientX - rect.left - 80,
    y: event.clientY - rect.top - 30,
  }
  const id = `n${Date.now().toString(36)}`
  const meta = NODE_TYPES[type]
  nodes.value.push({
    id,
    type,
    position,
    data: {
      label: meta.label,
      config: defaultConfig(type),
    },
  })
}

function onNodesChange() {
  // VueFlow 自动更新 nodes, 这里可补充业务逻辑
}

function onEdgesChange() {
  // VueFlow 自动更新 edges
}

function onConnect(connection) {
  // 新连线: 添加到 edges
  const newEdge = {
    id: `e-${connection.source}-${connection.target}-${connection.sourceHandle || ''}`,
    source: connection.source,
    target: connection.target,
    sourceHandle: connection.sourceHandle || undefined,
    targetHandle: connection.targetHandle || undefined,
    label: connection.sourceHandle || undefined,
    animated: false,
  }
  edges.value.push(newEdge)
}

function onNodeClick({ node }) {
  selectedNode.value = node
  // 同步 headersInput
  if (node.type === 'http') {
    headersInput.value = JSON.stringify(node.data.config.headers || {}, null, 2)
  }
}

function onPaneClick() {
  selectedNode.value = null
}

function handleDeleteNode() {
  if (!selectedNode.value) return
  const id = selectedNode.value.id
  nodes.value = nodes.value.filter((n) => n.id !== id)
  edges.value = edges.value.filter((e) => e.source !== id && e.target !== id)
  selectedNode.value = null
}

// 监听 headersInput 变化同步到 config
watch(headersInput, (val) => {
  if (!selectedNode.value || selectedNode.value.type !== 'http') return
  try {
    selectedNode.value.data.config.headers = JSON.parse(val || '{}')
  } catch {
    // 解析失败保留原值, 用户编辑中
  }
})

// ============================================================
// 创建工作流
// ============================================================
function openCreateDialog() {
  createForm.name = ''
  createForm.description = ''
  inputSchemaInput.value = '{"variables": []}'
  createDialogVisible.value = true
}

function resetCreateForm() {
  createForm.name = ''
  createForm.description = ''
  inputSchemaInput.value = '{"variables": []}'
}

async function handleCreate() {
  if (!createFormRef.value) return
  await createFormRef.value.validate(async (valid) => {
    if (!valid) return
    let inputSchema
    try {
      inputSchema = JSON.parse(inputSchemaInput.value || '{"variables": []}')
    } catch {
      ElMessage.error('输入变量 Schema 不是合法 JSON')
      return
    }
    // 默认图: start → end
    const defaultGraph = {
      nodes: [
        {
          id: 'start',
          type: 'start',
          position: { x: 100, y: 200 },
          data: { label: '开始', config: {} },
        },
        {
          id: 'end',
          type: 'end',
          position: { x: 600, y: 200 },
          data: { label: '结束', config: {} },
        },
      ],
      edges: [{ source: 'start', target: 'end' }],
    }
    creating.value = true
    try {
      const data = await workflowAdminApi.create({
        name: createForm.name,
        description: createForm.description,
        graph: defaultGraph,
        input_schema: inputSchema,
        enabled: true,
      })
      ElMessage.success(`工作流 ${data.name} 创建成功`)
      createDialogVisible.value = false
      await loadWorkflows()
      // 自动选中新创建的
      selectedWorkflowId.value = data.id
      await handleSelectWorkflow(data.id)
    } catch (e) {
      ElMessage.error(`创建失败: ${e.message}`)
    } finally {
      creating.value = false
    }
  })
}

// ============================================================
// 保存 / 验证 / 删除 / 启用
// ============================================================
function buildGraphFromCanvas() {
  return {
    nodes: nodes.value.map((n) => ({
      id: n.id,
      type: n.type,
      position: n.position,
      data: {
        label: n.data?.label,
        config: n.data?.config || {},
      },
    })),
    edges: edges.value.map((e) => {
      const edge = { source: e.source, target: e.target }
      if (e.sourceHandle) edge.source_handle = e.sourceHandle
      return edge
    }),
  }
}

async function handleSave() {
  if (!currentWorkflow.value) return
  const graph = buildGraphFromCanvas()
  saving.value = true
  try {
    const data = await workflowAdminApi.update(currentWorkflow.value.id, {
      graph,
      input_schema: currentWorkflow.value.input_schema,
    })
    currentWorkflow.value = data
    ElMessage.success(`已保存, 版本 v${data.version}`)
  } catch (e) {
    ElMessage.error(`保存失败: ${e.message}`)
  } finally {
    saving.value = false
  }
}

async function handleValidate() {
  if (!currentWorkflow.value) return
  const graph = buildGraphFromCanvas()
  validating.value = true
  try {
    const data = await workflowAdminApi.validate(currentWorkflow.value.id, {
      graph,
    })
    if (data.valid) {
      ElMessage.success('工作流图校验通过')
    } else {
      ElMessage.warning(`校验失败:\n${data.errors.join('\n')}`)
    }
  } catch (e) {
    ElMessage.error(`验证请求失败: ${e.message}`)
  } finally {
    validating.value = false
  }
}

async function handleDelete() {
  if (!currentWorkflow.value) return
  try {
    await ElMessageBox.confirm(`确认删除工作流 "${currentWorkflow.value.name}"?`, '删除确认', {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await workflowAdminApi.delete(currentWorkflow.value.id)
    ElMessage.success('已删除')
    selectedWorkflowId.value = ''
    currentWorkflow.value = null
    nodes.value = []
    edges.value = []
    await loadWorkflows()
  } catch (e) {
    ElMessage.error(`删除失败: ${e.message}`)
  }
}

async function handleToggle() {
  if (!currentWorkflow.value) return
  const newEnabled = !currentWorkflow.value.enabled
  try {
    const data = await workflowAdminApi.toggle(currentWorkflow.value.id, newEnabled)
    currentWorkflow.value.enabled = data.enabled
    ElMessage.success(`已${newEnabled ? '启用' : '禁用'}`)
  } catch (e) {
    ElMessage.error(`切换失败: ${e.message}`)
  }
}

// ============================================================
// 运行
// ============================================================
function openRunDialog() {
  if (!currentWorkflow.value) {
    ElMessage.warning('请先选择工作流')
    return
  }
  runInputsInput.value = JSON.stringify(
    seedInputsFromSchema(currentWorkflow.value.input_schema),
    null,
    2,
  )
  runResult.value = null
  activeResultTab.value = 'nodes'
  runDialogVisible.value = true
}

function seedInputsFromSchema(schema) {
  const inputs = {}
  const variables = (schema || {}).variables || []
  for (const v of variables) {
    if (v.name !== undefined) inputs[v.name] = v.default ?? null
  }
  return inputs
}

function resetRunForm() {
  runInputsInput.value = '{}'
  runResult.value = null
}

async function handleRun() {
  if (!currentWorkflow.value) return
  let inputs
  try {
    inputs = JSON.parse(runInputsInput.value || '{}')
  } catch {
    ElMessage.error('输入变量不是合法 JSON')
    return
  }
  running.value = true
  try {
    const data = await workflowAdminApi.run(currentWorkflow.value.id, { inputs })
    runResult.value = data
    if (data.status === 'completed') {
      ElMessage.success('工作流执行完成')
    } else {
      ElMessage.warning(`工作流状态: ${data.status}`)
    }
  } catch (e) {
    ElMessage.error(`运行失败: ${e.message}`)
  } finally {
    running.value = false
  }
}

// ============================================================
// 运行历史
// ============================================================
async function openRunHistoryDialog() {
  if (!currentWorkflow.value) {
    ElMessage.warning('请先选择工作流')
    return
  }
  runHistoryDialogVisible.value = true
  historyLoading.value = true
  try {
    const data = await workflowAdminApi.listRuns(currentWorkflow.value.id, {
      limit: 50,
    })
    runHistory.value = data.items || []
  } catch (e) {
    ElMessage.error(`加载运行历史失败: ${e.message}`)
  } finally {
    historyLoading.value = false
  }
}

// ============================================================
// 工具方法
// ============================================================
function formatJson(obj) {
  try {
    return JSON.stringify(obj, null, 2)
  } catch {
    return String(obj)
  }
}

function runStatusTagType(status) {
  const map = {
    completed: 'success',
    failed: 'danger',
    running: 'warning',
    pending: 'info',
    interrupted: 'warning',
  }
  return map[status] || 'info'
}

function nodeStatusTagType(status) {
  const map = {
    completed: 'success',
    failed: 'danger',
    running: 'warning',
    skipped: 'info',
    pending: 'info',
  }
  return map[status] || 'info'
}

const nodeStatesList = computed(() => {
  if (!runResult.value?.node_states) return []
  return Object.entries(runResult.value.node_states).map(([id, state]) => ({
    id,
    ...state,
  }))
})
</script>

<style scoped>
.admin-workflows {
  padding: 16px;
}
.mb-16 {
  margin-bottom: 16px;
}
.mt-8 {
  margin-top: 8px;
}
.ml-8 {
  margin-left: 8px;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.toolbar-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
}
.main-area {
  display: grid;
  grid-template-columns: 220px 1fr 320px;
  gap: 12px;
  height: calc(100vh - 240px);
  min-height: 500px;
}
.node-palette,
.canvas-card,
.property-panel {
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.canvas-card :deep(.el-card__body),
.property-panel :deep(.el-card__body) {
  flex: 1;
  overflow: auto;
  padding: 0;
}
.canvas-body {
  height: 100%;
}
.panel-title {
  font-weight: 600;
  font-size: 14px;
}
.palette-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.palette-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 10px;
  border-left: 3px solid #ddd;
  background: #f7f7f9;
  border-radius: 4px;
  cursor: grab;
  font-size: 13px;
  user-select: none;
}
.palette-item:hover {
  background: #eef2ff;
}
.palette-meta {
  margin-left: auto;
  font-size: 11px;
  color: #888;
}
.palette-tip {
  display: flex;
  gap: 6px;
  font-size: 12px;
  color: #888;
  align-items: flex-start;
}
.canvas-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.canvas-tip {
  font-size: 12px;
  color: #999;
}
.flow-container {
  width: 100%;
  height: 100%;
  min-height: 400px;
  background: #fafafa;
}
.empty-property {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
}
.run-result {
  margin-top: 12px;
}
.run-status {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.run-meta {
  font-size: 12px;
  color: #888;
}
.output-pre {
  background: #f5f5f5;
  padding: 6px;
  border-radius: 4px;
  font-size: 12px;
  max-height: 200px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
/* 自定义节点样式 */
:deep(.wf-node) {
  background: #fff;
  border: 2px solid #ddd;
  border-radius: 6px;
  min-width: 140px;
  font-size: 12px;
  box-shadow: 0 2px 4px rgba(0, 0, 0, 0.05);
}
:deep(.wf-node.selected) {
  box-shadow: 0 0 0 2px var(--wf-color, #409eff);
}
:deep(.wf-node-header) {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  color: #fff;
  border-radius: 4px 4px 0 0;
  font-weight: 600;
}
:deep(.wf-node-icon) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  background: rgba(255, 255, 255, 0.3);
  border-radius: 50%;
  font-size: 10px;
}
:deep(.wf-node-body) {
  padding: 6px 8px;
  color: #555;
  text-align: center;
}
:deep(.vue-flow__handle) {
  width: 8px;
  height: 8px;
  background: #555;
}
</style>
