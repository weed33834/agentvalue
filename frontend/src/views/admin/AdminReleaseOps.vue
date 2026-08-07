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
          <el-select v-model="currentAgentId" placeholder="选择 Agent" style="width: 240px" :loading="agentLoading" @change="loadVersions">
            <el-option v-for="a in agents" :key="a.id" :label="a.name" :value="a.id" />
          </el-select>
          <el-button :loading="verLoading" @click="loadVersions"><el-icon><RefreshLeft /></el-icon>刷新</el-button>
          <el-button type="primary" :disabled="!currentAgentId" @click="openVersionDialog"><el-icon><Upload /></el-icon>发布新版本</el-button>
        </div>
        <el-card v-loading="verLoading">
          <el-table :data="versions" stripe empty-text="暂无版本">
            <el-table-column prop="version_number" label="版本号" width="100" align="center">
              <template #default="{ row }">v{{ row.version_number }}</template>
            </el-table-column>
            <el-table-column prop="changelog" label="变更日志" min-width="200" show-overflow-tooltip />
            <el-table-column label="温度" width="90" align="center"><template #default="{ row }">{{ (row.temperature ?? 0) / 100 }}</template></el-table-column>
            <el-table-column label="状态" width="110">
              <template #default="{ row }"><el-tag size="small" :type="verStatusType(row.status)">{{ verStatusLabel(row.status) }}</el-tag></template>
            </el-table-column>
            <el-table-column label="发布时间" width="180"><template #default="{ row }">{{ formatTime(row.published_at || row.created_at) }}</template></el-table-column>
            <el-table-column label="操作" width="260" fixed="right">
              <template #default="{ row }">
                <el-button size="small" link type="success" @click="openCompareDialog(row)">对比</el-button>
                <el-button v-if="row.status !== 'published'" size="small" link type="primary" :loading="actingId === row.id" @click="handlePublishVersion(row)">发布</el-button>
                <el-button size="small" link type="warning" :loading="actingId === row.id" @click="handleRollback(row)">回滚到此</el-button>
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
          <el-table :data="publishConfigs" stripe empty-text="暂无发布记录">
            <el-table-column prop="id" label="ID" width="70" align="center" />
            <el-table-column label="Agent" min-width="140" show-overflow-tooltip>
              <template #default="{ row }">{{ agentName(row.agent_id) }}</template>
            </el-table-column>
            <el-table-column prop="channel" label="渠道" width="110">
              <template #default="{ row }"><el-tag size="small">{{ row.channel }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="version_id" label="版本 ID" width="100" align="center" />
            <el-table-column label="状态" width="110">
              <template #default="{ row }"><el-tag size="small" :type="pubStatusType(row.status)">{{ pubStatusLabel(row.status) }}</el-tag></template>
            </el-table-column>
            <el-table-column label="发布时间" width="180"><template #default="{ row }">{{ formatTime(row.published_at) }}</template></el-table-column>
            <el-table-column label="操作" width="200" fixed="right">
              <template #default="{ row }">
                <el-button v-if="row.status !== 'published'" size="small" link type="success" :loading="actingId === row.id" @click="handleDeploy(row)">部署</el-button>
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
            <el-table-column prop="release_type" label="类型" width="110">
              <template #default="{ row }"><el-tag size="small">{{ row.release_type || 'canary' }}</el-tag></template>
            </el-table-column>
            <el-table-column label="灰度比例" width="110"><template #default="{ row }">{{ row.traffic_percentage != null ? row.traffic_percentage + '%' : '—' }}</template></el-table-column>
            <el-table-column label="状态" width="110">
              <template #default="{ row }"><el-tag size="small" :type="grayStatusType(row.status)">{{ grayStatusLabel(row.status) }}</el-tag></template>
            </el-table-column>
            <el-table-column label="操作" width="330" fixed="right">
              <template #default="{ row }">
                <el-button size="small" link type="info" @click="openGrayStats(row)">统计</el-button>
                <el-button v-if="row.status === 'draft'" size="small" link type="success" :loading="actingId === row.id" @click="handleGrayAction(row, 'start')">启动</el-button>
                <el-button v-if="row.status === 'active'" size="small" link type="warning" :loading="actingId === row.id" @click="handleGrayAction(row, 'pause')">暂停</el-button>
                <el-button v-if="row.status === 'paused'" size="small" link type="success" :loading="actingId === row.id" @click="handleGrayAction(row, 'start')">继续</el-button>
                <el-button v-if="row.status === 'active' || row.status === 'paused'" size="small" link type="primary" :loading="actingId === row.id" @click="handleGrayAction(row, 'complete')">完成</el-button>
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
    <el-dialog v-model="versionDialogVisible" title="发布新版本" width="560px">
      <el-form ref="verFormRef" :model="verForm" label-position="top" v-loading="verSubmitting">
        <el-form-item label="所属 Agent">
          <el-input :model-value="agentName(currentAgentId)" disabled />
        </el-form-item>
        <el-form-item label="系统提示词（留空则继承 Agent 预设）">
          <el-input v-model="verForm.system_prompt" type="textarea" :rows="4" placeholder="留空继承" />
        </el-form-item>
        <el-form-item :label="`温度: ${(verForm.temperature / 100).toFixed(2)}`">
          <el-slider v-model="verForm.temperature" :min="0" :max="100" :step="5" show-input />
        </el-form-item>
        <el-form-item label="变更日志"><el-input v-model="verForm.changelog" type="textarea" :rows="3" placeholder="本次版本改动说明" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="versionDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="verSubmitting" @click="handleSubmitVersion">创建版本</el-button>
      </template>
    </el-dialog>

    <!-- 版本对比 -->
    <el-dialog v-model="compareDialogVisible" title="版本对比" width="640px">
      <el-form label-position="top">
        <el-form-item label="对比版本">
          <el-select v-model="compareForm.from" placeholder="源版本" style="width: 45%">
            <el-option v-for="v in versions" :key="v.id" :label="`v${v.version_number}`" :value="v.id" />
          </el-select>
          <span style="margin: 0 8px">→</span>
          <el-select v-model="compareForm.to" placeholder="目标版本" style="width: 45%">
            <el-option v-for="v in versions" :key="v.id" :label="`v${v.version_number}`" :value="v.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <pre v-if="compareResult" class="compare-pre">{{ compareResult }}</pre>
      <template #footer>
        <el-button @click="compareDialogVisible = false">关闭</el-button>
        <el-button type="primary" :loading="compareLoading" @click="handleCompare">对比</el-button>
      </template>
    </el-dialog>

    <!-- 发布记录 -->
    <el-dialog v-model="publishDialogVisible" title="新建发布记录" width="520px">
      <el-form ref="pubFormRef" :model="pubForm" :rules="pubRules" label-position="top" v-loading="pubSubmitting">
        <el-form-item label="Agent" prop="agent_id">
          <el-select v-model="pubForm.agent_id" placeholder="选择 Agent" style="width: 100%">
            <el-option v-for="a in agents" :key="a.id" :label="a.name" :value="a.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="渠道" prop="channel">
          <el-select v-model="pubForm.channel" style="width: 100%">
            <el-option v-for="c in PUBLISH_CHANNELS" :key="c" :label="c" :value="c" />
          </el-select>
        </el-form-item>
        <el-form-item label="版本 ID（留空自动取最新可用版本）">
          <el-input-number v-model="pubForm.version_id" :min="1" controls-position="right" style="width: 100%" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="publishDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="pubSubmitting" @click="handleSubmitPublish">创建</el-button>
      </template>
    </el-dialog>

    <!-- 灰度任务 -->
    <el-dialog v-model="grayDialogVisible" title="新建灰度任务" width="520px">
      <el-form ref="grayFormRef" :model="grayForm" :rules="grayRules" label-position="top" v-loading="graySubmitting">
        <el-form-item label="任务名称" prop="name"><el-input v-model="grayForm.name" /></el-form-item>
        <el-form-item label="Agent" prop="agent_id">
          <el-select v-model="grayForm.agent_id" placeholder="选择 Agent" style="width: 100%">
            <el-option v-for="a in agents" :key="a.id" :label="a.name" :value="a.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="灰度目标版本 ID" prop="version_id">
          <el-input-number v-model="grayForm.version_id" :min="1" controls-position="right" style="width: 100%" />
        </el-form-item>
        <el-form-item label="发布类型">
          <el-select v-model="grayForm.release_type" style="width: 100%">
            <el-option label="canary（金丝雀）" value="canary" />
            <el-option label="blue_green（蓝绿）" value="blue_green" />
            <el-option label="rolling（滚动）" value="rolling" />
          </el-select>
        </el-form-item>
        <el-form-item :label="`灰度比例: ${grayForm.traffic_percentage}%`"><el-slider v-model="grayForm.traffic_percentage" :min="0" :max="100" :step="5" show-input /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="grayDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="graySubmitting" @click="handleSubmitGray">创建</el-button>
      </template>
    </el-dialog>

    <!-- 灰度统计 -->
    <el-dialog v-model="grayStatsVisible" title="灰度发布统计" width="600px">
      <div v-loading="grayStatsLoading">
        <template v-if="grayStats">
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="名称">{{ grayStats.name }}</el-descriptions-item>
            <el-descriptions-item label="状态">{{ grayStatusLabel(grayStats.status) }}</el-descriptions-item>
            <el-descriptions-item label="灰度流量">{{ grayStats.traffic?.gray_percentage }}%</el-descriptions-item>
            <el-descriptions-item label="基线流量">{{ grayStats.traffic?.baseline_percentage }}%</el-descriptions-item>
            <el-descriptions-item label="发布类型">{{ grayStats.progress?.release_type }}</el-descriptions-item>
            <el-descriptions-item label="完成度">{{ grayStats.progress?.percent_complete != null ? grayStats.progress.percent_complete + '%' : '—' }}</el-descriptions-item>
            <el-descriptions-item label="启动时间">{{ formatTime(grayStats.timeline?.started_at) }}</el-descriptions-item>
            <el-descriptions-item label="运行时长">{{ grayStats.timeline?.running_seconds != null ? grayStats.timeline.running_seconds + 's' : '—' }}</el-descriptions-item>
          </el-descriptions>
        </template>
        <el-empty v-else description="暂无统计" />
      </div>
      <template #footer><el-button @click="grayStatsVisible = false">关闭</el-button></template>
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

