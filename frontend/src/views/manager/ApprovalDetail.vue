<template>
  <div class="approval-detail">
    <el-page-header @back="goBack" title="评估审批" />

    <el-card v-if="evaluation" v-loading="loading" :aria-busy="loading" class="detail-card">
      <template #header>
        <div class="card-header">
          <span>评估详情 — {{ evaluation.employee_id }} / {{ evaluation.period }}</span>
          <el-tag :type="statusType">{{ evaluation.status }}</el-tag>
        </div>
      </template>

      <el-row :gutter="20">
        <el-col :span="12">
          <h3>员工视图（建设性）</h3>
          <p><strong>总结：</strong>{{ employeeView.summary }}</p>
          <p><strong>优势：</strong>{{ (employeeView.strengths || []).join('；') }}</p>
          <p><strong>下周聚焦：</strong>{{ (employeeView.next_week_focus || []).join('；') }}</p>
        </el-col>
        <el-col :span="12">
          <h3>管理视图（尖锐诊断）</h3>
          <p><strong>总体判断：</strong>{{ managerView.harsh_assessment }}</p>
          <p><strong>ROI 分析：</strong>{{ managerView.roi_analysis }}</p>
          <p><strong>调配建议：</strong>{{ managerView.reallocation_suggestion }}</p>
          <p><strong>隐藏问题：</strong>{{ (managerView.hidden_issues || []).join('；') }}</p>
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

      <el-divider />

      <h3>审批操作</h3>
      <el-form label-position="top">
        <el-form-item label="审批意见">
          <el-input v-model="comment" type="textarea" :rows="3" placeholder="请输入审批意见" />
        </el-form-item>
        <el-form-item>
          <el-button type="success" :loading="submitting" @click="approve">通过</el-button>
          <el-button type="danger" :loading="submitting" @click="reject">驳回</el-button>
          <el-button type="warning" :loading="submitting" @click="requestHrReview">
            提交 HR 复核
          </el-button>
          <el-button :loading="submitting" @click="reEvaluate">重新评估</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 无障碍：骨架屏加载态用 role=status 通告屏幕阅读器 -->
    <el-skeleton v-else-if="loading" :rows="6" animated role="status" aria-label="评估详情加载中" />

    <el-empty v-else description="未找到评估数据" />
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
const comment = ref('')

const employeeView = computed(() => evaluation.value?.employee_view || {})
const managerView = computed(() => evaluation.value?.manager_view || {})

const statusType = computed(() => statusTagType(evaluation.value?.status))

async function loadEvaluation() {
  loading.value = true
  try {
    const data = await evaluationApi.get(evaluationId.value)
    evaluation.value = data
  } catch (err) {
    console.error('加载评估失败:', err)
    ElMessage.error('加载评估失败')
  } finally {
    loading.value = false
  }
}

async function approve() {
  if (!evaluation.value) return
  submitting.value = true
  try {
    await evaluationApi.approve(evaluationId.value, {
      comment: comment.value,
    })
    ElMessage.success('操作成功')
    router.push('/manager')
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    submitting.value = false
  }
}

async function reject() {
  if (!evaluation.value) return
  if (!comment.value.trim()) {
    ElMessage.warning('驳回请填写理由')
    return
  }
  submitting.value = true
  try {
    await evaluationApi.reject(evaluationId.value, {
      comment: comment.value,
    })
    ElMessage.success('操作成功')
    router.push('/manager')
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    submitting.value = false
  }
}

async function requestHrReview() {
  if (!evaluation.value) return
  submitting.value = true
  try {
    await evaluationApi.requestHrReview(evaluationId.value, {
      comment: comment.value,
    })
    ElMessage.success('操作成功')
    router.push('/manager')
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    submitting.value = false
  }
}

async function reEvaluate() {
  if (!evaluation.value) return
  try {
    await ElMessageBox.confirm(
      '确认基于现有输入重新运行 AI 评估？将生成新的草稿结果。',
      '重新评估',
      { confirmButtonText: '确认', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  submitting.value = true
  try {
    const res = await evaluationApi.reEvaluate(evaluationId.value, {
      feedback: comment.value ? [comment.value] : [],
    })
    ElMessage.success(`已重新评估，状态：${res.status}`)
    comment.value = ''
    await loadEvaluation()
  } catch (err) {
    ElMessage.error(err.message || '重新评估失败')
  } finally {
    submitting.value = false
  }
}

function goBack() {
  router.push('/manager')
}

onMounted(loadEvaluation)
</script>

<style scoped>
.approval-detail {
  padding: 10px;
}
.detail-card {
  margin-top: 20px;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.risk-alert {
  margin-bottom: 12px;
}
</style>
