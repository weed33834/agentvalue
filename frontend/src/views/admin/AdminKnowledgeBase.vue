<template>
  <div class="admin-kb">
    <!-- 顶部说明 -->
    <el-alert type="info" :closable="false" show-icon class="mb-16">
      <template #title>
        知识库管理 —— 文档 CRUD、向量索引重建、RAG 检索测试台、分块配置。 所有操作仅 admin
        可访问,向后兼容现有 /api/v1/kb 路由。
      </template>
    </el-alert>

    <!-- 顶部统计卡片 -->
    <el-row :gutter="16" class="mb-16">
      <el-col :xs="24" :sm="12" :md="6">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-label">文档总数</div>
          <div class="stat-value">{{ total }}</div>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :md="6">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-label">已索引数</div>
          <div class="stat-value">{{ total }}</div>
          <div class="stat-sub">创建时自动嵌入向量库</div>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :md="6">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-label">Embedding 模型</div>
          <div class="stat-value-sm" :title="config.embedding_model">
            {{ config.embedding_model || '—' }}
          </div>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :md="6">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-label">分块配置</div>
          <div class="stat-value-sm">{{ config.chunk_size }} / {{ config.chunk_overlap }}</div>
          <div class="stat-sub">chunk_size / overlap</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 工具栏 -->
    <div class="toolbar mb-16">
      <el-input
        v-model="search"
        placeholder="搜索标题或内容"
        clearable
        class="search-input"
        @keyup.enter="loadDocs(1)"
        @clear="loadDocs(1)"
      >
        <template #prefix
          ><el-icon><Search /></el-icon
        ></template>
      </el-input>
      <el-button :loading="loading" @click="loadDocs(1)">
        <el-icon><Search /></el-icon>搜索
      </el-button>
      <div class="toolbar-right">
        <el-button :loading="loadingConfig" @click="openConfigDialog">
          <el-icon><Setting /></el-icon>分块配置
        </el-button>
        <el-button type="warning" plain @click="openRetrievalDialog">
          <el-icon><Aim /></el-icon>检索测试台
        </el-button>
        <el-button type="primary" @click="openCreateDialog">
          <el-icon><Plus /></el-icon>创建文档
        </el-button>
      </div>
    </div>

    <!-- 文档列表 -->
    <el-table :data="docs" v-loading="loading" border stripe>
      <el-table-column prop="kb_id" label="kb_id" min-width="140" show-overflow-tooltip />
      <el-table-column prop="title" label="标题" min-width="160" show-overflow-tooltip />
      <el-table-column
        prop="content_snippet"
        label="内容片段"
        min-width="240"
        show-overflow-tooltip
      />
      <el-table-column prop="created_at" label="创建时间" min-width="160">
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="260" fixed="right">
        <template #default="{ row }">
          <el-button size="small" @click="openEditDialog(row)">
            <el-icon><Edit /></el-icon>编辑
          </el-button>
          <el-button
            size="small"
            type="warning"
            :loading="reindexing === row.kb_id"
            @click="handleReindex(row)"
          >
            <el-icon><Refresh /></el-icon>重建
          </el-button>
          <el-button size="small" type="danger" @click="handleDelete(row)">
            <el-icon><Delete /></el-icon>删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      v-model:current-page="page"
      v-model:page-size="pageSize"
      :total="total"
      :page-sizes="[10, 20, 50, 100]"
      layout="total, sizes, prev, pager, next"
      class="mt-16"
      @size-change="loadDocs(1)"
      @current-change="loadDocs()"
    />

    <!-- 创建/编辑 Dialog -->
    <el-dialog
      v-model="formDialogVisible"
      :title="formMode === 'create' ? '创建知识库文档' : '编辑文档'"
      width="780px"
    >
      <el-form :model="form" label-width="100px">
        <el-form-item label="kb_id" required>
          <el-input
            v-model="form.kb_id"
            :disabled="formMode === 'edit'"
            placeholder="文档唯一标识,如 KB-001"
          />
        </el-form-item>
        <el-form-item label="标题" required>
          <el-input v-model="form.title" placeholder="文档标题" />
        </el-form-item>
        <el-form-item label="内容" required>
          <el-input
            v-model="form.content"
            type="textarea"
            :rows="10"
            placeholder="文档正文(纯文本)"
          />
        </el-form-item>
        <el-form-item label="元信息(JSON)">
          <el-input
            v-model="form.metadataRaw"
            type="textarea"
            :rows="4"
            placeholder='可选 JSON,如 {"tag":"demo"}'
          />
          <div class="form-tip">
            可选,JSON 格式;创建时可在元信息中携带 chunk_size / chunk_overlap
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="formDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="handleSubmit"> 保存 </el-button>
      </template>
    </el-dialog>

    <!-- 分块配置 Dialog -->
    <el-dialog v-model="configDialogVisible" title="分块配置" width="540px">
      <el-form label-width="120px">
        <el-form-item label="chunk_size">
          <el-slider v-model="configForm.chunk_size" :min="100" :max="2000" :step="50" show-input />
          <div class="form-tip">单块字符数(100-2000)</div>
        </el-form-item>
        <el-form-item label="chunk_overlap">
          <el-slider v-model="configForm.chunk_overlap" :min="0" :max="500" :step="10" show-input />
          <div class="form-tip">相邻块重叠字符数(0-500)</div>
        </el-form-item>
        <el-form-item label="Embedding 模型">
          <el-select
            v-model="configForm.embedding_model"
            filterable
            allow-create
            default-first-option
            placeholder="选择或输入模型名"
            style="width: 100%"
          >
            <el-option v-for="m in embeddingModelOptions" :key="m" :label="m" :value="m" />
          </el-select>
          <div class="form-tip">选项来自 default-models(embedding 类型),亦可手动输入</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="configDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="handleSaveConfig"> 保存 </el-button>
      </template>
    </el-dialog>

    <!-- 检索测试台 Dialog -->
    <el-dialog v-model="retrievalDialogVisible" title="检索测试台" width="820px">
      <el-form label-width="80px">
        <el-form-item label="查询">
          <el-input
            v-model="retrievalForm.query"
            type="textarea"
            :rows="3"
            placeholder="输入测试查询,验证 RAG 召回效果"
          />
        </el-form-item>
        <el-form-item label="top_k">
          <el-input-number v-model="retrievalForm.top_k" :min="1" :max="50" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="retrieving" @click="handleTestRetrieval">
            <el-icon><Aim /></el-icon>测试
          </el-button>
          <el-button @click="retrievalMatches = []">清空结果</el-button>
        </el-form-item>
      </el-form>
      <el-divider />
      <div v-if="!retrievalMatches.length">
        <el-empty description="输入查询后点击「测试」查看召回结果" />
      </div>
      <div v-else>
        <div v-for="(m, i) in retrievalMatches" :key="i" class="match-item">
          <div class="match-header">
            <span class="match-title">{{ m.title || m.kb_id || '—' }}</span>
            <el-tag size="small" type="info">{{ m.kb_id }}</el-tag>
          </div>
          <div class="match-snippet">{{ m.content_snippet || '(空)' }}</div>
          <div class="match-score">
            <span class="score-label">score</span>
            <el-progress
              :percentage="scorePercent(m.score)"
              :stroke-width="14"
              :format="() => (m.score || 0).toFixed(4)"
            />
          </div>
          <el-collapse>
            <el-collapse-item title="元信息">
              <pre class="metadata-pre">{{ JSON.stringify(m.metadata, null, 2) }}</pre>
            </el-collapse-item>
          </el-collapse>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { kbAdminApi, providerAdminApi } from '@/api/client'

