<template>
  <el-container class="main-layout">
    <!-- 安全合规：管理视图水印防截图（仅 manager/hr/admin 显示，employee 隐藏） -->
    <Watermark v-if="['manager', 'hr', 'admin'].includes(auth.role)" />
    <!-- 无障碍：跳转到主内容，键盘用户可快速跳过导航 -->
    <a href="#main-content" class="skip-link">跳转到主内容</a>

    <!-- 移动端遮罩层 -->
    <transition name="fade">
      <div v-if="mobileSidebarVisible" class="sidebar-overlay" @click="closeMobileSidebar"></div>
    </transition>

    <!-- 侧边栏 -->
    <el-aside
      :width="asideWidth"
      class="sidebar"
      :class="{ 'sidebar--mobile-open': mobileSidebarVisible }"
    >
      <div class="logo" role="heading" aria-level="1">
        <BrandLogo :size="26" :text-size="17" />
      </div>
      <el-menu
        :default-active="activeMenu"
        class="menu"
        router
        aria-label="主导航"
        background-color="transparent"
        text-color="#e5e7eb"
        active-text-color="#60a5fa"
        @select="handleMenuSelect"
      >
        <template v-if="auth.role === 'employee'">
          <el-menu-item index="/employee">
            <el-icon><TrendCharts /></el-icon>
            <span>成长看板</span>
          </el-menu-item>
          <el-menu-item index="/employee/input">
            <el-icon><Document /></el-icon>
            <span>录入日报</span>
          </el-menu-item>
          <el-menu-item index="/employee/history">
            <el-icon><Timer /></el-icon>
            <span>历史评估</span>
          </el-menu-item>
          <el-menu-item index="/employee/feedback">
            <el-icon><ChatDotRound /></el-icon>
            <span>反馈申诉</span>
          </el-menu-item>
          <el-menu-item index="/employee/growth-path">
            <el-icon><Aim /></el-icon>
            <span>成长路径</span>
          </el-menu-item>
          <el-menu-item index="/employee/assistant">
            <el-icon><ChatRound /></el-icon>
            <span>AI 助手</span>
          </el-menu-item>
        </template>

        <template v-if="auth.role === 'hr' || auth.role === 'admin'">
          <el-menu-item index="/hr">
            <el-icon><View /></el-icon>
            <span>HR复核</span>
          </el-menu-item>
          <el-menu-item index="/hr/assistant">
            <el-icon><ChatRound /></el-icon>
            <span>AI 助手</span>
          </el-menu-item>
        </template>

        <template v-if="auth.role === 'manager' || auth.role === 'admin'">
          <el-menu-item index="/manager">
            <el-icon><UserFilled /></el-icon>
            <span>团队诊断</span>
          </el-menu-item>
        </template>

        <template v-if="auth.role === 'manager' || auth.role === 'hr' || auth.role === 'admin'">
          <el-menu-item index="/manager/team">
            <el-icon><DataAnalysis /></el-icon>
            <span>团队分析</span>
          </el-menu-item>
          <el-menu-item index="/manager/roi">
            <el-icon><Coin /></el-icon>
            <span>团队ROI</span>
          </el-menu-item>
          <el-menu-item index="/manager/attrition-risk">
            <el-icon><Warning /></el-icon>
            <span>离职风险</span>
          </el-menu-item>
          <el-menu-item index="/manager/reviews-360">
            <el-icon><ChatDotSquare /></el-icon>
            <span>360° 环评</span>
          </el-menu-item>
          <el-menu-item index="/manager/calibration">
            <el-icon><ScaleToOriginal /></el-icon>
            <span>校准会</span>
          </el-menu-item>
          <el-menu-item index="/manager/assistant">
            <el-icon><ChatRound /></el-icon>
            <span>AI 助手</span>
          </el-menu-item>
        </template>

        <template v-if="auth.role === 'admin'">
          <el-menu-item index="/admin">
            <el-icon><Setting /></el-icon>
            <span>模型管理</span>
          </el-menu-item>
          <el-menu-item index="/admin/llm-config">
            <el-icon><Key /></el-icon>
            <span>LLM 配置</span>
          </el-menu-item>
          <el-menu-item index="/admin/prompts">
            <el-icon><Document /></el-icon>
            <span>Prompt 管理</span>
          </el-menu-item>
          <el-menu-item index="/admin/providers">
            <el-icon><Connection /></el-icon>
            <span>模型供应商</span>
          </el-menu-item>
          <el-menu-item index="/admin/playground">
            <el-icon><Cpu /></el-icon>
            <span>Prompt 调试台</span>
          </el-menu-item>
          <el-menu-item index="/admin/chat">
            <el-icon><ChatRound /></el-icon>
            <span>AI 助手</span>
          </el-menu-item>
          <el-menu-item index="/admin/feature-flags">
            <el-icon><Switch /></el-icon>
            <span>功能开关</span>
          </el-menu-item>
          <el-menu-item index="/admin/multi-agent">
            <el-icon><Share /></el-icon>
            <span>多 Agent 协作</span>
          </el-menu-item>
          <el-menu-item index="/admin/workflows">
            <el-icon><Connection /></el-icon>
            <span>工作流编排</span>
          </el-menu-item>
          <el-menu-item index="/admin/talent-matrix">
            <el-icon><Grid /></el-icon>
            <span>人才九宫格</span>
          </el-menu-item>
          <el-menu-item index="/admin/tools">
            <el-icon><Tools /></el-icon>
            <span>工具管理</span>
          </el-menu-item>
          <el-menu-item index="/admin/skills">
            <el-icon><MagicStick /></el-icon>
            <span>技能管理</span>
          </el-menu-item>
          <el-menu-item index="/admin/agent-presets">
            <el-icon><Shop /></el-icon>
            <span>Agent预设</span>
          </el-menu-item>
          <el-menu-item index="/admin/debug">
            <el-icon><Monitor /></el-icon>
            <span>调试追踪</span>
          </el-menu-item>
          <el-menu-item index="/admin/trace">
            <el-icon><Histogram /></el-icon>
            <span>链路追踪</span>
          </el-menu-item>
          <el-menu-item index="/admin/knowledge-base">
            <el-icon><Collection /></el-icon>
            <span>知识库</span>
          </el-menu-item>
          <el-menu-item index="/admin/audit-logs">
            <el-icon><Tickets /></el-icon>
            <span>审计日志</span>
          </el-menu-item>
          <el-menu-item index="/admin/metrics">
            <el-icon><DataLine /></el-icon>
            <span>系统指标</span>
          </el-menu-item>
          <el-menu-item index="/admin/webhooks">
            <el-icon><Connection /></el-icon>
            <span>Webhook 事件</span>
          </el-menu-item>
          <el-menu-item index="/admin/api-keys">
            <el-icon><Key /></el-icon>
            <span>API Key 管理</span>
          </el-menu-item>
          <el-menu-item index="/admin/users">
            <el-icon><User /></el-icon>
            <span>用户管理</span>
          </el-menu-item>
          <el-menu-item index="/admin/export">
            <el-icon><Download /></el-icon>
            <span>数据导出</span>
          </el-menu-item>
          <el-menu-item index="/admin/rlhf">
            <el-icon><TrendCharts /></el-icon>
            <span>RLHF 反馈</span>
          </el-menu-item>
          <el-menu-item index="/admin/alerts">
            <el-icon><BellFilled /></el-icon>
            <span>告警管理</span>
          </el-menu-item>
          <el-menu-item index="/admin/quota-budget">
            <el-icon><Coin /></el-icon>
            <span>配额与预算</span>
          </el-menu-item>
          <el-menu-item index="/admin/scheduler">
            <el-icon><Timer /></el-icon>
            <span>定时任务</span>
          </el-menu-item>
          <el-menu-item index="/admin/security">
            <el-icon><Lock /></el-icon>
            <span>安全治理</span>
          </el-menu-item>
          <el-menu-item index="/admin/model-ops">
            <el-icon><Cpu /></el-icon>
            <span>模型运维</span>
          </el-menu-item>
          <el-menu-item index="/admin/release-ops">
            <el-icon><Promotion /></el-icon>
            <span>发布运维</span>
          </el-menu-item>
          <el-menu-item index="/admin/eval-center">
            <el-icon><DataAnalysis /></el-icon>
            <span>评测中心</span>
          </el-menu-item>
          <el-menu-item index="/admin/knowledge-ops">
            <el-icon><MagicStick /></el-icon>
            <span>知识增强</span>
          </el-menu-item>
          <el-menu-item index="/admin/agent-templates">
            <el-icon><Box /></el-icon>
            <span>Agent 模板</span>
          </el-menu-item>
          <el-menu-item index="/admin/billing">
            <el-icon><Wallet /></el-icon>
            <span>计费管理</span>
          </el-menu-item>
        </template>

      </el-menu>
      <!-- 退出登录按钮（独立于 el-menu，避免 router 模式冲突） -->
      <div class="logout-section">
        <el-button text class="logout-btn" @click="handleLogout">
          <el-icon><SwitchButton /></el-icon>
          <span>退出登录</span>
        </el-button>
      </div>
    </el-aside>

    <el-container class="main-container">
      <!-- 顶部导航 -->
      <el-header class="header" role="banner">
        <div class="header-left">
          <!-- 移动端汉堡菜单 -->
          <el-button
            class="hamburger-btn av-hide-desktop"
            text
            :aria-label="mobileSidebarVisible ? '关闭菜单' : '打开菜单'"
            @click="toggleMobileSidebar"
          >
            <el-icon :size="22">
              <Close v-if="mobileSidebarVisible" />
              <Menu v-else />
            </el-icon>
          </el-button>
          <span class="header-title">{{ pageTitle }}</span>
        </div>
        <div class="header-right">
          <el-tooltip
            :content="theme.isDark ? '切换到亮色模式' : '切换到暗色模式'"
            placement="bottom"
          >
            <el-button
              class="theme-toggle"
              circle
              size="small"
              :aria-label="theme.isDark ? '切换到亮色模式' : '切换到暗色模式'"
              @click="theme.toggle"
            >
              <el-icon v-if="theme.isDark"><Sunny /></el-icon>
              <el-icon v-else><Moon /></el-icon>
            </el-button>
          </el-tooltip>
          <el-badge
            :value="notification.pendingCount + notification.unreadCount"
            :max="99"
            class="approval-badge"
          >
            <el-icon class="bell-icon" @click="handleBellClick"><Bell /></el-icon>
          </el-badge>
          <span class="header-role av-hide-mobile" aria-live="polite">当前角色：{{ roleLabel }}</span>
        </div>
      </el-header>

      <!-- 页面内容区 -->
      <el-main id="main-content" class="main-content" tabindex="-1">
        <router-view v-slot="{ Component, route }">
          <transition name="fade-slide" mode="out-in" appear>
            <component :is="Component" :key="route.path" />
          </transition>
        </router-view>
      </el-main>
    </el-container>

    <!-- 通知抽屉 -->
    <el-drawer
      v-model="notification.notificationDrawerVisible"
      title="站内通知"
      :size="drawerSize"
      direction="rtl"
    >
      <div class="notification-drawer">
        <div class="notification-actions">
          <el-button size="small" @click="notification.markAllAsRead" :disabled="notification.unreadCount === 0">
            全部标记已读
          </el-button>
          <el-button size="small" @click="notification.fetchNotifications({ page: 1, page_size: 20 })">
            刷新
          </el-button>
        </div>
        <el-empty v-if="notification.notifications.length === 0" description="暂无通知" />
        <div
          v-for="item in notification.notifications"
          :key="item.notification_id"
          class="notification-item"
          :class="{ unread: !item.is_read }"
          @click="notification.markAsRead(item.notification_id)"
        >
          <div class="notification-title">{{ item.title }}</div>
          <div class="notification-content">{{ item.content }}</div>
          <div class="notification-time">{{ item.created_at }}</div>
        </div>
      </div>
    </el-drawer>
  </el-container>
