<template>
  <div class="hr-dashboard">
    <el-row :gutter="20">
      <el-col :span="24">
        <el-card v-loading="loading" :aria-busy="loading">
          <template #header>
            <div class="card-header">
              <span>待复核队列（{{ evaluations.length }}）</span>
              <el-button size="small" :loading="loading" @click="loadData">刷新</el-button>
            </div>
          </template>
          <el-table :data="evaluations" style="width: 100%" empty-text="暂无待复核评估">
            <el-table-column prop="employee_id" label="员工ID" />
            <el-table-column prop="period" label="周期" />
            <el-table-column prop="overall_score" label="综合得分" sortable />
            <el-table-column label="风险标记">
              <template #default="{ row }">
                <el-tag v-if="row.overall_score < 60" type="danger">高风险</el-tag>
                <el-tag v-else-if="row.overall_score < 75" type="warning">中风险</el-tag>
                <el-tag v-else type="success">低风险</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="created_at" label="创建时间" />
            <el-table-column label="操作" width="280">
              <template #default="{ row }">
                <el-button
                  size="small"
                  type="primary"
                  aria-label="跳转到 HR 复核详情页"
                  @click="viewDetail(row)"
                >
                  查看详情
                </el-button>
                <el-button size="small" type="success" :loading="submitting" @click="approve(row)">
                  通过
                </el-button>
                <el-button size="small" type="danger" :loading="submitting" @click="reject(row)">
                  驳回
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { hrApi, evaluationApi } from '@/api/client'

const router = useRouter()
const loading = ref(false)
const submitting = ref(false)
const evaluations = ref([])

async function loadData() {
  loading.value = true
  try {
    const data = await hrApi.auditQueue()
    evaluations.value = data.pending || []
  } catch (err) {
    console.error('加载复核队列失败:', err)
    ElMessage.error('加载复核队列失败')
  } finally {
    loading.value = false
  }
}

function viewDetail(row) {
  // 跳转到 HR 复核详情页(查看完整评估、审批历史、申诉记录并执行复核动作)
  router.push(`/hr/audit/${row.evaluation_id}`)
}

async function approve(row) {
  try {
    const { value } = await ElMessageBox.prompt('请输入复核意见', '通过复核', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      inputType: 'textarea',
    })
    submitting.value = true
    await evaluationApi.approve(row.evaluation_id, { comment: value })
    ElMessage.success('已通过复核')
    await loadData()
  } catch (err) {
    if (err === 'cancel') return
    ElMessage.error(err.message || '操作失败')
  } finally {
    submitting.value = false
  }
}

async function reject(row) {
  try {
    const { value } = await ElMessageBox.prompt('请输入驳回理由', '驳回评估', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      inputType: 'textarea',
    })
    submitting.value = true
    await evaluationApi.reject(row.evaluation_id, { comment: value })
    ElMessage.success('已驳回')
    await loadData()
  } catch (err) {
    if (err === 'cancel') return
    ElMessage.error(err.message || '操作失败')
  } finally {
    submitting.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
</style>
