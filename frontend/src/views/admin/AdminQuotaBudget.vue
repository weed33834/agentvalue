<template>
  <div class="admin-quota-budget av-fade-in-up">
    <div class="page-header mb-16">
      <div class="page-title">
        <el-icon><Coin /></el-icon>
        <span>配额与预算管理</span>
      </div>
    </div>

    <el-tabs v-model="activeTab" @tab-change="onTabChange">
      <!-- ============ 配额管理 ============ -->
      <el-tab-pane label="配额管理" name="quota">
        <div class="toolbar mb-16">
          <el-input v-model="quotaFilter.tenant" placeholder="按租户筛选" clearable style="width: 220px" @change="loadQuotas" />
          <el-button :loading="quotaLoading" @click="loadQuotas">
            <el-icon><RefreshLeft /></el-icon>刷新
          </el-button>
        </div>
        <el-card v-loading="quotaLoading">
          <el-table :data="quotas" stripe empty-text="暂无配额记录">
            <el-table-column prop="tenant_id" label="租户 ID" min-width="160" show-overflow-tooltip />
            <el-table-column prop="resource" label="资源" min-width="140" />
            <el-table-column label="用量 / 配额" min-width="220">
              <template #default="{ row }">
                <div class="usage-cell">
                  <el-progress
                    :percentage="usagePercent(row)"
                    :color="usageColor(usagePercent(row))"
                    :stroke-width="14"
                    :text-inside="true"
                  />
                  <span class="usage-text">{{ formatNum(row.used) }} / {{ formatNum(row.limit) }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="周期" width="120">
              <template #default="{ row }">{{ row.period || '—' }}</template>
            </el-table-column>
            <el-table-column label="重置时间" width="180">
              <template #default="{ row }">{{ formatTime(row.reset_at) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="120" fixed="right">
              <template #default="{ row }">
                <el-button
                  size="small" link type="warning"
                  :loading="resettingId === row.id"
                  @click="handleResetQuota(row)"
                >
                  重置
                </el-button>
              </template>
            </el-table-column>
          </el-table>
          <div class="pagination-wrapper">
            <el-pagination
              v-model:current-page="quotaPage"
              v-model:page-size="quotaPageSize"
              :total="quotaTotal"
              :page-sizes="[10, 20, 50]"
              layout="total, sizes, prev, pager, next, jumper"
              @size-change="loadQuotas"
              @current-change="loadQuotas"
            />
          </div>
        </el-card>
      </el-tab-pane>

      <!-- ============ 预算管理 ============ -->
      <el-tab-pane label="预算管理" name="budget">
        <div class="toolbar mb-16">
          <el-button :loading="budgetLoading" @click="loadBudgets">
            <el-icon><RefreshLeft /></el-icon>刷新
          </el-button>
          <el-button type="primary" @click="openBudgetDialog()">
            <el-icon><Plus /></el-icon>新建预算
          </el-button>
        </div>
        <el-card v-loading="budgetLoading">
          <el-table :data="budgets" stripe empty-text="暂无预算记录">
            <el-table-column prop="name" label="预算名称" min-width="160" show-overflow-tooltip />
            <el-table-column prop="tenant_id" label="租户" min-width="140" show-overflow-tooltip />
            <el-table-column label="预算额度" width="130">
              <template #default="{ row }">{{ formatNum(row.amount) }} {{ row.currency || '' }}</template>
            </el-table-column>
            <el-table-column label="已用 / 剩余" min-width="220">
              <template #default="{ row }">
                <div class="usage-cell">
                  <el-progress
                    :percentage="budgetPercent(row)"
                    :color="usageColor(budgetPercent(row))"
                    :stroke-width="14"
                    :text-inside="true"
                  />
                  <span class="usage-text">{{ formatNum(row.used) }} / {{ formatNum(row.remaining) }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="预警阈值" width="120">
              <template #default="{ row }">
                <el-tag size="small" :type="budgetPercent(row) >= (row.alert_threshold || 0) ? 'danger' : 'info'">
                  {{ row.alert_threshold != null ? row.alert_threshold + '%' : '—' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="110">
              <template #default="{ row }">
                <el-tag size="small" :type="budgetStatusType(row)">{{ budgetStatusLabel(row) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="160" fixed="right">
              <template #default="{ row }">
                <el-button size="small" link @click="openBudgetDialog(row)">编辑</el-button>
                <el-button size="small" link type="danger" @click="handleDeleteBudget(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div class="pagination-wrapper">
            <el-pagination
              v-model:current-page="budgetPage"
              v-model:page-size="budgetPageSize"
              :total="budgetTotal"
              :page-sizes="[10, 20, 50]"
              layout="total, sizes, prev, pager, next, jumper"
              @size-change="loadBudgets"
              @current-change="loadBudgets"
            />
          </div>
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <!-- 预算创建/编辑弹窗 -->
    <el-dialog v-model="budgetDialogVisible" :title="budgetIsEdit ? '编辑预算' : '新建预算'" width="560px" @closed="resetBudgetForm">
      <el-form ref="budgetFormRef" :model="budgetForm" :rules="budgetRules" label-position="top" v-loading="budgetSubmitting">
        <el-form-item label="预算名称" prop="name">
          <el-input v-model="budgetForm.name" placeholder="如 2026Q1 推理预算" />
        </el-form-item>
        <el-form-item label="租户 ID" prop="tenant_id">
          <el-input v-model="budgetForm.tenant_id" placeholder="留空表示全局预算" />
        </el-form-item>
        <el-form-item label="预算额度" prop="amount">
          <el-input-number v-model="budgetForm.amount" :min="0" :precision="2" style="width: 100%" />
        </el-form-item>
        <el-form-item label="币种">
          <el-input v-model="budgetForm.currency" placeholder="CNY / USD" />
        </el-form-item>
        <el-form-item label="预警阈值 (%)">
          <el-input-number v-model="budgetForm.alert_threshold" :min="0" :max="100" style="width: 100%" />
        </el-form-item>
        <el-form-item label="周期">
          <el-select v-model="budgetForm.period" style="width: 100%">
            <el-option label="月度" value="monthly" />
            <el-option label="季度" value="quarterly" />
            <el-option label="年度" value="yearly" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="budgetDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="budgetSubmitting" @click="handleSubmitBudget">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { quotaApi, budgetApi } from '@/api/client'

const activeTab = ref('quota')

// ====== 配额 ======
const quotaLoading = ref(false)
const quotas = ref([])
const quotaTotal = ref(0)
const quotaPage = ref(1)
const quotaPageSize = ref(20)
const quotaFilter = reactive({ tenant: '' })
const resettingId = ref(null)

async function loadQuotas() {
  quotaLoading.value = true
  try {
    const params = { page: quotaPage.value, page_size: quotaPageSize.value }
    if (quotaFilter.tenant) params.tenant_id = quotaFilter.tenant
    const data = await quotaApi.list(params)
    quotas.value = data.items || []
    quotaTotal.value = data.total || 0
  } catch (err) {
    ElMessage.error('加载配额列表失败: ' + (err.message || ''))
  } finally {
    quotaLoading.value = false
  }
}

async function handleResetQuota(row) {
  try {
    await ElMessageBox.confirm(`确认重置租户 ${row.tenant_id} 的 ${row.resource} 配额用量?`, '重置确认', { type: 'warning' })
  } catch {
    return
  }
  resettingId.value = row.id
  try {
    await quotaApi.reset(row.id)
    ElMessage.success('配额用量已重置')
    await loadQuotas()
  } catch (err) {
    ElMessage.error('重置配额失败: ' + (err.message || ''))
  } finally {
    resettingId.value = null
  }
}

// ====== 预算 ======
const budgetLoading = ref(false)
const budgets = ref([])
const budgetTotal = ref(0)
const budgetPage = ref(1)
const budgetPageSize = ref(20)

async function loadBudgets() {
  budgetLoading.value = true
  try {
    const data = await budgetApi.list({ page: budgetPage.value, page_size: budgetPageSize.value })
    budgets.value = data.items || []
    budgetTotal.value = data.total || 0
  } catch (err) {
    ElMessage.error('加载预算列表失败: ' + (err.message || ''))
  } finally {
    budgetLoading.value = false
  }
}

const budgetDialogVisible = ref(false)
const budgetSubmitting = ref(false)
const budgetIsEdit = ref(false)
const budgetFormRef = ref(null)
const budgetForm = reactive({ id: null, name: '', tenant_id: '', amount: 0, currency: 'CNY', alert_threshold: 80, period: 'monthly' })
const budgetRules = {
  name: [{ required: true, message: '请输入预算名称', trigger: 'blur' }],
  amount: [{ required: true, message: '请输入预算额度', trigger: 'blur' }],
}

function openBudgetDialog(row = null) {
  budgetIsEdit.value = !!row
  if (row) {
    Object.assign(budgetForm, { id: row.id, name: row.name, tenant_id: row.tenant_id || '', amount: row.amount || 0, currency: row.currency || 'CNY', alert_threshold: row.alert_threshold ?? 80, period: row.period || 'monthly' })
  } else {
    resetBudgetForm()
  }
  budgetDialogVisible.value = true
}

function resetBudgetForm() {
  Object.assign(budgetForm, { id: null, name: '', tenant_id: '', amount: 0, currency: 'CNY', alert_threshold: 80, period: 'monthly' })
  budgetFormRef.value?.clearValidate?.()
}

async function handleSubmitBudget() {
  if (!budgetFormRef.value) return
  try { await budgetFormRef.value.validate() } catch { return }
  budgetSubmitting.value = true
  const payload = {
    name: budgetForm.name,
    tenant_id: budgetForm.tenant_id || null,
    amount: budgetForm.amount,
    currency: budgetForm.currency,
    alert_threshold: budgetForm.alert_threshold,
    period: budgetForm.period,
  }
  try {
    if (budgetIsEdit.value) {
      await budgetApi.update(budgetForm.id, payload)
      ElMessage.success('更新成功')
    } else {
      await budgetApi.create(payload)
      ElMessage.success('创建成功')
    }
    budgetDialogVisible.value = false
    await loadBudgets()
  } catch (err) {
    ElMessage.error('保存预算失败: ' + (err.message || ''))
  } finally {
    budgetSubmitting.value = false
  }
}

async function handleDeleteBudget(row) {
  try {
    await ElMessageBox.confirm(`确认删除预算 "${row.name}"?`, '删除确认', { type: 'warning' })
  } catch { return }
  try {
    await budgetApi.delete(row.id)
    ElMessage.success('删除成功')
    await loadBudgets()
  } catch (err) {
    ElMessage.error('删除预算失败: ' + (err.message || ''))
  }
}

// ====== 工具函数 ======
function usagePercent(row) {
  const limit = Number(row.limit) || 0
  if (limit <= 0) return 0
  return Math.min(100, Math.round((Number(row.used) || 0) / limit * 100))
}
function budgetPercent(row) {
  const amount = Number(row.amount) || 0
  if (amount <= 0) return 0
  return Math.min(100, Math.round((Number(row.used) || 0) / amount * 100))
}
function usageColor(pct) {
  if (pct >= 90) return '#f56c6c'
  if (pct >= 70) return '#e6a23c'
  return '#67c23a'
}
function budgetStatusType(row) {
  const pct = budgetPercent(row)
  if (pct >= 100) return 'danger'
  if (pct >= (row.alert_threshold || 80)) return 'warning'
  return 'success'
}
function budgetStatusLabel(row) {
  const pct = budgetPercent(row)
  if (pct >= 100) return '已超支'
  if (pct >= (row.alert_threshold || 80)) return '预警'
  return '正常'
}
function formatNum(n) {
  if (n == null) return '—'
  return Number(n).toLocaleString('zh-CN')
}
function formatTime(iso) {
  if (!iso) return '—'
  try { return new Date(iso).toLocaleString('zh-CN', { hour12: false }) } catch { return iso }
}

function onTabChange(name) {
  if (name === 'quota' && quotas.value.length === 0) loadQuotas()
  if (name === 'budget' && budgets.value.length === 0) loadBudgets()
}

onMounted(() => {
  loadQuotas()
})
</script>

<style scoped>
.mb-16 { margin-bottom: 16px; }
.page-header { display: flex; justify-content: space-between; align-items: center; }
.page-title { display: inline-flex; align-items: center; gap: 8px; font-size: 18px; font-weight: 600; color: var(--el-text-color-primary); }
.toolbar { display: flex; gap: 12px; align-items: center; }
.pagination-wrapper { margin-top: 16px; display: flex; justify-content: flex-end; }
.usage-cell { display: flex; flex-direction: column; gap: 4px; }
.usage-text { font-size: 12px; color: var(--el-text-color-secondary); }
</style>
