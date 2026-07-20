import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { authFlowState, isTokenExpired } from '@/utils/auth'

/**
 * 角色默认首页映射(单一来源):登录跳转与兜底路由共用,避免不一致。
 */
export function roleHome(role) {
  const map = {
    employee: '/employee',
    manager: '/manager',
    hr: '/hr',
    admin: '/admin',
  }
  return map[role] || '/login'
}

const routes = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/LoginView.vue'),
  },
  {
    path: '/employee',
    name: 'EmployeeLayout',
    component: () => import('@/layouts/MainLayout.vue'),
    meta: { role: ['employee'] },
    children: [
      {
        path: '',
        name: 'EmployeeDashboard',
        component: () => import('@/views/employee/EmployeeDashboard.vue'),
        meta: { title: '成长看板' },
      },
      {
        path: 'input',
        name: 'EmployeeInput',
        component: () => import('@/views/employee/EmployeeInput.vue'),
        meta: { title: '工作录入' },
      },
      {
        path: 'history',
        name: 'EmployeeHistory',
        component: () => import('@/views/employee/EmployeeHistory.vue'),
        meta: { title: '历史评估' },
      },
      {
        path: 'feedback',
        name: 'EmployeeFeedback',
        component: () => import('@/views/employee/EmployeeFeedback.vue'),
        meta: { title: '反馈申诉' },
      },
      {
        // Phase 9.2 员工成长路径推荐
        path: 'growth-path',
        name: 'EmployeeGrowthPath',
        component: () => import('@/views/employee/GrowthPath.vue'),
        meta: { title: '成长路径' },
      },
      {
        // AI 助手：流式对话 + 工具调用 + 引用（移植 opencode session/prompt）
        path: 'assistant',
        name: 'EmployeeAssistant',
        component: () => import('@/views/admin/ChatView.vue'),
        meta: { title: 'AI 助手' },
      },
    ],
  },
  {
    path: '/manager',
    name: 'ManagerLayout',
    component: () => import('@/layouts/MainLayout.vue'),
    meta: { role: ['manager', 'hr', 'admin'] },
    children: [
      {
        path: '',
        name: 'ManagerDashboard',
        component: () => import('@/views/manager/ManagerDashboard.vue'),
        meta: { title: '主管看板' },
      },
      {
        path: 'approval/:id',
        name: 'ApprovalDetail',
        component: () => import('@/views/manager/ApprovalDetail.vue'),
        meta: { title: '审批详情' },
      },
      {
        path: 'team',
        name: 'TeamAnalytics',
        component: () => import('@/views/manager/TeamAnalytics.vue'),
        meta: { title: '团队分析' },
      },
      {
        // Phase 9.2 团队 ROI 仪表盘
        path: 'roi',
        name: 'TeamROI',
        component: () => import('@/views/manager/TeamROI.vue'),
        meta: { title: '团队ROI' },
      },
      {
        // Phase 9.2 离职风险预警
        path: 'attrition-risk',
        name: 'AttritionRisk',
        component: () => import('@/views/manager/AttritionRisk.vue'),
        meta: { title: '离职风险' },
      },
      {
        // HR 评估增强: 360° 环评 (发起 / 填写 / 汇总)
        path: 'reviews-360',
        name: 'Review360',
        component: () => import('@/views/manager/Review360View.vue'),
        meta: { title: '360° 环评' },
      },
      {
        // HR 评估增强: 校准会 (列表 / 详情 / 调整 / 完成)
        path: 'calibration',
        name: 'CalibrationView',
        component: () => import('@/views/manager/CalibrationView.vue'),
        meta: { title: '校准会' },
      },
      {
        // AI 助手：流式对话 + 工具调用 + 引用（移植 opencode session/prompt）
        path: 'assistant',
        name: 'ManagerAssistant',
        component: () => import('@/views/admin/ChatView.vue'),
        meta: { title: 'AI 助手' },
      },
    ],
  },
  {
    path: '/hr',
    name: 'HRLayout',
    component: () => import('@/layouts/MainLayout.vue'),
    meta: { role: ['hr', 'admin'] },
    children: [
      {
        path: '',
        name: 'HRDashboard',
        component: () => import('@/views/hr/HRDashboard.vue'),
        meta: { title: 'HR复核' },
      },
      {
        // HR 复核详情页:查看评估完整内容、审批历史、申诉记录并执行复核动作
        path: 'audit/:id',
        name: 'HRAuditDetail',
        component: () => import('@/views/hr/HRAuditDetail.vue'),
        meta: { title: 'HR复核详情' },
      },
      {
        // AI 助手：流式对话 + 工具调用 + 引用（移植 opencode session/prompt）
        path: 'assistant',
        name: 'HRAssistant',
        component: () => import('@/views/admin/ChatView.vue'),
        meta: { title: 'AI 助手' },
      },
    ],
  },
  {
    path: '/admin',
    name: 'AdminLayout',
    component: () => import('@/layouts/MainLayout.vue'),
    meta: { role: ['admin'] },
    children: [
      {
        path: '',
        name: 'AdminModel',
        component: () => import('@/views/admin/AdminModel.vue'),
        meta: { title: '模型管理' },
      },
      {
        // LLM 配置中心：admin 在前端输入 API Key / base_url / 模型名等
        path: 'llm-config',
        name: 'AdminLLMConfig',
        component: () => import('@/views/admin/AdminLLMConfig.vue'),
        meta: { title: 'LLM 配置' },
      },
      {
        path: 'audit-logs',
        name: 'AdminAuditLogs',
        component: () => import('@/views/admin/AdminAuditLogs.vue'),
        meta: { title: '审计日志' },
      },
      {
        // Phase 6 可观测性入口：解析 /metrics 关键指标,4 个数字卡片
        path: 'metrics',
        name: 'AdminMetrics',
        component: () => import('@/views/admin/AdminMetrics.vue'),
        meta: { title: '系统指标' },
      },
      {
        // P1 Prompt 管理：模板/版本/标签 CRUD + Diff + Rollback + A-B + 灰度
        path: 'prompts',
        name: 'AdminPrompts',
        component: () => import('@/views/admin/AdminPrompts.vue'),
        meta: { title: 'Prompt 管理' },
      },
      {
        // P1 工具管理：工具列表 + 测试 + MCP 服务器 + ReAct Agent
        path: 'tools',
        name: 'AdminTools',
        component: () => import('@/views/admin/AdminTools.vue'),
        meta: { title: '工具管理' },
      },
      {
        // P1 调试与可观测性：Prompt 版本追溯 + Trace 链路 + 系统健康
        path: 'debug',
        name: 'AdminDebug',
        component: () => import('@/views/admin/AdminDebug.vue'),
        meta: { title: '调试追踪' },
      },
      {
        // P1-2 应用内 Trace 可视化：评估列表 + Span 树 + 时间线 Gantt(对标 Langfuse Trace UI)
        path: 'trace',
        name: 'AdminTrace',
        component: () => import('@/views/admin/AdminTrace.vue'),
        meta: { title: '链路追踪' },
      },
      {
        // P1-1 知识库管理：文档 CRUD + 重建索引 + 检索测试台 + 分块配置
        path: 'knowledge-base',
        name: 'AdminKnowledgeBase',
        component: () => import('@/views/admin/AdminKnowledgeBase.vue'),
        meta: { title: '知识库' },
      },
      {
        // P2 深水区: Provider CRUD(对标 Dify model-providers)
        path: 'providers',
        name: 'AdminProviders',
        component: () => import('@/views/admin/AdminProviders.vue'),
        meta: { title: '模型供应商' },
      },
      {
        // P2 深水区: Prompt Playground(对标 Langfuse Playground)
        path: 'playground',
        name: 'AdminPlayground',
        component: () => import('@/views/admin/AdminPlayground.vue'),
        meta: { title: 'Prompt 调试台' },
      },
      {
        // P3-2: Feature Flag 系统(应用级功能开关, 对标 Langfuse Feature Flag)
        path: 'feature-flags',
        name: 'AdminFeatureFlags',
        component: () => import('@/views/admin/AdminFeatureFlags.vue'),
        meta: { title: '功能开关' },
      },
      {
        // P4-1: 多 Agent 协作(supervisor 模式, 对标 Coze Multi-Agent)
        path: 'multi-agent',
        name: 'AdminMultiAgent',
        component: () => import('@/views/admin/AdminMultiAgent.vue'),
        meta: { title: '多 Agent 协作' },
      },
      {
        // P4-2: 工作流可视化编排(对标 Dify Workflow / Coze Bot 编排)
        path: 'workflows',
        name: 'AdminWorkflows',
        component: () => import('@/views/admin/AdminWorkflows.vue'),
        meta: { title: '工作流编排' },
      },
      {
        // HR 评估增强: 人才九宫格 (绩效 × 潜力 3x3 矩阵)
        path: 'talent-matrix',
        name: 'AdminTalentMatrix',
        component: () => import('@/views/admin/AdminTalentMatrix.vue'),
        meta: { title: '人才九宫格' },
      },
      {
        // Agent 预设市场 (对标 ChatGPT GPTs / LobeChat 助手市场 / Coze Bot 商店)
        path: '/admin/agent-presets',
        name: 'AgentPresets',
        component: () => import('@/views/admin/AgentPresets.vue'),
        meta: { title: 'Agent预设', icon: 'Collection', roles: ['admin', 'hr'] },
      },
      {
        // AI 助手：流式对话 + 工具调用 + 引用（移植 opencode session/prompt）
        path: 'chat',
        name: 'AdminChat',
        component: () => import('@/views/admin/ChatView.vue'),
        meta: { title: 'AI 助手' },
      },
    ],
  },
  {
    path: '/',
    redirect: '/login',
  },
  {
    path: '/:pathMatch(.*)*',
    redirect: () => {
      // 兜底路由：已登录用户按角色跳转对应首页，未登录跳 /login
      const auth = useAuthStore()
      if (auth.isLoggedIn) return roleHome(auth.role)
      return '/login'
    },
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach(async (to, from, next) => {
  const auth = useAuthStore()
  if (to.path === '/login') {
    next()
    return
  }
  if (!auth.isLoggedIn) {
    authFlowState.authChecked = false
    next('/login')
    return
  }
  // JWT 过期时主动清理并跳转登录
  if (auth.useJwt && auth.token && isTokenExpired(auth.token)) {
    auth.logout()
    next('/login')
    return
  }
  if (!authFlowState.authChecked) {
    authFlowState.authChecked = true
    const ok = await auth.checkAuth()
    if (!ok) {
      next('/login')
      return
    }
  }
  const requiredRole = to.meta.role || to.matched.find((r) => r.meta.role)?.meta.role
  if (requiredRole) {
    const roles = Array.isArray(requiredRole) ? requiredRole : [requiredRole]
    if (!roles.includes(auth.role)) {
      // 已登录但角色不符:跳自家首页而非登录页,避免已登录用户看到登录页困惑
      next(roleHome(auth.role))
      return
    }
  }
  next()
})

export default router
