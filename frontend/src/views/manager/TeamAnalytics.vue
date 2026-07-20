<template>
  <div class="team-analytics">
    <el-card>
      <template #header>
        <span>团队分析</span>
      </template>
      <el-form :inline="true">
        <el-form-item label="团队成员">
          <el-input
            v-model="membersInput"
            placeholder="输入员工ID，逗号分隔，例如：E1001,E1002,E1003"
            style="width: 360px"
            @keyup.enter="loadData"
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="loadData">分析</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-row :gutter="20" class="mt-20" v-if="hasData">
      <el-col :span="16">
        <el-card v-loading="loading">
          <template #header>
            <span>团队成员得分对比</span>
          </template>
          <!-- 无障碍：柱状图为纯图形，提供 role="img" 与文字摘要供屏幕阅读器读取 -->
          <div role="img" :aria-label="barSummary">
            <v-chart class="bar-chart" :option="barOption" autoresize />
          </div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card>
          <template #header>
            <span>团队平均分</span>
          </template>
          <div class="avg-block">
            <el-statistic
              title="团队平均得分"
              :value="analytics.overall_avg || 0"
              :precision="2"
              value-style="color: #2563eb"
            />
            <p class="member-count">成员数：{{ members.length }}</p>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="mt-20" v-if="hasData">
      <el-col :span="24">
        <el-card>
          <template #header>
            <span>成员评估明细</span>
          </template>
          <el-table :data="members" style="width: 100%" empty-text="暂无数据">
            <el-table-column prop="employee_id" label="员工ID" />
            <el-table-column prop="eval_count" label="评估次数" sortable />
            <el-table-column prop="avg_score" label="平均分" sortable>
              <template #default="{ row }">
                <el-tag :type="scoreType(row.avg_score)">{{ row.avg_score }}</el-tag>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { computed, ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { managerApi } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import VChart from 'vue-echarts'
import '@/utils/echarts'

const loading = ref(false)
const membersInput = ref('E1001,E1002,E1003')
const analytics = ref({})
const auth = useAuthStore()

const members = computed(() => analytics.value.members || [])
const hasData = computed(() => members.value.length > 0)

// 无障碍：构造团队得分对比柱状图的文字替代描述
const barSummary = computed(() => {
  const items = members.value.map((m) => `${m.employee_id} 平均${m.avg_score}分`).join('；')
  return `团队成员得分对比柱状图，共${members.value.length}名成员：${items}`
})

const barOption = computed(() => ({
  tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
  grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
  xAxis: {
    type: 'category',
    data: members.value.map((m) => m.employee_id),
    axisLabel: { rotate: 30 },
  },
  yAxis: { type: 'value', min: 0, max: 100 },
  series: [
    {
      name: '平均得分',
      type: 'bar',
      data: members.value.map((m) => m.avg_score),
      itemStyle: { color: '#2563eb' },
      barWidth: '40%',
    },
  ],
}))

function scoreType(score) {
  if (score >= 85) return 'success'
  if (score >= 70) return 'warning'
  return 'danger'
}

async function loadData() {
  const memberList = membersInput.value
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
  if (!memberList.length) {
    ElMessage.warning('请输入团队成员ID')
    return
  }
  loading.value = true
  try {
    if (!auth.userId) {
      ElMessage.error('用户信息缺失，请重新登录')
      return
    }
    const data = await managerApi.teamAnalytics(auth.userId, memberList)
    analytics.value = data
    if (!data.members || data.members.length === 0) {
      ElMessage.info('未查询到团队成员的评估数据')
    }
  } catch (err) {
    console.error('加载团队分析失败:', err)
    ElMessage.error('加载团队分析失败')
  } finally {
    loading.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.mt-20 {
  margin-top: 20px;
}
.bar-chart {
  width: 100%;
  height: 360px;
}
.avg-block {
  text-align: center;
  padding: 20px 0;
}
.member-count {
  margin-top: 16px;
  color: #606266;
}
</style>
