<template>
  <div class="m-page m-placeholder">
    <div class="m-placeholder__icon">🖥️</div>
    <h2 class="m-placeholder__title">{{ title }}</h2>
    <p class="m-placeholder__desc">
      此功能以桌面端操作为主（表格 / 图表 / 复杂表单），<br />
      建议在电脑上打开以获得完整体验。
    </p>

    <button class="m-btn" @click="goDesktop">前往桌面端使用 →</button>
    <button class="m-btn m-btn--ghost" style="margin-top: 10px" @click="goHome">
      返回移动端首页
    </button>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const title = computed(() => route.meta.title || '该功能')

// 桌面端等价路径：去掉 /m 前缀即可（/m/manager/roi → /manager/roi）
const desktopPath = computed(() => {
  const p = route.path.replace(/^\/m/, '')
  return (p || '/') + '?desktop=1'
})

function goDesktop() {
  router.push(desktopPath.value)
}

function goHome() {
  const home = { employee: '/m/employee', manager: '/m/manager', hr: '/m/hr', admin: '/m/admin' }
  router.push(home[auth.role] || '/m/login')
}
</script>

<style scoped>
.m-placeholder {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  min-height: 60dvh;
  padding: 24px;
}
.m-placeholder__icon {
  font-size: 52px;
  margin-bottom: 12px;
}
.m-placeholder__title {
  font-size: 18px;
  font-weight: 600;
  margin: 0 0 10px;
}
.m-placeholder__desc {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  line-height: 1.7;
  margin: 0 0 24px;
}
.m-placeholder .m-btn {
  max-width: 280px;
}
</style>