// 后端 services/publish_service.py PUBLISH_CHANNELS
const PUBLISH_CHANNELS = ['web', 'api', 'feishu', 'dingtalk', 'wechat']

// ====== Agent 列表（版本/发布/灰度共用） ======
const agentLoading = ref(false)
const agents = ref([])
const currentAgentId = ref(null)

function agentName(id) {
  if (id == null) return '—'
  const hit = agents.value.find((a) => a.id === id)
  return hit ? hit.name : `#${id}`
}

async function loadAgents() {
  agentLoading.value = true
  try {
    const data = await agentVersionApi.listAgents({ page_size: 100 })
    agents.value = data.items || []
    if (!currentAgentId.value && agents.value.length) currentAgentId.value = agents.value[0].id
  } catch (err) { ElMessage.error('加载 Agent 列表失败: ' + (err.message || '')) } finally { agentLoading.value = false }
}

// ====== Agent 版本 ======
const verLoading = ref(false)
const versions = ref([])

async function loadVersions() {
  if (!currentAgentId.value) { versions.value = []; return }
  verLoading.value = true
  try {
    const data = await agentVersionApi.listVersions(currentAgentId.value)
    versions.value = data.versions || data.items || []
  } catch (err) { ElMessage.error('加载版本失败: ' + (err.message || '')) } finally { verLoading.value = false }
}

