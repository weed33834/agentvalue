<template>
  <div class="employee-input">
    <el-card>
      <template #header>
        <span>录入本周工作数据</span>
      </template>

      <el-form ref="formRef" label-position="top" :model="form" :rules="rules">
        <el-form-item label="评估周期" prop="period">
          <el-input v-model="form.period" placeholder="例如：2026-W25" @keyup.enter="submit" />
        </el-form-item>

        <el-form-item label="日报内容" prop="content">
          <el-input
            v-model="form.content"
            type="textarea"
            :rows="6"
            placeholder="请描述本周工作内容、成果、遇到的阻塞等"
          />
        </el-form-item>

        <el-form-item label="任务进度">
          <el-input
            v-model="form.tasks"
            type="textarea"
            :rows="3"
            placeholder="例如：JIRA-2048 进度 100%；JIRA-2051 进度 60%"
          />
        </el-form-item>

        <!-- 附件上传：Phase 7 多模态后端已就绪，这里补前端入口,组装进 raw_inputs.attachments -->
        <el-form-item label="附件（可选）">
          <div class="attachment-upload">
            <el-upload
              :show-file-list="false"
              :auto-upload="true"
              :http-request="handleUpload"
              accept="image/*,application/pdf,audio/*"
              multiple
            >
              <el-button :loading="uploadingCount > 0" aria-label="上传附件，支持图片、PDF、音频">
                <el-icon><UploadFilled /></el-icon>
                <span>上传附件</span>
              </el-button>
              <template #tip>
                <span class="upload-tip">
                  支持图片 / PDF / 音频，单文件 ≤20MB，最多 20 个；上传后随日报一并提交评估
                </span>
              </template>
            </el-upload>

            <!-- 附件清单与上传状态，aria-live 通告屏幕阅读器 -->
            <ul
              v-if="attachments.length"
              class="attachment-list"
              aria-live="polite"
              aria-label="附件上传状态"
            >
              <li v-for="att in attachments" :key="att.uid" class="attachment-item">
                <div class="att-row">
                  <el-icon class="att-icon"><Document /></el-icon>
                  <span class="att-name" :title="att.filename">{{ att.filename }}</span>
                  <span class="att-size">{{ formatSize(att.size) }}</span>
                  <el-tag v-if="att.status === 'success'" size="small" type="success">
                    已上传
                  </el-tag>
                  <el-tag v-else-if="att.status === 'error'" size="small" type="danger">
                    失败
                  </el-tag>
                  <el-tag v-else size="small" type="info">上传中 {{ att.progress }}%</el-tag>
                  <el-button
                    v-if="att.status !== 'uploading'"
                    link
                    type="danger"
                    :aria-label="`删除附件 ${att.filename}`"
                    @click="removeAttachment(att.uid)"
                  >
                    删除
                  </el-button>
                </div>
                <el-progress
                  v-if="att.status === 'uploading'"
                  :percentage="att.progress"
                  :stroke-width="4"
                  :show-text="false"
                />
                <div v-if="att.status === 'error'" class="att-error" role="alert">
                  {{ att.error }}
                </div>
              </li>
            </ul>
          </div>
        </el-form-item>

        <el-form-item>
          <el-button
            type="primary"
            :loading="evalStore.loading || polling"
            :disabled="!isFormValid"
            @click="submit"
          >
            提交并生成评估
          </el-button>
        </el-form-item>
      </el-form>

      <!-- 无障碍：评估结果在轮询中动态更新，用 role=status + aria-live 通告屏幕阅读器 -->
      <el-result
        v-if="resultVisible"
        role="status"
        aria-live="polite"
        :icon="resultIcon"
        :title="resultTitle"
        :sub-title="resultSubtitle"
      >
        <template #extra>
          <el-button type="primary" @click="goDashboard">查看成长看板</el-button>
        </template>
      </el-result>
    </el-card>
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

const formRef = ref(null)

// 计算当前 ISO 周期，例如 2026-W26
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

const form = reactive({
  period: currentIsoWeek(),
  content: '',
  tasks: '',
})

const rules = {
  period: [
    { required: true, message: '请输入评估周期', trigger: 'blur' },
    {
      pattern: /^\d{4}-W(?:0[1-9]|[1-4]\d|5[0-3])$/,
      message: '周期格式不正确，例如：2026-W25',
      trigger: 'blur',
    },
  ],
  content: [{ required: true, message: '请输入日报内容', trigger: 'blur' }],
}

const isFormValid = computed(() => {
  return (
    /^\d{4}-W(?:0[1-9]|[1-4]\d|5[0-3])$/.test(form.period.trim()) && form.content.trim().length > 0
  )
})

const resultVisible = ref(false)
const resultIcon = ref('success')
const resultTitle = ref('')
const resultSubtitle = ref('')
const polling = ref(false)

// 附件上传状态：每项 {uid,filename,size,mime,key,url,status,progress,error}
const attachments = ref([])
const uploadingCount = computed(
  () => attachments.value.filter((a) => a.status === 'uploading').length,
)
const MAX_ATTACHMENTS = 20

