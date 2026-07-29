<template>
  <div class="admin-knowledge-ops av-fade-in-up">
    <div class="page-header mb-16">
      <div class="page-title">
        <el-icon><MagicStick /></el-icon>
        <span>知识增强</span>
      </div>
    </div>

    <el-tabs v-model="activeTab" @tab-change="onTabChange">
      <!-- ============ GraphRAG ============ -->
      <el-tab-pane label="GraphRAG" name="graphrag">
        <div class="toolbar mb-16">
          <el-button :loading="grLoading" @click="loadGraphTasks"><el-icon><RefreshLeft /></el-icon>刷新</el-button>
          <el-button type="primary" @click="openGraphSearch"><el-icon><Search /></el-icon>图谱搜索</el-button>
        </div>
        <el-card v-loading="grLoading">
          <el-table :data="graphTasks" stripe empty-text="暂无 GraphRAG 任务">
            <el-table-column prop="id" label="ID" width="70" align="center" />
            <el-table-column prop="name" label="任务名称" min-width="150" show-overflow-tooltip />
            <el-table-column prop="kb_id" label="知识库" min-width="140" show-overflow-tooltip />
            <el-table-column label="状态" width="120">
              <template #default="{ row }"><el-tag size="small" :type="taskStatusType(row.status)">{{ taskStatusLabel(row.status) }}</el-tag></template>
            </el-table-column>
            <el-table-column label="实体/关系" width="130"><template #default="{ row }">{{ row.entity_count ?? 0 }} / {{ row.relation_count ?? 0 }}</template></el-table-column>
            <el-table-column label="操作" width="220" fixed="right">
              <template #default="{ row }">
                <el-button size="small" link type="primary" @click="openEntitiesDialog(row)">实体</el-button>
                <el-button size="small" link @click="openRelationsDialog(row)">关系</el-button>
                <el-button size="small" link type="success" @click="openGraphSearch(row)">搜索</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- ============ 知识库同步 ============ -->
      <el-tab-pane label="知识库同步" name="sync">
        <div class="toolbar mb-16">
          <el-button :loading="syncLoading" @click="loadSources"><el-icon><RefreshLeft /></el-icon>刷新</el-button>
        </div>
        <el-card v-loading="syncLoading">
          <el-table :data="sources" stripe empty-text="暂无数据源">
            <el-table-column prop="id" label="ID" width="70" align="center" />
            <el-table-column prop="name" label="数据源名称" min-width="150" show-overflow-tooltip />
            <el-table-column prop="type" label="类型" width="120"><template #default="{ row }"><el-tag size="small">{{ row.type || '—' }}</el-tag></template></el-table-column>
            <el-table-column label="状态" width="120">
              <template #default="{ row }"><el-tag size="small" :type="syncStatusType(row.status)">{{ syncStatusLabel(row.status) }}</el-tag></template>
            </el-table-column>
            <el-table-column label="上次同步" width="180"><template #default="{ row }">{{ formatTime(row.last_synced_at) }}</template></el-table-column>
            <el-table-column label="操作" width="180" fixed="right">
              <template #default="{ row }">
                <el-button size="small" link type="success" :loading="actingId === row.id" @click="handleSync(row)">同步</el-button>
                <el-button size="small" link @click="openSyncLogs(row)">日志</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- ============ 深度文档解析 ============ -->
      <el-tab-pane label="深度文档解析" name="parse">
        <div class="toolbar mb-16">
          <el-button :loading="parseLoading" @click="loadParseTasks"><el-icon><RefreshLeft /></el-icon>刷新</el-button>
        </div>
        <el-card v-loading="parseLoading">
          <el-table :data="parseTasks" stripe empty-text="暂无解析任务">
            <el-table-column prop="id" label="ID" width="70" align="center" />
            <el-table-column prop="filename" label="文件名" min-width="180" show-overflow-tooltip />
            <el-table-column prop="parser" label="解析器" width="130" />
            <el-table-column label="状态" width="120">
              <template #default="{ row }"><el-tag size="small" :type="taskStatusType(row.status)">{{ taskStatusLabel(row.status) }}</el-tag></template>
            </el-table-column>
            <el-table-column label="创建时间" width="180"><template #default="{ row }">{{ formatTime(row.created_at) }}</template></el-table-column>
            <el-table-column label="操作" width="180" fixed="right">
              <template #default="{ row }">
                <el-button v-if="row.status !== 'running'" size="small" link type="success" :loading="actingId === row.id" @click="handleProcess(row)">处理</el-button>
                <el-button size="small" link type="primary" @click="openParseResult(row)">结果</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- ============ NL2SQL ============ -->
      <el-tab-pane label="NL2SQL" name="nl2sql">
        <el-card class="mb-16">
          <div class="toolbar mb-16">
            <span class="section-title">自然语言转 SQL</span>
            <el-button :loading="translateLoading" type="primary" @click="handleTranslate">翻译为 SQL</el-button>
            <el-button :loading="execLoading" type="success" @click="handleExecute">执行 SQL</el-button>
          </div>
          <el-form label-position="top">
            <el-form-item label="自然语言问题">
              <el-input v-model="nlQuestion" type="textarea" :rows="2" placeholder="查询最近 7 天的活跃用户数" />
            </el-form-item>
            <el-form-item label="Schema">
              <el-select v-model="nlSchema" placeholder="选择 Schema" style="width: 100%">
                <el-option v-for="s in schemas" :key="s.id || s.name" :label="s.name" :value="s.id || s.name" />
              </el-select>
            </el-form-item>
            <el-form-item label="生成的 SQL">
              <el-input v-model="nlSql" type="textarea" :rows="3" />
            </el-form-item>
          </el-form>
        </el-card>
        <el-card v-if="execResult !== null">
          <div class="section-title mb-16">执行结果</div>
          <pre class="result-pre">{{ typeof execResult === 'string' ? execResult : JSON.stringify(execResult, null, 2) }}</pre>
        </el-card>
        <el-card class="mt-16">
          <div class="toolbar mb-16">
            <span class="section-title">Schema 管理</span>
            <el-button :loading="schemaLoading" @click="loadSchemas"><el-icon><RefreshLeft /></el-icon>刷新</el-button>
          </div>
          <el-table :data="schemas" stripe empty-text="暂无 Schema">
            <el-table-column prop="id" label="ID" width="70" align="center" />
            <el-table-column prop="name" label="名称" min-width="160" show-overflow-tooltip />
            <el-table-column prop="database" label="数据库" min-width="160" show-overflow-tooltip />
            <el-table-column label="表数量" width="100"><template #default="{ row }">{{ row.table_count ?? 0 }}</template></el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- ============ 混合检索配置 ============ -->
      <el-tab-pane label="混合检索配置" name="search">
        <el-card v-loading="searchLoading">
          <div class="toolbar mb-16">
            <span class="section-title">检索配置</span>
            <el-button :loading="searchLoading" @click="loadSearchConfig"><el-icon><RefreshLeft /></el-icon>刷新</el-button>
            <el-button type="success" :loading="searchConfigSubmitting" @click="handleSaveSearchConfig">保存</el-button>
          </div>
          <el-form label-position="top" v-loading="searchConfigSubmitting">
            <el-form-item :label="`向量检索权重: ${searchConfig.vector_weight}`">
              <el-slider v-model="searchConfig.vector_weight" :min="0" :max="1" :step="0.05" show-input />
            </el-form-item>
            <el-form-item :label="`关键词检索权重: ${searchConfig.keyword_weight}`">
              <el-slider v-model="searchConfig.keyword_weight" :min="0" :max="1" :step="0.05" show-input />
            </el-form-item>
            <el-form-item label="Top K"><el-input-number v-model="searchConfig.top_k" :min="1" :max="100" style="width: 100%" /></el-form-item>
            <el-form-item label="Rerank 模型"><el-input v-model="searchConfig.rerank_model" placeholder="bge-reranker-v2" /></el-form-item>
            <el-form-item label="启用 Rerank"><el-switch v-model="searchConfig.enable_rerank" /></el-form-item>
          </el-form>
        </el-card>
        <el-card class="mt-16">
          <div class="toolbar mb-16">
            <span class="section-title">检索测试</span>
            <el-button type="primary" :loading="testLoading" @click="handleSearchTest">测试</el-button>
          </div>
          <el-form label-position="top">
            <el-form-item label="查询"><el-input v-model="testQuery" placeholder="输入测试查询" /></el-form-item>
          </el-form>
          <div v-if="testResult" class="test-result">
            <el-divider content-position="left">检索结果</el-divider>
            <pre class="result-pre">{{ typeof testResult === 'string' ? testResult : JSON.stringify(testResult, null, 2) }}</pre>
          </div>
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <!-- 实体查看 -->
    <el-dialog v-model="entitiesDialogVisible" :title="`实体 - ${entitiesTarget?.name || ''}`" width="720px">
      <el-table :data="entities" v-loading="entitiesLoading" stripe empty-text="暂无实体" max-height="440">
        <el-table-column prop="id" label="ID" width="80" align="center" />
        <el-table-column prop="name" label="名称" min-width="160" show-overflow-tooltip />
        <el-table-column prop="type" label="类型" width="140" />
        <el-table-column prop="description" label="描述" min-width="200" show-overflow-tooltip />
      </el-table>
      <template #footer><el-button @click="entitiesDialogVisible = false">关闭</el-button></template>
    </el-dialog>

    <!-- 关系查看 -->
    <el-dialog v-model="relationsDialogVisible" :title="`关系 - ${relationsTarget?.name || ''}`" width="720px">
      <el-table :data="relations" v-loading="relationsLoading" stripe empty-text="暂无关系" max-height="440">
        <el-table-column prop="id" label="ID" width="80" align="center" />
        <el-table-column prop="source" label="源实体" min-width="140" show-overflow-tooltip />
        <el-table-column prop="relation" label="关系" width="140" />
        <el-table-column prop="target" label="目标实体" min-width="140" show-overflow-tooltip />
      </el-table>
      <template #footer><el-button @click="relationsDialogVisible = false">关闭</el-button></template>
    </el-dialog>

    <!-- 图谱搜索 -->
    <el-dialog v-model="searchDialogVisible" title="图谱搜索" width="560px">
      <el-form label-position="top" v-loading="searchSubmitting">
        <el-form-item label="查询"><el-input v-model="graphQuery" placeholder="输入搜索内容" /></el-form-item>
      </el-form>
      <div v-if="searchResult" class="test-result">
        <el-divider content-position="left">搜索结果</el-divider>
        <pre class="result-pre">{{ typeof searchResult === 'string' ? searchResult : JSON.stringify(searchResult, null, 2) }}</pre>
      </div>
      <template #footer>
        <el-button @click="searchDialogVisible = false">关闭</el-button>
        <el-button type="primary" :loading="searchSubmitting" @click="handleGraphSearch">搜索</el-button>
      </template>
    </el-dialog>

    <!-- 同步日志 -->
    <el-dialog v-model="syncLogsVisible" :title="`同步日志 - ${syncLogsTarget?.name || ''}`" width="720px">
      <el-table :data="syncLogs" v-loading="syncLogsLoading" stripe empty-text="暂无日志" max-height="440">
        <el-table-column prop="id" label="ID" width="80" align="center" />
        <el-table-column label="时间" width="180"><template #default="{ row }">{{ formatTime(row.started_at) }}</template></el-table-column>
        <el-table-column label="状态" width="110"><template #default="{ row }"><el-tag size="small" :type="syncStatusType(row.status)">{{ syncStatusLabel(row.status) }}</el-tag></template></el-table-column>
        <el-table-column prop="message" label="消息" min-width="200" show-overflow-tooltip />
      </el-table>
      <template #footer><el-button @click="syncLogsVisible = false">关闭</el-button></template>
    </el-dialog>

    <!-- 解析结果 -->
    <el-dialog v-model="parseResultVisible" :title="`解析结果 - ${parseResultTarget?.filename || ''}`" width="680px">
      <pre v-if="parseResultText" class="result-pre">{{ parseResultText }}</pre>
      <el-empty v-else description="暂无结果" :image-size="60" />
      <template #footer><el-button @click="parseResultVisible = false">关闭</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { graphRagApi, kbSyncApi, docParsingApi, nl2sqlApi, searchAdminApi } from '@/api/client'

