<template>
  <div class="admin-eval-center av-fade-in-up">
    <div class="page-header mb-16">
      <div class="page-title">
        <el-icon><DataAnalysis /></el-icon>
        <span>评测中心</span>
      </div>
    </div>

    <el-tabs v-model="activeTab" @tab-change="onTabChange">
      <!-- ============ 数据集 ============ -->
      <el-tab-pane label="数据集" name="dataset">
        <div class="toolbar mb-16">
          <el-button :loading="dsLoading" @click="loadDatasets"><el-icon><RefreshLeft /></el-icon>刷新</el-button>
          <el-button type="primary" @click="openDatasetDialog"><el-icon><Plus /></el-icon>新建数据集</el-button>
        </div>
        <el-card v-loading="dsLoading">
          <el-table :data="datasets" stripe empty-text="暂无数据集">
            <el-table-column prop="id" label="ID" width="70" align="center" />
            <el-table-column prop="name" label="名称" min-width="160" show-overflow-tooltip />
            <el-table-column prop="type" label="类型" width="120"><template #default="{ row }"><el-tag size="small">{{ row.type || '—' }}</el-tag></template></el-table-column>
            <el-table-column label="条目数" width="100"><template #default="{ row }">{{ row.item_count ?? 0 }}</template></el-table-column>
            <el-table-column label="创建时间" width="180"><template #default="{ row }">{{ formatTime(row.created_at) }}</template></el-table-column>
            <el-table-column label="操作" width="280" fixed="right">
              <template #default="{ row }">
                <el-button size="small" link type="primary" @click="openItemsDialog(row)">条目</el-button>
                <el-button size="small" link @click="openImportDialog(row)">导入</el-button>
                <el-button size="small" link type="success" :loading="exportingId === row.id" @click="handleExport(row)">导出</el-button>
                <el-button size="small" link type="danger" @click="handleDeleteDataset(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- ============ LLM Judge ============ -->
      <el-tab-pane label="LLM Judge" name="judge">
        <div class="toolbar mb-16">
          <el-button :loading="judgeLoading" @click="loadJudges"><el-icon><RefreshLeft /></el-icon>刷新</el-button>
          <el-button type="primary" @click="openJudgeDialog"><el-icon><VideoPlay /></el-icon>新建评测任务</el-button>
        </div>
        <el-card v-loading="judgeLoading">
          <el-table :data="judges" stripe empty-text="暂无评测任务">
            <el-table-column prop="id" label="ID" width="70" align="center" />
            <el-table-column prop="name" label="任务名称" min-width="160" show-overflow-tooltip />
            <el-table-column prop="dataset" label="数据集" min-width="140" show-overflow-tooltip />
            <el-table-column prop="judge_model" label="Judge 模型" width="140" />
            <el-table-column label="状态" width="120">
              <template #default="{ row }"><el-tag size="small" :type="evalStatusType(row.status)">{{ evalStatusLabel(row.status) }}</el-tag></template>
            </el-table-column>
            <el-table-column label="进度" width="100"><template #default="{ row }">{{ row.progress != null ? row.progress + '%' : '—' }}</template></el-table-column>
            <el-table-column label="操作" width="180" fixed="right">
              <template #default="{ row }">
                <el-button v-if="row.status !== 'running'" size="small" link type="success" :loading="actingId === row.id" @click="handleRunJudge(row)">运行</el-button>
                <el-button size="small" link type="primary" @click="openJudgeResultDialog(row)">结果</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- ============ RAG 评测 ============ -->
      <el-tab-pane label="RAG 评测" name="rag">
        <div class="toolbar mb-16">
          <el-button :loading="ragLoading" @click="loadRags"><el-icon><RefreshLeft /></el-icon>刷新</el-button>
          <el-button type="primary" @click="openRagDialog"><el-icon><VideoPlay /></el-icon>新建评测任务</el-button>
        </div>
        <el-card v-loading="ragLoading">
          <el-table :data="ragTasks" stripe empty-text="暂无 RAG 评测任务">
            <el-table-column prop="id" label="ID" width="70" align="center" />
            <el-table-column prop="name" label="任务名称" min-width="160" show-overflow-tooltip />
            <el-table-column prop="dataset" label="数据集" min-width="140" show-overflow-tooltip />
            <el-table-column prop="retriever" label="检索器" width="140" />
            <el-table-column label="状态" width="120">
              <template #default="{ row }"><el-tag size="small" :type="evalStatusType(row.status)">{{ evalStatusLabel(row.status) }}</el-tag></template>
            </el-table-column>
            <el-table-column label="操作" width="180" fixed="right">
              <template #default="{ row }">
                <el-button v-if="row.status !== 'running'" size="small" link type="success" :loading="actingId === row.id" @click="handleRunRag(row)">运行</el-button>
                <el-button size="small" link type="primary" @click="openRagResultDialog(row)">结果</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- ============ 人工标注 ============ -->
      <el-tab-pane label="人工标注" name="annotation">
        <div class="toolbar mb-16">
          <el-button :loading="annoLoading" @click="loadAnnotations"><el-icon><RefreshLeft /></el-icon>刷新</el-button>
          <el-button type="primary" @click="openAnnoDialog"><el-icon><Plus /></el-icon>新建标注任务</el-button>
        </div>
        <el-card v-loading="annoLoading">
          <el-table :data="annotations" stripe empty-text="暂无标注任务">
            <el-table-column prop="id" label="ID" width="70" align="center" />
            <el-table-column prop="name" label="任务名称" min-width="160" show-overflow-tooltip />
            <el-table-column prop="dataset" label="数据集" min-width="140" show-overflow-tooltip />
            <el-table-column label="进度" min-width="180">
              <template #default="{ row }">
                <el-progress :percentage="annoPercent(row)" :stroke-width="14" :text-inside="true" />
              </template>
            </el-table-column>
            <el-table-column label="操作" width="220" fixed="right">
              <template #default="{ row }">
                <el-button size="small" link @click="openAssignDialog(row)">分配</el-button>
                <el-button size="small" link type="primary" @click="openSubmitDialog(row)">标注</el-button>
                <el-button size="small" link type="success" @click="openAnnoStats(row)">统计</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <!-- 数据集新建 -->
    <el-dialog v-model="dsDialogVisible" title="新建数据集" width="480px">
      <el-form ref="dsFormRef" :model="dsForm" :rules="dsRules" label-position="top" v-loading="dsSubmitting">
        <el-form-item label="名称" prop="name"><el-input v-model="dsForm.name" /></el-form-item>
        <el-form-item label="类型"><el-select v-model="dsForm.type" style="width: 100%"><el-option label="LLM Judge" value="llm_judge" /><el-option label="RAG" value="rag" /><el-option label="标注" value="annotation" /></el-select></el-form-item>
        <el-form-item label="描述"><el-input v-model="dsForm.description" type="textarea" :rows="2" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dsDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="dsSubmitting" @click="handleSubmitDataset">创建</el-button>
      </template>
    </el-dialog>

    <!-- 条目管理 -->
    <el-dialog v-model="itemsDialogVisible" :title="`条目管理 - ${itemsTarget?.name || ''}`" width="760px">
      <el-table :data="items" v-loading="itemsLoading" stripe empty-text="暂无条目" max-height="420">
        <el-table-column prop="id" label="ID" width="70" align="center" />
        <el-table-column prop="input" label="输入" min-width="200" show-overflow-tooltip />
        <el-table-column prop="expected" label="期望输出" min-width="200" show-overflow-tooltip />
      </el-table>
      <div class="pagination-wrapper">
        <el-pagination v-model:current-page="itemsPage" :total="itemsTotal" :page-size="20" layout="total, prev, pager, next" @current-change="loadItems" />
      </div>
      <template #footer><el-button @click="itemsDialogVisible = false">关闭</el-button></template>
    </el-dialog>

    <!-- 导入 -->
    <el-dialog v-model="importDialogVisible" :title="`导入条目 - ${importTarget?.name || ''}`" width="560px">
      <el-alert type="info" :closable="false" show-icon class="mb-16">粘贴 JSON 数组，每项含 input / expected 字段</el-alert>
      <el-input v-model="importText" type="textarea" :rows="8" :placeholder="importPlaceholder" />
      <template #footer>
        <el-button @click="importDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="importSubmitting" @click="handleImport">导入</el-button>
      </template>
    </el-dialog>

    <!-- LLM Judge 新建 -->
    <el-dialog v-model="judgeDialogVisible" title="新建 LLM Judge 任务" width="500px">
      <el-form ref="judgeFormRef" :model="judgeForm" :rules="judgeRules" label-position="top" v-loading="judgeSubmitting">
        <el-form-item label="任务名称" prop="name"><el-input v-model="judgeForm.name" /></el-form-item>
        <el-form-item label="数据集" prop="dataset_id"><el-select v-model="judgeForm.dataset_id" style="width: 100%"><el-option v-for="d in datasets" :key="d.id" :label="d.name" :value="d.id" /></el-select></el-form-item>
        <el-form-item label="Judge 模型" prop="judge_model"><el-input v-model="judgeForm.judge_model" placeholder="gpt-4o" /></el-form-item>
        <el-form-item label="待评测模型"><el-input v-model="judgeForm.target_model" placeholder="claude-3.5-sonnet" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="judgeDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="judgeSubmitting" @click="handleSubmitJudge">创建并运行</el-button>
      </template>
    </el-dialog>

    <!-- RAG 新建 -->
    <el-dialog v-model="ragDialogVisible" title="新建 RAG 评测任务" width="500px">
      <el-form ref="ragFormRef" :model="ragForm" :rules="ragRules" label-position="top" v-loading="ragSubmitting">
        <el-form-item label="任务名称" prop="name"><el-input v-model="ragForm.name" /></el-form-item>
        <el-form-item label="数据集" prop="dataset_id"><el-select v-model="ragForm.dataset_id" style="width: 100%"><el-option v-for="d in datasets" :key="d.id" :label="d.name" :value="d.id" /></el-select></el-form-item>
        <el-form-item label="检索器"><el-input v-model="ragForm.retriever" placeholder="hybrid / vector / bm25" /></el-form-item>
        <el-form-item label="知识库 ID"><el-input v-model="ragForm.kb_id" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="ragDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="ragSubmitting" @click="handleSubmitRag">创建并运行</el-button>
      </template>
    </el-dialog>

    <!-- 结果弹窗 (Judge / RAG 共用) -->
    <el-dialog v-model="resultDialogVisible" :title="`评测结果 - ${resultTarget?.name || ''}`" width="640px">
      <pre v-if="resultText" class="result-pre">{{ resultText }}</pre>
      <el-empty v-else description="暂无结果" :image-size="60" />
      <template #footer><el-button @click="resultDialogVisible = false">关闭</el-button></template>
    </el-dialog>

    <!-- 标注新建 -->
    <el-dialog v-model="annoDialogVisible" title="新建标注任务" width="480px">
      <el-form ref="annoFormRef" :model="annoForm" :rules="annoRules" label-position="top" v-loading="annoSubmitting">
        <el-form-item label="任务名称" prop="name"><el-input v-model="annoForm.name" /></el-form-item>
        <el-form-item label="数据集" prop="dataset_id"><el-select v-model="annoForm.dataset_id" style="width: 100%"><el-option v-for="d in datasets" :key="d.id" :label="d.name" :value="d.id" /></el-select></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="annoDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="annoSubmitting" @click="handleSubmitAnno">创建</el-button>
      </template>
    </el-dialog>

    <!-- 分配 -->
    <el-dialog v-model="assignDialogVisible" :title="`分配标注 - ${assignTarget?.name || ''}`" width="460px">
      <el-form label-position="top" v-loading="assignSubmitting">
        <el-form-item label="标注人 (逗号分隔)"><el-input v-model="assignForm.assignees" placeholder="user1, user2" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="assignDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="assignSubmitting" @click="handleAssign">分配</el-button>
      </template>
    </el-dialog>

    <!-- 标注提交 -->
    <el-dialog v-model="submitDialogVisible" :title="`提交标注 - ${submitTarget?.name || ''}`" width="560px">
      <el-form label-position="top" v-loading="submitSubmitting">
        <el-form-item label="条目 ID"><el-input v-model.number="submitForm.item_id" /></el-form-item>
        <el-form-item label="标注结果"><el-input v-model="submitForm.label" type="textarea" :rows="4" /></el-form-item>
        <el-form-item label="评分"><el-input-number v-model="submitForm.score" :min="0" :max="5" :step="0.5" style="width: 100%" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="submitDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitSubmitting" @click="handleSubmitLabel">提交</el-button>
      </template>
    </el-dialog>

    <!-- 标注统计 -->
    <el-dialog v-model="annoStatsVisible" :title="`标注统计 - ${annoStatsTarget?.name || ''}`" width="560px">
      <div v-loading="annoStatsLoading">
        <el-descriptions v-if="annoStats" :column="2" border size="small">
          <el-descriptions-item label="总条目">{{ annoStats.total ?? 0 }}</el-descriptions-item>
          <el-descriptions-item label="已完成">{{ annoStats.completed ?? 0 }}</el-descriptions-item>
          <el-descriptions-item label="待标注">{{ annoStats.pending ?? 0 }}</el-descriptions-item>
          <el-descriptions-item label="完成率">{{ annoPercent(annoStats) }}%</el-descriptions-item>
        </el-descriptions>
      </div>
      <template #footer><el-button @click="annoStatsVisible = false">关闭</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { datasetApi, llmJudgeApi, ragEvalApi, annotationApi } from '@/api/client'