</template>

<script setup>
import { computed, ref, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useNotificationStore } from '@/stores/notification'
import { useThemeStore } from '@/stores/theme'
import Watermark from '@/components/Watermark.vue'
import BrandLogo from '@/components/BrandLogo.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const notification = useNotificationStore()
const theme = useThemeStore()

const activeMenu = computed(() => route.path)
const mobileSidebarVisible = ref(false)
const windowWidth = ref(typeof window !== 'undefined' ? window.innerWidth : 1200)

const asideWidth = computed(() => {
  if (windowWidth.value <= 768) {
    return mobileSidebarVisible.value ? '240px' : '0px'
  }
  return '220px'
})

const drawerSize = computed(() => {
  return windowWidth.value <= 768 ? '85%' : '400px'
})

const roleLabel = computed(() => {
  const map = { employee: '员工', manager: '主管', hr: 'HR', admin: '管理员' }
  return map[auth.role] || auth.role
})

const pageTitle = computed(() => route.meta.title || 'AgentValue')

function toggleMobileSidebar() {
  mobileSidebarVisible.value = !mobileSidebarVisible.value
}

function closeMobileSidebar() {
  mobileSidebarVisible.value = false
}

function handleMenuSelect() {
  // 移动端选择菜单后自动关闭侧边栏
  if (windowWidth.value <= 768) {
    mobileSidebarVisible.value = false
  }
}