const activeTab = ref('graphrag')
const actingId = ref(null)

// ====== GraphRAG ======
const grLoading = ref(false)
const graphTasks = ref([])

async function loadGraphTasks() {
  grLoading.value = true
  try { const data = await graphRagApi.list(); graphTasks.value = data.items || [] }
  catch (err) { ElMessage.error('加载 GraphRAG 任务失败: ' + (err.message || '')) } finally { grLoading.value = false }
}

const entitiesDialogVisible = ref(false)
const entitiesLoading = ref(false)
const entities = ref([])
const entitiesTarget = ref(null)

async function openEntitiesDialog(row) {
  entitiesTarget.value = row
  entitiesDialogVisible.value = true
  entitiesLoading.value = true
  entities.value = []
  try { const data = await graphRagApi.entities(row.id); entities.value = data.items || [] }
  catch (err) { ElMessage.error('加载实体失败: ' + (err.message || '')) } finally { entitiesLoading.value = false }
}

const relationsDialogVisible = ref(false)
const relationsLoading = ref(false)
const relations = ref([])
const relationsTarget = ref(null)

async function openRelationsDialog(row) {
  relationsTarget.value = row
  relationsDialogVisible.value = true
  relationsLoading.value = true
  relations.value = []
  try { const data = await graphRagApi.relations(row.id); relations.value = data.items || [] }
  catch (err) { ElMessage.error('加载关系失败: ' + (err.message || '')) } finally { relationsLoading.value = false }
}