// ====== 文档列表 ======
const docs = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const search = ref('')
const loading = ref(false)
const submitting = ref(false)
const reindexing = ref('')

// ====== 分块配置 ======
const config = reactive({
  chunk_size: 800,
  chunk_overlap: 100,
  embedding_model: '',
})
const loadingConfig = ref(false)
const configDialogVisible = ref(false)
const configForm = reactive({
  chunk_size: 800,
  chunk_overlap: 100,
  embedding_model: '',
})
// 常见 embedding 模型默认列表,后续从 default-models 合并
const embeddingModelOptions = ref([
  'text-embedding-3-small',
  'text-embedding-3-large',
  'text-embedding-ada-002',
  'bge-large-zh-v1.5',
  'bge-m3',
  'qwen-text-embedding-v3',
])

// ====== 创建/编辑表单 ======
const formDialogVisible = ref(false)
const formMode = ref('create')
const form = reactive({
  kb_id: '',
  title: '',
  content: '',
  metadataRaw: '{}',
})

// ====== 检索测试台 ======
const retrievalDialogVisible = ref(false)
const retrievalForm = reactive({ query: '', top_k: 5 })
const retrievalMatches = ref([])
const retrieving = ref(false)

async function loadDocs(targetPage) {
  if (targetPage) page.value = targetPage
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (search.value) params.search = search.value
    const data = await kbAdminApi.listDocs(params)
    docs.value = data.items || []
    total.value = data.total || 0
  } catch (e) {
    ElMessage.error(e.message || '加载失败')
  } finally {
    loading.value = false
  }
}

