<template>
  <div class="admin-release-ops av-fade-in-up">
    <div class="page-header mb-16">
      <div class="page-title">
        <el-icon><Promotion /></el-icon>
        <span>发布运维</span>
      </div>
    </div>

    <el-tabs v-model="activeTab" @tab-change="onTabChange">
      <!-- ============ Agent 版本 ============ -->
      <el-tab-pane label="Agent 版本" name="version">
        <div class="toolbar mb-16">
          <el-button :loading="verLoading" @click="loadVersions"><el-icon><RefreshLeft /></el-icon>刷新</el-button>
          <el-button type="primary" @click="openVersionDialog"><el-icon><Upload /></el-icon>发布新版本</el-button>
        </div>
        <el-card v-loading="verLoading">
          <el-table :data="versions" stripe empty-text="暂无版本">
            <el-table-column prop="version" label="版本号" width="130" />
            <el-table-column prop="channel" label="渠道" width="120"><template #default="{ row }"><el-tag size="small">{{ row.channel || '—' }}</el-tag></template></el-table-column>
            <el-table-column prop="description" label="说明" min-width="200" show-overflow-tooltip />
            <el-table-column label="状态" width="110">
              <template #default="{ row }"><el-tag size="small" :type="verStatusType(row.status)">{{ verStatusLabel(row.status) }}</el-tag></template>
            </el-table-column>
            <el-table-column label="发布时间" width="180"><template #default="{ row }">{{ formatTime(row.released_at) }}</template></el-table-column>
            <el-table-column label="操作" width="220" fixed="right">
              <template #default="{ row }">
                <el-button size="small" link type="success" @click="openCompareDialog(row)">对比</el-button>
                <el-button size="small" link type="warning" :loading="actingId === row.id" @click="handleRollback(row)">回滚</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- ============ 多渠道发布 ============ -->
      <el-tab-pane label="多渠道发布" name="publish">
        <div class="toolbar mb-16">
          <el-button :loading="pubLoading" @click="loadPublish"><el-icon><RefreshLeft /></el-icon>刷新</el-button>
          <el-button type="primary" @click="openPublishDialog"><el-icon><Plus /></el-icon>新建发布配置</el-button>
        </div>
        <el-card v-loading="pubLoading">
          <el-table :data="publishConfigs" stripe empty-text="暂无发布配置">
            <el-table-column prop="id" label="ID" width="70" align="center" />
            <el-table-column prop="name" label="名称" min-width="140" show-overflow-tooltip />
            <el-table-column prop="channel" label="渠道" width="120" />
            <el-table-column prop="version" label="版本" width="120" />
            <el-table-column label="状态" width="110">
              <template #default="{ row }"><el-tag size="small" :type="pubStatusType(row.status)">{{ pubStatusLabel(row.status) }}</el-tag></template>
            </el-table-column>
            <el-table-column label="操作" width="200" fixed="right">
              <template #default="{ row }">
                <el-button v-if="row.status !== 'deployed'" size="small" link type="success" :loading="actingId === row.id" @click="handleDeploy(row)">部署</el-button>
                <el-button v-else size="small" link type="danger" :loading="actingId === row.id" @click="handleOffline(row)">下线</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- ============ 灰度发布 ============ -->
      <el-tab-pane label="灰度发布" name="gray">
        <div class="toolbar mb-16">
          <el-button :loading="grayLoading" @click="loadGray"><el-icon><RefreshLeft /></el-icon>刷新</el-button>
          <el-button type="primary" @click="openGrayDialog"><el-icon><Plus /></el-icon>新建灰度任务</el-button>
        </div>
        <el-card v-loading="grayLoading">
          <el-table :data="grayTasks" stripe empty-text="暂无灰度任务">
            <el-table-column prop="id" label="ID" width="70" align="center" />
            <el-table-column prop="name" label="任务名称" min-width="140" show-overflow-tooltip />
            <el-table-column label="灰度比例" width="120"><template #default="{ row }">{{ row.percentage != null ? row.percentage + '%' : '—' }}</template></el-table-column>
            <el-table-column label="状态" width="120">
              <template #default="{ row }"><el-tag size="small" :type="grayStatusType(row.status)">{{ grayStatusLabel(row.status) }}</el-tag></template>
            </el-table-column>
            <el-table-column label="操作" width="280" fixed="right">
              <template #default="{ row }">
                <el-button v-if="row.status === 'pending'" size="small" link type="success" :loading="actingId === row.id" @click="handleGrayAction(row, 'start')">启动</el-button>
                <el-button v-if="row.status === 'running'" size="small" link type="warning" :loading="actingId === row.id" @click="handleGrayAction(row, 'pause')">暂停</el-button>
                <el-button v-if="row.status === 'paused'" size="small" link type="success" :loading="actingId === row.id" @click="handleGrayAction(row, 'start')">继续</el-button>
                <el-button v-if="row.status === 'running' || row.status === 'paused'" size="small" link type="primary" :loading="actingId === row.id" @click="handleGrayAction(row, 'complete')">完成</el-button>
                <el-button v-if="row.status !== 'rolled_back' && row.status !== 'completed'" size="small" link type="danger" :loading="actingId === row.id" @click="handleGrayAction(row, 'rollback')">回滚</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- ============ 环境管理 ============ -->
      <el-tab-pane label="环境管理" name="env">
        <div class="toolbar mb-16">
          <el-button :loading="envLoading" @click="loadEnvs"><el-icon><RefreshLeft /></el-icon>刷新</el-button>
        </div>
        <el-card v-loading="envLoading">
          <el-table :data="environments" stripe empty-text="暂无环境">
            <el-table-column prop="id" label="ID" width="70" align="center" />
            <el-table-column prop="name" label="环境名称" min-width="140" show-overflow-tooltip />
            <el-table-column prop="type" label="类型" width="120"><template #default="{ row }"><el-tag size="small">{{ row.type || '—' }}</el-tag></template></el-table-column>
            <el-table-column prop="version" label="当前版本" width="130" />
            <el-table-column label="状态" width="110">
              <template #default="{ row }"><el-tag size="small" :type="envStatusType(row.status)">{{ envStatusLabel(row.status) }}</el-tag></template>
            </el-table-column>
            <el-table-column label="操作" width="200" fixed="right">
              <template #default="{ row }">
                <el-button size="small" link type="success" :loading="actingId === row.id" @click="openDeployDialog(row)">部署</el-button>
                <el-button v-if="row.status === 'deployed'" size="small" link type="danger" :loading="actingId === row.id" @click="handleUndeploy(row)">卸载</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <!-- 版本发布 -->
    <el-dialog v-model="versionDialogVisible" title="发布新版本" width="520px">
      <el-form ref="verFormRef" :model="verForm" :rules="verRules" label-position="top" v-loading="verSubmitting">
        <el-form-item label="版本号" prop="version"><el-input v-model="verForm.version" placeholder="v1.2.0" /></el-form-item>
        <el-form-item label="渠道"><el-select v-model="verForm.channel" style="width: 100%"><el-option label="stable" value="stable" /><el-option label="beta" value="beta" /><el-option label="canary" value="canary" /></el-select></el-form-item>
        <el-form-item label="说明"><el-input v-model="verForm.description" type="textarea" :rows="3" /></el-form-item>
        <el-form-item label="制品地址"><el-input v-model="verForm.artifact_url" placeholder="https://..." /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="versionDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="verSubmitting" @click="handleSubmitVersion">发布</el-button>
      </template>
    </el-dialog>

    <!-- 版本对比 -->
    <el-dialog v-model="compareDialogVisible" title="版本对比" width="640px">
      <el-form label-position="top">
        <el-form-item label="对比版本">
          <el-select v-model="compareForm.from" placeholder="源版本" style="width: 45%">
            <el-option v-for="v in versions" :key="v.version" :label="v.version" :value="v.version" />
          </el-select>
          <span style="margin: 0 8px">→</span>
          <el-select v-model="compareForm.to" placeholder="目标版本" style="width: 45%">
            <el-option v-for="v in versions" :key="v.version" :label="v.version" :value="v.version" />
          </el-select>
        </el-form-item>
      </el-form>
      <pre v-if="compareResult" class="compare-pre">{{ compareResult }}</pre>
      <template #footer>
        <el-button @click="compareDialogVisible = false">关闭</el-button>
        <el-button type="primary" :loading="compareLoading" @click="handleCompare">对比</el-button>
      </template>
    </el-dialog>

    <!-- 发布配置 -->
    <el-dialog v-model="publishDialogVisible" title="新建发布配置" width="500px">
      <el-form ref="pubFormRef" :model="pubForm" :rules="pubRules" label-position="top" v-loading="pubSubmitting">
        <el-form-item label="名称" prop="name"><el-input v-model="pubForm.name" /></el-form-item>
        <el-form-item label="渠道" prop="channel"><el-input v-model="pubForm.channel" placeholder="web / desktop / mobile" /></el-form-item>
        <el-form-item label="版本" prop="version"><el-input v-model="pubForm.version" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="publishDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="pubSubmitting" @click="handleSubmitPublish">创建</el-button>
      </template>
    </el-dialog>

    <!-- 灰度任务 -->
    <el-dialog v-model="grayDialogVisible" title="新建灰度任务" width="500px">
      <el-form ref="grayFormRef" :model="grayForm" :rules="grayRules" label-position="top" v-loading="graySubmitting">
        <el-form-item label="任务名称" prop="name"><el-input v-model="grayForm.name" /></el-form-item>
        <el-form-item label="版本" prop="version"><el-input v-model="grayForm.version" /></el-form-item>
        <el-form-item :label="`灰度比例: ${grayForm.percentage}%`"><el-slider v-model="grayForm.percentage" :min="0" :max="100" :step="5" show-input /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="grayDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="graySubmitting" @click="handleSubmitGray">创建</el-button>
      </template>
    </el-dialog>

    <!-- 环境部署 -->
    <el-dialog v-model="deployDialogVisible" :title="`部署到 ${deployTarget?.name || ''}`" width="460px">
      <el-form label-position="top" v-loading="deploySubmitting">
        <el-form-item label="部署版本"><el-input v-model="deployForm.version" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="deployDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="deploySubmitting" @click="handleDeployEnv">部署</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { agentVersionApi, publishApi, grayReleaseApi, environmentApi } from '@/api/client'

