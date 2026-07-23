<template>
  <!-- HR 复核详情页:查看评估完整内容、审批历史、申诉记录,并执行 approve/reject/request-more-info -->
  <div class="hr-audit-detail">
    <el-page-header @back="goBack" title="HR 复核详情" />

    <!-- aria-live:操作结果以礼貌方式通告给屏幕阅读器 -->
    <div aria-live="polite" aria-atomic="true" class="sr-only">
      {{ liveStatus }}
    </div>

    <!-- 加载骨架:role=status 让辅助技术感知加载态 -->
    <el-skeleton
      v-if="loading && !evaluation"
      :rows="8"
      animated
      role="status"
      aria-label="评估详情加载中"
      class="detail-skeleton"
    />

    <el-empty v-else-if="!evaluation" description="未找到评估数据" />

    <el-card v-else v-loading="loading" :aria-busy="loading" class="detail-card">
      <template #header>
        <div class="card-header">
          <span class="card-title">
            评估详情 — {{ evaluation.employee_id }} / {{ evaluation.period }}
          </span>
          <el-tag :type="statusType" :aria-label="`状态: ${evaluation.status}`">
            {{ evaluation.status }}
          </el-tag>
        </div>
      </template>

      <el-row :gutter="20">
        <el-col :xs="24" :md="12">
          <h3 class="section-title">员工视图（建设性）</h3>
          <p><strong>总结：</strong>{{ employeeView.summary || '—' }}</p>
          <p><strong>优势：</strong>{{ (employeeView.strengths || []).join('；') || '—' }}</p>
          <p>
            <strong>下周聚焦：</strong>{{ (employeeView.next_week_focus || []).join('；') || '—' }}
          </p>
        </el-col>
        <el-col :xs="24" :md="12">
          <h3 class="section-title">管理视图（尖锐诊断）</h3>
          <p><strong>总体判断：</strong>{{ managerView.harsh_assessment || '—' }}</p>
          <p><strong>ROI 分析：</strong>{{ managerView.roi_analysis || '—' }}</p>
          <p><strong>调配建议：</strong>{{ managerView.reallocation_suggestion || '—' }}</p>
          <p>
            <strong>隐藏问题：</strong>{{ (managerView.hidden_issues || []).join('；') || '—' }}
          </p>
        </el-col>
      </el-row>

      <el-divider />

      <h3 class="section-title">风险标记</h3>
      <el-alert
        v-for="(flag, idx) in managerView.risk_flags || []"
        :key="idx"
        :title="`${flag.level} - ${flag.category}`"
        :description="flag.description"
        :type="riskTagType(flag.level)"
        show-icon
        class="risk-alert"
      />
      <p v-if="!(managerView.risk_flags || []).length" class="empty-line">无风险标记</p>

      <el-divider />

      <!-- 申诉记录:从审计日志中筛出 appeal/feedback 类动作单独展示 -->
      <h3 class="section-title">申诉记录（{{ appealLogs.length }}）</h3>
      <el-empty v-if="!appealLogs.length" description="暂无申诉记录" :image-size="60" />
      <el-timeline v-else>
        <el-timeline-item
          v-for="(log, idx) in appealLogs"
          :key="idx"
          :timestamp="log.created_at"
          placement="top"
        >
          <h4 class="timeline-title">{{ log.action }}</h4>
          <p class="timeline-meta">操作人：{{ log.actor_id || '—' }}</p>
          <p v-if="log.comment" class="timeline-comment">{{ log.comment }}</p>
        </el-timeline-item>
      </el-timeline>

      <el-divider />

      <!-- 审批历史:完整审计日志时间线 -->
      <h3 class="section-title">审批历史（{{ auditLogs.length }}）</h3>
      <el-empty v-if="!auditLogs.length" description="暂无审批历史" :image-size="60" />
      <el-timeline v-else>
        <el-timeline-item
          v-for="(log, idx) in auditLogs"
          :key="idx"
          :timestamp="log.created_at"
          :type="auditNodeType(log.action)"
          placement="top"
        >
          <h4 class="timeline-title">{{ log.action }}</h4>
          <p class="timeline-meta">
            操作人：{{ log.actor_id || '—' }}
            <span v-if="log.ip_address"> · IP：{{ log.ip_address }}</span>
          </p>
          <p v-if="log.comment" class="timeline-comment">{{ log.comment }}</p>
        </el-timeline-item>
      </el-timeline>

      <el-divider />

      <h3 class="section-title">复核操作</h3>
      <el-form label-position="top">
        <el-form-item label="复核意见">
          <el-input
            v-model="comment"
            type="textarea"
            :rows="3"
            placeholder="请输入复核意见或补充资料要求"
            aria-describedby="comment-help"
          />
          <span id="comment-help" class="sr-only">
            该意见将随复核动作一并提交,并记录到审计日志
          </span>
        </el-form-item>
        <el-form-item>
          <el-button type="success" :loading="submitting" :disabled="!evaluation" @click="approve">
            通过复核
          </el-button>
          <el-button type="danger" :loading="submitting" :disabled="!evaluation" @click="reject">
            驳回
          </el-button>
          <el-button
            type="warning"
            :loading="submitting"
            :disabled="!evaluation"
            @click="requestMoreInfo"
          >
            要求补充资料
          </el-button>
          <el-button :loading="submitting" :disabled="!evaluation" @click="goBack">
            返回
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<script setup>
import { computed, ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { evaluationApi } from '@/api/client'
import { statusTagType, riskTagType } from '@/utils/evaluationStatus'

const route = useRoute()
const router = useRouter()

const evaluationId = computed(() => route.params.id)
const loading = ref(false)
const submitting = ref(false)
const evaluation = ref(null)
const auditLogs = ref([])
const comment = ref('')
// 屏幕阅读器实时通告文本
const liveStatus = ref('')

const employeeView = computed(() => evaluation.value?.employee_view || {})
const managerView = computed(() => evaluation.value?.manager_view || {})

// 申诉记录:审计日志中动作包含 appeal 或 feedback 的条目
const appealLogs = computed(() =>
  auditLogs.value.filter((log) => {
    const action = (log.action || '').toLowerCase()
    return action.includes('appeal') || action.includes('feedback')
  }),
)

const statusType = computed(() => statusTagType(evaluation.value?.status))

// 审批历史时间线节点颜色:通过=success,驳回=danger,其余=primary
function auditNodeType(action) {
  const act = (action || '').toLowerCase()
  if (act.includes('approve')) return 'success'
  if (act.includes('reject')) return 'danger'
  return 'primary'
}

async function loadEvaluation() {
  loading.value = true
  liveStatus.value = '正在加载评估详情'
  try {
    const data = await evaluationApi.get(evaluationId.value)
    evaluation.value = data
  } catch (err) {
    console.error('加载评估失败:', err)
    ElMessage.error('加载评估失败')
    liveStatus.value = '加载评估失败'
  } finally {
    loading.value = false
  }
}

async function loadAuditLogs() {
  try {
    const data = await evaluationApi.auditLogs(evaluationId.value)
    // 兼容后端返回 {logs: []} 或直接数组两种结构
    auditLogs.value = Array.isArray(data) ? data : data?.logs || []
  } catch (err) {
    console.error('加载审批历史失败:', err)
    // 审批历史加载失败不阻断主流程,仅提示
    ElMessage.warning('审批历史加载失败')
    auditLogs.value = []
  }
}

// 通用操作前置:二次确认并收集意见
async function confirmAction(title, promptText) {
  try {
    const { value } = await ElMessageBox.prompt(promptText, title, {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      inputType: 'textarea',
      inputValue: comment.value,
    })
    return value
  } catch {
    // 用户取消,静默返回
    return null
  }
}

async function approve() {
  if (!evaluation.value) return
  const value = await confirmAction('通过复核', '请输入复核意见(可选)')
  if (value === null) return
  submitting.value = true
  liveStatus.value = '正在提交通过复核'
  try {
    await evaluationApi.approve(evaluationId.value, { comment: value })
    ElMessage.success('已通过复核')
    comment.value = ''
    await Promise.all([loadEvaluation(), loadAuditLogs()])
  } catch (err) {
    ElMessage.error(err.message || '操作失败')
    liveStatus.value = '通过复核失败'
  } finally {
    submitting.value = false
  }
}

async function reject() {
  if (!evaluation.value) return
  const value = await confirmAction('驳回评估', '请输入驳回理由')
  if (value === null) return
  if (!value || !value.trim()) {
    ElMessage.warning('驳回必须填写理由')
    return
  }
  submitting.value = true
  liveStatus.value = '正在提交驳回'
  try {
    await evaluationApi.reject(evaluationId.value, { comment: value })
    ElMessage.success('已驳回')
    comment.value = ''
    await Promise.all([loadEvaluation(), loadAuditLogs()])
  } catch (err) {
    ElMessage.error(err.message || '操作失败')
    liveStatus.value = '驳回失败'
  } finally {
    submitting.value = false
  }
}

// 要求补充资料:复用 feedback 接口提交 HR 端补充资料请求(后端无独立端点,以 feedback 形式留痕)
async function requestMoreInfo() {
  if (!evaluation.value) return
  const value = await confirmAction('要求补充资料', '请说明需要补充的资料或澄清点')
  if (value === null) return
  if (!value || !value.trim()) {
    ElMessage.warning('请填写需要补充的资料说明')
    return
  }
  submitting.value = true
  liveStatus.value = '正在提交补充资料要求'
  try {
    await evaluationApi.feedback(evaluationId.value, {
      // 以类型字段标识这是 HR 端的补充资料请求,便于后续按类型筛选
      type: 'hr_request_more_info',
      content: value,
    })
    ElMessage.success('已要求补充资料')
    comment.value = ''
    await loadAuditLogs()
  } catch (err) {
    ElMessage.error(err.message || '操作失败')
    liveStatus.value = '提交补充资料要求失败'
  } finally {
    submitting.value = false
  }
}

function goBack() {
  router.push('/hr')
}

onMounted(async () => {
  await Promise.all([loadEvaluation(), loadAuditLogs()])
})
</script>

<style scoped>
.hr-audit-detail {
  padding: 10px;
}
.detail-skeleton {
  margin-top: 20px;
}
.detail-card {
  margin-top: 20px;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}
.card-title {
  font-weight: 600;
}
.section-title {
  margin: 8px 0 12px;
  font-size: 16px;
  font-weight: 600;
}
.risk-alert {
  margin-bottom: 12px;
}
.empty-line {
  color: #606266;
}
.timeline-title {
  margin: 0 0 4px;
  font-size: 14px;
  font-weight: 600;
}
.timeline-meta {
  margin: 0 0 4px;
  color: #606266;
  font-size: 13px;
}
.timeline-comment {
  margin: 0;
  color: #303133;
  font-size: 13px;
}
/* 屏幕阅读器专用文本:视觉隐藏但可被辅助技术读取 */
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
</style>
