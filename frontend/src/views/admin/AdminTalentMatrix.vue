<template>
  <div class="talent-matrix">
    <el-card>
      <template #header><span>人才九宫格（绩效 × 潜力）</span></template>
      <el-form :inline="true">
        <el-form-item label="周期">
          <el-input
            v-model="periodInput"
            placeholder="留空取最新一次评估，例如 2026-W25 / 2026-Q2"
            style="width: 280px"
            @keyup.enter="loadData"
          />
        </el-form-item>
        <el-form-item label="员工ID">
          <el-input
            v-model="memberIdsInput"
            placeholder="可选，逗号分隔，例如 E1001,E1002"
            style="width: 280px"
            @keyup.enter="loadData"
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="loadData">生成九宫格</el-button>
        </el-form-item>
      </el-form>
      <div class="muted" style="margin-top: 8px; font-size: 12px">
        横轴 = 绩效（低 / 中 / 高），纵轴 = 潜力（低 / 中 / 高）。点击散点查看员工详情。
      </div>
    </el-card>

    <el-row :gutter="20" class="mt-20" v-if="hasData">
      <el-col :span="16">
        <el-card v-loading="loading">
          <template #header><span>九宫格分布</span></template>
          <div role="img" :aria-label="matrixSummary">
            <v-chart
              class="matrix-chart"
              :option="matrixOption"
              autoresize
              @click="onPointClick"
            />
          </div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card>
          <template #header><span>统计概览</span></template>
          <el-descriptions :column="1" border>
            <el-descriptions-item label="总人数">{{ data.total || 0 }}</el-descriptions-item>
            <el-descriptions-item label="高绩效高潜力（明星）">
              <el-tag type="success">{{ cellCount('high-high') }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="高绩效中潜力（核心）">
              <el-tag type="primary">{{ cellCount('high-mid') }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="低绩效低潜力（待改进）">
              <el-tag type="danger">{{ cellCount('low-low') }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="中绩效高潜力（潜力股）">
              <el-tag type="warning">{{ cellCount('mid-high') }}</el-tag>
            </el-descriptions-item>
          </el-descriptions>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="mt-20" v-if="hasData">
      <el-col :span="24">
        <el-card>
          <template #header><span>九宫格明细</span></template>
          <el-table :data="members" style="width: 100%" empty-text="暂无数据" @row-click="onRowClick">
            <el-table-column prop="employee_id" label="员工ID" width="120" />
            <el-table-column label="绩效分数" width="120" sortable :sort-by="(r) => r.performance_score">
              <template #default="{ row }">
                <el-tag :type="bucketTagType(row.performance_bucket)" size="small">
                  {{ row.performance_score }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="绩效档位" width="100">
              <template #default="{ row }">
                {{ bucketLabel(row.performance_bucket) }}
              </template>
            </el-table-column>
            <el-table-column label="潜力分数" width="120" sortable :sort-by="(r) => r.potential_score">
              <template #default="{ row }">
                <el-tag :type="bucketTagType(row.potential_bucket)" size="small" effect="plain">
                  {{ row.potential_score }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="潜力档位" width="100">
              <template #default="{ row }">
                {{ bucketLabel(row.potential_bucket) }}
              </template>
            </el-table-column>
            <el-table-column label="九宫格分类" width="160">
              <template #default="{ row }">
                <el-tag :type="cellTagType(row)" size="small">
                  {{ cellLabel(row.performance_bucket, row.potential_bucket) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="评估次数" prop="eval_count" width="100" sortable />
            <el-table-column label="首/末评分" width="160">
              <template #default="{ row }">
                <span class="muted">{{ row.first_score }} → {{ row.latest_score }}</span>
              </template>
            </el-table-column>
            <el-table-column label="成长斜率" prop="score_slope" width="110" sortable>
              <template #default="{ row }">
                <span :class="slopeClass(row.score_slope)">{{ row.score_slope }}</span>
              </template>
            </el-table-column>
            <el-table-column label="评估周期" prop="period" width="120" />
            <el-table-column label="操作" width="120" fixed="right">
              <template #default="{ row }">
                <el-button size="small" link type="primary" @click.stop="viewEmployee(row)">
                  查看详情
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>

    <!-- 员工详情抽屉 -->
    <el-drawer v-model="detailVisible" title="员工九宫格详情" size="480px">
      <template v-if="selectedMember">
        <el-descriptions :column="1" border>
          <el-descriptions-item label="员工ID">{{ selectedMember.employee_id }}</el-descriptions-item>
          <el-descriptions-item label="评估ID">{{ selectedMember.evaluation_id }}</el-descriptions-item>
          <el-descriptions-item label="评估周期">{{ selectedMember.period }}</el-descriptions-item>
          <el-descriptions-item label="绩效分数">
            <el-tag :type="bucketTagType(selectedMember.performance_bucket)">
              {{ selectedMember.performance_score }}
            </el-tag>
            （{{ bucketLabel(selectedMember.performance_bucket) }}）
          </el-descriptions-item>
          <el-descriptions-item label="潜力分数">
            <el-tag :type="bucketTagType(selectedMember.potential_bucket)" effect="plain">
              {{ selectedMember.potential_score }}
            </el-tag>
            （{{ bucketLabel(selectedMember.potential_bucket) }}）
          </el-descriptions-item>
          <el-descriptions-item label="九宫格分类">
            <el-tag :type="cellTagType(selectedMember)">
              {{ cellLabel(selectedMember.performance_bucket, selectedMember.potential_bucket) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="评估次数">{{ selectedMember.eval_count }}</el-descriptions-item>
          <el-descriptions-item label="首/末评分">
            {{ selectedMember.first_score }} → {{ selectedMember.latest_score }}
          </el-descriptions-item>
          <el-descriptions-item label="成长斜率">
            <span :class="slopeClass(selectedMember.score_slope)">{{ selectedMember.score_slope }}</span>
          </el-descriptions-item>
        </el-descriptions>
        <div class="muted" style="margin-top: 16px; font-size: 12px; line-height: 1.8">
          <strong>分类解读：</strong>{{ cellLabel(selectedMember.performance_bucket, selectedMember.potential_bucket) }}
          —— {{ cellDescription(selectedMember.performance_bucket, selectedMember.potential_bucket) }}
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { analyticsApi } from '@/api/client'
import VChart from 'vue-echarts'
import '@/utils/echarts'

const loading = ref(false)
const periodInput = ref('')
const memberIdsInput = ref('')
const data = ref({})
const detailVisible = ref(false)
const selectedMember = ref(null)

const hasData = computed(() => !!data.value.cells)
const members = computed(() => data.value.members || [])

// 九宫格档位与配色
const BUCKET_LABEL = { low: '低', mid: '中', high: '高' }

// 9 个格子的标签与描述（performance × potential）
const CELL_INFO = {
  'high-high': { label: '明星员工', desc: '高绩效高潜力：未来领导者，重点保留与培养，给予更大舞台。', color: '#67c23a' },
  'high-mid': { label: '核心骨干', desc: '高绩效中潜力：稳定输出，适合专家路径或当前岗位深化。', color: '#409eff' },
  'high-low': { label: '专业专家', desc: '高绩效低潜力：在当前岗位持续贡献，避免过度提拔。', color: '#909399' },
  'mid-high': { label: '潜力股', desc: '中绩效高潜力：成长快但尚未兑现，需辅导与挑战性任务。', color: '#e6a23c' },
  'mid-mid': { label: '稳定贡献者', desc: '中绩效中潜力：团队主力，按需培训提升。', color: '#d3dce6' },
  'mid-low': { label: '待开发', desc: '中绩效低潜力：观察是否在岗匹配，必要时调整。', color: '#c0c4cc' },
  'low-high': { label: '问题员工', desc: '低绩效高潜力：可能角色错配，需深入沟通调整岗位。', color: '#f56c6c' },
  'low-mid': { label: '观察对象', desc: '低绩效中潜力：需制定改进计划，限期复盘。', color: '#f4a0a0' },
  'low-low': { label: '待改进', desc: '低绩效低潜力：启动绩效改进流程，必要时退出。', color: '#fab6b6' },
}

function cellKey(perf, pot) {
  return `${perf}-${pot}`
}

function cellCount(key) {
  return data.value.cells?.[key]?.count || 0
}

function bucketLabel(bucket) {
  return BUCKET_LABEL[bucket] || bucket
}

function bucketTagType(bucket) {
  return { low: 'danger', mid: 'warning', high: 'success' }[bucket] || 'info'
}

function cellLabel(perf, pot) {
  return CELL_INFO[cellKey(perf, pot)]?.label || `${bucketLabel(perf)}-${bucketLabel(pot)}`
}

function cellDescription(perf, pot) {
  return CELL_INFO[cellKey(perf, pot)]?.desc || ''
}

function cellTagType(member) {
  const key = cellKey(member.performance_bucket, member.potential_bucket)
  // 仅返回标准 type，颜色用其他方式控制
  if (key === 'high-high') return 'success'
  if (key === 'low-low') return 'danger'
  if (key.startsWith('mid-high') || key === 'low-high') return 'warning'
  if (key.startsWith('high')) return 'primary'
  return 'info'
}

function slopeClass(slope) {
  if (slope > 1) return 'slope-up'
  if (slope < -1) return 'slope-down'
  return 'slope-flat'
}

// ECharts 散点图配置：3x3 矩阵
// 横轴 = 绩效 (0-100), 纵轴 = 潜力 (0-100)
// 用 visualMap 分区着色，标记线划分低/中/高 (33.3/66.7)
const matrixOption = computed(() => {
  const membersList = members.value
  // 按九宫格分组散点
  const seriesMap = {}
  membersList.forEach((m) => {
    const key = cellKey(m.performance_bucket, m.potential_bucket)
    if (!seriesMap[key]) {
      seriesMap[key] = {
        name: cellLabel(m.performance_bucket, m.potential_bucket),
        type: 'scatter',
        symbolSize: 18,
        data: [],
        itemStyle: { color: CELL_INFO[key]?.color || '#909399' },
        emphasis: { focus: 'series', scale: 1.4 },
      }
    }
    seriesMap[key].data.push({
      value: [m.performance_score, m.potential_score],
      member: m,
    })
  })

  return {
    tooltip: {
      trigger: 'item',
      formatter: (p) => {
        const m = p.data?.member
        if (!m) return p.name
        return [
          `员工: ${m.employee_id}`,
          `绩效: ${m.performance_score} (${bucketLabel(m.performance_bucket)})`,
          `潜力: ${m.potential_score} (${bucketLabel(m.potential_bucket)})`,
          `分类: ${cellLabel(m.performance_bucket, m.potential_bucket)}`,
          `周期: ${m.period || '-'}`,
        ].join('<br/>')
      },
    },
    legend: { bottom: 0, type: 'scroll' },
    grid: { left: 60, right: 30, top: 30, bottom: 60 },
    xAxis: {
      name: '绩效 →',
      nameLocation: 'middle',
      nameGap: 30,
      min: 0,
      max: 100,
      splitLine: { show: true, lineStyle: { type: 'dashed' } },
      axisLine: { lineStyle: { color: '#909399' } },
    },
    yAxis: {
      name: '潜力 ↑',
      nameLocation: 'middle',
      nameGap: 40,
      min: 0,
      max: 100,
      splitLine: { show: true, lineStyle: { type: 'dashed' } },
      axisLine: { lineStyle: { color: '#909399' } },
    },
    // 3x3 分隔线 (33.3 / 66.7)
    series: Object.values(seriesMap),
    markLine: {
      symbol: ['none', 'none'],
      lineStyle: { color: '#c0c4cc', type: 'dashed', width: 1 },
      data: [
        { xAxis: 33.33, label: { formatter: '低/中' } },
        { xAxis: 66.67, label: { formatter: '中/高' } },
        { yAxis: 33.33, label: { formatter: '低/中' } },
        { yAxis: 66.67, label: { formatter: '中/高' } },
      ],
    },
  }
})

const matrixSummary = computed(() => {
  if (!hasData.value) return '人才九宫格未生成'
  return `人才九宫格：共 ${data.value.total || 0} 人，明星 ${cellCount('high-high')} 人，待改进 ${cellCount('low-low')} 人`
})

function onPointClick(params) {
  const member = params.data?.member
  if (member) {
    selectedMember.value = member
    detailVisible.value = true
  }
}

function onRowClick(row) {
  selectedMember.value = row
  detailVisible.value = true
}

function viewEmployee(row) {
  selectedMember.value = row
  detailVisible.value = true
}

async function loadData() {
  const params = {}
  if (periodInput.value.trim()) params.period = periodInput.value.trim()
  const memberList = memberIdsInput.value
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
  if (memberList.length) params.memberIds = memberList

  loading.value = true
  try {
    data.value = await analyticsApi.talentMatrix(params)
    if (!data.value.total) {
      ElMessage.info('当前条件下无已审批评估数据')
    }
  } catch (err) {
    console.error('加载人才九宫格失败:', err)
    ElMessage.error(err.message || '加载人才九宫格失败')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.mt-20 {
  margin-top: 20px;
}
.matrix-chart {
  width: 100%;
  height: 520px;
}
.muted {
  color: #909399;
}
.slope-up {
  color: #67c23a;
  font-weight: 600;
}
.slope-flat {
  color: #909399;
}
.slope-down {
  color: #f56c6c;
  font-weight: 600;
}
</style>