const versionDialogVisible = ref(false)
const verSubmitting = ref(false)
const verFormRef = ref(null)
const verForm = reactive({ system_prompt: '', temperature: 70, changelog: '' })

function openVersionDialog() {
  Object.assign(verForm, { system_prompt: '', temperature: 70, changelog: '' })
  versionDialogVisible.value = true
}

async function handleSubmitVersion() {
  if (!currentAgentId.value) { ElMessage.warning('请先选择 Agent'); return }
  verSubmitting.value = true
  try {
    await agentVersionApi.createVersion(currentAgentId.value, {
      system_prompt: verForm.system_prompt || undefined,
      temperature: verForm.temperature,
      changelog: verForm.changelog || undefined,
    })
    ElMessage.success('版本创建成功')
    versionDialogVisible.value = false
    await loadVersions()
  } catch (err) { ElMessage.error('创建失败: ' + (err.message || '')) } finally { verSubmitting.value = false }
}

async function handlePublishVersion(row) {
  try { await ElMessageBox.confirm(`确认发布版本 v${row.version_number}?`, '发布确认', { type: 'warning' }) } catch { return }
  actingId.value = row.id
  try {
    await agentVersionApi.publish(currentAgentId.value, row.id, { targets: ['web'] })
    ElMessage.success('发布成功')
    await loadVersions()
  } catch (err) { ElMessage.error('发布失败: ' + (err.message || '')) } finally { actingId.value = null }
}