// 路由变化时关闭移动端侧边栏
watch(() => route.path, () => {
  if (windowWidth.value <= 768) {
    mobileSidebarVisible.value = false
  }
})

function handleResize() {
  windowWidth.value = window.innerWidth
  // 大屏幕时自动关闭移动端侧边栏
  if (windowWidth.value > 768) {
    mobileSidebarVisible.value = false
  }
}

// 铃铛点击:有未读通知则打开通知抽屉,否则跳转审批看板
function handleBellClick() {
  if (notification.unreadCount > 0 || notification.notifications.length > 0) {
    notification.openNotificationDrawer()
  } else if (['manager', 'hr', 'admin'].includes(auth.role)) {
    if (auth.role === 'hr') {
      router.push('/hr')
    } else {
      router.push('/manager')
    }
  } else {
    notification.openNotificationDrawer()
  }
}

async function handleLogout() {
  notification.stopPolling()
  await auth.logout()
  router.push('/login')
}

onMounted(() => {
  notification.startPolling(auth.role)
  window.addEventListener('resize', handleResize)
})

onUnmounted(() => {
  notification.stopPolling()
  window.removeEventListener('resize', handleResize)
})
</script>

<style scoped>
.main-layout {
  height: 100vh;
  overflow: hidden;
}

/* 无障碍：跳转链接默认隐藏，键盘聚焦时显现 */
.skip-link {
  position: absolute;
  left: -9999px;
  top: 0;
  z-index: 1000;
  padding: 8px 16px;
  background: #2563eb;
  color: #fff;
  border-radius: 0 0 4px 0;
  text-decoration: none;
  font-size: 14px;
  transition: left var(--av-transition-fast);
}
.skip-link:focus {
  left: 0;
}
#main-content:focus {
  outline: none;
}