const searchDialogVisible = ref(false)
const graphQuery = ref('')
const searchResult = ref(null)
const searchSubmitting = ref(false)

function openGraphSearch(row = null) {
  graphQuery.value = ''
  searchResult.value = null
  if (row) graphQuery.value = ''
  searchDialogVisible.value = true
}

async function handleGraphSearch() {
  if (!graphQuery.value.trim()) { ElMessage.warning('请输入搜索内容'); return }
  searchSubmitting.value = true
  try { searchResult.value = await graphRagApi.search({ query: graphQuery.value }) }
  catch (err) { ElMessage.error('搜索失败: ' + (err.message || '')) } finally { searchSubmitting.value = false }
}

// ====== 同步 ======
const syncLoading = ref(false)
const sources = ref([])

async function loadSources() {
  syncLoading.value = true
  try { const data = await kbSyncApi.listSources(); sources.value = data.items || [] }
  catch (err) { ElMessage.error('加载数据源失败: ' + (err.message || '')) } finally { syncLoading.value = false }
}

async function handleSync(row) {
  actingId.value = row.id
  try { await kbSyncApi.sync(row.id); ElMessage.success('同步任务已启动') }
  catch (err) { ElMessage.error('同步失败: ' + (err.message || '')) } finally { actingId.value = null }
}