async function handleRollback(row) {
  try { await ElMessageBox.confirm(`确认回滚到版本 v${row.version_number}?`, '回滚确认', { type: 'warning' }) } catch { return }
  actingId.value = row.id
  try {
    await agentVersionApi.rollback(currentAgentId.value, row.version_number)
    ElMessage.success('回滚成功')
    await loadVersions()
  } catch (err) { ElMessage.error('回滚失败: ' + (err.message || '')) } finally { actingId.value = null }
}

const compareDialogVisible = ref(false)
const compareLoading = ref(false)
const compareResult = ref('')
const compareForm = reactive({ from: null, to: null })

function openCompareDialog(row) {
  compareForm.from = row.id
  compareForm.to = null
  compareResult.value = ''
  compareDialogVisible.value = true
}

async function handleCompare() {
  if (!compareForm.from || !compareForm.to) { ElMessage.warning('请选择两个版本'); return }
  compareLoading.value = true
  try {
    const data = await agentVersionApi.compare(currentAgentId.value, compareForm.from, compareForm.to)
    compareResult.value = typeof data === 'string' ? data : JSON.stringify(data, null, 2)
  } catch (err) { ElMessage.error('对比失败: ' + (err.message || '')) } finally { compareLoading.value = false }
}

// ====== 发布记录 ======
const pubLoading = ref(false)
const publishConfigs = ref([])

async function loadPublish() {
  pubLoading.value = true
  try { const data = await publishApi.list(); publishConfigs.value = data.items || [] }
  catch (err) { ElMessage.error('加载发布记录失败: ' + (err.message || '')) } finally { pubLoading.value = false }
}

const publishDialogVisible = ref(false)
const pubSubmitting = ref(false)
const pubFormRef = ref(null)
const pubForm = reactive({ agent_id: null, channel: 'web', version_id: null })
const pubRules = {
  agent_id: [{ required: true, message: '请选择 Agent', trigger: 'change' }],
  channel: [{ required: true, message: '请选择渠道', trigger: 'change' }],
}

function openPublishDialog() {
  Object.assign(pubForm, { agent_id: currentAgentId.value, channel: 'web', version_id: null })
  publishDialogVisible.value = true
}

async function handleSubmitPublish() {
  if (!pubFormRef.value) return
  try { await pubFormRef.value.validate() } catch { return }
  pubSubmitting.value = true
  try {
    await publishApi.create({
      agent_id: pubForm.agent_id,
      channel: pubForm.channel,
      version_id: pubForm.version_id || undefined,
    })
    ElMessage.success('创建成功')
    publishDialogVisible.value = false
    await loadPublish()
  } catch (err) { ElMessage.error('创建失败: ' + (err.message || '')) } finally { pubSubmitting.value = false }
}

async function handleDeploy(row) {
  actingId.value = row.id
  try {
    await publishApi.deploy(row.agent_id, row.channel, { version_id: row.version_id })
    ElMessage.success('部署成功')
    await loadPublish()
  } catch (err) { ElMessage.error('部署失败: ' + (err.message || '')) } finally { actingId.value = null }
}

