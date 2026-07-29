<template>
  <div class="m-page">
    <div v-if="loading" class="m-empty">加载中…</div>
    <div v-else-if="error" class="m-login__error">{{ error }}</div>
    <div v-else-if="items.length" class="m-list">
      <div
        v-for="ev in items"
        :key="ev.id"
        class="m-list-item"
        style="flex-direction: column; align-items: flex-start; gap: 4px"
      >
        <div class="m-list-item__title">{{ ev.period }}</div>
        <div class="m-list-item__sub">
          得分 {{ ev.overall_score ?? '—' }} ·
          <span :class="statusClass(ev.status)">{{ statusLabel(ev.status) }}</span>
        </div>
      </div>
    </div>
    <div v-else class="m-empty">暂无历史评估</div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { employeeApi } from '@/api/client'

const auth = useAuthStore()
const loading = ref(false)
const error = ref('')
const items = ref([])

function statusLabel(s) {
  return { approved: '已通过', rejected: '已驳回', manager_review: '待主管', hr_audit: '待HR', ai_drafted: '草稿' }[s] || s || '未知'
}
function statusClass(s) {
  if (s === 'approved') return 'm-badge'
  if (s === 'rejected') return 'm-badge m-badge--danger'
  return 'm-badge m-badge--warn'
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await employeeApi.history(auth.userId)
    items.value = data.evaluations || data.items || []
  } catch (e) {
    error.value = '加载失败：' + (e?.message || e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>
