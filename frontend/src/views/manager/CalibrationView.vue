<template>
  <div class="calibration-view">
    <!-- ============ 顶部: 校准会列表 + 创建 ============ -->
    <el-card v-if="!currentSession">
      <template #header>
        <div class="card-header">
          <span>校准会列表</span>
          <el-button type="primary" size="small" @click="showCreateDialog = true">
            新建校准会
          </el-button>
        </div>
      </template>

      <el-form :inline="true" class="filter-form">
        <el-form-item label="周期">
          <el-input
            v-model="listFilter.period"
            placeholder="例如 2026-Q2"
            style="width: 180px"
            clearable
            @keyup.enter="loadList"
          />
        </el-form-item>
        <el-form-item label="状态">
          <el-select v-model="listFilter.status" placeholder="全部" clearable style="width: 140px">
            <el-option label="已排期" value="scheduled" />
            <el-option label="进行中" value="in_progress" />
            <el-option label="已完成" value="completed" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="listLoading" @click="loadList">查询</el-button>
        </el-form-item>
      </el-form>

      <el-table
        :data="sessionList"
        v-loading="listLoading"
        style="width: 100%"
        empty-text="暂无校准会"
      >
        <el-table-column prop="session_id" label="校准会ID" width="180" />
        <el-table-column prop="title" label="标题" min-width="180" />
        <el-table-column prop="period" label="周期" width="120" />
        <el-table-column prop="facilitator_id" label="主持人" width="120" />
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="statusTagType(row.status)">
              {{ statusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="参与者" width="120">
          <template #default="{ row }">
            <span class="muted">{{ (row.participants || []).length }} 人</span>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" min-width="160">
          <template #default="{ row }">
            <span class="muted">{{ formatTime(row.created_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="160" fixed="right">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click="openSession(row.session_id)">
              进入详情
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- ============ 校准会详情: 左侧评估列表 + 右侧调整面板 ============ -->
    <div v-else class="session-detail">
      <el-page-header @back="backToList" class="mb-16">
        <template #content>
          <span class="page-title">
            {{ currentSession.title }}
            <el-tag
              size="small"
              :type="statusTagType(currentSession.status)"
              style="margin-left: 8px"
            >
              {{ statusLabel(currentSession.status) }}
            </el-tag>
          </span>
        </template>
        <template #extra>
          <div class="header-actions">
            <span class="muted"
              >周期: {{ currentSession.period }} | 主持人: {{ currentSession.facilitator_id }}</span
            >
            <el-button
              v-if="currentSession.status !== 'completed'"
              type="success"
              :loading="completing"
              :disabled="!canComplete"
              @click="completeSession"
              style="margin-left: 12px"
            >
              完成校准（应用分数）
            </el-button>
            <el-button v-else type="info" disabled style="margin-left: 12px"> 已完成 </el-button>
          </div>
        </template>
      </el-page-header>

      <el-row :gutter="20">
        <!-- 左侧: 校准项列表 -->
        <el-col :span="14">
          <el-card v-loading="detailLoading">
            <template #header>
              <div class="card-header">
                <span>校准项列表（{{ currentSession.item_count || 0 }}）</span>
                <div>
                  <el-button
                    size="small"
                    type="primary"
                    :disabled="currentSession.status === 'completed'"
                    @click="showAddItemDialog = true"
                  >
                    添加校准项
                  </el-button>
                  <el-button
                    size="small"
                    :disabled="currentSession.status === 'completed' || !selectedItems.length"
                    @click="showBatchAdjust = true"
                  >
                    批量调整 ({{ selectedItems.length }})
                  </el-button>
                </div>
              </div>
            </template>

            <el-table
              :data="currentSession.items || []"
              style="width: 100%"
              empty-text="暂无校准项"
              @selection-change="onSelectionChange"
              @row-click="onRowClick"
              row-key="item_id"
            >
              <el-table-column type="selection" width="40" :selectable="canSelectRow" />
              <el-table-column prop="employee_id" label="员工" width="110" />
              <el-table-column prop="evaluation_id" label="评估ID" width="180" />
              <el-table-column label="原始分" width="90">
                <template #default="{ row }">
                  <span>{{ row.original_score }}</span>
                </template>
              </el-table-column>
              <el-table-column label="校准分" width="100">
                <template #default="{ row }">
                  <span v-if="row.calibrated_score != null" :class="deltaClass(row)">
                    {{ row.calibrated_score }}
                  </span>
                  <span v-else class="muted">-</span>
                </template>
              </el-table-column>
              <el-table-column label="变化" width="80">
                <template #default="{ row }">
                  <el-tag v-if="row.delta != null" size="small" :type="deltaTagType(row.delta)">
                    {{ row.delta > 0 ? '+' : '' }}{{ row.delta }}
                  </el-tag>
                  <span v-else class="muted">-</span>
                </template>
              </el-table-column>
              <el-table-column label="调整原因" min-width="200">
                <template #default="{ row }">
                  <span class="muted"
                    >{{ (row.adjustment_reason || '').slice(0, 60)
                    }}{{ (row.adjustment_reason || '').length > 60 ? '...' : '' }}</span
                  >
                </template>
              </el-table-column>
              <el-table-column label="已应用" width="80">
                <template #default="{ row }">
                  <el-tag v-if="row.applied" size="small" type="info">已应用</el-tag>
                  <el-tag v-else size="small" type="warning">待应用</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="120" fixed="right">
                <template #default="{ row }">
                  <el-button
                    size="small"
                    link
                    type="primary"
                    :disabled="row.applied || currentSession.status === 'completed'"
                    @click.stop="openAdjustDialog(row)"
                  >
                    调整
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
          </el-card>
        </el-col>

        <!-- 右侧: 调整面板 -->
        <el-col :span="10">
          <el-card>
            <template #header><span>调整面板</span></template>
            <template v-if="adjustTarget">
              <el-descriptions :column="1" border size="small">
                <el-descriptions-item label="员工">{{
                  adjustTarget.employee_id
                }}</el-descriptions-item>
                <el-descriptions-item label="评估ID">{{
                  adjustTarget.evaluation_id
                }}</el-descriptions-item>
                <el-descriptions-item label="原始分">{{
                  adjustTarget.original_score
                }}</el-descriptions-item>
                <el-descriptions-item label="当前校准分">
                  {{ adjustTarget.calibrated_score ?? '-' }}
                </el-descriptions-item>
              </el-descriptions>

              <el-form label-width="100px" style="margin-top: 16px">
                <el-form-item label="校准分数" required>
                  <el-input-number
                    v-model="adjustForm.calibrated_score"
                    :min="0"
                    :max="100"
                    :step="0.5"
                    style="width: 200px"
                  />
                  <span class="muted" style="margin-left: 8px; font-size: 12px">
                    变化: {{ deltaPreview }}
                  </span>
                </el-form-item>
                <el-form-item label="调整原因">
                  <el-input
                    v-model="adjustForm.adjustment_reason"
                    type="textarea"
                    :rows="4"
                    placeholder="请输入调整原因, 最多 5000 字"
                    maxlength="5000"
                    show-word-limit
                  />
                </el-form-item>
                <el-form-item>
                  <el-button
                    type="primary"
                    :loading="adjusting"
                    :disabled="adjustTarget.applied || currentSession.status === 'completed'"
                    @click="applyAdjust"
                  >
                    应用调整
                  </el-button>
                  <el-button @click="adjustTarget = null">取消</el-button>
                </el-form-item>
              </el-form>
            </template>
            <el-empty v-else description="点击左侧列表中的「调整」按钮, 在此调整分数" />
          </el-card>
        </el-col>
      </el-row>
    </div>

    <!-- ============ 创建校准会对话框 ============ -->
    <el-dialog v-model="showCreateDialog" title="新建校准会" width="560px">
      <el-form :model="createForm" label-width="100px">
        <el-form-item label="标题" required>
          <el-input
            v-model="createForm.title"
            placeholder="例如 2026 Q2 研发部校准会"
            maxlength="256"
          />
        </el-form-item>
        <el-form-item label="周期" required>
          <el-input
            v-model="createForm.period"
            placeholder="例如 2026-Q2 / 2026-W25"
            maxlength="32"
          />
        </el-form-item>
        <el-form-item label="参与者">
          <el-input
            v-model="createForm.participantsInput"
            type="textarea"
            :rows="3"
            placeholder="参与者员工ID, 逗号分隔, 例如 M001,HR001,E1001"
          />
        </el-form-item>
        <el-form-item label="备注">
          <el-input
            v-model="createForm.notes"
            type="textarea"
            :rows="3"
            placeholder="会议纪要或备注"
            maxlength="5000"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showCreateDialog = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="createSession">创建</el-button>
      </template>
    </el-dialog>

    <!-- ============ 添加校准项对话框 ============ -->
    <el-dialog v-model="showAddItemDialog" title="添加校准项" width="560px">
      <el-form label-width="100px">
        <el-form-item label="评估ID" required>
          <el-input
            v-model="addItemForm.evaluationId"
            placeholder="单个评估ID, 例如 EVAL-XXXX"
            style="width: 320px"
          />
          <el-button
            type="primary"
            size="small"
            style="margin-left: 8px"
            :loading="addingItem"
            @click="addItem"
          >
            添加
          </el-button>
        </el-form-item>
      </el-form>

      <el-divider content-position="center">或批量添加</el-divider>

      <el-form label-width="100px">
        <el-form-item label="评估ID列表">
          <el-input
            v-model="addItemForm.evaluationIdsInput"
            type="textarea"
            :rows="4"
            placeholder="多个评估ID, 一行一个或逗号分隔"
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="addingBatch" @click="batchAddItems">
            批量添加
          </el-button>
        </el-form-item>
      </el-form>
    </el-dialog>

    <!-- ============ 批量调整对话框 ============ -->
    <el-dialog v-model="showBatchAdjust" title="批量调整校准分" width="640px">
      <el-alert
        title="所有选中项将统一调整为下面的分数, 各项原有的调整原因会被覆盖"
        type="warning"
        :closable="false"
        show-icon
        style="margin-bottom: 16px"
      />
      <el-form label-width="100px">
        <el-form-item label="选中项数">
          <el-tag>{{ selectedItems.length }}</el-tag>
        </el-form-item>
        <el-form-item label="统一校准分" required>
          <el-input-number v-model="batchForm.calibrated_score" :min="0" :max="100" :step="0.5" />
        </el-form-item>
        <el-form-item label="调整原因">
          <el-input
            v-model="batchForm.adjustment_reason"
            type="textarea"
            :rows="3"
            placeholder="批量调整原因"
            maxlength="5000"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showBatchAdjust = false">取消</el-button>
        <el-button type="primary" :loading="batching" @click="applyBatchAdjust">
          应用批量调整
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { calibrationApi } from '@/api/client'

// ============ 列表与筛选 ============
const listLoading = ref(false)
const sessionList = ref([])
const listFilter = ref({ period: '', status: '' })

const currentSession = ref(null)
const detailLoading = ref(false)

async function loadList() {
  listLoading.value = true
  try {
    const params = {}
    if (listFilter.value.period) params.period = listFilter.value.period
    if (listFilter.value.status) params.status_filter = listFilter.value.status
    const res = await calibrationApi.list(params)
    sessionList.value = res.items || []
  } catch (err) {
    ElMessage.error(err.message || '加载校准会列表失败')
  } finally {
    listLoading.value = false
  }
}

async function openSession(sessionId) {
  currentSession.value = { session_id: sessionId, title: '加载中...', status: 'scheduled' }
  await loadSessionDetail(sessionId)
}

async function loadSessionDetail(sessionId) {
  detailLoading.value = true
  try {
    currentSession.value = await calibrationApi.get(sessionId)
  } catch (err) {
    ElMessage.error(err.message || '加载校准会详情失败')
    currentSession.value = null
  } finally {
    detailLoading.value = false
  }
}

function backToList() {
  currentSession.value = null
  loadList()
}

// ============ 创建校准会 ============
const showCreateDialog = ref(false)
const creating = ref(false)
const createForm = ref({
  title: '',
  period: '',
  participantsInput: '',
  notes: '',
})

async function createSession() {
  if (!createForm.value.title.trim() || !createForm.value.period.trim()) {
    ElMessage.warning('请填写标题和周期')
    return
  }
  creating.value = true
  try {
    const participants = createForm.value.participantsInput
      .split(/[,\n]/)
      .map((s) => s.trim())
      .filter(Boolean)
    const result = await calibrationApi.create({
      title: createForm.value.title.trim(),
      period: createForm.value.period.trim(),
      participants,
      notes: createForm.value.notes || null,
    })
    ElMessage.success('校准会已创建')
    showCreateDialog.value = false
    createForm.value = { title: '', period: '', participantsInput: '', notes: '' }
    await loadList()
    // 直接进入新建的校准会
    await openSession(result.session_id)
  } catch (err) {
    ElMessage.error(err.message || '创建校准会失败')
  } finally {
    creating.value = false
  }
}

// ============ 添加校准项 ============
const showAddItemDialog = ref(false)
const addingItem = ref(false)
const addingBatch = ref(false)
const addItemForm = ref({
  evaluationId: '',
  evaluationIdsInput: '',
})

async function addItem() {
  if (!addItemForm.value.evaluationId.trim()) {
    ElMessage.warning('请输入评估ID')
    return
  }
  addingItem.value = true
  try {
    await calibrationApi.addItem(
      currentSession.value.session_id,
      addItemForm.value.evaluationId.trim(),
    )
    ElMessage.success('校准项已添加')
    addItemForm.value.evaluationId = ''
    await loadSessionDetail(currentSession.value.session_id)
  } catch (err) {
    ElMessage.error(err.message || '添加校准项失败')
  } finally {
    addingItem.value = false
  }
}

async function batchAddItems() {
  const ids = addItemForm.value.evaluationIdsInput
    .split(/[,\n]/)
    .map((s) => s.trim())
    .filter(Boolean)
  if (!ids.length) {
    ElMessage.warning('请输入至少一个评估ID')
    return
  }
  addingBatch.value = true
  try {
    const res = await calibrationApi.batchAddItems(currentSession.value.session_id, ids)
    ElMessage.success(`已添加 ${res.created_count} 项, 跳过 ${res.skipped_count} 项`)
    addItemForm.value.evaluationIdsInput = ''
    await loadSessionDetail(currentSession.value.session_id)
  } catch (err) {
    ElMessage.error(err.message || '批量添加失败')
  } finally {
    addingBatch.value = false
  }
}

// ============ 单个调整 ============
const adjustTarget = ref(null)
const adjusting = ref(false)
const adjustForm = ref({
  calibrated_score: 80,
  adjustment_reason: '',
})

function openAdjustDialog(row) {
  adjustTarget.value = row
  adjustForm.value.calibrated_score = row.calibrated_score ?? row.original_score
  adjustForm.value.adjustment_reason = row.adjustment_reason || ''
}

const deltaPreview = computed(() => {
  if (!adjustTarget.value) return ''
  const delta = Number(adjustForm.value.calibrated_score) - adjustTarget.value.original_score
  return `${delta > 0 ? '+' : ''}${delta.toFixed(2)}`
})

async function applyAdjust() {
  if (!adjustTarget.value) return
  adjusting.value = true
  try {
    await calibrationApi.adjustItem(currentSession.value.session_id, adjustTarget.value.item_id, {
      calibrated_score: Number(adjustForm.value.calibrated_score),
      adjustment_reason: adjustForm.value.adjustment_reason || null,
    })
    ElMessage.success('调整已保存')
    adjustTarget.value = null
    await loadSessionDetail(currentSession.value.session_id)
  } catch (err) {
    ElMessage.error(err.message || '调整失败')
  } finally {
    adjusting.value = false
  }
}

// ============ 批量调整 ============
const selectedItems = ref([])
const showBatchAdjust = ref(false)
const batching = ref(false)
const batchForm = ref({
  calibrated_score: 80,
  adjustment_reason: '',
})

function onSelectionChange(rows) {
  selectedItems.value = rows
}

function canSelectRow(row) {
  return !row.applied && currentSession.value?.status !== 'completed'
}

function onRowClick(_row) {
  // 点击行不自动打开调整, 避免与选择冲突
}

async function applyBatchAdjust() {
  if (!selectedItems.value.length) {
    ElMessage.warning('请先选择校准项')
    return
  }
  batching.value = true
  try {
    const items = selectedItems.value.map((it) => ({
      item_id: it.item_id,
      calibrated_score: Number(batchForm.value.calibrated_score),
      adjustment_reason: batchForm.value.adjustment_reason || null,
    }))
    const res = await calibrationApi.batchAdjustItems(currentSession.value.session_id, items)
    ElMessage.success(`已调整 ${res.adjusted_count} 项, 跳过 ${res.skipped_count} 项`)
    showBatchAdjust.value = false
    await loadSessionDetail(currentSession.value.session_id)
  } catch (err) {
    ElMessage.error(err.message || '批量调整失败')
  } finally {
    batching.value = false
  }
}

// ============ 完成校准 ============
const completing = ref(false)

const canComplete = computed(() => {
  if (!currentSession.value) return false
  if (currentSession.value.status === 'completed') return false
  const items = currentSession.value.items || []
  return items.length > 0
})

async function completeSession() {
  try {
    await ElMessageBox.confirm(
      '完成校准将把所有调整后的分数应用回 Evaluation, 此操作不可撤销。确认完成？',
      '确认完成校准',
      { confirmButtonText: '确认完成', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return // 用户取消
  }
  completing.value = true
  try {
    const res = await calibrationApi.complete(currentSession.value.session_id)
    ElMessage.success(`校准会已完成, 应用 ${res.applied_count} 项分数调整`)
    await loadSessionDetail(currentSession.value.session_id)
  } catch (err) {
    ElMessage.error(err.message || '完成校准失败')
  } finally {
    completing.value = false
  }
}

// ============ 通用工具 ============
function statusLabel(s) {
  return { scheduled: '已排期', in_progress: '进行中', completed: '已完成' }[s] || s
}

function statusTagType(s) {
  return { scheduled: 'info', in_progress: 'warning', completed: 'success' }[s] || 'info'
}

function deltaClass(row) {
  if (row.delta == null) return ''
  if (row.delta > 0) return 'delta-up'
  if (row.delta < 0) return 'delta-down'
  return 'delta-flat'
}

function deltaTagType(delta) {
  if (delta > 0) return 'success'
  if (delta < 0) return 'danger'
  return 'info'
}

function formatTime(iso) {
  if (!iso) return '-'
  try {
    return new Date(iso).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

onMounted(() => {
  loadList()
})
</script>

<style scoped>
.mb-16 {
  margin-bottom: 16px;
}
.mt-20 {
  margin-top: 20px;
}
.muted {
  color: #909399;
  font-size: 13px;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.filter-form {
  margin-bottom: 12px;
}
.page-title {
  font-size: 16px;
  font-weight: 600;
}
.header-actions {
  display: flex;
  align-items: center;
}
.delta-up {
  color: #67c23a;
  font-weight: 600;
}
.delta-down {
  color: #f56c6c;
  font-weight: 600;
}
.delta-flat {
  color: #909399;
}
</style>