const activeTab = ref('version')
const actingId = ref(null)

// ====== Agent 版本 ======
const verLoading = ref(false)
const versions = ref([])

async function loadVersions() {
  verLoading.value = true
  try { const data = await agentVersionApi.list(); versions.value = data.items || [] }
  catch (err) { ElMessage.error('加载版本失败: ' + (err.message || '')) } finally { verLoading.value = false }
}

const versionDialogVisible = ref(false)
const verSubmitting = ref(false)
const verFormRef = ref(null)
const verForm = reactive({ version: '', channel: 'stable', description: '', artifact_url: '' })
const verRules = { version: [{ required: true, message: '请输入版本号', trigger: 'blur' }] }

function openVersionDialog() {
  Object.assign(verForm, { version: '', channel: 'stable', description: '', artifact_url: '' })
  versionDialogVisible.value = true
}

async function handleSubmitVersion() {
  if (!verFormRef.value) return
  try { await verFormRef.value.validate() } catch { return }
  verSubmitting.value = true
  try {
    await agentVersionApi.release({ ...verForm })
    ElMessage.success('版本发布成功')
    versionDialogVisible.value = false
    await loadVersions()
  } catch (err) { ElMessage.error('发布失败: ' + (err.message || '')) } finally { verSubmitting.value = false }
}