/* ==================== 侧边栏 ==================== */
.sidebar {
  background: linear-gradient(180deg, #1a1f2e 0%, #1f2937 100%);
  color: #fff;
  transition: width var(--av-transition-base) var(--av-ease-out),
    transform var(--av-transition-base) var(--av-ease-out);
  overflow: hidden;
  position: relative;
  z-index: 1001;
}

/* 侧边栏顶部 Logo */
.logo {
  height: var(--av-header-height);
  display: flex;
  align-items: center;
  justify-content: center;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  color: #ffffff;
  overflow: hidden;
}

/* 菜单样式 */
.menu {
  border-right: none;
  height: calc(100vh - var(--av-header-height) - 60px);
  overflow-y: auto;
  overflow-x: hidden;
}
.menu :deep(.el-menu-item) {
  transition: all var(--av-transition-fast) var(--av-ease-smooth);
  border-radius: 8px;
  margin: 2px 8px;
  width: calc(100% - 16px);
}
.menu :deep(.el-menu-item:hover) {
  background-color: rgba(96, 165, 250, 0.1) !important;
}
.menu :deep(.el-menu-item.is-active) {
  background: linear-gradient(135deg, rgba(37, 99, 235, 0.2), rgba(96, 165, 250, 0.1)) !important;
  border-left: 3px solid #60a5fa;
}

/* 菜单滚动条 */
.menu::-webkit-scrollbar {
  width: 4px;
}
.menu::-webkit-scrollbar-thumb {
  background: rgba(255, 255, 255, 0.1);
  border-radius: 2px;
}
.menu::-webkit-scrollbar-thumb:hover {
  background: rgba(255, 255, 255, 0.2);
}

/* 退出登录按钮 */
.logout-section {
  padding: 8px 12px;
  border-top: 1px solid rgba(255, 255, 255, 0.08);
}
.logout-btn {
  width: 100%;
  justify-content: flex-start;
  color: #e5e7eb;
  height: 44px;
  border-radius: 8px;
  transition: all var(--av-transition-fast) var(--av-ease-smooth);
}
.logout-btn:hover {
  background-color: rgba(239, 68, 68, 0.1) !important;
  color: #f87171 !important;
}
html.dark .logout-btn {
  color: var(--el-text-color-regular);
}

/* ==================== 移动端遮罩 ==================== */
.sidebar-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(2px);
  z-index: 1000;
}

/* ==================== 主容器 ==================== */
.main-container {
  height: 100vh;
  overflow: hidden;
}