const activeTab = ref('dataset')
const actingId = ref(null)
const exportingId = ref(null)

// ====== 数据集 ======
const dsLoading = ref(false)
const datasets = ref([])

async function loadDatasets() {
  dsLoading.value = true
  try { const data = await datasetApi.list(); datasets.value = data.items || [] }
  catch (err) { ElMessage.error('加载数据集失败: ' + (err.message || '')) } finally { dsLoading.value = false }
}

const dsDialogVisible = ref(false)
const dsSubmitting = ref(false)
const dsFormRef = ref(null)
const dsForm = reactive({ name: '', type: 'llm_judge', description: '' })
const dsRules = { name: [{ required: true, message: '请输入名称', trigger: 'blur' }] }

function openDatasetDialog() { Object.assign(dsForm, { name: '', type: 'llm_judge', description: '' }); dsDialogVisible.value = true }

async function handleSubmitDataset() {
  if (!dsFormRef.value) return
  try { await dsFormRef.value.validate() } catch { return }
  dsSubmitting.value = true
  try { await datasetApi.create({ ...dsForm }); ElMessage.success('创建成功'); dsDialogVisible.value = false; await loadDatasets() }
  catch (err) { ElMessage.error('创建失败: ' + (err.message || '')) } finally { dsSubmitting.value = false }
}

