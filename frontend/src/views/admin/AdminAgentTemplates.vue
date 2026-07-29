<template>
  <div class="admin-agent-templates av-fade-in-up">
    <div class="page-header mb-16">
      <div class="page-title">
        <el-icon><Box /></el-icon>
        <span>Agent 模板市场</span>
      </div>
      <div class="toolbar-actions">
        <el-input v-model="keyword" placeholder="搜索模板" clearable style="width: 200px" @change="loadTemplates" />
        <el-button :loading="loading" @click="loadTemplates"><el-icon><RefreshLeft /></el-icon>刷新</el-button>
        <el-button type="primary" @click="openDialog()"><el-icon><Plus /></el-icon>新建模板</el-button>
      </div>
    </div>

    <!-- 统计概览 -->
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

    <!-- 模板卡片网格 -->
    <div v-loading="loading" class="template-grid">
      <el-empty v-if="!loading && templates.length === 0" description="暂无模板" />
      <el-card v-for="tpl in templates" :key="tpl.id" class="template-card" shadow="hover">
        <div class="card-header">
          <div class="card-avatar" :style="{ background: avatarColor(tpl) }">{{ (tpl.name || '?').charAt(0).toUpperCase() }}</div>
          <div class="card-title-wrap">
            <div class="card-title">{{ tpl.name }}</div>
            <div class="card-category">
              <el-tag size="small" :type="categoryTagType(tpl.category)">{{ categoryLabel(tpl.category) }}</el-tag>
            </div>
          </div>
        </div>
        <div class="card-desc">{{ tpl.description || '暂无描述' }}</div>
        <div class="card-stats">
          <span><el-icon><Download /></el-icon> {{ tpl.use_count ?? 0 }} 次实例化</span>
          <span><el-icon><Star /></el-icon> {{ tpl.rating ?? 0 }}</span>
        </div>
        <div class="card-actions">
          <el-button size="small" type="primary" :loading="instantiatingId === tpl.id" @click="handleInstantiate(tpl)">
            <el-icon><MagicStick /></el-icon>实例化
          </el-button>
          <el-button size="small" @click="openDialog(tpl)">编辑</el-button>
          <el-button size="small" type="danger" link @click="handleDelete(tpl)">删除</el-button>
        </div>
      </el-card>
    </div>

    <div class="pagination-wrapper">
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="total"
        :page-sizes="[12, 24, 48]"
        layout="total, sizes, prev, pager, next, jumper"
        @size-change="loadTemplates"
        @current-change="loadTemplates"
      />
    </div>

    <!-- 创建/编辑弹窗 -->
    <el-dialog v-model="dialogVisible" :title="isEdit ? '编辑模板' : '新建模板'" width="640px" @closed="resetForm">
      <el-form ref="formRef" :model="form" :rules="formRules" label-position="top" v-loading="submitting">
        <el-form-item label="模板名称" prop="name">
          <el-input v-model="form.name" placeholder="如 资深 HRBP Agent" />
        </el-form-item>
        <el-form-item label="分类" prop="category">
          <el-select v-model="form.category" style="width: 100%">
            <el-option v-for="c in categories" :key="c.value" :label="c.label" :value="c.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="描述" prop="description">
          <el-input v-model="form.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="System Prompt" prop="system_prompt">
          <el-input v-model="form.system_prompt" type="textarea" :rows="5" placeholder="你是一个..." />
        </el-form-item>
        <el-form-item label="关联模型">
          <el-input v-model="form.model" placeholder="gpt-4o" />
        </el-form-item>
        <el-form-item label="工具 (逗号分隔)">
          <el-input v-model="form.tools" placeholder="search, calculator" />
        </el-form-item>
        <el-form-item label="是否公开">
          <el-switch v-model="form.is_public" />
          <span class="form-hint">公开模板所有用户可见</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="handleSubmit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 实例化弹窗 -->
    <el-dialog v-model="instantiateDialogVisible" :title="`实例化模板 - ${instantiateTarget?.name || ''}`" width="480px">
      <el-form label-position="top" v-loading="instantiating">
        <el-form-item label="实例名称"><el-input v-model="instantiateForm.name" :placeholder="`${instantiateTarget?.name} 实例`" /></el-form-item>
        <el-form-item label="所属租户 (可选)"><el-input v-model="instantiateForm.tenant_id" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="instantiateDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="instantiating" @click="confirmInstantiate">实例化</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { agentTemplateApi } from '@/api/client'

