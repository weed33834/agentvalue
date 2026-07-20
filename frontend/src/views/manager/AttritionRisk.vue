<template>
  <div class="attrition-risk">
    <el-card>
      <template #header><span>离职风险预警</span></template>
      <el-form :inline="true">
        <el-form-item label="团队成员">
          <el-input
            v-model="membersInput"
            placeholder="员工ID，逗号分隔，例如：E1001,E1002"
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
      <el-col :span="8">
        <el-card>
          <el-statistic
            title="团队平均风险分"
            :value="data.avg_risk_score || 0"
            :precision="1"
            value-style="color: #f56c6c"
          />
        </el-card>
      </el-col>
      <el-col :span="16">
        <el-card>
          <template #header><span>风险分布</span></template>
          <div role="img" :aria-label="pieSummary">
            <v-chart class="chart" :option="pieOption" autoresize />
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="mt-20" v-if="hasData">
      <el-col :span="24">
        <el-card v-loading="loading">
          <template #header><span>风险员工列表</span></template>
          <el-table :data="members" style="width: 100%" empty-text="暂无数据">
            <el-table-column prop="employee_id" label="员工ID" width="140" />
            <el-table-column label="风险分" width="100" sortable :sort-by="(r) => r.risk_score">
              <template #default="{ row }">
                <span :class="scoreClass(row.risk_level)">{{ row.risk_score }}</span>
              </template>
            </el-table-column>
            <el-table-column label="风险等级" width="120">
              <template #default="{ row }">
                <el-tag :type="levelType(row.risk_level)">{{ levelLabel(row.risk_level) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="主要风险因子">
              <template #default="{ row }">
                <el-tag
                  v-for="f in (row.factors || [])"
                  :key="f.factor"
                  size="small"
                  type="danger"
                  class="factor-tag"
                >
                  {{ f.factor }}({{ f.weight }})
                </el-tag>
                <span v-if="!row.factors?.length" class="muted">无</span>
              </template>
            </el-table-column>
            <el-table-column label="建议" min-width="220">
              <template #default="{ row }">
                <ul class="sug">
                  <li v-for="(s, i) in row.suggestions" :key="i">{{ s }}</li>
                </ul>
              </template>
            </el-table-column>
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
const data = ref({})

const hasData = computed(() => !!data.value.distribution)
const members = computed(() => data.value.members || [])

const pieOption = computed(() => {
  const dist = data.value.distribution || { low: 0, medium: 0, high: 0 }
  return {
    tooltip: { trigger: 'item', formatter: '{b}: {c}人 ({d}%)' },
    legend: { bottom: 0 },
    series: [
      {
        type: 'pie',
        radius: ['40%', '70%'],
        data: [
          { value: dist.low || 0, name: '低风险', itemStyle: { color: '#67c23a' } },
          { value: dist.medium || 0, name: '中风险', itemStyle: { color: '#e6a23c' } },
          { value: dist.high || 0, name: '高风险', itemStyle: { color: '#f56c6c' } },
        ],
      },
    ],
  }
})

const pieSummary = computed(() => {
  const dist = data.value.distribution || { low: 0, medium: 0, high: 0 }
  return `风险分布饼图：低风险 ${dist.low} 人，中风险 ${dist.medium} 人，高风险 ${dist.high} 人`
})

function levelType(level) {
  return { low: 'success', medium: 'warning', high: 'danger' }[level] || 'info'
}
function levelLabel(level) {
  return { low: '低风险', medium: '中风险', high: '高风险' }[level] || level
}
function scoreClass(level) {
  return { low: 'score-low', medium: 'score-mid', high: 'score-high' }[level]
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
    data.value = await analyticsApi.attritionRisk(memberList)
  } catch (err) {
    console.error('加载离职风险失败:', err)
    ElMessage.error(err.message || '加载离职风险失败')
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
  height: 280px;
}
.factor-tag {
  margin-right: 6px;
  margin-bottom: 4px;
}
.muted {
  color: #909399;
}
.sug {
  margin: 0;
  padding-left: 16px;
  line-height: 1.8;
  font-size: 13px;
  color: #374151;
}
.score-low {
  color: #67c23a;
  font-weight: 600;
}
.score-mid {
  color: #e6a23c;
  font-weight: 600;
}
.score-high {
  color: #f56c6c;
  font-weight: 700;
}
</style>