async function handleDeleteDataset(row) {
  try { await ElMessageBox.confirm(`确认删除数据集 "${row.name}"?`, '删除确认', { type: 'warning' }) } catch { return }
  try { await datasetApi.delete(row.id); ElMessage.success('删除成功'); await loadDatasets() }
  catch (err) { ElMessage.error('删除失败: ' + (err.message || '')) }
}

// 条目
const itemsDialogVisible = ref(false)
const itemsLoading = ref(false)
const items = ref([])
const itemsTotal = ref(0)
const itemsPage = ref(1)
const itemsTarget = ref(null)

async function openItemsDialog(row) {
  itemsTarget.value = row
  itemsDialogVisible.value = true
  itemsPage.value = 1
  await loadItems()
}

async function loadItems() {
  itemsLoading.value = true
  try { const data = await datasetApi.listEntries(itemsTarget.value.id, { page: itemsPage.value, page_size: 20 }); items.value = data.items || []; itemsTotal.value = data.total || 0 }
  catch (err) { ElMessage.error('加载条目失败: ' + (err.message || '')) } finally { itemsLoading.value = false }
}

// 导入
const importDialogVisible = ref(false)
const importTarget = ref(null)
const importText = ref('')
const importSubmitting = ref(false)
const importPlaceholder = '[{"input":"问题","expected":"答案"}]'