const syncLogsVisible = ref(false)
const syncLogsLoading = ref(false)
const syncLogs = ref([])
const syncLogsTarget = ref(null)

async function openSyncLogs(row) {
  syncLogsTarget.value = row
  syncLogsVisible.value = true
  syncLogsLoading.value = true
  syncLogs.value = []
  try { const data = await kbSyncApi.logs(row.id); syncLogs.value = data.items || [] }
  catch (err) { ElMessage.error('加载日志失败: ' + (err.message || '')) } finally { syncLogsLoading.value = false }
}

// ====== 解析 ======
const parseLoading = ref(false)
const parseTasks = ref([])

async function loadParseTasks() {
  parseLoading.value = true
  try { const data = await docParsingApi.list(); parseTasks.value = data.items || [] }
  catch (err) { ElMessage.error('加载解析任务失败: ' + (err.message || '')) } finally { parseLoading.value = false }
}

async function handleProcess(row) {
  actingId.value = row.id
  try { await docParsingApi.process(row.id); ElMessage.success('处理任务已启动'); await loadParseTasks() }
  catch (err) { ElMessage.error('处理失败: ' + (err.message || '')) } finally { actingId.value = null }
}

const parseResultVisible = ref(false)
const parseResultTarget = ref(null)
const parseResultText = ref('')

async function openParseResult(row) {
  parseResultTarget.value = row
  parseResultVisible.value = true
  parseResultText.value = '加载中...'
  try {
    const data = await docParsingApi.result(row.id)
    parseResultText.value = typeof data === 'string' ? data : JSON.stringify(data, null, 2)
  } catch (err) { parseResultText.value = '加载失败: ' + (err.message || '') }
}

// ====== NL2SQL ======
const nlQuestion = ref('')
const nlSql = ref('')
const nlSchema = ref('')
const schemas = ref([])
const schemaLoading = ref(false)
const translateLoading = ref(false)
const execLoading = ref(false)
const execResult = ref(null)

async function loadSchemas() {
  schemaLoading.value = true
  try { const data = await nl2sqlApi.schemas(); schemas.value = data.items || [] }
  catch (err) { ElMessage.error('加载 Schema 失败: ' + (err.message || '')) } finally { schemaLoading.value = false }
}