async function handleRollback(row) {
  try { await ElMessageBox.confirm(`确认回滚到版本 ${row.version}?`, '回滚确认', { type: 'warning' }) } catch { return }
  actingId.value = row.id
  try { await agentVersionApi.rollback(row.id); ElMessage.success('回滚成功'); await loadVersions() }
  catch (err) { ElMessage.error('回滚失败: ' + (err.message || '')) } finally { actingId.value = null }
}

const compareDialogVisible = ref(false)
const compareLoading = ref(false)
const compareResult = ref('')
const compareForm = reactive({ from: '', to: '' })

function openCompareDialog(row) {
  compareForm.from = row.version
  compareForm.to = ''
  compareResult.value = ''
  compareDialogVisible.value = true
}

async function handleCompare() {
  if (!compareForm.from || !compareForm.to) { ElMessage.warning('请选择两个版本'); return }
  compareLoading.value = true
  try {
    const data = await agentVersionApi.compare(compareForm.from, compareForm.to)
    compareResult.value = typeof data === 'string' ? data : JSON.stringify(data, null, 2)
  } catch (err) { ElMessage.error('对比失败: ' + (err.message || '')) } finally { compareLoading.value = false }
}

// ====== 发布 ======
const pubLoading = ref(false)
const publishConfigs = ref([])

