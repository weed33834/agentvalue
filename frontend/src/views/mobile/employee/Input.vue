<template>
  <div class="m-page">
    <div v-if="resultVisible" class="m-result">
      <div class="m-result__icon">{{ resultIcon }}</div>
      <div class="m-result__title">{{ resultTitle }}</div>
      <div class="m-result__sub">{{ resultSubtitle }}</div>
      <button v-if="resultIcon === '✅'" class="m-btn" style="margin-top: 20px" @click="resetForm">
        继续录入
      </button>
      <button class="m-btn m-btn--ghost" style="margin-top: 10px" @click="goDashboard">
        返回成长看板
      </button>
    </div>

    <template v-else>
      <div class="m-card">
        <div class="m-card__title">录入本周工作</div>

        <label class="m-field">
          <span class="m-field__label">评估周期</span>
          <input v-model="form.period" class="m-input" inputmode="text" placeholder="例如：2026-W25" />
          <span class="m-field__hint">格式：YYYY-W周数，默认当前 ISO 周</span>
        </label>

        <label class="m-field">
          <span class="m-field__label">日报内容 <i style="color: var(--el-color-danger)">*</i></span>
          <textarea
            v-model="form.content"
            class="m-textarea"
            placeholder="请描述本周工作内容、成果、遇到的阻塞等"
            rows="6"
          ></textarea>
        </label>

        <label class="m-field">
          <span class="m-field__label">任务进度</span>
          <textarea
            v-model="form.tasks"
            class="m-textarea"
            placeholder="例如：JIRA-2048 进度 100%；JIRA-2051 进度 60%"
            rows="3"
          ></textarea>
        </label>

        <div class="m-field">
          <span class="m-field__label">附件（可选）</span>
          <input
            type="file"
            multiple
            accept="image/*,application/pdf,audio/*"
            class="m-input"
            style="padding: 8px"
            @change="onFiles"
          />
          <span class="m-field__hint">图片 / PDF / 音频，单文件 ≤20MB，最多 20 个</span>
          <div v-if="attachments.length" style="margin-top: 8px">
            <div v-for="a in attachments" :key="a.uid" class="m-attach">
              <span class="m-attach__name">{{ a.filename }}</span>
              <span class="m-tag" :class="{ 'm-tag--ok': a.status === 'success', 'm-tag--err': a.status === 'error' }">
                {{ a.status === 'success' ? '已上传' : a.status === 'error' ? '失败' : `上传中 ${a.progress}%` }}
              </span>
              <button class="m-icon-btn" style="font-size: 16px" @click="removeAttachment(a.uid)">✕</button>
            </div>
          </div>
        </div>

        <button
          class="m-btn"
          style="width: 100%"
          :disabled="!isFormValid || uploadingCount > 0 || loading"
          @click="submit"
        >
          {{ loading ? '正在生成评估…' : uploadingCount > 0 ? '附件上传中…' : '提交并生成评估' }}
        </button>
      </div>
    </template>
  </div>
</template>

<script setup>
import { reactive, ref, computed, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useEvaluationStore, cancelPolling } from '@/stores/evaluation'
import { useAuthStore } from '@/stores/auth'
import { inputApi } from '@/api/client'

const router = useRouter()
const evalStore = useEvaluationStore()
const auth = useAuthStore()

function currentIsoWeek() {
  const now = new Date()
  const target = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()))
  const dayNum = (target.getUTCDay() + 6) % 7
  target.setUTCDate(target.getUTCDate() - dayNum + 3)
  const firstThursday = new Date(Date.UTC(target.getUTCFullYear(), 0, 4))
  const firstDayNum = (firstThursday.getUTCDay() + 6) % 7
  firstThursday.setUTCDate(firstThursday.getUTCDate() - firstDayNum + 3)
  const weekNum = 1 + Math.round((target - firstThursday) / (7 * 24 * 3600 * 1000))
  return `${target.getUTCFullYear()}-W${String(weekNum).padStart(2, '0')}`
}

const form = reactive({ period: currentIsoWeek(), content: '', tasks: '' })
const isFormValid = computed(
  () => /^\d{4}-W(?:0[1-9]|[1-4]\d|5[0-3])$/.test(form.period.trim()) && form.content.trim().length > 0,
)

