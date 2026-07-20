<template>
  <div class="employee-dashboard">
    <el-row :gutter="20">
      <el-col :span="16">
        <el-card v-loading="loading" :aria-busy="loading">
          <template #header>
            <div class="card-header">
              <span>我的成长看板</span>
              <span v-if="latestEvaluation">
                {{ latestEvaluation.period }} · 得分 {{ latestEvaluation.overall_score }}
              </span>
            </div>
          </template>
          <!-- 无障碍：评估数据加载后用 role=status 通告屏幕阅读器 -->
          <div v-if="latestEvaluation" role="status" aria-live="polite">
            <h3>评估总结</h3>
            <p class="summary">{{ employeeView.summary }}</p>

            <h4>优势</h4>
            <ul>
              <li v-for="(s, idx) in employeeView.strengths" :key="idx">{{ s }}</li>
            </ul>

            <h4>成长方向</h4>
            <el-timeline>
              <el-timeline-item
                v-for="area in employeeView.growth_areas"
                :key="area.dimension"
                type="primary"
              >
                <strong>{{ area.dimension }}</strong> — {{ area.score }} 分
                <div class="evidence">依据：{{ (area.evidence || []).join('；') }}</div>
                <div class="action">建议：{{ (area.improvement_actions || []).join('；') }}</div>
              </el-timeline-item>
            </el-timeline>

            <h4>下周聚焦</h4>
            <el-tag
              v-for="(focus, idx) in employeeView.next_week_focus"
              :key="idx"
              class="focus-tag"
            >
              {{ focus }}
            </el-tag>
          </div>
          <el-empty v-else description="暂无已审批的评估数据" />
        </el-card>
      </el-col>

      <el-col :span="8">
        <el-card>
          <template #header>
            <span>成长维度雷达</span>
          </template>
          <RadarChart
            v-if="radarDimensions.length"
            :dimensions="radarDimensions"
            :scores="radarScores"
          />
          <el-empty v-else description="暂无维度数据" />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { computed, ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { employeeApi } from '@/api/client'
import RadarChart from '@/components/RadarChart.vue'

const auth = useAuthStore()
const loading = ref(false)
const evaluations = ref([])

const latestEvaluation = computed(() => evaluations.value[0] || null)
const employeeView = computed(() => latestEvaluation.value?.employee_view || {})

const radarDimensions = computed(() => {
  return (employeeView.value.growth_areas || []).map((a) => a.dimension)
})

const radarScores = computed(() => {
  return (employeeView.value.growth_areas || []).map((a) => a.score)
})

async function loadData() {
  loading.value = true
  try {
    const data = await employeeApi.dashboard(auth.userId)
    evaluations.value = data.evaluations || []
  } catch (err) {
    console.error('加载员工看板失败:', err)
    ElMessage.error('加载员工看板失败')
  } finally {
    loading.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.employee-dashboard h3 {
  margin-top: 0;
}
.summary {
  color: #374151;
  line-height: 1.6;
}
.evidence {
  color: #4b5563;
  font-size: 13px;
  margin-top: 4px;
}
.action {
  /* 无障碍：#409eff 对白底约 2.8:1 不达标，改为对比度约 5.2:1 的深蓝 */
  color: #2563eb;
  font-size: 13px;
  margin-top: 4px;
}
.focus-tag {
  margin-right: 8px;
  margin-bottom: 8px;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
</style>
