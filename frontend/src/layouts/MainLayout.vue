<template>
  <el-container class="main-layout">
    <!-- 安全合规：管理视图水印防截图（仅 manager/hr/admin 显示，employee 隐藏） -->
    <Watermark v-if="['manager', 'hr', 'admin'].includes(auth.role)" />
    <!-- 无障碍：跳转到主内容，键盘用户可快速跳过导航 -->
    <a href="#main-content" class="skip-link">跳转到主内容</a>
    <el-aside width="220px" class="sidebar">
      <div class="logo" role="heading" aria-level="1">AgentValue-AI</div>
      <el-menu
        :default-active="activeMenu"
        class="menu"
        router
        aria-label="主导航"
        background-color="#1f2937"
        text-color="#e5e7eb"
        active-text-color="#2563eb"
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
        </template>

        <el-menu-item aria-label="退出登录" @click="handleLogout">
          <el-icon><SwitchButton /></el-icon>
          <span>退出登录</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="header" role="banner">
        <span class="header-title">{{ pageTitle }}</span>
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
            v-if="notification.pendingCount > 0"
            :value="notification.pendingCount"
            :max="99"
            class="approval-badge"
          >
            <el-icon class="bell-icon" @click="goToApprovalDashboard"><Bell /></el-icon>
          </el-badge>
          <span class="header-role" aria-live="polite">当前角色：{{ roleLabel }}</span>
        </div>
      </el-header>
      <el-main id="main-content" class="main-content" tabindex="-1">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useNotificationStore } from '@/stores/notification'
import { useThemeStore } from '@/stores/theme'
import Watermark from '@/components/Watermark.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const notification = useNotificationStore()
const theme = useThemeStore()

const activeMenu = computed(() => route.path)

const roleLabel = computed(() => {
  const map = { employee: '员工', manager: '主管', hr: 'HR', admin: '管理员' }
  return map[auth.role] || auth.role
})

const pageTitle = computed(() => route.meta.title || 'AgentValue-AI')

// 按角色跳转到对应的审批看板:manager → 团队诊断,hr → HR复核,admin → 团队诊断
function goToApprovalDashboard() {
  if (auth.role === 'hr') {
    router.push('/hr')
  } else {
    router.push('/manager')
  }
}

function handleLogout() {
  notification.stopPolling()
  auth.logout()
  router.push('/login')
}

onMounted(() => {
  notification.startPolling(auth.role)
})

onUnmounted(() => {
  notification.stopPolling()
})
</script>

<style scoped>
.main-layout {
  height: 100vh;
}
/* 无障碍：跳转链接默认隐藏，键盘聚焦时显现 */
.skip-link {
  position: absolute;
  left: -9999px;
  top: 0;
  z-index: 1000;
  padding: 8px 16px;
  /* 无障碍：加深底色使白色文字对比度达到 AA（原 #409eff 对白文字仅约 2.8:1） */
  background: #2563eb;
  color: #fff;
  border-radius: 0 0 4px 0;
  text-decoration: none;
  font-size: 14px;
}
.skip-link:focus {
  left: 0;
}
/* 主内容区获得焦点时去除默认轮廓偏移，保留可见焦点环 */
#main-content:focus {
  outline: none;
}
.sidebar {
  background-color: #1f2937;
  color: #fff;
}
.logo {
  height: 60px;
  line-height: 60px;
  text-align: center;
  font-size: 18px;
  font-weight: bold;
  border-bottom: 1px solid #374151;
}
.menu {
  border-right: none;
}
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background-color: #fff;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.08);
  z-index: 10;
}
/* 暗色模式：头部/主内容区跟随 Element Plus 暗色变量 */
html.dark .header {
  background-color: var(--el-bg-color);
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.4);
}
html.dark .main-content {
  background-color: var(--el-bg-color-page);
}
.header-title {
  font-size: 16px;
  font-weight: 600;
}
.header-right {
  display: flex;
  align-items: center;
  gap: 16px;
}
.theme-toggle {
  /* 与 bell-icon 视觉对齐 */
  border: 1px solid var(--el-border-color);
  color: var(--el-text-color-regular);
}
.theme-toggle:hover {
  color: var(--el-color-primary);
  border-color: var(--el-color-primary);
}
.approval-badge {
  display: inline-flex;
  align-items: center;
}
.bell-icon {
  font-size: 20px;
  color: #606266;
  cursor: pointer;
}
.bell-icon:hover {
  color: #2563eb;
}
.header-role {
  font-size: 14px;
  color: #606266;
}
.main-content {
  background-color: #f3f4f6;
  overflow-y: auto;
}
</style>
