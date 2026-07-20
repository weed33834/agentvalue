<template>
  <div class="admin-audit-logs">
    <el-card v-loading="loading" :aria-busy="loading">
      <template #header>
        <div class="card-header">
          <span>审计日志</span>
          <el-button size="small" :loading="loading" @click="loadData">刷新</el-button>
        </div>
      </template>

      <el-form :inline="true" class="filter-form">
        <el-form-item label="操作人">
          <el-input
            v-model="filters.actor_id"
            placeholder="按操作人ID筛选"
            clearable
            @keyup.enter="handleSearch"
          />
        </el-form-item>
        <el-form-item label="动作类型">
          <el-select v-model="filters.action" placeholder="全部动作" clearable style="width: 200px">
            <el-option v-for="act in actionOptions" :key="act" :label="act" :value="act" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="handleSearch">查询</el-button>
          <el-button @click="handleReset">重置</el-button>
        </el-form-item>
      </el-form>

      <el-table :data="logs" style="width: 100%" empty-text="暂无审计日志">
        <el-table-column prop="created_at" label="时间" width="200" />
        <el-table-column prop="actor_id" label="操作人" width="140" />
        <el-table-column prop="action" label="动作" width="220">
          <template #default="{ row }">
            <el-tag size="small">{{ row.action }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column
          prop="evaluation_id"
          label="评估ID"
          min-width="180"
          show-overflow-tooltip
        />
        <el-table-column prop="employee_id" label="员工ID" width="140" />
        <el-table-column prop="ip_address" label="IP地址" width="160" />
      </el-table>

      <div class="pagination-wrapper">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next, jumper"
          @size-change="handleSearch"
          @current-change="loadData"
        />
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { reactive, ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { adminApi } from '@/api/client'

const loading = ref(false)
const logs = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)

const filters = reactive({
  actor_id: '',
  action: '',
})

const actionOptions = [
  'create_input',
  'create_evaluation_async',
  'approve_evaluation',
  'reject_evaluation',
  'request_hr_review',
  'appeal_evaluation',
  'create_feedback',
  're_evaluate',
]

async function loadData() {
  loading.value = true
  try {
    const params = {
      page: page.value,
      page_size: pageSize.value,
    }
    if (filters.actor_id.trim()) params.actor_id = filters.actor_id.trim()
    if (filters.action) params.action = filters.action
    const data = await adminApi.auditLogs(params)
    logs.value = data.logs || []
    total.value = data.total || 0
  } catch (err) {
    console.error('加载审计日志失败:', err)
    ElMessage.error('加载审计日志失败')
  } finally {
    loading.value = false
  }
}

function handleSearch() {
  page.value = 1
  loadData()
}

function handleReset() {
  filters.actor_id = ''
  filters.action = ''
  page.value = 1
  loadData()
}

onMounted(loadData)
</script>

<style scoped>
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.filter-form {
  margin-bottom: 12px;
}
.pagination-wrapper {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}
</style>
