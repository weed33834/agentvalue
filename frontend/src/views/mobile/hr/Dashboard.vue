<template>
  <div class="m-page">
    <div v-if="loading" class="m-empty">加载中…</div>
    <div v-else-if="error" class="m-login__error">{{ error }}</div>
    <div v-else-if="queue.length" class="m-list">
      <div
        v-for="item in queue"
        :key="item.id || item.evaluation_id"
        class="m-list-item"
        @click="goDetail(item)"
      >
        <div style="flex: 1">
          <div class="m-list-item__title">{{ item.employee_name || item.employee_id || '待复核评估' }}</div>
          <div class="m-list-item__sub">{{ item.period || '' }} · 提交于 {{ item.submitted_at || '—' }}</div>
        </div>
        <span class="m-badge m-badge--warn">待复核</span>
        <span class="m-list-item__arrow">›</span>
      </div>
    </div>
    <div v-else class="m-empty">复核队列为空 🎉</div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { hrApi } from '@/api/client'

const router = useRouter()
const loading = ref(false)
const error = ref('')
const queue = ref([])

function goDetail(item) {
  const id = item.id || item.evaluation_id
  if (id) router.push(`/m/hr/audit/${id}`)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await hrApi.auditQueue()
    queue.value = Array.isArray(res) ? res : res?.items || res?.queue || []
  } catch (e) {
    error.value = '加载失败：' + (e?.message || e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>
