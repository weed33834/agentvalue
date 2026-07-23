<template>
  <div class="admin-presets">
    <!-- 顶部说明 -->
    <el-alert type="info" :closable="false" show-icon class="mb-16">
      <template #title>
        Agent 预设市场 —— 对标 ChatGPT GPTs / LobeChat 助手市场 / Coze Bot 商店。
        选择预设快速开始对话，或创建自定义 Agent（系统提示词 + 模型层级 + 工具组合）。 第二个 Tab
        提供可复用提示词模板，支持变量插值。
      </template>
    </el-alert>

    <el-tabs v-model="activeMainTab" class="preset-tabs">
      <!-- ============ Tab 1: Agent 预设市场 ============ -->
      <el-tab-pane name="presets">
        <template #label>
          <span class="tab-label">
            <el-icon><Collection /></el-icon>
            Agent 预设市场
          </span>
        </template>

        <!-- 工具栏: 分类筛选 + 搜索 + 创建按钮 -->
        <div class="toolbar mb-16">
          <el-radio-group v-model="filterCategory" @change="loadPresets">
            <el-radio-button label="all">全部</el-radio-button>
            <el-radio-button v-for="cat in categoryOptions" :key="cat.value" :label="cat.value">
              {{ cat.label }}
            </el-radio-button>
          </el-radio-group>
          <el-input
            v-model="searchQuery"
            placeholder="搜索预设名称 / 描述 / 标签"
            clearable
            class="search-input"
            @input="handleSearch"
            @clear="loadPresets"
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>
          <el-button :loading="loading" @click="loadPresets">
            <el-icon><RefreshLeft /></el-icon>
            刷新
          </el-button>
          <el-button type="primary" @click="openCreateDialog">
            <el-icon><Plus /></el-icon>
            创建预设
          </el-button>
        </div>

        <!-- 预设网格卡片 -->
        <div v-loading="loading" :aria-busy="loading">
          <el-row v-if="filteredPresets.length" :gutter="16">
            <el-col
              v-for="preset in filteredPresets"
              :key="preset.id"
              :xs="24"
              :sm="12"
              :md="8"
              :lg="6"
              class="mb-16"
            >
              <el-card shadow="hover" class="preset-card">
                <!-- 卡片头部: 头像 + 名称 + 模型层级 -->
                <div class="preset-header">
                  <div class="preset-avatar">
                    <img
                      v-if="isImageUrl(preset.avatar)"
                      :src="preset.avatar"
                      :alt="preset.name"
                      class="avatar-img"
                    />
                    <span v-else class="avatar-emoji">{{ preset.avatar || defaultAvatar }}</span>
                  </div>
                  <div class="preset-title">
                    <div class="preset-name" :title="preset.name">{{ preset.name }}</div>
                    <el-tag size="small" :type="tierTagType(preset.model_tier)">
                      {{ tierLabel(preset.model_tier) }}
                    </el-tag>
                  </div>
                </div>

                <!-- 描述 -->
                <div class="preset-desc">{{ preset.description || '暂无描述' }}</div>

                <!-- 标签 -->
                <div class="preset-tags">
                  <el-tag
                    v-for="tag in (preset.tags || []).slice(0, 4)"
                    :key="tag"
                    size="small"
                    type="info"
                    class="preset-tag"
                  >
                    {{ tag }}
                  </el-tag>
                  <el-tag
                    v-if="preset.category"
                    size="small"
                    :type="categoryTagType(preset.category)"
                  >
                    {{ categoryLabel(preset.category) }}
                  </el-tag>
                </div>

                <!-- 统计信息 -->
                <div class="preset-stats">
                  <span class="stat-item">
                    <el-icon><View /></el-icon>
                    使用 {{ preset.use_count || 0 }} 次
                  </span>
                  <span
                    v-if="preset.enabled_tools && preset.enabled_tools.length"
                    class="stat-item"
                  >
                    <el-icon><Tools /></el-icon>
                    {{ preset.enabled_tools.length }} 个工具
                  </span>
                </div>

                <!-- 操作按钮 -->
                <div class="preset-actions">
                  <el-button
                    type="primary"
                    size="small"
                    :loading="usingId === preset.id"
                    @click="usePreset(preset)"
                  >
                    <el-icon><ChatRound /></el-icon>
                    使用
                  </el-button>
                  <el-button size="small" @click="openEditDialog(preset)">
                    <el-icon><Edit /></el-icon>
                    编辑
                  </el-button>
                  <el-button size="small" type="danger" plain @click="handleDelete(preset)">
                    <el-icon><Delete /></el-icon>
                    删除
                  </el-button>
                </div>
              </el-card>
            </el-col>
          </el-row>
          <el-empty v-else description="暂无预设，点击「创建预设」开始" />
        </div>
      </el-tab-pane>

      <!-- ============ Tab 2: 提示词模板 ============ -->
      <el-tab-pane name="templates">
        <template #label>
          <span class="tab-label">
            <el-icon><Document /></el-icon>
            提示词模板
          </span>
        </template>

        <!-- 模板工具栏 -->
        <div class="toolbar mb-16">
          <el-select
            v-model="templateFilterCategory"
            placeholder="按分类过滤"
            clearable
            style="width: 200px"
            @change="loadTemplates"
          >
            <el-option label="全部分类" :value="''" />
            <el-option
              v-for="cat in categoryOptions"
              :key="cat.value"
              :label="cat.label"
              :value="cat.value"
            />
          </el-select>
          <el-button :loading="templateLoading" @click="loadTemplates">
            <el-icon><RefreshLeft /></el-icon>
            刷新
          </el-button>
          <el-button type="primary" @click="openTemplateCreateDialog">
            <el-icon><Plus /></el-icon>
            创建模板
          </el-button>
        </div>

        <!-- 模板列表表格 -->
        <el-card v-loading="templateLoading">
          <el-table :data="templates" stripe empty-text="暂无模板">
            <el-table-column prop="name" label="模板名" min-width="160">
              <template #default="{ row }">
                <el-link type="primary" @click="openTemplateEditDialog(row)">{{
                  row.name
                }}</el-link>
              </template>
            </el-table-column>
            <el-table-column label="分类" width="110">
              <template #default="{ row }">
                <el-tag size="small" :type="categoryTagType(row.category)">
                  {{ categoryLabel(row.category) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="content" label="内容预览" min-width="280" show-overflow-tooltip>
              <template #default="{ row }">
                {{ (row.content || '').slice(0, 80)
                }}{{ (row.content || '').length > 80 ? '...' : '' }}
              </template>
            </el-table-column>
            <el-table-column label="变量" width="160">
              <template #default="{ row }">
                <el-tag
                  v-for="v in row.variables || []"
                  :key="v.name || v"
                  size="small"
                  type="warning"
                  class="var-tag"
                >
                  {{ typeof v === 'string' ? v : v.name }}
                </el-tag>
                <span v-if="!(row.variables && row.variables.length)" class="muted">无</span>
              </template>
            </el-table-column>
            <el-table-column prop="use_count" label="使用次数" width="100" align="center" />
            <el-table-column label="操作" width="160">
              <template #default="{ row }">
                <el-button size="small" link @click="openTemplateEditDialog(row)">编辑</el-button>
                <el-button size="small" link type="danger" @click="handleDeleteTemplate(row)">
                  删除
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <!-- ============ 预设 创建/编辑 对话框 ============ -->
    <el-dialog
      v-model="presetDialogVisible"
      :title="editingPreset ? '编辑预设' : '创建预设'"
      width="720px"
      :close-on-click-modal="false"
    >
      <el-form ref="presetFormRef" :model="presetForm" :rules="presetRules" label-position="top">
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="名称" prop="name">
              <el-input
                v-model="presetForm.name"
                placeholder="如：代码审查助手"
                maxlength="60"
                show-word-limit
              />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="分类" prop="category">
              <el-select v-model="presetForm.category" placeholder="选择分类" style="width: 100%">
                <el-option
                  v-for="cat in categoryOptions"
                  :key="cat.value"
                  :label="cat.label"
                  :value="cat.value"
                />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>

        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="头像 (emoji 或图片 URL)">
              <div class="avatar-editor">
                <div class="avatar-preview">
                  <img
                    v-if="isImageUrl(presetForm.avatar)"
                    :src="presetForm.avatar"
                    class="avatar-img"
                    alt="avatar"
                  />
                  <span v-else class="avatar-emoji">{{ presetForm.avatar || defaultAvatar }}</span>
                </div>
                <el-input
                  v-model="presetForm.avatar"
                  placeholder="🤖 或 https://..."
                  maxlength="200"
                />
              </div>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="模型层级" prop="model_tier">
              <el-select
                v-model="presetForm.model_tier"
                placeholder="选择模型层级"
                style="width: 100%"
              >
                <el-option
                  v-for="tier in tierOptions"
                  :key="tier.value"
                  :label="tier.label"
                  :value="tier.value"
                />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>

        <el-form-item label="描述" prop="description">
          <el-input
            v-model="presetForm.description"
            type="textarea"
            :rows="2"
            placeholder="简短描述该预设的用途与适用场景"
            maxlength="200"
            show-word-limit
          />
        </el-form-item>

        <el-form-item label="系统提示词 (System Prompt)" prop="system_prompt">
          <el-input
            v-model="presetForm.system_prompt"
            type="textarea"
            :rows="6"
            placeholder="你是一个经验丰富的代码审查工程师，擅长发现潜在 bug、安全漏洞与性能问题..."
          />
          <span class="field-hint">
            定义 Agent 的角色、行为与约束。支持 {{ varSyntax }} 变量插值。
          </span>
        </el-form-item>

        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="标签">
              <el-select
                v-model="presetForm.tags"
                multiple
                filterable
                allow-create
                default-first-option
                placeholder="输入后回车添加标签"
                style="width: 100%"
              >
                <el-option v-for="tag in tagSuggestions" :key="tag" :label="tag" :value="tag" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="启用工具">
              <el-select
                v-model="presetForm.enabled_tools"
                multiple
                filterable
                allow-create
                default-first-option
                placeholder="选择或输入工具名"
                style="width: 100%"
              >
                <el-option v-for="tool in toolOptions" :key="tool" :label="tool" :value="tool" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>

        <el-form-item label="温度 (Temperature): {{ presetForm.temperature / 100 }}">
          <el-slider v-model="presetForm.temperature" :min="0" :max="100" :step="5" show-stops />
          <span class="field-hint">
            0 = 严谨确定，100 = 高度创造性。对话类建议 70，分析类建议 30。
          </span>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="presetDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingPreset" @click="savePreset">
          {{ editingPreset ? '保存' : '创建' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- ============ 模板 创建/编辑 对话框 ============ -->
    <el-dialog
      v-model="templateDialogVisible"
      :title="editingTemplate ? '编辑模板' : '创建模板'"
      width="720px"
      :close-on-click-modal="false"
    >
      <el-form
        ref="templateFormRef"
        :model="templateForm"
        :rules="templateRules"
        label-position="top"
      >
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="模板名" prop="name">
              <el-input
                v-model="templateForm.name"
                placeholder="如：周报生成模板"
                maxlength="60"
                show-word-limit
              />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="分类" prop="category">
              <el-select v-model="templateForm.category" placeholder="选择分类" style="width: 100%">
                <el-option
                  v-for="cat in categoryOptions"
                  :key="cat.value"
                  :label="cat.label"
                  :value="cat.value"
                />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>

        <el-form-item label="模板内容" prop="content">
          <el-input
            v-model="templateForm.content"
            type="textarea"
            :rows="8"
            placeholder="请根据以下信息生成本周工作周报：&#10;员工：{{employee_name}}&#10;周期：{{period}}&#10;工作内容：{{work_content}}"
          />
          <span class="field-hint">
            使用 {{ varSyntax }} 语法定义变量，下方可配置每个变量的描述与默认值。
          </span>
        </el-form-item>

        <el-form-item label="变量配置">
          <div class="var-editor">
            <div v-for="(v, idx) in templateForm.variables" :key="idx" class="var-row">
              <el-input
                v-model="v.name"
                placeholder="变量名 (如 employee_name)"
                style="width: 200px"
              />
              <el-input v-model="v.description" placeholder="变量描述" style="flex: 1" />
              <el-input
                v-model="v.default_value"
                placeholder="默认值 (可选)"
                style="width: 180px"
              />
              <el-button type="danger" link @click="removeVariable(idx)">
                <el-icon><Delete /></el-icon>
              </el-button>
            </div>
            <el-button size="small" @click="addVariable">
              <el-icon><Plus /></el-icon>
              添加变量
            </el-button>
          </div>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="templateDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingTemplate" @click="saveTemplate">
          {{ editingTemplate ? '保存' : '创建' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/api/client'

const router = useRouter()

// ====== 常量配置 ======
const defaultAvatar = '🤖'

// 变量插值语法提示文本 (在模板中直接写字面量 {{variable}} 会触发 Vue 编译器误判,
// 故提取为常量引用)
const varSyntax = '{{variable}}'

const categoryOptions = [
  { value: 'general', label: '通用' },
  { value: 'programming', label: '编程' },
  { value: 'writing', label: '写作' },
  { value: 'analysis', label: '分析' },
  { value: 'hr', label: 'HR' },
]

const tierOptions = [
  { value: 'fast', label: '快速 (经济)' },
  { value: 'balanced', label: '均衡' },
  { value: 'powerful', label: '强力 (高质)' },
]

const tagSuggestions = ['高效', '创意', '严谨', '中文', '英文', '代码', '文档', '分析', '报告']

// 启用工具可选项 (与 AdminTools 中的内置工具对齐,支持自定义输入)
const toolOptions = ['calculator', 'datetime', 'employee_history', 'company_kb']

function categoryLabel(value) {
  const found = categoryOptions.find((c) => c.value === value)
  return found ? found.label : value || '通用'
}

function categoryTagType(value) {
  const map = {
    general: 'info',
    programming: 'success',
    writing: 'warning',
    analysis: 'primary',
    hr: 'danger',
  }
  return map[value] || 'info'
}

function tierLabel(value) {
  const found = tierOptions.find((t) => t.value === value)
  return found ? found.label : value || '均衡'
}

function tierTagType(value) {
  const map = { fast: 'success', balanced: 'primary', powerful: 'danger' }
  return map[value] || 'info'
}

function isImageUrl(str) {
  if (!str) return false
  return /^https?:\/\//i.test(str)
}

// ====== 主 Tab 切换 ======
const activeMainTab = ref('presets')

// ====== 预设市场状态 ======
const loading = ref(false)
const presets = ref([])
const filterCategory = ref('all')
const searchQuery = ref('')
const usingId = ref('')

// 前端二次过滤 (搜索词匹配名称/描述/标签)
const filteredPresets = computed(() => {
  if (!searchQuery.value.trim()) return presets.value
  const q = searchQuery.value.trim().toLowerCase()
  return presets.value.filter((p) => {
    const name = (p.name || '').toLowerCase()
    const desc = (p.description || '').toLowerCase()
    const tags = (p.tags || []).join(' ').toLowerCase()
    return name.includes(q) || desc.includes(q) || tags.includes(q)
  })
})

async function loadPresets() {
  loading.value = true
  try {
    const category = filterCategory.value === 'all' ? undefined : filterCategory.value
    const data = await api.presetApi.list(category)
    // 兼容 {items: [...]} 或 [...] 两种返回结构
    presets.value = Array.isArray(data) ? data : data?.items || data?.presets || []
  } catch (err) {
    ElMessage.error('加载预设列表失败: ' + err.message)
    presets.value = []
  } finally {
    loading.value = false
  }
}

let searchTimer = null
function handleSearch() {
  // 搜索词仅做前端过滤,无需重新请求;清除时重新加载
  if (searchTimer) clearTimeout(searchTimer)
}

// ====== 预设 创建/编辑 ======
const presetDialogVisible = ref(false)
const editingPreset = ref(null)
const savingPreset = ref(false)
const presetFormRef = ref(null)

const presetForm = reactive({
  name: '',
  description: '',
  avatar: '',
  category: 'general',
  system_prompt: '',
  tags: [],
  model_tier: 'balanced',
  temperature: 70,
  enabled_tools: [],
})

const presetRules = {
  name: [{ required: true, message: '请输入预设名称', trigger: 'blur' }],
  category: [{ required: true, message: '请选择分类', trigger: 'change' }],
  model_tier: [{ required: true, message: '请选择模型层级', trigger: 'change' }],
  system_prompt: [{ required: true, message: '请输入系统提示词', trigger: 'blur' }],
}

function resetPresetForm() {
  presetForm.name = ''
  presetForm.description = ''
  presetForm.avatar = ''
  presetForm.category = 'general'
  presetForm.system_prompt = ''
  presetForm.tags = []
  presetForm.model_tier = 'balanced'
  presetForm.temperature = 70
  presetForm.enabled_tools = []
}

function openCreateDialog() {
  editingPreset.value = null
  resetPresetForm()
  presetDialogVisible.value = true
}

function openEditDialog(preset) {
  editingPreset.value = preset
  presetForm.name = preset.name || ''
  presetForm.description = preset.description || ''
  presetForm.avatar = preset.avatar || ''
  presetForm.category = preset.category || 'general'
  presetForm.system_prompt = preset.system_prompt || ''
  presetForm.tags = [...(preset.tags || [])]
  presetForm.model_tier = preset.model_tier || 'balanced'
  presetForm.temperature = preset.temperature ?? 70
  presetForm.enabled_tools = [...(preset.enabled_tools || [])]
  presetDialogVisible.value = true
}

async function savePreset() {
  if (!presetFormRef.value) return
  try {
    await presetFormRef.value.validate()
  } catch {
    return
  }
  savingPreset.value = true
  try {
    const payload = {
      name: presetForm.name,
      description: presetForm.description,
      avatar: presetForm.avatar || null,
      category: presetForm.category,
      system_prompt: presetForm.system_prompt,
      tags: presetForm.tags,
      model_tier: presetForm.model_tier,
      temperature: presetForm.temperature,
      enabled_tools: presetForm.enabled_tools,
    }
    if (editingPreset.value) {
      await api.presetApi.update(editingPreset.value.id, payload)
      ElMessage.success('预设已更新')
    } else {
      await api.presetApi.create(payload)
      ElMessage.success('预设已创建')
    }
    presetDialogVisible.value = false
    await loadPresets()
  } catch (err) {
    ElMessage.error('保存失败: ' + err.message)
  } finally {
    savingPreset.value = false
  }
}

async function handleDelete(preset) {
  try {
    await ElMessageBox.confirm(`确认删除预设「${preset.name}」?此操作不可恢复`, '删除确认', {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await api.presetApi.delete(preset.id)
    ElMessage.success(`已删除 ${preset.name}`)
    await loadPresets()
  } catch (err) {
    ElMessage.error('删除失败: ' + err.message)
  }
}

async function usePreset(preset) {
  usingId.value = preset.id
  try {
    // 调用后端记录使用并返回完整配置
    await api.presetApi.use(preset.id)
    ElMessage.success(`已启用预设「${preset.name}」, 正在跳转聊天...`)
    // 跳转聊天页并通过 query 传递 preset_id,聊天页据此加载预设配置
    router.push({ path: '/admin/chat', query: { preset_id: preset.id } })
  } catch (err) {
    ElMessage.error('使用预设失败: ' + err.message)
  } finally {
    usingId.value = ''
  }
}

// ====== 提示词模板状态 ======
const templateLoading = ref(false)
const templates = ref([])
const templateFilterCategory = ref('')

async function loadTemplates() {
  templateLoading.value = true
  try {
    const params = templateFilterCategory.value ? { category: templateFilterCategory.value } : {}
    const data = await api.templateApi.list(params)
    templates.value = Array.isArray(data) ? data : data?.items || data?.templates || []
  } catch (err) {
    ElMessage.error('加载模板列表失败: ' + err.message)
    templates.value = []
  } finally {
    templateLoading.value = false
  }
}

// ====== 模板 创建/编辑 ======
const templateDialogVisible = ref(false)
const editingTemplate = ref(null)
const savingTemplate = ref(false)
const templateFormRef = ref(null)

const templateForm = reactive({
  name: '',
  category: 'general',
  content: '',
  variables: [],
})

const templateRules = {
  name: [{ required: true, message: '请输入模板名', trigger: 'blur' }],
  category: [{ required: true, message: '请选择分类', trigger: 'change' }],
  content: [{ required: true, message: '请输入模板内容', trigger: 'blur' }],
}

function resetTemplateForm() {
  templateForm.name = ''
  templateForm.category = 'general'
  templateForm.content = ''
  templateForm.variables = []
}

function openTemplateCreateDialog() {
  editingTemplate.value = null
  resetTemplateForm()
  templateDialogVisible.value = true
}

function openTemplateEditDialog(row) {
  editingTemplate.value = row
  templateForm.name = row.name || ''
  templateForm.category = row.category || 'general'
  templateForm.content = row.content || ''
  // 后端变量可能是 [{name, description, default_value}] 或 ['var1', 'var2']
  templateForm.variables = (row.variables || []).map((v) => {
    if (typeof v === 'string') return { name: v, description: '', default_value: '' }
    return {
      name: v.name || '',
      description: v.description || '',
      default_value: v.default_value || '',
    }
  })
  templateDialogVisible.value = true
}

function addVariable() {
  templateForm.variables.push({ name: '', description: '', default_value: '' })
}

function removeVariable(idx) {
  templateForm.variables.splice(idx, 1)
}

async function saveTemplate() {
  if (!templateFormRef.value) return
  try {
    await templateFormRef.value.validate()
  } catch {
    return
  }
  // 过滤掉变量名为空的行
  const cleanVars = templateForm.variables
    .filter((v) => v.name && v.name.trim())
    .map((v) => ({
      name: v.name.trim(),
      description: v.description || '',
      default_value: v.default_value || '',
    }))
  savingTemplate.value = true
  try {
    const payload = {
      name: templateForm.name,
      category: templateForm.category,
      content: templateForm.content,
      variables: cleanVars,
    }
    if (editingTemplate.value) {
      await api.templateApi.update(editingTemplate.value.id, payload)
      ElMessage.success('模板已更新')
    } else {
      await api.templateApi.create(payload)
      ElMessage.success('模板已创建')
    }
    templateDialogVisible.value = false
    await loadTemplates()
  } catch (err) {
    ElMessage.error('保存失败: ' + err.message)
  } finally {
    savingTemplate.value = false
  }
}

async function handleDeleteTemplate(row) {
  try {
    await ElMessageBox.confirm(`确认删除模板「${row.name}」?此操作不可恢复`, '删除确认', {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await api.templateApi.delete(row.id)
    ElMessage.success(`已删除 ${row.name}`)
    await loadTemplates()
  } catch (err) {
    ElMessage.error('删除失败: ' + err.message)
  }
}

onMounted(() => {
  loadPresets()
  loadTemplates()
})
</script>

<style scoped>
.mb-16 {
  margin-bottom: 16px;
}
.preset-tabs {
  margin-top: 4px;
}
.tab-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.search-input {
  width: 280px;
}
/* 预设卡片 */
.preset-card {
  height: 100%;
  display: flex;
  flex-direction: column;
}
.preset-card :deep(.el-card__body) {
  display: flex;
  flex-direction: column;
  height: 100%;
  padding: 16px;
}
.preset-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.preset-avatar {
  width: 48px;
  height: 48px;
  border-radius: 10px;
  background-color: #f0f2f5;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  overflow: hidden;
}
html.dark .preset-avatar {
  background-color: #374151;
}
.avatar-emoji {
  font-size: 28px;
  line-height: 1;
}
.avatar-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.preset-title {
  flex: 1;
  min-width: 0;
}
.preset-name {
  font-size: 15px;
  font-weight: 600;
  color: var(--el-text-color-primary);
  margin-bottom: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.preset-desc {
  font-size: 13px;
  color: var(--el-text-color-regular);
  line-height: 1.5;
  margin-bottom: 12px;
  min-height: 39px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.preset-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: 10px;
}
.preset-tag {
  margin: 0;
}
.preset-stats {
  display: flex;
  gap: 16px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-bottom: 12px;
  flex: 1;
}
.stat-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.preset-actions {
  display: flex;
  gap: 8px;
  border-top: 1px solid var(--el-border-color-lighter);
  padding-top: 12px;
}
.preset-actions .el-button {
  flex: 1;
}
/* 表单辅助 */
.field-hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  display: block;
  margin-top: 4px;
}
.avatar-editor {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
}
.avatar-preview {
  width: 48px;
  height: 48px;
  border-radius: 10px;
  background-color: #f0f2f5;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  overflow: hidden;
  border: 1px solid var(--el-border-color);
}
html.dark .avatar-preview {
  background-color: #374151;
}
/* 变量编辑器 */
.var-editor {
  width: 100%;
}
.var-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.var-tag {
  margin-right: 4px;
}
.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