async function loadConfig() {
  loadingConfig.value = true
  try {
    const data = await kbAdminApi.getConfig()
    Object.assign(config, data)
  } catch {
    // 静默失败,使用默认值
  } finally {
    loadingConfig.value = false
  }
}

async function loadEmbeddingModels() {
  try {
    const data = await providerAdminApi.listDefaultModels()
    const list = (data.data || [])
      .filter((m) => m.model_type === 'embedding')
      .map((m) => m.model_name)
      .filter(Boolean)
    // 合并去重
    embeddingModelOptions.value = Array.from(new Set([...embeddingModelOptions.value, ...list]))
  } catch {
    // 静默失败,使用默认列表
  }
}

function openCreateDialog() {
  formMode.value = 'create'
  form.kb_id = ''
  form.title = ''
  form.content = ''
  form.metadataRaw = '{}'
  formDialogVisible.value = true
}

function openEditDialog(row) {
  formMode.value = 'edit'
  form.kb_id = row.kb_id
  form.title = row.title
  form.content = row.content || ''
  form.metadataRaw = JSON.stringify(row.metadata || {}, null, 2)
  formDialogVisible.value = true
}

function openRetrievalDialog() {
  retrievalDialogVisible.value = true
}

function parseMetadata() {
  const raw = (form.metadataRaw || '').trim()
  if (!raw) return {}
  try {
    return JSON.parse(raw)
  } catch (e) {
    throw new Error('元信息 JSON 格式错误: ' + e.message, { cause: e })
  }
}

async function handleSubmit() {
  if (!form.kb_id || !form.title || !form.content) {
    ElMessage.warning('kb_id / 标题 / 内容 不能为空')
    return
  }
  let metadata
  try {
    metadata = parseMetadata()
  } catch (e) {
    ElMessage.error(e.message)
    return
  }
  submitting.value = true
  try {
    if (formMode.value === 'create') {
      await kbAdminApi.createDoc({
        kb_id: form.kb_id,
        title: form.title,
        content: form.content,
        metadata,
      })
      ElMessage.success('创建成功')
    } else {
      await kbAdminApi.updateDoc(form.kb_id, {
        title: form.title,
        content: form.content,
        metadata,
      })
      ElMessage.success('更新成功')
    }
    formDialogVisible.value = false
    await loadDocs()
  } catch (e) {
    ElMessage.error(e.message || '保存失败')
  } finally {
    submitting.value = false
  }
}

async function handleReindex(row) {
  reindexing.value = row.kb_id
  try {
    await kbAdminApi.reindexDoc(row.kb_id)
    ElMessage.success(`已为 ${row.kb_id} 重建索引`)
  } catch (e) {
    ElMessage.error(e.message || '重建索引失败')
  } finally {
    reindexing.value = ''
  }
}

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(`确认删除文档 "${row.title}" (${row.kb_id})?`, '提示', {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await kbAdminApi.deleteDoc(row.kb_id)
    ElMessage.success('已删除')
    await loadDocs()
  } catch (e) {
    ElMessage.error(e.message || '删除失败')
  }
}