async function handleOffline(row) {
  try { await ElMessageBox.confirm(`确认下线 ${agentName(row.agent_id)} 的 ${row.channel} 渠道?`, '下线确认', { type: 'warning' }) } catch { return }
  actingId.value = row.id
  try { await publishApi.undeploy(row.agent_id, row.channel); ElMessage.success('下线成功'); await loadPublish() }
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
const grayForm = reactive({ name: '', agent_id: null, version_id: null, release_type: 'canary', traffic_percentage: 10 })
const grayRules = {
  name: [{ required: true, message: '请输入任务名称', trigger: 'blur' }],
  agent_id: [{ required: true, message: '请选择 Agent', trigger: 'change' }],
  version_id: [{ required: true, message: '请输入灰度目标版本 ID', trigger: 'blur' }],
}

function openGrayDialog() {
  Object.assign(grayForm, { name: '', agent_id: currentAgentId.value, version_id: null, release_type: 'canary', traffic_percentage: 10 })
  grayDialogVisible.value = true
}

async function handleSubmitGray() {
  if (!grayFormRef.value) return
  try { await grayFormRef.value.validate() } catch { return }
  graySubmitting.value = true
  try { await grayReleaseApi.create({ ...grayForm }); ElMessage.success('创建成功'); grayDialogVisible.value = false; await loadGray() }
  catch (err) { ElMessage.error('创建失败: ' + (err.message || '')) } finally { graySubmitting.value = false }
}

const grayStatsVisible = ref(false)
const grayStatsLoading = ref(false)
const grayStats = ref(null)

async function openGrayStats(row) {
  grayStats.value = null
  grayStatsVisible.value = true
  grayStatsLoading.value = true
  try { grayStats.value = await grayReleaseApi.stats(row.id) }
  catch (err) { ElMessage.error('加载统计失败: ' + (err.message || '')) } finally { grayStatsLoading.value = false }
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
function verStatusType(v) { return { published: 'success', archived: 'warning', draft: 'info' }[v] || 'info' }
function verStatusLabel(v) { return { published: '已发布', archived: '已归档', draft: '草稿' }[v] || v || '—' }
function pubStatusType(v) { return { published: 'success', pending: 'warning', failed: 'danger' }[v] || 'info' }
function pubStatusLabel(v) { return { published: '已发布', pending: '待发布', failed: '失败' }[v] || v || '—' }
function grayStatusType(v) { return { draft: 'info', active: 'success', paused: 'warning', completed: 'success', rolled_back: 'danger' }[v] || 'info' }
function grayStatusLabel(v) { return { draft: '草稿', active: '运行中', paused: '已暂停', completed: '已完成', rolled_back: '已回滚' }[v] || v || '—' }
function envStatusType(v) { return { deployed: 'success', idle: 'info', failed: 'danger' }[v] || 'info' }
function envStatusLabel(v) { return { deployed: '已部署', idle: '空闲', failed: '失败' }[v] || v || '—' }
function formatTime(iso) { if (!iso) return '—'; try { return new Date(iso).toLocaleString('zh-CN', { hour12: false }) } catch { return iso } }

function onTabChange(name) {
  if (name === 'publish' && publishConfigs.value.length === 0) loadPublish()
  if (name === 'gray' && grayTasks.value.length === 0) loadGray()
  if (name === 'env' && environments.value.length === 0) loadEnvs()
}

onMounted(async () => {
  await loadAgents()
  await loadVersions()
})
</script>

<style scoped>
.mb-16 { margin-bottom: 16px; }
.page-header { display: flex; justify-content: space-between; align-items: center; }
.page-title { display: inline-flex; align-items: center; gap: 8px; font-size: 18px; font-weight: 600; color: var(--el-text-color-primary); }
.toolbar { display: flex; gap: 12px; align-items: center; }
.compare-pre { background: var(--el-fill-color-light); padding: 12px; border-radius: 6px; font-size: 12px; font-family: ui-monospace, monospace; max-height: 320px; overflow: auto; white-space: pre-wrap; word-break: break-all; margin: 0; }
</style>