const loading = ref(false)
const templates = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(12)
const keyword = ref('')
const stats = ref({})
const instantiatingId = ref(null)

const categories = [
  { value: 'hr', label: '人力资源' },
  { value: 'dev', label: '研发助手' },
  { value: 'analytics', label: '数据分析' },
  { value: 'customer', label: '客户服务' },
  { value: 'general', label: '通用' },
]

const statCards = [
  { key: 'total', label: '模板总数', icon: 'Box', color: '#409eff' },
  { key: 'public', label: '公开模板', icon: 'View', color: '#67c23a' },
  { key: 'instantiations', label: '实例化次数', icon: 'MagicStick', color: '#e6a23c' },
  { key: 'avg_rating', label: '平均评分', icon: 'Star', color: '#f56c6c' },
]

async function loadStats() {
  try { stats.value = await agentTemplateApi.stats() || {} }
  catch (err) { ElMessage.error('加载统计失败: ' + (err.message || '')) }
}

async function loadTemplates() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (keyword.value) params.keyword = keyword.value
    const data = await agentTemplateApi.list(params)
    templates.value = data.items || []
    total.value = data.total || 0
  } catch (err) {
    ElMessage.error('加载模板列表失败: ' + (err.message || ''))
  } finally {
    loading.value = false
  }
}

// ====== 创建/编辑 ======
const dialogVisible = ref(false)
const submitting = ref(false)
const isEdit = ref(false)
const formRef = ref(null)
const form = reactive({ id: null, name: '', category: 'general', description: '', system_prompt: '', model: '', tools: '', is_public: true })
const formRules = {
  name: [{ required: true, message: '请输入模板名称', trigger: 'blur' }],
  category: [{ required: true, message: '请选择分类', trigger: 'change' }],
}

function openDialog(row = null) {
  isEdit.value = !!row
  if (row) {
    Object.assign(form, { id: row.id, name: row.name, category: row.category || 'general', description: row.description || '', system_prompt: row.system_prompt || '', model: row.model || '', tools: (row.tools || []).join(', '), is_public: !!row.is_public })
  } else {
    resetForm()
  }
  dialogVisible.value = true
}

function resetForm() {
  Object.assign(form, { id: null, name: '', category: 'general', description: '', system_prompt: '', model: '', tools: '', is_public: true })
  formRef.value?.clearValidate?.()
}

async function handleSubmit() {
  if (!formRef.value) return
  try { await formRef.value.validate() } catch { return }
  submitting.value = true
  const payload = {
    name: form.name,
    category: form.category,
    description: form.description,
    system_prompt: form.system_prompt,
    model: form.model || null,
    tools: form.tools.split(',').map((s) => s.trim()).filter(Boolean),
    is_public: form.is_public,
  }
  try {
    if (isEdit.value) {
      await agentTemplateApi.update(form.id, payload)
      ElMessage.success('更新成功')
    } else {
      await agentTemplateApi.create(payload)
      ElMessage.success('创建成功')
    }
    dialogVisible.value = false
    await loadTemplates()
    await loadStats()
  } catch (err) {
    ElMessage.error('保存失败: ' + (err.message || ''))
  } finally {
    submitting.value = false
  }
}