function openImportDialog(row) { importTarget.value = row; importText.value = ''; importDialogVisible.value = true }

async function handleImport() {
  let items
  try { items = JSON.parse(importText.value) } catch { ElMessage.error('不是合法的 JSON 数组'); return }
  if (!Array.isArray(items)) { ElMessage.error('需为 JSON 数组'); return }
  importSubmitting.value = true
  try { const data = await datasetApi.importEntries(importTarget.value.id, { items }); ElMessage.success(`导入 ${data.imported ?? items.length} 条`); importDialogVisible.value = false }
  catch (err) { ElMessage.error('导入失败: ' + (err.message || '')) } finally { importSubmitting.value = false }
}

async function handleExport(row) {
  exportingId.value = row.id
  try {
    const blob = await datasetApi.export(row.id)
    const url = window.URL.createObjectURL(new Blob([blob]))
    const a = document.createElement('a')
    a.href = url
    a.download = `dataset-${row.name}.json`
    a.click()
    window.URL.revokeObjectURL(url)
    ElMessage.success('导出成功')
  } catch (err) { ElMessage.error('导出失败: ' + (err.message || '')) } finally { exportingId.value = null }
}

// ====== LLM Judge ======
const judgeLoading = ref(false)
const judges = ref([])

async function loadJudges() {
  judgeLoading.value = true
  try { const data = await llmJudgeApi.list(); judges.value = data.items || [] }
  catch (err) { ElMessage.error('加载评测任务失败: ' + (err.message || '')) } finally { judgeLoading.value = false }
}

