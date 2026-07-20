<template>
  <div class="growth-path">
    <el-row :gutter="20">
      <el-col :span="24">
        <el-card v-loading="loading" :aria-busy="loading">
          <template #header>
            <div class="card-header">
              <span>成长路径推荐</span>
              <el-button size="small" :loading="loading" @click="loadData">刷新</el-button>
            </div>
          </template>
          <el-alert
            v-if="data.status && data.status !== 'ok'"
            :title="statusText"
            type="warning"
            :closable="false"
            show-icon
          />
          <el-descriptions :column="2" border size="small" class="mt-10">
            <el-descriptions-item label="分析窗口">
              {{ data.window_weeks || 0 }} 周
            </el-descriptions-item>
            <el-descriptions-item label="推荐方向">
              <el-tag :type="directionType">{{ direction }}</el-tag>
            </el-descriptions-item>
          </el-descriptions>
          <p v-if="reason" class="reason">方向依据：{{ reason }}</p>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="mt-20">
      <el-col :span="12">
        <el-card>
          <template #header><span>能力雷达对比（当前 vs 历史）</span></template>
          <div v-if="capability.dimensions.length" role="img" :aria-label="radarSummary">
            <v-chart class="chart" :option="radarOption" autoresize />
          </div>
          <el-empty v-else description="暂无维度数据" />
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card>
          <template #header><span>成长趋势</span></template>
          <div v-if="growthTrend.length" role="img" :aria-label="trendSummary">
            <v-chart class="chart" :option="trendOption" autoresize />
          </div>
          <el-empty v-else description="暂无历史评估数据" />
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="mt-20">
      <el-col :span="8">
        <el-card>
          <template #header><span>核心优势</span></template>
          <el-empty v-if="!strengths.length" description="暂无数据" />
          <ul v-else class="list">
            <li v-for="s in strengths" :key="s.item">
              {{ s.item }} <el-tag size="small" type="info">{{ s.frequency }}次</el-tag>
            </li>
          </ul>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card>
          <template #header><span>高频成长领域</span></template>
          <el-empty v-if="!growthAreas.length" description="暂无数据" />
          <ul v-else class="list">
            <li v-for="g in growthAreas" :key="g.dimension">
              {{ g.dimension }}
              <el-tag size="small" type="warning">{{ g.frequency }}次</el-tag>
            </li>
          </ul>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card>
          <template #header><span>建议行动</span></template>
          <el-empty v-if="!actions.length" description="暂无建议" />
          <ul v-else class="list">
            <li v-for="(a, i) in actions" :key="i">
              {{ a.action }}
              <el-tag v-if="a.source === 'direction'" size="small" type="success">方向建议</el-tag>
            </li>
          </ul>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { computed, ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { analyticsApi } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import VChart from 'vue-echarts'
import '@/utils/echarts'

const auth = useAuthStore()
const loading = ref(false)
const data = ref({})

const capability = computed(
  () => data.value.capability_change || { dimensions: [], current: [], history: [] },
)
const growthTrend = computed(() => data.value.growth_trend || [])
const strengths = computed(() => data.value.strengths || [])
const growthAreas = computed(() => data.value.growth_areas || [])
const actions = computed(() => data.value.suggested_actions || [])
const direction = computed(() => data.value.recommended_direction?.direction || '—')
const reason = computed(() => data.value.recommended_direction?.reason || '')

const statusText = computed(() => {
  if (data.value.status === 'no_data') return '暂无历史评估数据，无法生成成长路径'
  if (data.value.status === 'insufficient_data')
    return '历史评估不足 4 周，结果仅供参考，建议持续积累'
  return ''
})

const directionType = computed(() => {
  const map = { 技术深耕: 'success', 管理转型: 'warning', 跨领域: 'primary' }
  return map[direction.value] || 'info'
})

const radarOption = computed(() => ({
  tooltip: {},
  legend: { data: ['当前', '历史'] },
  radar: {
    indicator: capability.value.dimensions.map((name) => ({ name, max: 100 })),
    radius: '62%',
  },
  series: [
    {
      type: 'radar',
      data: [
        {
          value: capability.value.current,
          name: '当前',
          areaStyle: { color: 'rgba(37,99,235,0.3)' },
          lineStyle: { color: '#2563eb' },
        },
        {
          value: capability.value.history,
          name: '历史',
          areaStyle: { color: 'rgba(230,162,60,0.2)' },
          lineStyle: { color: '#e6a23c' },
        },
      ],
    },
  ],
}))

const radarSummary = computed(() => {
  const items = capability.value.dimensions
    .map((d, i) => `${d} 当前${capability.value.current[i]} 历史${capability.value.history[i]}`)
    .join('；')
  return `能力雷达对比图：${items || '暂无维度'}`
})

const trendOption = computed(() => ({
  tooltip: { trigger: 'axis' },
  grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
  xAxis: { type: 'category', data: growthTrend.value.map((t) => t.period), boundaryGap: false },
  yAxis: { type: 'value', min: 0, max: 100 },
  series: [
    {
      name: '综合得分',
      type: 'line',
      smooth: true,
      data: growthTrend.value.map((t) => t.score),
      areaStyle: { color: 'rgba(103,194,58,0.2)' },
      lineStyle: { color: '#67c23a' },
      itemStyle: { color: '#67c23a' },
    },
  ],
}))

const trendSummary = computed(() => {
  const items = growthTrend.value.map((t) => `${t.period} ${t.score}分`).join('；')
  return `成长趋势折线图：${items || '暂无趋势数据'}`
})

async function loadData() {
  if (!auth.userId) {
    ElMessage.error('用户信息缺失，请重新登录')
    return
  }
  loading.value = true
  try {
    data.value = await analyticsApi.growthPath(auth.userId)
  } catch (err) {
    console.error('加载成长路径失败:', err)
    ElMessage.error(err.message || '加载成长路径失败')
  } finally {
    loading.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.mt-10 {
  margin-top: 10px;
}
.mt-20 {
  margin-top: 20px;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.chart {
  width: 100%;
  height: 340px;
}
.reason {
  margin-top: 10px;
  color: #606266;
  line-height: 1.6;
}
.list {
  margin: 0;
  padding-left: 18px;
  line-height: 2;
}
</style>
