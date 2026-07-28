<template>
  <div class="admin-skills">
    <!-- 顶部说明 -->
    <el-alert type="info" :closable="false" show-icon class="mb-16">
      <template #title>
        Skill 技能管理 —— 对标 Claude Skills / Trae Skills。支持技能 CRUD、AI 自动生成、
        导入/导出、执行测试与使用计数。内置技能不可编辑/删除，仅可查看与执行。
      </template>
    </el-alert>

    <!-- 顶部工具栏: 标题 + 操作按钮 -->
    <div class="toolbar mb-16">
      <span class="section-title">
        <el-icon><MagicStick /></el-icon>
        Skill 技能管理
      </span>
      <div class="toolbar-actions">
        <el-button type="primary" @click="openCreateDialog">
          <el-icon><Plus /></el-icon>
          新建
        </el-button>
        <el-button type="warning" @click="openGenerateDialog">
          <el-icon><MagicStick /></el-icon>
          AI 生成
        </el-button>
        <el-button type="success" @click="openImportDialog">
          <el-icon><Upload /></el-icon>
          导入
        </el-button>
        <el-button :loading="loading" @click="loadSkills">
          <el-icon><RefreshLeft /></el-icon>
          刷新
        </el-button>
      </div>
    </div>

    <!-- 筛选区 -->
    <div class="filter-bar mb-16">
      <el-input
        v-model="searchQuery"
        placeholder="搜索技能名称 / 描述 / 标签"
        clearable
        class="search-input"
      >
        <template #prefix>
          <el-icon><Search /></el-icon>
        </template>
      </el-input>
      <el-select
        v-model="categoryFilter"
        placeholder="按分类筛选"
        clearable
        class="category-select"
      >
        <el-option label="全部分类" :value="''" />
        <el-option
          v-for="cat in categoryOptions"
          :key="cat.value"
          :label="cat.label"
          :value="cat.value"
        />
      </el-select>
      <el-checkbox v-model="showBuiltinOnly">仅显示内置</el-checkbox>
    </div>

    <!-- 技能列表表格 -->
    <el-card v-loading="loading" :aria-busy="loading">
      <el-table :data="pagedSkills" stripe empty-text="暂无技能，点击「AI 生成」或「导入」开始">
        <el-table-column label="显示名称" min-width="180">
          <template #default="{ row }">
            <div class="skill-name-cell">
              <el-link type="primary" @click="openEditDialog(row)">{{ row.display_name || row.name }}</el-link>
              <div class="skill-name-raw">{{ row.name }}</div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="描述" min-width="240" show-overflow-tooltip>
          <template #default="{ row }">
            {{ row.description || '暂无描述' }}
          </template>
        </el-table-column>
        <el-table-column label="分类" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="categoryTagType(row.category)">
              {{ categoryLabel(row.category) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="version" label="版本" width="90" align="center">
          <template #default="{ row }">
            <el-tag size="small" type="info">v{{ row.version || '1.0' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="所需工具" min-width="180">
          <template #default="{ row }">
            <template v-if="row.required_tools && row.required_tools.length">
              <el-tag
                v-for="tool in row.required_tools.slice(0, 3)"
                :key="tool"
                size="small"
                type="warning"
                class="tool-tag"
              >
                {{ tool }}
              </el-tag>
              <el-tag
                v-if="row.required_tools.length > 3"
                size="small"
                type="info"
              >
                +{{ row.required_tools.length - 3 }}
              </el-tag>
            </template>
            <span v-else class="muted">无</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="180">
          <template #default="{ row }">
            <el-tag v-if="row.is_builtin" size="small" type="danger">内置</el-tag>
            <el-tag v-else size="small" type="info">自定义</el-tag>
            <el-tag
              v-if="row.is_public"
              size="small"
              type="success"
              class="status-tag"
            >
              公开
            </el-tag>
            <el-tag
              v-if="row.is_active"
              size="small"
              type="primary"
              class="status-tag"
            >
              激活
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="use_count" label="使用次数" width="100" align="center">
          <template #default="{ row }">
            <span class="use-count">{{ row.use_count || 0 }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="280" fixed="right">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click="openExecuteDialog(row)">
              <el-icon><VideoPlay /></el-icon>
              执行
            </el-button>
            <el-button
              v-if="!row.is_builtin"
              size="small"
              link
              @click="openEditDialog(row)"
            >
              <el-icon><Edit /></el-icon>
              编辑
            </el-button>
            <el-button size="small" link @click="handleExport(row)">
              <el-icon><Download /></el-icon>
              导出
            </el-button>
            <el-button
              v-if="!row.is_builtin"
              size="small"
              link
              type="danger"
              @click="handleDelete(row)"
            >
              <el-icon><Delete /></el-icon>
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- 前端分页 -->
      <div class="pagination-wrapper">
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :total="filteredSkills.length"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next, jumper"
          background
        />
      </div>
    </el-card>

    <!-- ============ 创建/编辑 对话框 ============ -->
    <el-dialog
      v-model="editDialogVisible"
      :title="editingSkill ? '编辑技能' : '创建技能'"
      width="820px"
      :close-on-click-modal="false"
      top="5vh"
    >
      <el-form ref="skillFormRef" :model="skillForm" :rules="skillRules" label-position="top">
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="技能名 (唯一标识)" prop="name">
              <el-input
                v-model="skillForm.name"
                placeholder="如：code-reviewer"
                maxlength="60"
                show-word-limit
                :disabled="!!editingSkill"
              />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="显示名称" prop="display_name">
              <el-input
                v-model="skillForm.display_name"
                placeholder="如：代码审查助手"
                maxlength="60"
                show-word-limit
              />
            </el-form-item>
          </el-col>
        </el-row>

        <el-row :gutter="16">
          <el-col :span="8">
            <el-form-item label="分类" prop="category">
              <el-select v-model="skillForm.category" placeholder="选择分类" style="width: 100%">
                <el-option
                  v-for="cat in categoryOptions"
                  :key="cat.value"
                  :label="cat.label"
                  :value="cat.value"
                />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="版本" prop="version">
              <el-input v-model="skillForm.version" placeholder="1.0" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="模型层级" prop="model_tier">
              <el-select v-model="skillForm.model_tier" placeholder="选择模型层级" style="width: 100%">
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
            v-model="skillForm.description"
            type="textarea"
            :rows="2"
            placeholder="简短描述该技能的用途与适用场景"
            maxlength="200"
            show-word-limit
          />
        </el-form-item>

        <el-form-item label="系统提示词 (System Prompt)" prop="system_prompt">
          <el-input
            v-model="skillForm.system_prompt"
            type="textarea"
            :rows="8"
            placeholder="定义技能的角色、行为与约束。例如：你是一个经验丰富的代码审查工程师，擅长发现潜在 bug、安全漏洞与性能问题..."
          />
          <span class="field-hint">
            定义技能的行为指令。支持变量插值，将被注入到 LLM 的 system message 中。
          </span>
        </el-form-item>

        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="标签 (Tags)">
              <el-select
                v-model="skillForm.tags"
                multiple
                filterable
                allow-create
                default-first-option
                placeholder="输入后回车添加标签"
                style="width: 100%"
              >
                <el-option
                  v-for="tag in tagSuggestions"
                  :key="tag"
                  :label="tag"
                  :value="tag"
                />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="所需工具 (Required Tools)">
              <el-select
                v-model="skillForm.required_tools"
                multiple
                filterable
                allow-create
                default-first-option
                placeholder="选择或输入工具名"
                style="width: 100%"
              >
                <el-option
                  v-for="tool in toolOptions"
                  :key="tool"
                  :label="tool"
                  :value="tool"
                />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>

        <el-form-item :label="`温度 (Temperature): ${skillForm.temperature / 100}`">
          <el-slider
            v-model="skillForm.temperature"
            :min="0"
            :max="100"
            :step="5"
            show-stops
          />
          <span class="field-hint">
            0 = 严谨确定，100 = 高度创造性。对话类建议 70，分析类建议 30。
          </span>
        </el-form-item>

        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="输入 Schema (JSON)">
              <el-input
                v-model="skillForm.input_schema"
                type="textarea"
                :rows="6"
                placeholder="{&quot;type&quot;:&quot;object&quot;,&quot;properties&quot;:{&quot;query&quot;:{&quot;type&quot;:&quot;string&quot;,&quot;description&quot;:&quot;输入文本&quot;}},&quot;required&quot;:[&quot;query&quot;]}"
              />
              <span class="field-hint">JSON Schema 描述技能的输入参数</span>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="输出 Schema (JSON)">
              <el-input
                v-model="skillForm.output_schema"
                type="textarea"
                :rows="6"
                placeholder="{&quot;type&quot;:&quot;object&quot;,&quot;properties&quot;:{&quot;result&quot;:{&quot;type&quot;:&quot;string&quot;}}}"
              />
              <span class="field-hint">JSON Schema 描述技能的输出结构</span>
            </el-form-item>
          </el-col>
        </el-row>

        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="是否公开">
              <el-switch v-model="skillForm.is_public" />
              <span class="field-hint">公开技能可被所有租户用户使用</span>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="是否激活">
              <el-switch v-model="skillForm.is_active" />
              <span class="field-hint">激活的技能才会被 Agent 调用</span>
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>

      <template #footer>
        <el-button @click="editDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveSkill">
          {{ editingSkill ? '保存' : '创建' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- ============ AI 自动生成 对话框 ============ -->
    <el-dialog
      v-model="generateDialogVisible"
      title="AI 自动生成 Skill"
      width="760px"
      :close-on-click-modal="false"
    >
      <el-alert type="info" :closable="false" show-icon class="mb-16">
        描述你想要的技能，AI 将自动生成完整的 Skill 定义（含系统提示词、Schema、工具组合）。
      </el-alert>
      <el-form label-position="top">
        <el-form-item label="技能描述">
          <el-input
            v-model="generateForm.description"
            type="textarea"
            :rows="5"
            placeholder="例如：帮我创建一个用于分析员工周报、提炼关键成就与改进点的技能，输出结构化的总结与建议..."
          />
        </el-form-item>
        <el-form-item label="分类 (可选)">
          <el-select v-model="generateForm.category" placeholder="选择分类" style="width: 100%">
            <el-option label="通用 (general)" value="general" />
            <el-option
              v-for="cat in categoryOptions.filter((c) => c.value !== 'general')"
              :key="cat.value"
              :label="cat.label"
              :value="cat.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="generating" @click="handleGenerate">
            <el-icon><MagicStick /></el-icon>
            生成 Skill
          </el-button>
        </el-form-item>
      </el-form>

      <!-- 生成结果预览 -->
      <div v-if="generatedSkill" class="generate-preview">
        <div class="result-header">
          <span class="result-title">生成结果预览</span>
          <el-tag type="success" size="small">已生成</el-tag>
        </div>
        <el-descriptions :column="2" border size="small" class="mt-16">
          <el-descriptions-item label="技能名">{{ generatedSkill.name }}</el-descriptions-item>
          <el-descriptions-item label="显示名称">{{ generatedSkill.display_name || '-' }}</el-descriptions-item>
          <el-descriptions-item label="分类">
            <el-tag size="small" :type="categoryTagType(generatedSkill.category)">
              {{ categoryLabel(generatedSkill.category) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="版本">{{ generatedSkill.version || '1.0' }}</el-descriptions-item>
          <el-descriptions-item label="模型层级">{{ generatedSkill.model_tier || '-' }}</el-descriptions-item>
          <el-descriptions-item label="所需工具">
            <el-tag
              v-for="tool in (generatedSkill.required_tools || [])"
              :key="tool"
              size="small"
              type="warning"
              class="tool-tag"
            >
              {{ tool }}
            </el-tag>
            <span v-if="!(generatedSkill.required_tools && generatedSkill.required_tools.length)" class="muted">无</span>
          </el-descriptions-item>
          <el-descriptions-item label="描述" :span="2">
            {{ generatedSkill.description || '-' }}
          </el-descriptions-item>
          <el-descriptions-item label="系统提示词" :span="2">
            <pre class="preview-pre">{{ generatedSkill.system_prompt || '(空)' }}</pre>
          </el-descriptions-item>
          <el-descriptions-item v-if="generatedSkill.input_schema" label="输入 Schema" :span="2">
            <pre class="preview-pre">{{ formatJson(generatedSkill.input_schema) }}</pre>
          </el-descriptions-item>
          <el-descriptions-item v-if="generatedSkill.output_schema" label="输出 Schema" :span="2">
            <pre class="preview-pre">{{ formatJson(generatedSkill.output_schema) }}</pre>
          </el-descriptions-item>
        </el-descriptions>
        <div class="mt-16">
          <el-button type="primary" @click="createFromGenerated">
            <el-icon><Plus /></el-icon>
            直接创建
          </el-button>
        </div>
      </div>
    </el-dialog>

    <!-- ============ 导入 对话框 ============ -->
    <el-dialog
      v-model="importDialogVisible"
      title="导入 Skill"
      width="720px"
      :close-on-click-modal="false"
    >
      <el-alert type="info" :closable="false" show-icon class="mb-16">
        上传 .json 文件或直接粘贴 Skill JSON。导入前可预览将要创建的技能信息。
      </el-alert>
      <el-form label-position="top">
        <el-form-item label="文件上传 (.json)">
          <el-upload
            :auto-upload="false"
            accept=".json,application/json"
            :show-file-list="true"
            :limit="1"
            :on-change="handleFileChange"
            :on-exceed="handleExceed"
          >
            <el-button>
              <el-icon><Upload /></el-icon>
              选择文件
            </el-button>
            <template #tip>
              <div class="field-hint">仅支持 .json 格式的 Skill 定义文件</div>
            </template>
          </el-upload>
        </el-form-item>
        <el-form-item label="或粘贴 JSON">
          <el-input
            v-model="importForm.raw"
            type="textarea"
            :rows="8"
            placeholder="{&quot;name&quot;:&quot;my-skill&quot;,&quot;display_name&quot;:&quot;我的技能&quot;,&quot;description&quot;:&quot;...&quot;,&quot;system_prompt&quot;:&quot;...&quot;}"
          />
        </el-form-item>
        <el-form-item>
          <el-button :loading="parsingImport" @click="parseImport">解析预览</el-button>
        </el-form-item>
      </el-form>

      <!-- 导入预览 -->
      <div v-if="importPreview" class="import-preview">
        <div class="result-header">
          <span class="result-title">导入预览</span>
          <el-tag type="success" size="small">可导入</el-tag>
        </div>
        <el-descriptions :column="2" border size="small" class="mt-16">
          <el-descriptions-item label="技能名">{{ importPreview.name || '-' }}</el-descriptions-item>
          <el-descriptions-item label="显示名称">{{ importPreview.display_name || '-' }}</el-descriptions-item>
          <el-descriptions-item label="分类">
            <el-tag size="small" :type="categoryTagType(importPreview.category)">
              {{ categoryLabel(importPreview.category) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="版本">{{ importPreview.version || '1.0' }}</el-descriptions-item>
          <el-descriptions-item label="描述" :span="2">
            {{ importPreview.description || '-' }}
          </el-descriptions-item>
        </el-descriptions>
      </div>

      <el-form class="mt-16">
        <el-form-item>
          <el-checkbox v-model="importForm.overwrite">覆盖同名技能</el-checkbox>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="importDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="importing" :disabled="!importPreview" @click="handleImport">
          导入
        </el-button>
      </template>
    </el-dialog>

    <!-- ============ 执行/测试 对话框 ============ -->
    <el-dialog
      v-model="executeDialogVisible"
      :title="`执行测试 - ${executeTarget?.display_name || executeTarget?.name || ''}`"
      width="820px"
      :close-on-click-modal="false"
    >
      <el-alert type="info" :closable="false" show-icon class="mb-16">
        输入测试文本与可选上下文，执行技能并查看输出、解析结果与 Token 用量。
      </el-alert>
      <el-form label-position="top">
        <el-form-item label="输入文本">
          <el-input
            v-model="executeForm.input"
            type="textarea"
            :rows="5"
            placeholder="输入测试文本..."
          />
        </el-form-item>
        <el-form-item label="上下文 JSON (可选)">
          <el-input
            v-model="executeForm.context"
            type="textarea"
            :rows="4"
            placeholder="{&quot;employee_id&quot;:&quot;u001&quot;,&quot;period&quot;:&quot;2025-W01&quot;}"
          />
          <span class="field-hint">JSON 格式的上下文信息，将被注入到技能执行环境</span>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="executing" @click="handleExecute">
            <el-icon><VideoPlay /></el-icon>
            执行测试
          </el-button>
        </el-form-item>
      </el-form>

      <!-- 执行结果 -->
      <div v-if="executeResult" class="execute-result">
        <div class="result-header">
          <span class="result-title">执行结果</span>
          <el-tag :type="executeResult.success === false ? 'danger' : 'success'" size="small">
            {{ executeResult.success === false ? '失败' : '成功' }}
          </el-tag>
        </div>
        <div v-if="executeResult.output" class="result-section">
          <div class="result-section-title">输出文本</div>
          <pre class="result-pre" v-html="renderMarkdown(executeResult.output)"></pre>
        </div>
        <div v-if="executeResult.parsed_result" class="result-section">
          <div class="result-section-title">解析结果 (JSON)</div>
          <pre class="result-pre">{{ formatJson(executeResult.parsed_result) }}</pre>
        </div>
        <div v-if="executeResult.error" class="result-section">
          <div class="result-section-title">错误信息</div>
          <pre class="result-pre error-pre">{{ executeResult.error }}</pre>
        </div>
        <div v-if="executeResult.token_usage" class="token-usage">
          <el-tag size="small" type="info">Prompt: {{ executeResult.token_usage.prompt || 0 }}</el-tag>
          <el-tag size="small" type="info">Completion: {{ executeResult.token_usage.completion || 0 }}</el-tag>
          <el-tag size="small" type="success">Total: {{ executeResult.token_usage.total || 0 }}</el-tag>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { skillAdminApi } from '@/api/client'

// ====== 常量配置 ======
const categoryOptions = [
  { value: 'general', label: '通用' },
  { value: 'coding', label: '编程' },
  { value: 'analysis', label: '分析' },
  { value: 'writing', label: '写作' },
  { value: 'hr', label: 'HR' },
  { value: 'data', label: '数据' },
]

const tierOptions = [
  { value: 'L0', label: 'L0 - 快速 (经济)' },
  { value: 'L1', label: 'L1 - 均衡' },
  { value: 'L2', label: 'L2 - 强力 (高质)' },
  { value: 'L3', label: 'L3 - 旗舰 (最强)' },
]

const tagSuggestions = ['高效', '创意', '严谨', '中文', '英文', '代码', '文档', '分析', '报告']

const toolOptions = ['calculator', 'datetime', 'employee_history', 'company_kb']

function categoryLabel(value) {
  const found = categoryOptions.find((c) => c.value === value)
  return found ? found.label : value || '通用'
}

function categoryTagType(value) {
  const map = {
    general: 'info',
    coding: 'success',
    analysis: 'primary',
    writing: 'warning',
    hr: 'danger',
    data: 'success',
  }
  return map[value] || 'info'
}

// ====== 列表状态 ======
const loading = ref(false)
const skills = ref([])
const searchQuery = ref('')
const categoryFilter = ref('')
const showBuiltinOnly = ref(false)

// 前端分页
const currentPage = ref(1)
const pageSize = ref(20)

// 前端过滤 (搜索 + 分类 + 仅内置)
const filteredSkills = computed(() => {
  let list = skills.value
  if (showBuiltinOnly.value) {
    list = list.filter((s) => s.is_builtin)
  }
  if (categoryFilter.value) {
    list = list.filter((s) => s.category === categoryFilter.value)
  }
  const q = searchQuery.value.trim().toLowerCase()
  if (q) {
    list = list.filter((s) => {
      const name = (s.name || '').toLowerCase()
      const display = (s.display_name || '').toLowerCase()
      const desc = (s.description || '').toLowerCase()
      const tags = (s.tags || []).join(' ').toLowerCase()
      return name.includes(q) || display.includes(q) || desc.includes(q) || tags.includes(q)
    })
  }
  return list
})

const pagedSkills = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value
  return filteredSkills.value.slice(start, start + pageSize.value)
})

async function loadSkills() {
  loading.value = true
  try {
    const params = {}
    const data = await skillAdminApi.list(params)
    skills.value = Array.isArray(data) ? data : data?.items || data?.skills || []
    currentPage.value = 1
  } catch (err) {
    ElMessage.error('加载技能列表失败: ' + err.message)
    skills.value = []
  } finally {
    loading.value = false
  }
}

// ====== 创建/编辑 对话框 ======
const editDialogVisible = ref(false)
const editingSkill = ref(null)
const saving = ref(false)
const skillFormRef = ref(null)

const skillForm = reactive({
  name: '',
  display_name: '',
  description: '',
  category: 'general',
  version: '1.0',
  system_prompt: '',
  model_tier: 'L1',
  temperature: 70,
  is_public: false,
  is_active: true,
  tags: [],
  required_tools: [],
  input_schema: '',
  output_schema: '',
})

const skillRules = {
  name: [{ required: true, message: '请输入技能名', trigger: 'blur' }],
  display_name: [{ required: true, message: '请输入显示名称', trigger: 'blur' }],
  category: [{ required: true, message: '请选择分类', trigger: 'change' }],
  model_tier: [{ required: true, message: '请选择模型层级', trigger: 'change' }],
  system_prompt: [{ required: true, message: '请输入系统提示词', trigger: 'blur' }],
}

function resetSkillForm() {
  skillForm.name = ''
  skillForm.display_name = ''
  skillForm.description = ''
  skillForm.category = 'general'
  skillForm.version = '1.0'
  skillForm.system_prompt = ''
  skillForm.model_tier = 'L1'
  skillForm.temperature = 70
  skillForm.is_public = false
  skillForm.is_active = true
  skillForm.tags = []
  skillForm.required_tools = []
  skillForm.input_schema = ''
  skillForm.output_schema = ''
}

function openCreateDialog() {
  editingSkill.value = null
  resetSkillForm()
  editDialogVisible.value = true
}

function openEditDialog(skill) {
  editingSkill.value = skill
  skillForm.name = skill.name || ''
  skillForm.display_name = skill.display_name || ''
  skillForm.description = skill.description || ''
  skillForm.category = skill.category || 'general'
  skillForm.version = skill.version || '1.0'
  skillForm.system_prompt = skill.system_prompt || ''
  skillForm.model_tier = skill.model_tier || 'L1'
  skillForm.temperature = skill.temperature ?? 70
  skillForm.is_public = !!skill.is_public
  skillForm.is_active = skill.is_active !== false
  skillForm.tags = [...(skill.tags || [])]
  skillForm.required_tools = [...(skill.required_tools || [])]
  // Schema 可能是对象或字符串,统一转字符串展示
  skillForm.input_schema = stringifySchema(skill.input_schema)
  skillForm.output_schema = stringifySchema(skill.output_schema)
  editDialogVisible.value = true
}

function stringifySchema(schema) {
  if (!schema) return ''
  if (typeof schema === 'string') return schema
  try {
    return JSON.stringify(schema, null, 2)
  } catch {
    return ''
  }
}

// 构建 Schema payload: 空字符串转 undefined,字符串尝试解析为对象
function buildSchemaPayload(text) {
  if (!text || !text.trim()) return undefined
  try {
    return JSON.parse(text)
  } catch {
    throw new Error('Schema JSON 格式错误')
  }
}

async function saveSkill() {
  if (!skillFormRef.value) return
  try {
    await skillFormRef.value.validate()
  } catch {
    return
  }

  let inputSchema
  let outputSchema
  try {
    inputSchema = buildSchemaPayload(skillForm.input_schema)
    outputSchema = buildSchemaPayload(skillForm.output_schema)
  } catch (err) {
    ElMessage.error(err.message)
    return
  }

  saving.value = true
  try {
    const payload = {
      name: skillForm.name,
      display_name: skillForm.display_name,
      description: skillForm.description,
      category: skillForm.category,
      version: skillForm.version || '1.0',
      system_prompt: skillForm.system_prompt,
      model_tier: skillForm.model_tier,
      temperature: skillForm.temperature,
      is_public: skillForm.is_public,
      is_active: skillForm.is_active,
      tags: skillForm.tags,
      required_tools: skillForm.required_tools,
      input_schema: inputSchema,
      output_schema: outputSchema,
    }
    if (editingSkill.value) {
      await skillAdminApi.update(editingSkill.value.name || editingSkill.value.id, payload)
      ElMessage.success('技能已更新')
    } else {
      await skillAdminApi.create(payload)
      ElMessage.success('技能已创建')
    }
    editDialogVisible.value = false
    await loadSkills()
  } catch (err) {
    ElMessage.error('保存失败: ' + err.message)
  } finally {
    saving.value = false
  }
}

async function handleDelete(skill) {
  try {
    await ElMessageBox.confirm(
      `确认删除技能「${skill.display_name || skill.name}」?此操作不可恢复`,
      '删除确认',
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await skillAdminApi.delete(skill.name || skill.id)
    ElMessage.success(`已删除 ${skill.display_name || skill.name}`)
    await loadSkills()
  } catch (err) {
    ElMessage.error('删除失败: ' + err.message)
  }
}

// ====== AI 自动生成 对话框 ======
const generateDialogVisible = ref(false)
const generating = ref(false)
const generatedSkill = ref(null)
const generateForm = reactive({
  description: '',
  category: 'general',
})

function openGenerateDialog() {
  generateForm.description = ''
  generateForm.category = 'general'
  generatedSkill.value = null
  generateDialogVisible.value = true
}

async function handleGenerate() {
  if (!generateForm.description.trim()) {
    ElMessage.warning('请输入技能描述')
    return
  }
  generating.value = true
  generatedSkill.value = null
  try {
    const data = await skillAdminApi.generate({
      description: generateForm.description,
      category: generateForm.category || undefined,
    })
    generatedSkill.value = data?.skill || data
    ElMessage.success('Skill 已生成,请预览后确认创建')
  } catch (err) {
    ElMessage.error('生成失败: ' + err.message)
  } finally {
    generating.value = false
  }
}

// 将生成的 Skill 数据填入创建对话框
function createFromGenerated() {
  if (!generatedSkill.value) return
  editingSkill.value = null
  resetSkillForm()
  const g = generatedSkill.value
  skillForm.name = g.name || ''
  skillForm.display_name = g.display_name || g.name || ''
  skillForm.description = g.description || ''
  skillForm.category = g.category || 'general'
  skillForm.version = g.version || '1.0'
  skillForm.system_prompt = g.system_prompt || ''
  skillForm.model_tier = g.model_tier || 'L1'
  skillForm.temperature = g.temperature ?? 70
  skillForm.is_public = !!g.is_public
  skillForm.is_active = g.is_active !== false
  skillForm.tags = [...(g.tags || [])]
  skillForm.required_tools = [...(g.required_tools || [])]
  skillForm.input_schema = stringifySchema(g.input_schema)
  skillForm.output_schema = stringifySchema(g.output_schema)
  generateDialogVisible.value = false
  editDialogVisible.value = true
}

// ====== 导入 对话框 ======
const importDialogVisible = ref(false)
const importing = ref(false)
const parsingImport = ref(false)
const importPreview = ref(null)
const importForm = reactive({
  raw: '',
  overwrite: false,
})

function openImportDialog() {
  importForm.raw = ''
  importForm.overwrite = false
  importPreview.value = null
  importDialogVisible.value = true
}

function handleFileChange(file) {
  if (!file || !file.raw) return
  const reader = new FileReader()
  reader.onload = (e) => {
    importForm.raw = e.target?.result || ''
    parseImport()
  }
  reader.onerror = () => {
    ElMessage.error('文件读取失败')
  }
  reader.readAsText(file.raw)
}

function handleExceed() {
  ElMessage.warning('一次只能上传一个文件,请先移除已有文件')
}

function parseImport() {
  if (!importForm.raw.trim()) {
    ElMessage.warning('请粘贴 JSON 或上传文件')
    return
  }
  parsingImport.value = true
  try {
    const obj = JSON.parse(importForm.raw)
    // 兼容单个 skill 或 {skill: {...}} 或 {skills: [...]} 结构
    let skill = obj
    if (obj.skill) skill = obj.skill
    importPreview.value = skill
    ElMessage.success('JSON 解析成功,可点击导入')
  } catch (err) {
    importPreview.value = null
    ElMessage.error('JSON 格式错误: ' + err.message)
  } finally {
    parsingImport.value = false
  }
}

async function handleImport() {
  if (!importPreview.value) {
    ElMessage.warning('请先解析预览')
    return
  }
  importing.value = true
  try {
    await skillAdminApi.import({
      skill: importPreview.value,
      overwrite: importForm.overwrite,
    })
    ElMessage.success('技能已导入')
    importDialogVisible.value = false
    await loadSkills()
  } catch (err) {
    ElMessage.error('导入失败: ' + err.message)
  } finally {
    importing.value = false
  }
}

// ====== 执行/测试 对话框 ======
const executeDialogVisible = ref(false)
const executing = ref(false)
const executeTarget = ref(null)
const executeResult = ref(null)
const executeForm = reactive({
  input: '',
  context: '',
})

function openExecuteDialog(skill) {
  executeTarget.value = skill
  executeForm.input = ''
  executeForm.context = ''
  executeResult.value = null
  executeDialogVisible.value = true
}

async function handleExecute() {
  if (!executeTarget.value) return
  if (!executeForm.input.trim()) {
    ElMessage.warning('请输入测试文本')
    return
  }
  let context = undefined
  if (executeForm.context.trim()) {
    try {
      context = JSON.parse(executeForm.context)
    } catch {
      ElMessage.error('上下文 JSON 格式错误')
      return
    }
  }
  executing.value = true
  executeResult.value = null
  try {
    const skillId = executeTarget.value.name || executeTarget.value.id
    const data = await skillAdminApi.execute(skillId, {
      input: executeForm.input,
      context,
    })
    executeResult.value = data
    // 执行成功后刷新列表以更新使用计数
    await loadSkills()
  } catch (err) {
    executeResult.value = { success: false, error: err.message }
  } finally {
    executing.value = false
  }
}

// ====== 导出功能 ======
async function handleExport(skill) {
  try {
    const data = await skillAdminApi.export(skill.name || skill.id)
    const jsonStr = JSON.stringify(data, null, 2)
    const blob = new Blob([jsonStr], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `skill-${skill.name || skill.id}.json`
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    URL.revokeObjectURL(url)
    ElMessage.success(`已导出 ${skill.display_name || skill.name}`)
  } catch (err) {
    ElMessage.error('导出失败: ' + err.message)
  }
}

// ====== 工具函数 ======
function formatJson(value) {
  if (value == null) return '(空)'
  if (typeof value === 'string') {
    try {
      return JSON.stringify(JSON.parse(value), null, 2)
    } catch {
      return value
    }
  }
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

// 简单 markdown 渲染:转义 HTML + 换行 + 代码块 + 加粗
// (避免引入额外依赖,使用最小渲染策略; 输出已通过 LLM 生成,非用户直接输入)
function renderMarkdown(text) {
  if (!text) return ''
  let html = String(text)
  // 转义 HTML 防止 XSS
  html = html.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  // 代码块 ```
  html = html.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    return `<pre class="md-code-block">${code.replace(/\n$/, '')}</pre>`
  })
  // 行内代码 `
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>')
  // 加粗 **
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
  // 标题 ###
  html = html.replace(/^### (.+)$/gm, '<h4>$1</h4>')
  html = html.replace(/^## (.+)$/gm, '<h3>$1</h3>')
  html = html.replace(/^# (.+)$/gm, '<h2>$1</h2>')
  // 换行
  html = html.replace(/\n/g, '<br/>')
  return html
}

onMounted(() => {
  loadSkills()
})
</script>

<style scoped>
.mb-16 {
  margin-bottom: 16px;
}
.mt-16 {
  margin-top: 16px;
}
.section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  font-size: 16px;
}
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}
.toolbar-actions {
  display: flex;
  gap: 8px;
}
.filter-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.search-input {
  width: 320px;
}
.category-select {
  width: 200px;
}
/* 表格单元格 */
.skill-name-cell {
  display: flex;
  flex-direction: column;
}
.skill-name-raw {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 2px;
}
.tool-tag {
  margin-right: 4px;
  margin-bottom: 2px;
}
.status-tag {
  margin-left: 4px;
}
.use-count {
  font-weight: 600;
  color: var(--el-color-primary);
}
.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.pagination-wrapper {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}
/* 表单辅助 */
.field-hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  display: block;
  margin-top: 4px;
}
/* 结果区通用 */
.result-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.result-title {
  font-weight: 600;
  color: var(--el-text-color-primary);
}
.result-section {
  margin-top: 12px;
}
.result-section-title {
  font-weight: 600;
  color: var(--el-text-color-primary);
  margin-bottom: 6px;
  font-size: 13px;
}
.result-pre {
  background-color: #f5f7fa;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  padding: 12px;
  font-family: ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 400px;
  overflow: auto;
}
html.dark .result-pre {
  background-color: var(--el-bg-color-page);
  border-color: var(--el-border-color);
}
.error-pre {
  color: #f56c6c;
}
.preview-pre {
  background-color: #f5f7fa;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  padding: 8px;
  font-family: ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 200px;
  overflow: auto;
  margin: 0;
}
html.dark .preview-pre {
  background-color: var(--el-bg-color-page);
  border-color: var(--el-border-color);
}
.generate-preview,
.import-preview,
.execute-result {
  margin-top: 8px;
}
.token-usage {
  display: flex;
  gap: 8px;
  margin-top: 12px;
  flex-wrap: wrap;
}
:deep(.md-code-block) {
  display: block;
  background-color: #1e293b;
  color: #e2e8f0;
  padding: 12px;
  border-radius: 4px;
  font-family: ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 8px 0;
  overflow-x: auto;
}
:deep(code) {
  background-color: rgba(175, 184, 193, 0.2);
  padding: 2px 4px;
  border-radius: 3px;
  font-family: ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace;
  font-size: 0.9em;
}
:deep(h2),
:deep(h3),
:deep(h4) {
  margin: 8px 0 4px;
  color: var(--el-text-color-primary);
}
</style>