const judgeDialogVisible = ref(false)
const judgeSubmitting = ref(false)
const judgeFormRef = ref(null)
const judgeForm = reactive({ name: '', dataset_id: null, judge_model: 'gpt-4o', target_model: '' })
const judgeRules = { name: [{ required: true, message: '请输入名称', trigger: 'blur' }], dataset_id: [{ required: true, message: '请选择数据集', trigger: 'change' }] }

function openJudgeDialog() { Object.assign(judgeForm, { name: '', dataset_id: null, judge_model: 'gpt-4o', target_model: '' }); judgeDialogVisible.value = true }

async function handleSubmitJudge() {
  if (!judgeFormRef.value) return
  try { await judgeFormRef.value.validate() } catch { return }
  judgeSubmitting.value = true
  try { await llmJudgeApi.run({ ...judgeForm }); ElMessage.success('任务已创建并运行'); judgeDialogVisible.value = false; await loadJudges() }
  catch (err) { ElMessage.error('创建失败: ' + (err.message || '')) } finally { judgeSubmitting.value = false }
}

async function handleRunJudge(row) {
  actingId.value = row.id
  try { await llmJudgeApi.run({ id: row.id }); ElMessage.success('已重新运行'); await loadJudges() }
  catch (err) { ElMessage.error('运行失败: ' + (err.message || '')) } finally { actingId.value = null }
}

// ====== RAG ======
const ragLoading = ref(false)
const ragTasks = ref([])

async function loadRags() {
  ragLoading.value = true
  try { const data = await ragEvalApi.list(); ragTasks.value = data.items || [] }
  catch (err) { ElMessage.error('加载 RAG 任务失败: ' + (err.message || '')) } finally { ragLoading.value = false }
}

const ragDialogVisible = ref(false)
const ragSubmitting = ref(false)
const ragFormRef = ref(null)
const ragForm = reactive({ name: '', dataset_id: null, retriever: 'hybrid', kb_id: '' })
const ragRules = { name: [{ required: true, message: '请输入名称', trigger: 'blur' }], dataset_id: [{ required: true, message: '请选择数据集', trigger: 'change' }] }

function openRagDialog() { Object.assign(ragForm, { name: '', dataset_id: null, retriever: 'hybrid', kb_id: '' }); ragDialogVisible.value = true }

async function handleSubmitRag() {
  if (!ragFormRef.value) return
  try { await ragFormRef.value.validate() } catch { return }
  ragSubmitting.value = true
  try { await ragEvalApi.run({ ...ragForm }); ElMessage.success('任务已创建并运行'); ragDialogVisible.value = false; await loadRags() }
  catch (err) { ElMessage.error('创建失败: ' + (err.message || '')) } finally { ragSubmitting.value = false }
}

async function handleRunRag(row) {
  actingId.value = row.id
  try { await ragEvalApi.run({ id: row.id }); ElMessage.success('已重新运行'); await loadRags() }
  catch (err) { ElMessage.error('运行失败: ' + (err.message || '')) } finally { actingId.value = null }
}

// 结果弹窗共用
const resultDialogVisible = ref(false)
const resultTarget = ref(null)
const resultText = ref('')

async function openJudgeResultDialog(row) {
  resultTarget.value = row
  resultDialogVisible.value = true
  resultText.value = '加载中...'
  try {
    const data = await llmJudgeApi.get(row.id)
    resultText.value = data.results ? JSON.stringify(data.results, null, 2) : JSON.stringify(data, null, 2)
  } catch (err) { resultText.value = '加载失败: ' + (err.message || '') }
}

async function openRagResultDialog(row) {
  resultTarget.value = row
  resultDialogVisible.value = true
  resultText.value = '加载中...'
  try {
    const data = await ragEvalApi.get(row.id)
    resultText.value = data.results ? JSON.stringify(data.results, null, 2) : JSON.stringify(data, null, 2)
  } catch (err) { resultText.value = '加载失败: ' + (err.message || '') }
}

// ====== 标注 ======
const annoLoading = ref(false)
const annotations = ref([])

async function loadAnnotations() {
  annoLoading.value = true
  try { const data = await annotationApi.list(); annotations.value = data.items || [] }
  catch (err) { ElMessage.error('加载标注任务失败: ' + (err.message || '')) } finally { annoLoading.value = false }
}

const annoDialogVisible = ref(false)
const annoSubmitting = ref(false)
const annoFormRef = ref(null)
const annoForm = reactive({ name: '', dataset_id: null })
const annoRules = { name: [{ required: true, message: '请输入名称', trigger: 'blur' }], dataset_id: [{ required: true, message: '请选择数据集', trigger: 'change' }] }

