<template>
  <div class="m-page">
    <div v-if="loading" class="m-empty">加载中…</div>
    <div v-else-if="error" class="m-login__error">{{ error }}</div>

    <template v-else-if="latest">
      <div class="m-stat-grid">
        <div class="m-stat">
          <div class="m-stat__value">{{ latest.overall_score }}</div>
          <div class="m-stat__label">综合得分 · {{ latest.period }}</div>
        </div>
        <div class="m-stat">
          <div class="m-stat__value">{{ growthAreas.length }}</div>
          <div class="m-stat__label">成长维度</div>
        </div>
      </div>

      <div class="m-card">
        <div class="m-card__title">评估总结</div>
        <p style="margin: 0; line-height: 1.65">{{ view.summary || '暂无总结' }}</p>
      </div>

      <p class="m-section-title">我的优势</p>
      <div class="m-list">
        <div v-for="(s, i) in view.strengths || []" :key="i" class="m-list-item">
          <span class="m-list-item__title">✅ {{ s }}</span>
        </div>
        <div v-if="!(view.strengths || []).length" class="m-empty" style="padding: 16px">
          暂无数据
        </div>
      </div>

      <p class="m-section-title">成长方向</p>
      <div class="m-list">
        <div
          v-for="a in growthAreas"
          :key="a.dimension"
          class="m-list-item"
          style="flex-direction: column; align-items: flex-start; gap: 4px"
        >
          <div class="m-list-item__title">{{ a.dimension }} · <b>{{ a.score }}</b> 分</div>
          <div class="m-list-item__sub">{{ (a.improvement_actions || []).join('；') }}</div>
        </div>
      </div>

      <p class="m-section-title">下周聚焦</p>
      <div>
        <span
          v-for="(f, i) in view.next_week_focus || []"
          :key="i"
          class="m-badge"
          style="margin: 0 6px 6px 0; display: inline-block"
        >{{ f }}</span>
      </div>

      <p class="m-section-title">更多功能</p>
      <div class="m-list">
        <div class="m-list-item" @click="go('/m/employee/feedback')">
          <span class="m-list-item__title">反馈 / 申诉</span>
          <span class="m-list-item__arrow">›</span>
        </div>
        <div class="m-list-item" @click="go('/m/employee/growth-path')">
          <span class="m-list-item__title">成长路径推荐</span>
          <span class="m-list-item__arrow">›</span>
        </div>
        <div class="m-list-item" @click="go('/m/employee/history')">
          <span class="m-list-item__title">历史评估</span>
          <span class="m-list-item__arrow">›</span>
        </div>
      </div>
    </template>

    <div v-else class="m-empty">暂无已审批的评估数据</div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { employeeApi } from '@/api/client'

const router = useRouter()
const auth = useAuthStore()
const loading = ref(false)
const error = ref('')
const evaluations = ref([])

function go(path) {
  router.push(path)
}

const latest = computed(() => evaluations.value[0] || null)
const view = computed(() => latest.value?.employee_view || {})
const growthAreas = computed(() => view.value.growth_areas || [])

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await employeeApi.dashboard(auth.userId)
    evaluations.value = data.evaluations || []
  } catch (e) {
    error.value = '加载失败：' + (e?.message || e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>
