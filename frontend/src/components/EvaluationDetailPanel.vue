<template>
  <div class="evaluation-detail-panel">
    <el-row :gutter="20">
      <el-col :span="12">
        <h3>员工视图（建设性）</h3>
        <p><strong>总结：</strong>{{ employeeView.summary || '—' }}</p>
        <p><strong>优势：</strong>{{ formatList(employeeView.strengths) }}</p>
        <p><strong>下周聚焦：</strong>{{ formatList(employeeView.next_week_focus) }}</p>
      </el-col>
      <el-col :span="12">
        <h3>管理视图（尖锐诊断）</h3>
        <p><strong>总体判断：</strong>{{ managerView.harsh_assessment || '—' }}</p>
        <p><strong>ROI 分析：</strong>{{ managerView.roi_analysis || '—' }}</p>
        <p><strong>调配建议：</strong>{{ managerView.reallocation_suggestion || '—' }}</p>
        <p><strong>隐藏问题：</strong>{{ formatList(managerView.hidden_issues) }}</p>
      </el-col>
    </el-row>

    <el-divider />

    <h3>风险标记</h3>
    <el-alert
      v-for="(flag, idx) in managerView.risk_flags || []"
      :key="idx"
      :title="`${flag.level} - ${flag.category}`"
      :description="flag.description"
      :type="riskTagType(flag.level)"
      show-icon
      class="risk-alert"
    />
    <el-empty v-if="!(managerView.risk_flags || []).length" description="无风险标记" :image-size="60" />

    <el-divider />

    <h3>维度评分</h3>
    <el-table v-if="dimensionScores.length" :data="dimensionScores" border size="small">
      <el-table-column prop="dimension" label="维度" width="120" />
      <el-table-column prop="score" label="分数" width="80" />
      <el-table-column prop="evidence" label="证据" show-overflow-tooltip />
    </el-table>
    <el-empty v-else description="无维度评分" :image-size="60" />

    <el-divider v-if="auditLogs.length" />

    <div v-if="auditLogs.length">
      <h3>审计日志</h3>
      <el-timeline>
        <el-timeline-item
          v-for="log in auditLogs"
          :key="log.id"
          :timestamp="log.created_at"
          :type="log.action_type === 'approved' ? 'success' : log.action_type === 'rejected' ? 'danger' : 'primary'"
        >
          {{ log.action }} — {{ log.details || '' }}
        </el-timeline-item>
      </el-timeline>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { riskTagType } from '@/utils/evaluationStatus'

const props = defineProps({
  evaluation: {
    type: Object,
    default: null,
  },
  auditLogs: {
    type: Array,
    default: () => [],
  },
})

const employeeView = computed(() => props.evaluation?.employee_view || {})
const managerView = computed(() => props.evaluation?.manager_view || {})
const dimensionScores = computed(() => props.evaluation?.dimension_scores || [])

function formatList(arr) {
  if (!arr || !Array.isArray(arr) || arr.length === 0) return '—'
  return arr.join('；')
}
</script>

<style scoped>
.evaluation-detail-panel {
  width: 100%;
}
.risk-alert {
  margin-bottom: 8px;
}
</style>
