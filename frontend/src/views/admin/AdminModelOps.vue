<template>
  <div class="admin-model-ops av-fade-in-up">
    <div class="page-header mb-16">
      <div class="page-title">
        <el-icon><Cpu /></el-icon>
        <span>模型运维</span>
      </div>
    </div>

    <el-tabs v-model="activeTab" @tab-change="onTabChange">
      <!-- ============ 容灾策略 ============ -->
      <el-tab-pane label="容灾策略" name="fallback">
        <div class="toolbar mb-16">
          <el-button :loading="fbLoading" @click="loadFallback"><el-icon><RefreshLeft /></el-icon>刷新</el-button>
          <el-button type="primary" @click="openFallbackDialog"><el-icon><Plus /></el-icon>新建 Fallback 链</el-button>
        </div>
        <el-card v-loading="fbLoading">
          <el-table :data="fallbackChains" stripe empty-text="暂无容灾策略">
            <el-table-column prop="id" label="ID" width="70" align="center" />
            <el-table-column prop="name" label="名称" min-width="140" show-overflow-tooltip />
            <el-table-column label="Fallback 链" min-width="260">
              <template #default="{ row }">
                <div class="chain-cell">
                  <el-tag v-for="(m, i) in (row.models || [])" :key="i" size="small" class="chain-tag">{{ m }}</el-tag>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="100">
              <template #default="{ row }"><el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag></template>
            </el-table-column>
            <el-table-column label="操作" width="180" fixed="right">
              <template #default="{ row }">
                <el-button size="small" link type="primary" :loading="testingId === row.id" @click="handleTestFallback(row)">测试</el-button>
                <el-button size="small" link type="danger" @click="handleDeleteFallback(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- ============ 负载均衡 ============ -->
      <el-tab-pane label="负载均衡" name="lb">
        <div class="toolbar mb-16">
          <el-button :loading="lbLoading" @click="loadInstances"><el-icon><RefreshLeft /></el-icon>刷新</el-button>
        </div>
        <el-card v-loading="lbLoading">
          <el-table :data="instances" stripe empty-text="暂无实例">
            <el-table-column prop="id" label="ID" width="70" align="center" />
            <el-table-column prop="name" label="实例名称" min-width="140" show-overflow-tooltip />
            <el-table-column prop="model" label="模型" min-width="160" show-overflow-tooltip />
            <el-table-column prop="endpoint" label="端点" min-width="220" show-overflow-tooltip />
            <el-table-column label="权重" width="90"><template #default="{ row }">{{ row.weight ?? '—' }}</template></el-table-column>
            <el-table-column label="健康" width="110">
              <template #default="{ row }">
                <el-tag size="small" :type="healthTagType(row.health)">{{ healthLabel(row.health) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="200" fixed="right">
              <template #default="{ row }">
                <el-button size="small" link type="primary" :loading="hcId === row.id" @click="handleHealthCheck(row)">健康检查</el-button>
                <el-button size="small" link @click="openLbConfigDialog(row)">配置</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- ============ API 健康监控 ============ -->
      <el-tab-pane label="API 健康监控" name="health">
        <el-row :gutter="16" class="mb-16">
          <el-col :xs="12" :sm="6" v-for="card in endpointCards" :key="card.key">
            <el-card shadow="hover">
              <div class="stat-value">{{ endpointStats[card.key] ?? 0 }}</div>
              <div class="stat-label">{{ card.label }}</div>
            </el-card>
          </el-col>
        </el-row>
        <div class="toolbar mb-16">
          <span class="section-title">SLO 列表</span>
          <el-button :loading="sloLoading" @click="loadSlos"><el-icon><RefreshLeft /></el-icon>刷新</el-button>
          <el-button type="primary" @click="openSloDialog"><el-icon><Plus /></el-icon>新建 SLO</el-button>
        </div>
        <el-card v-loading="sloLoading">
          <el-table :data="slos" stripe empty-text="暂无 SLO">
            <el-table-column prop="id" label="ID" width="70" align="center" />
            <el-table-column prop="name" label="SLO 名称" min-width="160" show-overflow-tooltip />
            <el-table-column prop="endpoint" label="端点" min-width="180" show-overflow-tooltip />
            <el-table-column label="目标" width="120"><template #default="{ row }">{{ row.target != null ? row.target : '—' }}</template></el-table-column>
            <el-table-column label="当前" width="120"><template #default="{ row }">{{ row.current != null ? row.current : '—' }}</template></el-table-column>
            <el-table-column label="状态" width="120">
              <template #default="{ row }">
                <el-tag size="small" :type="sloStatusType(row)">{{ sloStatusLabel(row) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="100" fixed="right">
              <template #default="{ row }">
                <el-button size="small" link type="danger" @click="handleDeleteSlo(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <!-- Fallback 创建 -->
    <el-dialog v-model="fallbackDialogVisible" title="新建 Fallback 链" width="520px">
      <el-form ref="fbFormRef" :model="fbForm" :rules="fbRules" label-position="top" v-loading="fbSubmitting">
        <el-form-item label="名称" prop="name"><el-input v-model="fbForm.name" /></el-form-item>
        <el-form-item label="Fallback 链 (每行一个模型名)" prop="models">
          <el-input v-model="fbForm.modelsText" type="textarea" :rows="5" placeholder="gpt-4o&#10;claude-3.5-sonnet&#10;qwen-max" />
        </el-form-item>
        <el-form-item label="启用"><el-switch v-model="fbForm.enabled" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="fallbackDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="fbSubmitting" @click="handleSubmitFallback">保存</el-button>
      </template>
    </el-dialog>

    <!-- LB 配置 -->
    <el-dialog v-model="lbConfigVisible" :title="`配置实例 - ${lbTarget?.name || ''}`" width="460px">
      <el-form label-position="top" v-loading="lbConfigSubmitting">
        <el-form-item label="权重"><el-input-number v-model="lbConfigForm.weight" :min="0" :max="100" style="width: 100%" /></el-form-item>
        <el-form-item label="启用"><el-switch v-model="lbConfigForm.enabled" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="lbConfigVisible = false">取消</el-button>
        <el-button type="primary" :loading="lbConfigSubmitting" @click="handleSubmitLbConfig">保存</el-button>
      </template>
    </el-dialog>

    <!-- SLO 创建 -->
    <el-dialog v-model="sloDialogVisible" title="新建 SLO" width="500px">
      <el-form ref="sloFormRef" :model="sloForm" :rules="sloRules" label-position="top" v-loading="sloSubmitting">
        <el-form-item label="名称" prop="name"><el-input v-model="sloForm.name" /></el-form-item>
        <el-form-item label="端点" prop="endpoint"><el-input v-model="sloForm.endpoint" placeholder="/v1/chat/completions" /></el-form-item>
        <el-form-item label="目标 (如 99.9 表示 99.9%)" prop="target"><el-input-number v-model="sloForm.target" :min="0" :max="100" :precision="2" style="width: 100%" /></el-form-item>
        <el-form-item label="指标">
          <el-select v-model="sloForm.metric" style="width: 100%">
            <el-option label="可用率" value="availability" /><el-option label="P99 延迟" value="latency_p99" /><el-option label="错误率" value="error_rate" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="sloDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="sloSubmitting" @click="handleSubmitSlo">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { modelFallbackApi, modelLoadBalancerApi, apiHealthApi } from '@/api/client'

const activeTab = ref('fallback')

// ====== 容灾 ======
const fbLoading = ref(false)
const fallbackChains = ref([])
const testingId = ref(null)

async function loadFallback() {
  fbLoading.value = true
  try {
    const data = await modelFallbackApi.list()
    fallbackChains.value = data.items || []
  } catch (err) {
    ElMessage.error('加载容灾策略失败: ' + (err.message || ''))
  } finally {
    fbLoading.value = false
  }
}

const fallbackDialogVisible = ref(false)
const fbSubmitting = ref(false)
const fbFormRef = ref(null)
const fbForm = reactive({ name: '', modelsText: '', enabled: true })
const fbRules = { name: [{ required: true, message: '请输入名称', trigger: 'blur' }] }

function openFallbackDialog() {
  fbForm.name = ''
  fbForm.modelsText = ''
  fbForm.enabled = true
  fallbackDialogVisible.value = true
}

async function handleSubmitFallback() {
  if (!fbFormRef.value) return
  try { await fbFormRef.value.validate() } catch { return }
  const models = fbForm.modelsText.split('\n').map((s) => s.trim()).filter(Boolean)
  if (!models.length) { ElMessage.warning('请输入至少一个模型'); return }
  fbSubmitting.value = true
  try {
    await modelFallbackApi.create({ name: fbForm.name, models, enabled: fbForm.enabled })
    ElMessage.success('创建成功')
    fallbackDialogVisible.value = false
    await loadFallback()
  } catch (err) {
    ElMessage.error('创建失败: ' + (err.message || ''))
  } finally {
    fbSubmitting.value = false
  }
}

async function handleTestFallback(row) {
  testingId.value = row.id
  try {
    const data = await modelFallbackApi.test(row.id)
    ElMessage.success('测试完成: ' + (data.message || 'Fallback 链有效'))
  } catch (err) {
    ElMessage.error('测试失败: ' + (err.message || ''))
  } finally {
    testingId.value = null
  }
}

async function handleDeleteFallback(row) {
  try { await ElMessageBox.confirm(`确认删除容灾策略 "${row.name}"?`, '删除确认', { type: 'warning' }) } catch { return }
  try {
    await modelFallbackApi.delete(row.id)
    ElMessage.success('删除成功')
    await loadFallback()
  } catch (err) {
    ElMessage.error('删除失败: ' + (err.message || ''))
  }
}

// ====== 负载均衡 ======
const lbLoading = ref(false)
const instances = ref([])
const hcId = ref(null)

async function loadInstances() {
  lbLoading.value = true
  try {
    const data = await modelLoadBalancerApi.listInstances()
    instances.value = data.items || []
  } catch (err) {
    ElMessage.error('加载实例失败: ' + (err.message || ''))
  } finally {
    lbLoading.value = false
  }
}

async function handleHealthCheck(row) {
  hcId.value = row.id
  try {
    const data = await modelLoadBalancerApi.healthCheck(row.id)
    row.health = data.health || 'healthy'
    ElMessage.success('健康检查完成: ' + (data.health || 'healthy'))
  } catch (err) {
    ElMessage.error('健康检查失败: ' + (err.message || ''))
  } finally {
    hcId.value = null
  }
}

const lbConfigVisible = ref(false)
const lbConfigSubmitting = ref(false)
const lbTarget = ref(null)
const lbConfigForm = reactive({ weight: 1, enabled: true })

function openLbConfigDialog(row) {
  lbTarget.value = row
  lbConfigForm.weight = row.weight ?? 1
  lbConfigForm.enabled = row.enabled ?? true
  lbConfigVisible.value = true
}

async function handleSubmitLbConfig() {
  lbConfigSubmitting.value = true
  try {
    await modelLoadBalancerApi.updateConfig(lbTarget.value.id, { weight: lbConfigForm.weight, enabled: lbConfigForm.enabled })
    ElMessage.success('配置已更新')
    lbConfigVisible.value = false
    await loadInstances()
  } catch (err) {
    ElMessage.error('更新失败: ' + (err.message || ''))
  } finally {
    lbConfigSubmitting.value = false
  }
}

// ====== API 健康 ======
const sloLoading = ref(false)
const slos = ref([])
const endpointStats = ref({})
const endpointCards = [
  { key: 'total_endpoints', label: '端点总数' },
  { key: 'healthy', label: '健康端点' },
  { key: 'degraded', label: '降级端点' },
  { key: 'down', label: '不可用端点' },
]

async function loadSlos() {
  sloLoading.value = true
  try {
    const [sloData, epData] = await Promise.all([apiHealthApi.slos(), apiHealthApi.endpoints()])
    slos.value = sloData.items || []
    endpointStats.value = epData.summary || epData || {}
  } catch (err) {
    ElMessage.error('加载 SLO 失败: ' + (err.message || ''))
  } finally {
    sloLoading.value = false
  }
}

const sloDialogVisible = ref(false)
const sloSubmitting = ref(false)
const sloFormRef = ref(null)
const sloForm = reactive({ name: '', endpoint: '', target: 99.9, metric: 'availability' })
const sloRules = {
  name: [{ required: true, message: '请输入名称', trigger: 'blur' }],
  endpoint: [{ required: true, message: '请输入端点', trigger: 'blur' }],
}

function openSloDialog() {
  Object.assign(sloForm, { name: '', endpoint: '', target: 99.9, metric: 'availability' })
  sloDialogVisible.value = true
}

async function handleSubmitSlo() {
  if (!sloFormRef.value) return
  try { await sloFormRef.value.validate() } catch { return }
  sloSubmitting.value = true
  try {
    await apiHealthApi.createSlo({ name: sloForm.name, endpoint: sloForm.endpoint, target: sloForm.target, metric: sloForm.metric })
    ElMessage.success('创建成功')
    sloDialogVisible.value = false
    await loadSlos()
  } catch (err) {
    ElMessage.error('创建失败: ' + (err.message || ''))
  } finally {
    sloSubmitting.value = false
  }
}

async function handleDeleteSlo(row) {
  try { await ElMessageBox.confirm(`确认删除 SLO "${row.name}"?`, '删除确认', { type: 'warning' }) } catch { return }
  try {
    await apiHealthApi.deleteSlo(row.id)
    ElMessage.success('删除成功')
    await loadSlos()
  } catch (err) {
    ElMessage.error('删除失败: ' + (err.message || ''))
  }
}

function healthTagType(v) { return { healthy: 'success', degraded: 'warning', down: 'danger' }[v] || 'info' }
function healthLabel(v) { return { healthy: '健康', degraded: '降级', down: '不可用' }[v] || v || '—' }
function sloStatusType(row) {
  if (row.current == null || row.target == null) return 'info'
  return Number(row.current) >= Number(row.target) ? 'success' : 'danger'
}
function sloStatusLabel(row) {
  if (row.current == null || row.target == null) return '—'
  return Number(row.current) >= Number(row.target) ? '达标' : '未达标'
}

function onTabChange(name) {
  if (name === 'lb' && instances.value.length === 0) loadInstances()
  if (name === 'health' && slos.value.length === 0) loadSlos()
}

onMounted(() => { loadFallback() })
</script>

<style scoped>
.mb-16 { margin-bottom: 16px; }
.page-header { display: flex; justify-content: space-between; align-items: center; }
.page-title { display: inline-flex; align-items: center; gap: 8px; font-size: 18px; font-weight: 600; color: var(--el-text-color-primary); }
.toolbar { display: flex; gap: 12px; align-items: center; }
.section-title { font-weight: 600; color: var(--el-text-color-primary); }
.chain-cell { display: flex; flex-wrap: wrap; gap: 4px; }
.chain-tag { margin: 0; }
.stat-value { font-size: 24px; font-weight: 700; color: var(--el-text-color-primary); }
.stat-label { font-size: 13px; color: var(--el-text-color-secondary); margin-top: 4px; }
</style>
