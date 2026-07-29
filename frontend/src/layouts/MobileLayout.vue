<template>
  <div class="mobile-shell">
    <!-- 合规水印：与桌面端一致，manager/hr/admin 显示 -->
    <Watermark v-if="showWatermark" />

    <!-- 顶栏 -->
    <header class="m-topbar" role="banner">
      <div class="m-topbar__title">{{ pageTitle }}</div>
      <div class="m-topbar__actions">
        <button
          class="m-icon-btn"
          :aria-label="theme.isDark ? '切换到亮色' : '切换到暗色'"
          @click="theme.toggle"
        >
          {{ theme.isDark ? '☀️' : '🌙' }}
        </button>
        <button class="m-icon-btn" aria-label="退出登录" @click="handleLogout">⏻</button>
      </div>
    </header>

    <!-- 内容区 -->
    <main class="m-content" id="m-main-content" tabindex="-1">
      <router-view v-slot="{ Component, route }">
        <transition name="m-fade" mode="out-in">
          <component :is="Component" :key="route.path" />
        </transition>
      </router-view>
    </main>

    <!-- 底部 Tab 栏（角色感知，完全替代桌面侧边栏） -->
    <nav v-if="tabs.length" class="m-tabbar" role="navigation" aria-label="主导航">
      <button
        v-for="tab in tabs"
        :key="tab.path"
        class="m-tab"
        :class="{ 'm-tab--active': isActive(tab.path) }"
        :aria-current="isActive(tab.path) ? 'page' : undefined"
        @click="go(tab.path)"
      >
        <span class="m-tab__icon">{{ tab.icon }}</span>
        <span class="m-tab__label">{{ tab.label }}</span>
      </button>
    </nav>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useThemeStore } from '@/stores/theme'
import { tabsForRole } from '@/utils/mobileNav'
import Watermark from '@/components/Watermark.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const theme = useThemeStore()

// 底部 Tab 仅按当前角色展示对应入口
const tabs = computed(() => tabsForRole(auth.role))
const pageTitle = computed(() => route.meta.title || 'AgentValue')
// 合规：管理视图水印（与桌面端 MainLayout 一致）
const showWatermark = computed(() => ['manager', 'hr', 'admin'].includes(auth.role))

function isActive(path) {
  return route.path === path || route.path.startsWith(path + '/')
}

function go(path) {
  if (route.path !== path) router.push(path)
}

async function handleLogout() {
  await auth.logout()
  router.push('/m/login')
}
</script>