async function loadPublish() {
  pubLoading.value = true
  try { const data = await publishApi.list(); publishConfigs.value = data.items || [] }
  catch (err) { ElMessage.error('加载发布配置失败: ' + (err.message || '')) } finally { pubLoading.value = false }
}

const publishDialogVisible = ref(false)
const pubSubmitting = ref(false)
const pubFormRef = ref(null)
const pubForm = reactive({ name: '', channel: '', version: '' })
const pubRules = { name: [{ required: true, message: '请输入名称', trigger: 'blur' }], version: [{ required: true, message: '请输入版本', trigger: 'blur' }] }

function openPublishDialog() { Object.assign(pubForm, { name: '', channel: '', version: '' }); publishDialogVisible.value = true }

async function handleSubmitPublish() {
  if (!pubFormRef.value) return
  try { await pubFormRef.value.validate() } catch { return }
  pubSubmitting.value = true
  try { await publishApi.create({ ...pubForm }); ElMessage.success('创建成功'); publishDialogVisible.value = false; await loadPublish() }
  catch (err) { ElMessage.error('创建失败: ' + (err.message || '')) } finally { pubSubmitting.value = false }
}

async function handleDeploy(row) {
  actingId.value = row.id
  try { await publishApi.deploy(row.id); ElMessage.success('部署成功'); await loadPublish() }
  catch (err) { ElMessage.error('部署失败: ' + (err.message || '')) } finally { actingId.value = null }
}

async function handleOffline(row) {
  try { await ElMessageBox.confirm(`确认下线 "${row.name}"?`, '下线确认', { type: 'warning' }) } catch { return }
  actingId.value = row.id
  try { await publishApi.offline(row.id); ElMessage.success('下线成功'); await loadPublish() }
  catch (err) { ElMessage.error('下线失败: ' + (err.message || '')) } finally { actingId.value = null }
}

// ====== 灰度 ======
const grayLoading = ref(false)
const grayTasks = ref([])

async function loadGray() {
  grayLoading.value = true
  try { const data = await grayReleaseApi.list(); grayTasks.value = data.items || [] }
  catch (err) { ElMessage.error('加载灰度任务失败: ' + (err.message || '')) } finally { grayLoading.value = false }
}

const grayDialogVisible = ref(false)
const graySubmitting = ref(false)
const grayFormRef = ref(null)
const grayForm = reactive({ name: '', version: '', percentage: 10 })
const grayRules = { name: [{ required: true, message: '请输入任务名称', trigger: 'blur' }], version: [{ required: true, message: '请输入版本', trigger: 'blur' }] }

function openGrayDialog() { Object.assign(grayForm, { name: '', version: '', percentage: 10 }); grayDialogVisible.value = true }

async function handleSubmitGray() {
  if (!grayFormRef.value) return
  try { await grayFormRef.value.validate() } catch { return }
  graySubmitting.value = true
  try { await grayReleaseApi.create({ ...grayForm }); ElMessage.success('创建成功'); grayDialogVisible.value = false; await loadGray() }
  catch (err) { ElMessage.error('创建失败: ' + (err.message || '')) } finally { graySubmitting.value = false }
}