async function handleTranslate() {
  if (!nlQuestion.value.trim()) { ElMessage.warning('请输入自然语言问题'); return }
  translateLoading.value = true
  try {
    const data = await nl2sqlApi.translate({ question: nlQuestion.value, schema_id: nlSchema.value || undefined })
    nlSql.value = data.sql || data.query || JSON.stringify(data)
  } catch (err) { ElMessage.error('翻译失败: ' + (err.message || '')) } finally { translateLoading.value = false }
}

async function handleExecute() {
  if (!nlSql.value.trim()) { ElMessage.warning('请先生成或输入 SQL'); return }
  execLoading.value = true
  execResult.value = null
  try {
    const data = await nl2sqlApi.execute({ sql: nlSql.value, schema_id: nlSchema.value || undefined })
    execResult.value = data
  } catch (err) { execResult.value = '执行失败: ' + (err.message || '') } finally { execLoading.value = false }
}

// ====== 混合检索 ======
const searchLoading = ref(false)
const searchConfigSubmitting = ref(false)
const searchConfig = reactive({ vector_weight: 0.7, keyword_weight: 0.3, top_k: 10, rerank_model: '', enable_rerank: true })
const testQuery = ref('')
const testResult = ref(null)
const testLoading = ref(false)

async function loadSearchConfig() {
  searchLoading.value = true
  try {
    const data = await searchAdminApi.getConfig()
    Object.assign(searchConfig, { vector_weight: 0.7, keyword_weight: 0.3, top_k: 10, rerank_model: '', enable_rerank: true }, data || {})
  } catch (err) { ElMessage.error('加载检索配置失败: ' + (err.message || '')) } finally { searchLoading.value = false }
}

async function handleSaveSearchConfig() {
  searchConfigSubmitting.value = true
  try { await searchAdminApi.updateConfig({ ...searchConfig }); ElMessage.success('保存成功') }
  catch (err) { ElMessage.error('保存失败: ' + (err.message || '')) } finally { searchConfigSubmitting.value = false }
}

async function handleSearchTest() {
  if (!testQuery.value.trim()) { ElMessage.warning('请输入测试查询'); return }
  testLoading.value = true
  testResult.value = null
  try { testResult.value = await searchAdminApi.test({ query: testQuery.value }) }
  catch (err) { ElMessage.error('测试失败: ' + (err.message || '')) } finally { testLoading.value = false }
}

// ====== 工具 ======
function taskStatusType(v) { return { pending: 'info', running: 'warning', completed: 'success', failed: 'danger' }[v] || 'info' }
function taskStatusLabel(v) { return { pending: '待处理', running: '处理中', completed: '已完成', failed: '失败' }[v] || v || '—' }
function syncStatusType(v) { return { idle: 'info', syncing: 'warning', success: 'success', failed: 'danger' }[v] || 'info' }
function syncStatusLabel(v) { return { idle: '空闲', syncing: '同步中', success: '成功', failed: '失败' }[v] || v || '—' }
function formatTime(iso) { if (!iso) return '—'; try { return new Date(iso).toLocaleString('zh-CN', { hour12: false }) } catch { return iso } }

function onTabChange(name) {
  if (name === 'sync' && sources.value.length === 0) loadSources()
  if (name === 'parse' && parseTasks.value.length === 0) loadParseTasks()
  if (name === 'nl2sql' && schemas.value.length === 0) loadSchemas()
  if (name === 'search' && !searchConfig.vector_weight) loadSearchConfig()
}

onMounted(() => { loadGraphTasks() })
</script>

<style scoped>
.mb-16 { margin-bottom: 16px; }
.mt-16 { margin-top: 16px; }
.page-header { display: flex; justify-content: space-between; align-items: center; }
.page-title { display: inline-flex; align-items: center; gap: 8px; font-size: 18px; font-weight: 600; color: var(--el-text-color-primary); }
.toolbar { display: flex; gap: 12px; align-items: center; }
.section-title { font-weight: 600; color: var(--el-text-color-primary); }
.result-pre { background: var(--el-fill-color-light); padding: 12px; border-radius: 6px; font-size: 12px; font-family: ui-monospace, monospace; max-height: 360px; overflow: auto; white-space: pre-wrap; word-break: break-all; margin: 0; }
</style>