function openAnnoDialog() { Object.assign(annoForm, { name: '', dataset_id: null }); annoDialogVisible.value = true }

async function handleSubmitAnno() {
  if (!annoFormRef.value) return
  try { await annoFormRef.value.validate() } catch { return }
  annoSubmitting.value = true
  try { await annotationApi.create({ ...annoForm }); ElMessage.success('创建成功'); annoDialogVisible.value = false; await loadAnnotations() }
  catch (err) { ElMessage.error('创建失败: ' + (err.message || '')) } finally { annoSubmitting.value = false }
}

const assignDialogVisible = ref(false)
const assignTarget = ref(null)
const assignSubmitting = ref(false)
const assignForm = reactive({ assignees: '' })

function openAssignDialog(row) { assignTarget.value = row; assignForm.assignees = ''; assignDialogVisible.value = true }

async function handleAssign() {
  const assignees = assignForm.assignees.split(/[,\s]+/).filter(Boolean)
  if (!assignees.length) { ElMessage.warning('请输入标注人'); return }
  assignSubmitting.value = true
  try { await annotationApi.assign(assignTarget.value.id, { assignees }); ElMessage.success('分配成功'); assignDialogVisible.value = false }
  catch (err) { ElMessage.error('分配失败: ' + (err.message || '')) } finally { assignSubmitting.value = false }
}

const submitDialogVisible = ref(false)
const submitTarget = ref(null)
const submitSubmitting = ref(false)
const submitForm = reactive({ item_id: null, label: '', score: 0 })

function openSubmitDialog(row) { submitTarget.value = row; Object.assign(submitForm, { item_id: null, label: '', score: 0 }); submitDialogVisible.value = true }

async function handleSubmitLabel() {
  if (!submitForm.item_id) { ElMessage.warning('请输入条目 ID'); return }
  submitSubmitting.value = true
  try { await annotationApi.annotate(submitTarget.value.id, { item_id: submitForm.item_id, label: submitForm.label, score: submitForm.score }); ElMessage.success('提交成功'); submitDialogVisible.value = false; await loadAnnotations() }
  catch (err) { ElMessage.error('提交失败: ' + (err.message || '')) } finally { submitSubmitting.value = false }
}

const annoStatsVisible = ref(false)
const annoStatsTarget = ref(null)
const annoStats = ref(null)
const annoStatsLoading = ref(false)

async function openAnnoStats(row) {
  annoStatsTarget.value = row
  annoStatsVisible.value = true
  annoStats.value = null
  annoStatsLoading.value = true
  try { annoStats.value = await annotationApi.stats(row.id) }
  catch (err) { ElMessage.error('加载统计失败: ' + (err.message || '')) } finally { annoStatsLoading.value = false }
}

// ====== 工具 ======
function evalStatusType(v) { return { pending: 'info', running: 'warning', completed: 'success', failed: 'danger' }[v] || 'info' }
function evalStatusLabel(v) { return { pending: '待运行', running: '运行中', completed: '已完成', failed: '失败' }[v] || v || '—' }
function annoPercent(row) {
  const total = Number(row.total) || 0
  if (total <= 0) return 0
  return Math.min(100, Math.round((Number(row.completed) || 0) / total * 100))
}
function formatTime(iso) { if (!iso) return '—'; try { return new Date(iso).toLocaleString('zh-CN', { hour12: false }) } catch { return iso } }

function onTabChange(name) {
  if (name === 'judge' && judges.value.length === 0) loadJudges()
  if (name === 'rag' && ragTasks.value.length === 0) loadRags()
  if (name === 'annotation' && annotations.value.length === 0) loadAnnotations()
}

onMounted(() => { loadDatasets() })
</script>

<style scoped>
.mb-16 { margin-bottom: 16px; }
.page-header { display: flex; justify-content: space-between; align-items: center; }
.page-title { display: inline-flex; align-items: center; gap: 8px; font-size: 18px; font-weight: 600; color: var(--el-text-color-primary); }
.toolbar { display: flex; gap: 12px; align-items: center; }
.pagination-wrapper { margin-top: 16px; display: flex; justify-content: flex-end; }
.result-pre { background: var(--el-fill-color-light); padding: 12px; border-radius: 6px; font-size: 12px; font-family: ui-monospace, monospace; max-height: 420px; overflow: auto; white-space: pre-wrap; word-break: break-all; margin: 0; }
</style>
