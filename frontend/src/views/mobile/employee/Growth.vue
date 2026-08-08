<template>
  <div class="m-page">
    <div v-if="loading" class="m-empty">加载中…</div>
    <div v-else-if="error" class="m-login__error">{{ error }}</div>
    <template v-else>
      <div v-if="data.status && data.status !== 'ok'" class="m-card">
        <p style="margin: 0; font-size: 13px; color: var(--el-color-warning)">{{ statusText }}</p>
      </div>

      <div class="m-card">
        <div class="m-card__title">推荐方向</div>
        <p style="margin: 0 0 6px">
          <span class="m-badge" :class="directionClass" style="font-size: 15px">{{ direction }}</span>
        </p>
        <p v-if="reason" style="margin: 0; font-size: 13px; color: var(--el-text-color-secondary); line-height: 1.6">
          {{ reason }}
        </p>
        <p style="margin: 10px 0 0; font-size: 12px; color: var(--el-text-color-secondary)">
          分析窗口：{{ data.window_weeks || 0 }} 周
        </p>
      </div>

      <template v-if="growthTrend.length">
        <p class="m-section-title">成长趋势</p>
        <div class="m-card" style="padding: 4px 0">
          <div v-for="t in growthTrend" :key="t.period" class="m-attach">
            <span class="m-attach__name">{{ t.period }}</span>
            <span class="m-tag m-tag--go">{{ t.score }} 分</span>
          </div>
        </div>
      </template>

      <p class="m-section-title">核心优势</p>
      <div class="m-list">
        <div v-for="(s, i) in strengths" :key="i" class="m-list-item">
          <span class="m-list-item__title">{{ s.item }}</span>
          <span class="m-tag">{{ s.frequency }}次</span>
        </div>
        <div v-if="!strengths.length" class="m-empty" style="padding: 16px">暂无数据</div>
      </div>

      <p class="m-section-title">高频成长领域</p>
      <div class="m-list">
        <div v-for="(g, i) in growthAreas" :key="i" class="m-list-item">
          <span class="m-list-item__title">{{ g.dimension }}</span>
          <span class="m-tag m-tag--warn">{{ g.frequency }}次</span>
        </div>
        <div v-if="!growthAreas.length" class="m-empty" style="padding: 16px">暂无数据</div>
      </div>

      <template v-if="actions.length">
        <p class="m-section-title">建议行动</p>
        <div class="m-list">
          <div v-for="(a, i) in actions" :key="i" class="m-list-item">
            <span class="m-list-item__title">{{ a.action || a }}</span>
          </div>
        </div>
      </template>

      <button class="m-btn m-btn--ghost" style="width: 100%; margin-top: 12px" @click="load">刷新</button>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { analyticsApi } from '@/api/client'

const auth = useAuthStore()
const loading = ref(false)
const error = ref('')
const data = ref({})

const growthTrend = computed(() => data.value.growth_trend || [])
const strengths = computed(() => data.value.strengths || [])
const growthAreas = computed(() => data.value.growth_areas || [])
const actions = computed(() => data.value.suggested_actions || [])
const direction = computed(() => data.value.recommended_direction?.direction || '—')
const reason = computed(() => data.value.recommended_direction?.reason || '')

const directionClass = computed(() => {
  const map = { 技术深耕: 'm-badge--go', 管理转型: 'm-badge--warn', 跨领域: 'm-badge' }
  return map[direction.value] || 'm-badge--go'
})

const statusText = computed(() => {
  if (data.value.status === 'no_data') return '暂无历史评估数据，无法生成成长路径'
  if (data.value.status === 'insufficient_data') return '历史评估不足 4 周，结果仅供参考，建议持续积累'
  return ''
})

async function load() {
  if (!auth.userId) {
    error.value = '用户信息缺失，请重新登录'
    return
  }
  loading.value = true
  error.value = ''
  try {
    data.value = await analyticsApi.growthPath(auth.userId)
  } catch (e) {
    error.value = '加载失败：' + (e?.message || e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>