async function handleDelete(row) {
  try { await ElMessageBox.confirm(`确认删除模板 "${row.name}"?`, '删除确认', { type: 'warning' }) } catch { return }
  try { await agentTemplateApi.delete(row.id); ElMessage.success('删除成功'); await loadTemplates(); await loadStats() }
  catch (err) { ElMessage.error('删除失败: ' + (err.message || '')) }
}

// ====== 实例化 ======
const instantiateDialogVisible = ref(false)
const instantiating = ref(false)
const instantiateTarget = ref(null)
const instantiateForm = reactive({ name: '', tenant_id: '' })

function handleInstantiate(row) {
  instantiateTarget.value = row
  instantiateForm.name = `${row.name} 实例`
  instantiateForm.tenant_id = ''
  instantiateDialogVisible.value = true
}

async function confirmInstantiate() {
  if (!instantiateForm.name.trim()) { ElMessage.warning('请输入实例名称'); return }
  instantiating.value = true
  instantiatingId.value = instantiateTarget.value.id
  try {
    await agentTemplateApi.instantiate(instantiateTarget.value.id, { name: instantiateForm.name, tenant_id: instantiateForm.tenant_id || null })
    ElMessage.success('实例化成功')
    instantiateDialogVisible.value = false
    await loadStats()
  } catch (err) {
    ElMessage.error('实例化失败: ' + (err.message || ''))
  } finally {
    instantiating.value = false
    instantiatingId.value = null
  }
}

// ====== 工具 ======
function categoryLabel(v) {
  const item = categories.find((c) => c.value === v)
  return item ? item.label : v || '通用'
}
function categoryTagType(v) {
  return { hr: 'warning', dev: 'success', analytics: '', customer: 'info', general: '' }[v] || ''
}
const AVATAR_COLORS = ['#409eff', '#67c23a', '#e6a23c', '#f56c6c', '#909399', '#9254de']
function avatarColor(tpl) {
  const idx = (tpl.name || '').charCodeAt(0) % AVATAR_COLORS.length
  return AVATAR_COLORS[idx]
}

onMounted(() => {
  loadStats()
  loadTemplates()
})
</script>

<style scoped>
.mb-16 { margin-bottom: 16px; }
.page-header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }
.page-title { display: inline-flex; align-items: center; gap: 8px; font-size: 18px; font-weight: 600; color: var(--el-text-color-primary); }
.toolbar-actions { display: flex; gap: 12px; align-items: center; }
.stat-card-body { display: flex; align-items: center; gap: 14px; }
.stat-icon { width: 48px; height: 48px; border-radius: 10px; display: flex; align-items: center; justify-content: center; color: #fff; }
.stat-value { font-size: 24px; font-weight: 700; color: var(--el-text-color-primary); line-height: 1.2; }
.stat-label { font-size: 13px; color: var(--el-text-color-secondary); margin-top: 2px; }
.template-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; }
.template-card { display: flex; flex-direction: column; }
.template-card :deep(.el-card__body) { display: flex; flex-direction: column; flex: 1; }
.card-header { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.card-avatar { width: 44px; height: 44px; border-radius: 10px; color: #fff; display: flex; align-items: center; justify-content: center; font-size: 20px; font-weight: 700; flex-shrink: 0; }
.card-title { font-size: 16px; font-weight: 600; color: var(--el-text-color-primary); }
.card-category { margin-top: 4px; }
.card-desc { font-size: 13px; color: var(--el-text-color-secondary); line-height: 1.5; flex: 1; margin-bottom: 12px; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.card-stats { display: flex; gap: 16px; font-size: 12px; color: var(--el-text-color-secondary); margin-bottom: 12px; }
.card-stats span { display: inline-flex; align-items: center; gap: 4px; }
.card-actions { display: flex; gap: 8px; align-items: center; border-top: 1px solid var(--el-border-color-lighter); padding-top: 12px; }
.pagination-wrapper { margin-top: 16px; display: flex; justify-content: flex-end; }
.form-hint { margin-left: 8px; font-size: 12px; color: var(--el-text-color-secondary); }
</style>