const attachments = ref([])
const uploadingCount = computed(() => attachments.value.filter((a) => a.status === 'uploading').length)
const MAX_ATTACHMENTS = 20

function onFiles(evt) {
  const files = Array.from(evt.target.files || [])
  evt.target.value = ''
  files.forEach((file) => upload(file))
}

async function upload(file) {
  if (file.size > 20 * 1024 * 1024) {
    ElMessage.warning('单文件不能超过 20MB')
    return
  }
  if (attachments.value.length >= MAX_ATTACHMENTS) {
    ElMessage.warning(`附件数量上限 ${MAX_ATTACHMENTS} 个`)
    return
  }
  const uid = `${file.name}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  const item = reactive({
    uid,
    filename: file.name,
    size: file.size,
    mime: file.type,
    key: '',
    url: '',
    status: 'uploading',
    progress: 0,
    error: '',
  })
  attachments.value.push(item)
  try {
    const res = await inputApi.uploadAttachment(file, (evt2) => {
      if (evt2.total) item.progress = Math.min(99, Math.round((evt2.loaded / evt2.total) * 100))
    })
    item.key = res.key
    item.url = res.url
    item.filename = res.filename || item.filename
    item.size = res.size ?? item.size
    item.mime = res.mime || item.mime
    item.progress = 100
    item.status = 'success'
  } catch (err) {
    item.status = 'error'
    item.error = err.message || '上传失败'
    ElMessage.error(`${file.name} 上传失败`)
  }
}

function removeAttachment(uid) {
  const idx = attachments.value.findIndex((a) => a.uid === uid)
  if (idx >= 0) attachments.value.splice(idx, 1)
}

function genId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    const v = c === 'x' ? r : (r & 0x3) | 0x8
    return v.toString(16)
  })
}

const resultVisible = ref(false)
const resultIcon = ref('')
const resultTitle = ref('')
const resultSubtitle = ref('')
const loading = ref(false)

async function submit() {
  if (!isFormValid.value) {
    ElMessage.warning('请填写周期与日报内容')
    return
  }
  if (!auth.userId) {
    ElMessage.error('用户信息缺失，请重新登录')
    return
  }
  if (uploadingCount.value > 0) {
    ElMessage.warning('附件上传中，请等待完成后再提交')
    return
  }
  const collected = attachments.value
    .filter((a) => a.status === 'success')
    .map((a) => ({ key: a.key, url: a.url, filename: a.filename, size: a.size, mime: a.mime }))
  const rawInputs = [{ input_id: `daily-${genId()}`, type: 'daily_report', content: form.content, attachments: collected }]
  if (form.tasks.trim()) {
    rawInputs.push({ input_id: `task-${genId()}`, type: 'task_progress', content: form.tasks, attachments: [] })
  }
  loading.value = true
  resultVisible.value = true
  resultIcon.value = '⏳'
  resultTitle.value = '评估任务已提交'
  resultSubtitle.value = '正在后台生成，请稍候…'
  try {
    const { job_id } = await evalStore.createEvaluation({
      employee_id: auth.userId,
      period: form.period,
      raw_inputs: rawInputs,
    })
    const job = await evalStore.pollJob(job_id, (j) => {
      if (j.status === 'pending') resultSubtitle.value = 'AI 正在处理中，请稍候…'
    })
    if (job.status === 'failed') throw new Error(job.error || '评估任务失败')
    resultIcon.value = '✅'
    resultTitle.value = '评估已生成'
    resultSubtitle.value = `状态：${evalStore.currentEvaluation?.status}，综合得分：${evalStore.currentEvaluation?.overall_score}`
    form.content = ''
    form.tasks = ''
    attachments.value = []
  } catch (err) {
    resultIcon.value = '❌'
    resultTitle.value = '生成失败'
    resultSubtitle.value = err.message || '未知错误'
  } finally {
    loading.value = false
  }
}

function resetForm() {
  resultVisible.value = false
  form.period = currentIsoWeek()
}

function goDashboard() {
  router.push('/m/employee')
}

onBeforeUnmount(() => cancelPolling())
</script>
