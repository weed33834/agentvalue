<template>
  <div class="manager-dashboard">
    <el-row :gutter="20">
      <el-col :span="24">
        <el-card>
          <template #header>
            <div class="card-header">
              <span>待审批评估（{{ pendingCount }}）</span>
              <el-button size="small" :loading="loading" @click="loadData">刷新</el-button>
            </div>
          </template>
          <el-table
            v-loading="loading"
            :aria-busy="loading"
            :data="pendingApprovals"
            style="width: 100%"
            empty-text="暂无待审批评估"
          >
            <el-table-column prop="employee_id" label="员工ID" />
            <el-table-column prop="period" label="周期" />
            <el-table-column prop="overall_score" label="综合得分" sortable />
            <el-table-column prop="status" label="状态" />
            <el-table-column label="操作" width="180">
              <template #default="{ row }">
                <el-button size="small" type="primary" @click="viewDetail(row)"> 审批 </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="mt-20">
      <el-col :span="12">
        <el-card>
          <template #header>
            <span>最近已审批</span>
          </template>
          <el-table :data="recentApproved" style="width: 100%">
            <el-table-column prop="employee_id" label="员工ID" />
            <el-table-column prop="period" label="周期" />
            <el-table-column prop="overall_score" label="得分" />
          </el-table>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card>
          <template #header>
            <span>团队风险分布</span>
          </template>
          <div class="risk-summary">
            <!-- 无障碍：风险等级已由 title 文字传达，数值颜色加深以满足对比度要求 -->
            <el-statistic title="高风险" :value="riskStats.high" value-style="color: #cf4444" />
            <el-statistic title="中风险" :value="riskStats.medium" value-style="color: #b36a14" />
            <el-statistic title="低风险" :value="riskStats.low" value-style="color: #3d7d10" />
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { managerApi } from '@/api/client'

const router = useRouter()

const loading = ref(false)
const pendingCount = ref(0)
const pendingApprovals = ref([])
const recentApproved = ref([])
const riskStats = ref({ high: 0, medium: 0, low: 0 })

async function loadData() {
  loading.value = true
  try {
    const data = await managerApi.dashboard()
    pendingCount.value = data.pending_count || 0
    pendingApprovals.value = data.pending || []
    recentApproved.value = data.recent_approved || []
    // 从后端返回的 risk_stats 或基于 pending 数据计算风险分布
    if (data.risk_stats) {
      riskStats.value = data.risk_stats
    } else if (pendingApprovals.value.length > 0) {
      const stats = { high: 0, medium: 0, low: 0 }
      for (const evalItem of pendingApprovals.value) {
        const score = evalItem.overall_score ?? 100
        const riskFlags = evalItem.manager_view?.risk_flags || []
        const hasCritical = riskFlags.some((r) => r.level === 'critical')
        if (hasCritical || score < 60) stats.high++
        else if (score < 75) stats.medium++
        else stats.low++
      }
      riskStats.value = stats
    }
  } catch (err) {
    console.error('加载主管工作台失败:', err)
    ElMessage.error('加载主管工作台失败')
  } finally {
    loading.value = false
  }
}

function viewDetail(row) {
  router.push(`/manager/approval/${row.evaluation_id}`)
}

onMounted(loadData)
</script>

<style scoped>
.mt-20 {
  margin-top: 20px;
}
.risk-summary {
  display: flex;
  justify-content: space-around;
  padding: 20px 0;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
</style>
