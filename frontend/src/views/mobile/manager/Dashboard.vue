<template>
  <div class="m-page">
    <div v-if="loading" class="m-empty">加载中…</div>
    <div v-else-if="error" class="m-login__error">{{ error }}</div>

    <template v-else>
      <div class="m-stat-grid">
        <div class="m-stat">
          <div class="m-stat__value">{{ data.team_size ?? '—' }}</div>
          <div class="m-stat__label">团队人数</div>
        </div>
        <div class="m-stat">
          <div class="m-stat__value">{{ data.pending_approvals ?? data.pending_reviews ?? '—' }}</div>
          <div class="m-stat__label">待审批</div>
        </div>
      </div>

      <div v-if="data.summary" class="m-card">
        <div class="m-card__title">团队概览</div>
        <p style="margin: 0; line-height: 1.65">{{ data.summary }}</p>
      </div>

      <p class="m-section-title">团队分析</p>
      <div class="m-list">
        <div
          v-for="m in modules"
          :key="m.path"
          class="m-list-item"
          @click="go(m.path)"
        >
          <span class="m-list-item__title">{{ m.icon }} {{ m.label }}</span>
          <span class="m-list-item__arrow">›</span>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { managerApi } from '@/api/client'

const router = useRouter()
const loading = ref(false)
const error = ref('')
const data = ref({})

const modules = [
  { label: '团队 ROI', path: '/m/manager/roi', icon: '📈' },
  { label: '离职风险', path: '/m/manager/attrition-risk', icon: '⚠️' },
  { label: '人才九宫格', path: '/m/manager/team', icon: '🔢' },
  { label: '360° 环评', path: '/m/manager/reviews-360', icon: '🔄' },
  { label: '校准会', path: '/m/manager/calibration', icon: '⚖️' },
]

function go(path) {
  router.push(path)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = (await managerApi.dashboard()) || {}
  } catch (e) {
    error.value = '加载失败：' + (e?.message || e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>