function openConfigDialog() {
  configForm.chunk_size = config.chunk_size
  configForm.chunk_overlap = config.chunk_overlap
  configForm.embedding_model = config.embedding_model
  configDialogVisible.value = true
}

async function handleSaveConfig() {
  submitting.value = true
  try {
    const data = await kbAdminApi.updateConfig({
      chunk_size: configForm.chunk_size,
      chunk_overlap: configForm.chunk_overlap,
      embedding_model: configForm.embedding_model,
    })
    Object.assign(config, {
      chunk_size: data.chunk_size,
      chunk_overlap: data.chunk_overlap,
      embedding_model: data.embedding_model,
    })
    ElMessage.success('配置已保存')
    configDialogVisible.value = false
  } catch (e) {
    ElMessage.error(e.message || '保存配置失败')
  } finally {
    submitting.value = false
  }
}

async function handleTestRetrieval() {
  if (!retrievalForm.query.trim()) {
    ElMessage.warning('请输入查询')
    return
  }
  retrieving.value = true
  try {
    const data = await kbAdminApi.testRetrieval({
      query: retrievalForm.query,
      top_k: retrievalForm.top_k,
    })
    retrievalMatches.value = data.matches || []
    if (!retrievalMatches.value.length) {
      ElMessage.info('无匹配结果')
    }
  } catch (e) {
    ElMessage.error(e.message || '检索失败')
  } finally {
    retrieving.value = false
  }
}

function scorePercent(score) {
  // score 通常为 0-1 的 cosine 相似度,转百分比
  const v = Math.max(0, Math.min(1, score || 0)) * 100
  return Math.round(v)
}

function formatTime(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString('zh-CN')
  } catch {
    return iso
  }
}

onMounted(() => {
  loadDocs()
  loadConfig()
  loadEmbeddingModels()
})
</script>

<style scoped>
.admin-kb {
  padding: 0;
}
.mb-16 {
  margin-bottom: 16px;
}
.mt-16 {
  margin-top: 16px;
}
.stat-card {
  text-align: left;
}
.stat-label {
  font-size: 13px;
  color: #909399;
  margin-bottom: 8px;
}
.stat-value {
  font-size: 28px;
  font-weight: 600;
  color: #2563eb;
}
.stat-value-sm {
  font-size: 18px;
  font-weight: 600;
  color: #2563eb;
  word-break: break-all;
}
.stat-sub {
  font-size: 12px;
  color: #c0c4cc;
  margin-top: 4px;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.toolbar .search-input {
  width: 260px;
}
.toolbar-right {
  margin-left: auto;
  display: flex;
  gap: 8px;
}
.form-tip {
  font-size: 12px;
  color: #909399;
  line-height: 1.6;
  margin-top: 4px;
}
.match-item {
  border: 1px solid #ebeef5;
  border-radius: 4px;
  padding: 12px 16px;
  margin-bottom: 12px;
  background: #fafafa;
}
.match-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.match-title {
  font-weight: 600;
  color: #303133;
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.match-snippet {
  font-size: 13px;
  color: #606266;
  background: #fff;
  border: 1px dashed #dcdfe6;
  border-radius: 4px;
  padding: 8px 12px;
  margin-bottom: 8px;
  line-height: 1.6;
  max-height: 120px;
  overflow-y: auto;
}
.match-score {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}
.score-label {
  font-size: 12px;
  color: #909399;
  width: 40px;
  flex-shrink: 0;
}
.match-score :deep(.el-progress) {
  flex: 1;
}
.metadata-pre {
  background: #1f2937;
  color: #e5e7eb;
  padding: 12px;
  border-radius: 4px;
  font-size: 12px;
  overflow-x: auto;
  margin: 0;
}
</style>