async function handleGrayAction(row, action) {
  const labels = { start: '启动', pause: '暂停', complete: '完成', rollback: '回滚' }
  if (action === 'rollback') { try { await ElMessageBox.confirm(`确认回滚灰度任务 "${row.name}"?`, '回滚确认', { type: 'warning' }) } catch { return } }
  actingId.value = row.id
  try { await grayReleaseApi[action](row.id); ElMessage.success(`${labels[action]}成功`); await loadGray() }
  catch (err) { ElMessage.error(`${labels[action]}失败: ` + (err.message || '')) } finally { actingId.value = null }
}

// ====== 环境 ======
const envLoading = ref(false)
const environments = ref([])

async function loadEnvs() {
  envLoading.value = true
  try { const data = await environmentApi.list(); environments.value = data.items || [] }
  catch (err) { ElMessage.error('加载环境失败: ' + (err.message || '')) } finally { envLoading.value = false }
}

const deployDialogVisible = ref(false)
const deploySubmitting = ref(false)
const deployTarget = ref(null)
const deployForm = reactive({ version: '' })

function openDeployDialog(row) {
  deployTarget.value = row
  deployForm.version = row.version || ''
  deployDialogVisible.value = true
}

async function handleDeployEnv() {
  deploySubmitting.value = true
  actingId.value = deployTarget.value.id
  try { await environmentApi.deploy(deployTarget.value.id, { version: deployForm.version }); ElMessage.success('部署成功'); deployDialogVisible.value = false; await loadEnvs() }
  catch (err) { ElMessage.error('部署失败: ' + (err.message || '')) } finally { deploySubmitting.value = false; actingId.value = null }
}

async function handleUndeploy(row) {
  try { await ElMessageBox.confirm(`确认从环境 "${row.name}" 卸载?`, '卸载确认', { type: 'warning' }) } catch { return }
  actingId.value = row.id
  try { await environmentApi.undeploy(row.id); ElMessage.success('卸载成功'); await loadEnvs() }
  catch (err) { ElMessage.error('卸载失败: ' + (err.message || '')) } finally { actingId.value = null }
}

// ====== 工具函数 ======
function verStatusType(v) { return { released: 'success', deprecated: 'warning', draft: 'info' }[v] || 'info' }
function verStatusLabel(v) { return { released: '已发布', deprecated: '已弃用', draft: '草稿' }[v] || v || '—' }
function pubStatusType(v) { return { deployed: 'success', offline: 'info', deploying: 'warning' }[v] || 'info' }
function pubStatusLabel(v) { return { deployed: '已部署', offline: '已下线', deploying: '部署中' }[v] || v || '—' }
function grayStatusType(v) { return { pending: 'info', running: 'success', paused: 'warning', completed: 'success', rolled_back: 'danger' }[v] || 'info' }
function grayStatusLabel(v) { return { pending: '待启动', running: '运行中', paused: '已暂停', completed: '已完成', rolled_back: '已回滚' }[v] || v || '—' }
function envStatusType(v) { return { deployed: 'success', idle: 'info', failed: 'danger' }[v] || 'info' }
function envStatusLabel(v) { return { deployed: '已部署', idle: '空闲', failed: '失败' }[v] || v || '—' }
function formatTime(iso) { if (!iso) return '—'; try { return new Date(iso).toLocaleString('zh-CN', { hour12: false }) } catch { return iso } }

function onTabChange(name) {
  if (name === 'publish' && publishConfigs.value.length === 0) loadPublish()
  if (name === 'gray' && grayTasks.value.length === 0) loadGray()
  if (name === 'env' && environments.value.length === 0) loadEnvs()
}

onMounted(() => { loadVersions() })
</script>

<style scoped>
.mb-16 { margin-bottom: 16px; }
.page-header { display: flex; justify-content: space-between; align-items: center; }
.page-title { display: inline-flex; align-items: center; gap: 8px; font-size: 18px; font-weight: 600; color: var(--el-text-color-primary); }
.toolbar { display: flex; gap: 12px; align-items: center; }
.compare-pre { background: var(--el-fill-color-light); padding: 12px; border-radius: 6px; font-size: 12px; font-family: ui-monospace, monospace; max-height: 320px; overflow: auto; white-space: pre-wrap; word-break: break-all; margin: 0; }
</style>
