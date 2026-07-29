<template>
  <div class="admin-scheduler av-fade-in-up">
    <div class="page-header mb-16">
      <div class="page-title">
        <el-icon><Timer /></el-icon>
        <span>定时任务管理</span>
      </div>
      <div class="toolbar-actions">
        <el-button :loading="loading" @click="loadData">
          <el-icon><RefreshLeft /></el-icon>刷新
        </el-button>
        <el-button type="primary" @click="openDialog()">
          <el-icon><Plus /></el-icon>新建任务
        </el-button>
      </div>
    </div>

    <el-card v-loading="loading">
      <el-table :data="tasks" stripe empty-text="暂无定时任务">
        <el-table-column prop="id" label="ID" width="70" align="center" />
        <el-table-column prop="name" label="任务名称" min-width="160" show-overflow-tooltip />
        <el-table-column label="Cron 表达式" width="160">
          <template #default="{ row }"><code class="mono">{{ row.cron }}</code></template>
        </el-table-column>
        <el-table-column prop="task_type" label="类型" width="140" />
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '运行中' : '已停用' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="上次运行" width="180">
          <template #default="{ row }">
            <span v-if="row.last_run_at">{{ formatTime(row.last_run_at) }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="上次结果" width="110">
          <template #default="{ row }">
            <el-tag v-if="row.last_status" size="small" :type="lastStatusType(row.last_status)">{{ row.last_status }}</el-tag>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="240" fixed="right">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click="openDialog(row)">编辑</el-button>
            <el-button size="small" link type="success" :loading="triggeringId === row.id" @click="handleTrigger(row)">手动触发</el-button>
            <el-button size="small" link @click="openHistory(row)">历史</el-button>
            <el-button size="small" link type="danger" @click="handleDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="pagination-wrapper">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next, jumper"
          @size-change="loadData"
          @current-change="loadData"
        />
      </div>
    </el-card>

    <!-- 创建/编辑弹窗 -->
    <el-dialog v-model="dialogVisible" :title="isEdit ? '编辑任务' : '新建任务'" width="600px" @closed="resetForm">
      <el-form ref="formRef" :model="form" :rules="formRules" label-position="top" v-loading="submitting">
        <el-form-item label="任务名称" prop="name">
          <el-input v-model="form.name" placeholder="如 每日模型健康检查" />
        </el-form-item>
        <el-form-item label="Cron 表达式" prop="cron">
          <el-input v-model="form.cron" placeholder="如 0 2 * * *（每天凌晨2点）" />
        </el-form-item>
        <el-form-item label="任务类型" prop="task_type">
          <el-select v-model="form.task_type" style="width: 100%">
            <el-option label="模型健康检查" value="health_check" />
            <el-option label="数据同步" value="data_sync" />
            <el-option label="报表生成" value="report" />
            <el-option label="清理任务" value="cleanup" />
            <el-option label="自定义" value="custom" />
          </el-select>
        </el-form-item>
        <el-form-item label="参数 (JSON)">
          <el-input v-model="form.params" type="textarea" :rows="4" placeholder="{&quot;key&quot;: &quot;value&quot;}" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="handleSubmit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 运行历史弹窗 -->
    <el-dialog v-model="historyVisible" :title="`运行历史 - ${historyTarget?.name || ''}`" width="720px">
      <el-table :data="history" v-loading="historyLoading" stripe empty-text="暂无运行记录" max-height="420">
        <el-table-column prop="id" label="ID" width="70" align="center" />
        <el-table-column label="开始时间" width="180">
          <template #default="{ row }">{{ formatTime(row.started_at) }}</template>
        </el-table-column>
        <el-table-column label="结束时间" width="180">
          <template #default="{ row }">{{ formatTime(row.finished_at) }}</template>
        </el-table-column>
        <el-table-column label="耗时" width="110">
          <template #default="{ row }">{{ row.duration_ms ? row.duration_ms + 'ms' : '—' }}</template>
        </el-table-column>
        <el-table-column label="结果" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="lastStatusType(row.status)">{{ row.status || '—' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="message" label="消息" min-width="160" show-overflow-tooltip />
      </el-table>
      <template #footer>
        <el-button @click="historyVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { schedulerApi } from '@/api/client'

const loading = ref(false)
const tasks = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const triggeringId = ref(null)

async function loadData() {
  loading.value = true
  try {
    const data = await schedulerApi.list({ page: page.value, page_size: pageSize.value })
    tasks.value = data.items || []
    total.value = data.total || 0
  } catch (err) {
    ElMessage.error('加载任务列表失败: ' + (err.message || ''))
  } finally {
    loading.value = false
  }
}

// ====== 创建/编辑 ======
const dialogVisible = ref(false)
const submitting = ref(false)
const isEdit = ref(false)
const formRef = ref(null)
const form = reactive({ id: null, name: '', cron: '', task_type: 'health_check', params: '', enabled: true })
const formRules = {
  name: [{ required: true, message: '请输入任务名称', trigger: 'blur' }],
  cron: [{ required: true, message: '请输入 Cron 表达式', trigger: 'blur' }],
  task_type: [{ required: true, message: '请选择任务类型', trigger: 'change' }],
}

function openDialog(row = null) {
  isEdit.value = !!row
  if (row) {
    Object.assign(form, { id: row.id, name: row.name, cron: row.cron, task_type: row.task_type, params: row.params ? JSON.stringify(row.params, null, 2) : '', enabled: !!row.enabled })
  } else {
    resetForm()
  }
  dialogVisible.value = true
}

function resetForm() {
  Object.assign(form, { id: null, name: '', cron: '', task_type: 'health_check', params: '', enabled: true })
  formRef.value?.clearValidate?.()
}

async function handleSubmit() {
  if (!formRef.value) return
  try { await formRef.value.validate() } catch { return }
  let parsedParams = {}
  if (form.params.trim()) {
    try { parsedParams = JSON.parse(form.params) }
    catch { ElMessage.error('参数不是合法的 JSON'); return }
  }
  submitting.value = true
  const payload = { name: form.name, cron: form.cron, task_type: form.task_type, params: parsedParams, enabled: form.enabled }
  try {
    if (isEdit.value) {
      await schedulerApi.update(form.id, payload)
      ElMessage.success('更新成功')
    } else {
      await schedulerApi.create(payload)
      ElMessage.success('创建成功')
    }
    dialogVisible.value = false
    await loadData()
  } catch (err) {
    ElMessage.error('保存失败: ' + (err.message || ''))
  } finally {
    submitting.value = false
  }
}

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(`确认删除任务 "${row.name}"?`, '删除确认', { type: 'warning' })
  } catch { return }
  try {
    await schedulerApi.delete(row.id)
    ElMessage.success('删除成功')
    await loadData()
  } catch (err) {
    ElMessage.error('删除失败: ' + (err.message || ''))
  }
}

async function handleTrigger(row) {
  triggeringId.value = row.id
  try {
    await schedulerApi.trigger(row.id)
    ElMessage.success('任务已手动触发')
    await loadData()
  } catch (err) {
    ElMessage.error('触发失败: ' + (err.message || ''))
  } finally {
    triggeringId.value = null
  }
}

// ====== 历史 ======
const historyVisible = ref(false)
const historyLoading = ref(false)
const history = ref([])
const historyTarget = ref(null)

async function openHistory(row) {
  historyTarget.value = row
  historyVisible.value = true
  historyLoading.value = true
  history.value = []
  try {
    const data = await schedulerApi.history(row.id, { page: 1, page_size: 50 })
    history.value = data.items || []
  } catch (err) {
    ElMessage.error('加载运行历史失败: ' + (err.message || ''))
  } finally {
    historyLoading.value = false
  }
}

function lastStatusType(v) {
  return { success: 'success', failed: 'danger', running: 'warning', timeout: 'danger' }[v] || 'info'
}
function formatTime(iso) {
  if (!iso) return '—'
  try { return new Date(iso).toLocaleString('zh-CN', { hour12: false }) } catch { return iso }
}

onMounted(() => { loadData() })
</script>

<style scoped>
.mb-16 { margin-bottom: 16px; }
.page-header { display: flex; justify-content: space-between; align-items: center; }
.page-title { display: inline-flex; align-items: center; gap: 8px; font-size: 18px; font-weight: 600; color: var(--el-text-color-primary); }
.toolbar-actions { display: flex; gap: 12px; align-items: center; }
.pagination-wrapper { margin-top: 16px; display: flex; justify-content: flex-end; }
.muted { color: var(--el-text-color-placeholder); }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; color: #2563eb; }
</style>
