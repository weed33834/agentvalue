<template>
  <div class="m-page">
    <div class="m-card">
      <div class="m-card__title">发起反馈 / 申诉</div>

      <div class="m-field">
        <span class="m-field__label">选择评估</span>
        <div class="m-list" style="margin: 0">
          <div
            v-for="ev in evaluations"
            :key="ev.evaluation_id"
            class="m-list-item m-list-item--select"
            :class="{ 'm-list-item--active': selected && selected.evaluation_id === ev.evaluation_id }"
            @click="handleSelect(ev)"
          >
            <div class="m-list-item__title">{{ ev.period || '—' }} · {{ ev.overall_score ?? '—' }}分</div>
            <div class="m-list-item__sub">{{ ev.status || '—' }}</div>
          </div>
          <div v-if="!evaluations.length" class="m-empty" style="padding: 16px">
            暂无已评估记录
          </div>
        </div>
      </div>

      <div class="m-field">
        <span class="m-field__label">类型</span>
        <div class="m-seg">
          <button class="m-seg__item" :class="{ 'm-seg__item--active': form.type === 'feedback' }" @click="form.type = 'feedback'">
            反馈
          </button>
          <button class="m-seg__item" :class="{ 'm-seg__item--active': form.type === 'appeal' }" @click="form.type = 'appeal'">
            申诉
          </button>
        </div>
      </div>

      <label class="m-field">
        <span class="m-field__label">{{ form.type === 'appeal' ? '申诉理由' : '反馈内容' }}</span>
        <textarea v-model="form.content" class="m-textarea" rows="4" placeholder="请填写内容"></textarea>
      </label>

      <button class="m-btn" style="width: 100%" :disabled="!canSubmit || submitting" @click="submit">
        {{ submitting ? '提交中…' : '提交' }}
      </button>
    </div>

    <p class="m-section-title">我的反馈记录</p>
    <div v-if="recordsLoading" class="m-empty">加载中…</div>
    <div v-else class="m-list">
      <div v-for="(r, i) in records" :key="i" class="m-list-item" style="flex-direction: column; align-items: flex-start; gap: 4px">
        <div class="m-list-item__title">
          {{ r.type === 'appeal' ? '申诉' : '反馈' }} · {{ r.period || '—' }}
          <span class="m-tag" :class="statusClass(r.status)">{{ statusText(r.status) }}</span>
        </div>
        <div class="m-list-item__sub">{{ r.content || r.comment || '—' }}</div>
        <div v-if="r.reply" class="m-list-item__sub" style="color: var(--el-color-primary)">回复：{{ r.reply }}</div>
      </div>
      <div v-if="!records.length" class="m-empty" style="padding: 16px">暂无反馈记录</div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { employeeApi, evaluationApi } from '@/api/client'

const auth = useAuthStore()
const evaluations = ref([])
const records = ref([])
const selected = ref(null)
const submitting = ref(false)
const recordsLoading = ref(false)
const form = reactive({ type: 'feedback', content: '' })

const canSubmit = computed(() => !!selected.value && form.content.trim().length > 0)

function handleSelect(ev) {
  selected.value = ev
  form.content = ''
}

function statusClass(s) {
  return { approved: 'm-tag--ok', rejected: 'm-tag--err', pending: 'm-tag--warn' }[s] || ''
}
function statusText(s) {
  return { approved: '已处理', rejected: '已驳回', pending: '处理中' }[s] || s || '—'
}

async function loadData() {
  try {
    const data = await employeeApi.history(auth.userId)
    evaluations.value = data.evaluations || []
  } catch (e) {
    ElMessage.error('加载评估列表失败：' + (e?.message || e))
  }
}

async function loadRecords() {
  recordsLoading.value = true
  try {
    const data = await employeeApi.feedback(auth.userId)
    records.value = data.feedback || []
  } catch (e) {
    ElMessage.error('加载反馈记录失败：' + (e?.message || e))
  } finally {
    recordsLoading.value = false
  }
}

async function submit() {
  if (!selected.value) {
    ElMessage.warning('请先选择一条评估')
    return
  }
  submitting.value = true
  try {
    const evaluationId = selected.value.evaluation_id
    if (form.type === 'appeal') {
      await evaluationApi.appeal(evaluationId, { comment: form.content })
      ElMessage.success('申诉已提交')
    } else {
      await evaluationApi.feedback(evaluationId, { content: form.content, type: 'feedback' })
      ElMessage.success('反馈已提交')
    }
    form.content = ''
    await loadRecords()
  } catch (e) {
    ElMessage.error('提交失败：' + (e?.message || e))
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  loadData()
  loadRecords()
})
</script>