/* ==================== 头部 ==================== */
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: var(--av-header-height) !important;
  padding: 0 20px;
  background-color: var(--el-bg-color);
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.06);
  z-index: 10;
  transition: box-shadow var(--av-transition-base);
}
html.dark .header {
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.3);
}
.header-left {
  display: flex;
  align-items: center;
  gap: 12px;
}
.header-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--el-text-color-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.header-right {
  display: flex;
  align-items: center;
  gap: 16px;
}

/* 汉堡菜单按钮 */
.hamburger-btn {
  padding: 8px !important;
  color: var(--el-text-color-primary) !important;
}

/* 主题切换按钮 */
.theme-toggle {
  border: 1px solid var(--el-border-color);
  color: var(--el-text-color-regular);
  transition: all var(--av-transition-fast) var(--av-ease-smooth) !important;
}
.theme-toggle:hover {
  color: var(--el-color-primary);
  border-color: var(--el-color-primary);
  transform: rotate(15deg);
}

/* 铃铛图标 */
.approval-badge {
  display: inline-flex;
  align-items: center;
}
.bell-icon {
  font-size: 20px;
  color: var(--el-text-color-regular);
  cursor: pointer;
  transition: all var(--av-transition-fast) var(--av-ease-spring);
}
.bell-icon:hover {
  color: var(--el-color-primary);
  transform: scale(1.1) rotate(10deg);
}

/* 角色标签 */
.header-role {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  padding: 4px 12px;
  border-radius: 20px;
  background: var(--el-fill-color-light);
}

/* ==================== 主内容区 ==================== */
.main-content {
  background-color: var(--el-bg-color-page);
  overflow-y: auto;
  padding: 20px;
  transition: background-color var(--av-transition-base);
}
html.dark .main-content {
  background-color: var(--el-bg-color-page);
}

/* ==================== 通知抽屉 ==================== */
.notification-drawer {
  padding: 0 8px;
}
.notification-actions {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
}
.notification-item {
  padding: 12px 16px;
  border-radius: var(--av-radius-md);
  margin-bottom: 8px;
  cursor: pointer;
  transition: all var(--av-transition-fast) var(--av-ease-smooth);
  border: 1px solid var(--el-border-color-lighter);
  animation: fadeInUp var(--av-transition-base) var(--av-ease-out) both;
}
.notification-item:hover {
  background-color: var(--el-fill-color-light);
  transform: translateX(-4px);
  box-shadow: var(--av-shadow-sm);
}
.notification-item.unread {
  background-color: var(--el-color-primary-light-9);
  border-color: var(--el-color-primary-light-7);
}
.notification-item:nth-child(1) { animation-delay: 0.02s; }
.notification-item:nth-child(2) { animation-delay: 0.06s; }
.notification-item:nth-child(3) { animation-delay: 0.1s; }
.notification-item:nth-child(4) { animation-delay: 0.14s; }
.notification-item:nth-child(5) { animation-delay: 0.18s; }
.notification-title {
  font-weight: 600;
  font-size: 14px;
  margin-bottom: 4px;
}
.notification-content {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  margin-bottom: 4px;
  word-break: break-all;
}
.notification-time {
  font-size: 12px;
  color: var(--el-text-color-placeholder);
}

/* ==================== 过渡动画 ==================== */
.fade-enter-active,
.fade-leave-active {
  transition: opacity var(--av-transition-base) var(--av-ease-out);
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

/* ==================== 移动端样式 ==================== */
@media (max-width: 768px) {
  .sidebar {
    position: fixed;
    top: 0;
    left: 0;
    bottom: 0;
    width: 240px !important;
    transform: translateX(-100%);
    z-index: 1001;
    box-shadow: var(--av-shadow-xl);
  }
  .sidebar--mobile-open {
    transform: translateX(0);
  }

  .header {
    padding: 0 12px;
  }
  .header-title {
    font-size: 15px;
    max-width: 180px;
  }
  .header-right {
    gap: 8px;
  }
  .header-role {
    display: none;
  }

  .main-content {
    padding: 12px;
  }

  .notification-drawer {
    padding: 0 4px;
  }
  .notification-item {
    padding: 10px 12px;
  }
}

/* ==================== 平板端样式 ==================== */
@media (min-width: 769px) and (max-width: 1024px) {
  .main-content {
    padding: 16px;
  }
}

/* ==================== 大屏样式 ==================== */
@media (min-width: 1920px) {
  .main-content {
    padding: 32px;
  }
}
</style>
