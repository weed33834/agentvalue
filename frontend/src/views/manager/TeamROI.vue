<template>
  <div class="team-roi">
    <el-card>
      <template #header>
        <span>团队 ROI 分析</span>
      </template>
      <el-form :inline="true">
        <el-form-item label="团队成员">
          <el-input
            v-model="membersInput"
            placeholder="员工ID，逗号分隔，例如：E1001,E1002"
            style="width: 320px"
            @keyup.enter="loadData"
          />
        </el-form-item>
        <el-form-item label="起始周">
          <el-input v-model="startWeek" placeholder="2026-W20" style="width: 140px" />
        </el-form-item>
        <el-form-item label="结束周">
          <el-input v-model="endWeek" placeholder="2026-W25" style="width: 140px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="loadData">分析</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-row :gutter="20" class="mt-20" v-if="hasData">
      <el-col :span="6">
        <el-card>
          <el-statistic
            title="综合 ROI"
            :value="summary.roi || 0"
            :precision="2"
            value-style="color: #2563eb"
          />
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card>
          <el-statistic title="评估总次数" :value="summary.total_evaluations || 0" />
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card>
          <el-statistic
            title="高分员工占比(%)"
            :value="summary.high_score_ratio || 0"
            :precision="1"
            value-style="color: #67c23a"
          />
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card>
          <el-statistic
            title="风险降低率(%)"
            :value="summary.risk_reduction_rate || 0"
            :precision="1"
            value-style="color: #e6a23c"
          />
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="mt-20" v-if="hasData">
      <el-col :span="12">
        <el-card v-loading="loading">
          <template #header><span>九宫格分布（绩效 × 潜力）</span></template>
          <div role="img" :aria-label="nineBoxSummary">
            <v-chart class="chart" :option="nineBoxOption" autoresize />
          </div>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card v-loading="loading">
          <template #header><span>周度 ROI 趋势</span></template>
          <div role="img" :aria-label="trendSummary">
            <v-chart class="chart" :option="trendOption" autoresize />
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="mt-20" v-if="hasData">
      <el-col :span="12">
        <el-card>
          <template #header><span>Top 员工</span></template>
          <el-table :data="topEmployees" size="small" empty-text="暂无数据">
            <el-table-column prop="employee_id" label="员工ID" />
            <el-table-column prop="avg_score" label="平均分" sortable />
            <el-table-column prop="score_slope" label="得分斜率" sortable />
          </el-table>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card>
          <template #header><span>Bottom 员工</span></template>
          <el-table :data="bottomEmployees" size="small" empty-text="暂无数据">
            <el-table-column prop="employee_id" label="员工ID" />
            <el-table-column prop="avg_score" label="平均分" sortable />
            <el-table-column prop="score_slope" label="得分斜率" sortable />
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { analyticsApi } from '@/api/client'
import VChart from 'vue-echarts'
import '@/utils/echarts'

const loading = ref(false)
const membersInput = ref('E1001,E1002,E1003')
const startWeek = ref('')
const endWeek = ref('')
const result = ref({})

const hasData = computed(() => !!result.value.summary)
const summary = computed(() => result.value.summary || {})
const topEmployees = computed(() => result.value.top_employees || [])
const bottomEmployees = computed(() => result.value.bottom_employees || [])
const trend = computed(() => result.value.trend || [])
const nineBox = computed(() => result.value.nine_box || { cells: {} })

// 九宫格：以绩效为 X、潜力为 Y，气泡大小=人数
const nineBoxOption = computed(() => {
  const perfIdx = { low: 0, mid: 1, high: 2 }
  const potIdx = { low: 0, mid: 1, high: 2 }
  const cells = nineBox.value.cells || {}
  const data = []
  Object.values(cells).forEach((c) => {
    data.push({
      value: [perfIdx[c.performance], potIdx[c.potential], c.count],
      name: c.label,
      employees: c.employees,
    })
  })
  return {
    tooltip: {
      formatter: (p) =>
        `${p.data.name}<br/>人数：${p.data.value[2]}<br/>${(p.data.employees || []).join('、') || '无'}`,
    },
    grid: { left: '8%', right: '5%', bottom: '10%', containLabel: true },
    xAxis: {
      type: 'category',
      name: '绩效',
      data: ['低', '中', '高'],
      splitLine: { show: true, lineStyle: { type: 'dashed' } },
    },
    yAxis: {
      type: 'category',
      name: '潜力',
      data: ['低', '中', '高'],
      splitLine: { show: true, lineStyle: { type: 'dashed' } },
    },
    series: [
      {
        type: 'scatter',
        data,
        symbolSize: (val) => 18 + val[2] * 14,
        itemStyle: { color: '#2563eb', opacity: 0.75 },
        label: { show: true, formatter: (p) => p.data.value[2] || '' },
      },
    ],
  }
})

const nineBoxSummary = computed(() => {
  const cells = Object.values(nineBox.value.cells || {})
  const filled = cells.filter((c) => c.count > 0)
  const items = filled.map((c) => `${c.label} ${c.count}人`).join('；')
  return `九宫格分布图，共${nineBox.value.total || 0}人：${items || '暂无分布'}`
})

const trendOption = computed(() => ({
  tooltip: { trigger: 'axis' },
  legend: { data: ['平均分', 'ROI'] },
  grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
  xAxis: { type: 'category', data: trend.value.map((t) => t.week) },
  yAxis: [
    { type: 'value', name: '平均分', min: 0, max: 100 },
    { type: 'value', name: 'ROI' },
  ],
  series: [
    {
      name: '平均分',
      type: 'line',
      smooth: true,
      data: trend.value.map((t) => t.avg_score),
      itemStyle: { color: '#67c23a' },
    },
    {
      name: 'ROI',
      type: 'line',
      yAxisIndex: 1,
      smooth: true,
      data: trend.value.map((t) => t.roi),
      itemStyle: { color: '#2563eb' },
    },
  ],
}))

const trendSummary = computed(() => {
  const items = trend.value.map((t) => `${t.week} 均${t.avg_score}分 ROI${t.roi}`).join('；')
  return `周度 ROI 趋势折线图：${items || '暂无趋势数据'}`
})

async function loadData() {
  const memberList = membersInput.value
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
  if (!memberList.length) {
    ElMessage.warning('请输入团队成员ID')
    return
  }
  const start = startWeek.value.trim()
  const end = endWeek.value.trim()
  if ((start || end) && !(start && end)) {
    ElMessage.warning('起始周与结束周需同时填写')
    return
  }
  loading.value = true
  try {
    result.value = await analyticsApi.teamRoi(memberList, start || undefined, end || undefined)
  } catch (err) {
    console.error('加载团队 ROI 失败:', err)
    ElMessage.error(err.message || '加载团队 ROI 失败')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.mt-20 {
  margin-top: 20px;
}
.chart {
  width: 100%;
  height: 360px;
}
</style>
