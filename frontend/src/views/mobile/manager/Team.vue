<template>
  <div class="m-page">
    <div v-if="loading" class="m-empty">加载中…</div>
    <div v-else-if="error" class="m-login__error">{{ error }}</div>

    <template v-else>
      <div v-if="matrix.length" class="m-card">
        <div class="m-card__title">人才九宫格（绩效 × 潜力）</div>
        <div class="m-matrix">
          <div v-for="(cell, i) in matrix" :key="i" class="m-matrix__cell">
            <span class="m-matrix__count">{{ cell.count ?? cell.employees?.length ?? 0 }}</span>
            <span class="m-matrix__label">{{ cell.label || cell.quadrant || '—' }}</span>
          </div>
        </div>
      </div>

      <div class="m-card">
        <div class="m-card__title">深入分析</div>
        <p style="margin: 0 0 12px; color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.6">
          团队 ROI、离职风险、360° 环评等深度分析在手机上阅读体验有限，
          建议在桌面端查看完整图表。
        </p>
        <button class="m-btn m-btn--ghost" @click="goDesktop">在桌面端打开团队分析 →</button>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { analyticsApi } from '@/api/client'

const router = useRouter()
const loading = ref(false)
const error = ref('')
const matrix = ref([])

function goDesktop() {
  router.push('/manager/team?desktop=1')
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await analyticsApi.talentMatrix({})
    // 兼容不同返回结构：matrix / cells / data
    matrix.value = res?.matrix || res?.cells || res?.data || []
  } catch (e) {
    error.value = '加载失败：' + (e?.message || e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.m-matrix {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
}
.m-matrix__cell {
  background: var(--el-fill-color-light);
  border-radius: 10px;
  padding: 12px 8px;
  text-align: center;
}
.m-matrix__count {
  display: block;
  font-size: 22px;
  font-weight: 700;
  color: var(--el-color-primary);
}
.m-matrix__label {
  display: block;
  font-size: 11px;
  color: var(--el-text-color-secondary);
  margin-top: 2px;
}
</style>