// el-upload 自定义上传：调 /attachments 拿到 {key,url,filename,size,mime}
async function handleUpload(option) {
  const { file, onProgress, onSuccess, onError } = option
  if (file.size > 20 * 1024 * 1024) {
    ElMessage.warning('单文件不能超过 20MB')
    onError(new Error('文件过大'))
    return
  }
  if (attachments.value.length >= MAX_ATTACHMENTS) {
    ElMessage.warning(`附件数量上限 ${MAX_ATTACHMENTS} 个`)
    onError(new Error('超出附件数量上限'))
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
    const res = await inputApi.uploadAttachment(file, (evt) => {
      if (evt.total) {
        item.progress = Math.min(99, Math.round((evt.loaded / evt.total) * 100))
        onProgress({ percent: item.progress })
      }
    })
    item.key = res.key
    item.url = res.url
    item.filename = res.filename || item.filename
    item.size = res.size ?? item.size
    item.mime = res.mime || item.mime
    item.progress = 100
    item.status = 'success'
    onSuccess(res)
  } catch (err) {
    item.status = 'error'
    item.error = err.message || '上传失败'
    ElMessage.error(`${file.name} 上传失败：${item.error}`)
    onError(err)
  }
}

function removeAttachment(uid) {
  const idx = attachments.value.findIndex((a) => a.uid === uid)
  if (idx >= 0) attachments.value.splice(idx, 1)
}

function formatSize(bytes) {
  if (!bytes && bytes !== 0) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

// 仅保留上传成功的附件,组装成后端期望的结构
function collectedAttachments() {
  return attachments.value
    .filter((a) => a.status === 'success')
    .map((a) => ({
      key: a.key,
      url: a.url,
      filename: a.filename,
      size: a.size,
      mime: a.mime,
    }))
}

function genId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    const v = c === 'x' ? r : (r & 0x3) | 0x8
    return v.toString(16)
  })
}

async function submit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) {
    ElMessage.warning('请检查表单填写是否正确')
    return
  }
  if (!auth.userId) {
    ElMessage.error('用户信息缺失，请重新登录')
    return
  }
  // 上传中不允许提交,避免附件漏入评估
  if (uploadingCount.value > 0) {
    ElMessage.warning('附件上传中，请等待完成后再提交')
    return
  }
  const collected = collectedAttachments()
  const rawInputs = [
    {
      input_id: `daily-${genId()}`,
      type: 'daily_report',
      content: form.content,
      attachments: collected,
    },
  ]
  if (form.tasks.trim()) {
    rawInputs.push({
      input_id: `task-${genId()}`,
      type: 'task_progress',
      content: form.tasks,
      attachments: [],
    })
  }

  try {
    resultVisible.value = true
    resultIcon.value = 'info'
    resultTitle.value = '评估任务已提交'
    resultSubtitle.value = '正在后台生成，请稍候...'
    polling.value = true

    const { job_id } = await evalStore.createEvaluation({
      employee_id: auth.userId,
      period: form.period,
      raw_inputs: rawInputs,
    })

    const job = await evalStore.pollJob(job_id, (job) => {
      if (job.status === 'pending') {
        resultSubtitle.value = 'AI 正在处理中，请稍候...'
      }
    })

    if (job.status === 'failed') {
      throw new Error(job.error || '评估任务失败')
    }

    resultIcon.value = 'success'
    resultTitle.value = '评估已生成'
    resultSubtitle.value = `状态：${evalStore.currentEvaluation?.status}，综合得分：${evalStore.currentEvaluation?.overall_score}`
    // 提交成功后重置表单与附件，便于继续录入下一周期
    form.content = ''
    form.tasks = ''
    attachments.value = []
  } catch (err) {
    resultIcon.value = 'error'
    resultTitle.value = '生成失败'
    resultSubtitle.value = err.message
  } finally {
    polling.value = false
  }
}

function goDashboard() {
  router.push('/employee')
}

onBeforeUnmount(() => {
  cancelPolling()
})
</script>

<style scoped>
.attachment-upload {
  width: 100%;
}
.upload-tip {
  display: block;
  margin-top: 4px;
  /* 无障碍：12px 小字加深至约 7.5:1 对比度 */
  color: #4b5563;
  font-size: 12px;
  line-height: 1.6;
}
.attachment-list {
  margin: 12px 0 0;
  padding: 0;
  list-style: none;
}
.attachment-item {
  padding: 8px 0;
  border-bottom: 1px solid #f0f0f0;
}
.attachment-item:last-child {
  border-bottom: none;
}
.att-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.att-icon {
  color: #909399;
  flex-shrink: 0;
}
.att-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #303133;
}
.att-size {
  color: #909399;
  font-size: 12px;
  flex-shrink: 0;
}
.att-error {
  margin-top: 4px;
  color: #c45656;
  font-size: 12px;
}
</style>
