/**
 * 移动端路由树（/m 命名空间）。
 * 与桌面路由（/employee、/manager ...）完全平行但独立：
 * - 布局用 MobileLayout（底部 Tab），不复用桌面 MainLayout 侧边栏
 * - 页面用 views/mobile/*，不复用 views/* 的 Element Plus 桌面组件
 * 两端共享 store / api 层，仅 UI 彻底分家。
 *
 * 设计取舍：四端「核心流程」做真实移动端页面（看板 / AI 助手 / 关键列表），
 * 其余桌面子路由在移动端统一用 Placeholder 承接并引导回桌面端，
 * 避免把 40+ 个以表格/图表为主的管理页硬塞进手机（低价值且违背最简化）。
 */
import { useAuthStore } from '@/stores/auth'

const MobilePlaceholder = () => import('@/views/mobile/PlaceholderView.vue')

export const mobileRoutes = [
  {
    path: '/m/login',
    name: 'MobileLogin',
    component: () => import('@/views/mobile/LoginView.vue'),
    meta: { title: '登录', mobile: true },
  },
  {
    path: '/m/employee',
    component: () => import('@/layouts/MobileLayout.vue'),
    meta: { role: ['employee'], mobile: true },
    children: [
      {
        path: '',
        name: 'MobileEmployeeDashboard',
        component: () => import('@/views/mobile/employee/Dashboard.vue'),
        meta: { title: '成长看板' },
      },
      {
        path: 'input',
        name: 'MobileEmployeeInput',
        component: () => import('@/views/mobile/employee/Input.vue'),
        meta: { title: '工作录入' },
      },
      {
        path: 'history',
        name: 'MobileEmployeeHistory',
        component: () => import('@/views/mobile/employee/History.vue'),
        meta: { title: '历史评估' },
      },
      {
        path: 'feedback',
        name: 'MobileEmployeeFeedback',
        component: () => import('@/views/mobile/employee/Feedback.vue'),
        meta: { title: '反馈申诉' },
      },
      {
        path: 'growth-path',
        name: 'MobileEmployeeGrowth',
        component: () => import('@/views/mobile/employee/Growth.vue'),
        meta: { title: '成长路径' },
      },
      {
        path: 'assistant',
        name: 'MobileEmployeeAssistant',
        component: () => import('@/views/mobile/employee/Chat.vue'),
        meta: { title: 'AI助手' },
      },
    ],
  },
  {
    path: '/m/manager',
    component: () => import('@/layouts/MobileLayout.vue'),
    meta: { role: ['manager', 'hr', 'admin'], mobile: true },
    children: [
      {
        path: '',
        name: 'MobileManagerDashboard',
        component: () => import('@/views/mobile/manager/Dashboard.vue'),
        meta: { title: '团队诊断' },
      },
      {
        path: 'team',
        name: 'MobileManagerTeam',
        component: () => import('@/views/mobile/manager/Team.vue'),
        meta: { title: '团队分析' },
      },
      {
        path: 'approval/:id',
        name: 'MobileApprovalDetail',
        component: MobilePlaceholder,
        meta: { title: '审批详情' },
      },
      {
        path: 'roi',
        name: 'MobileTeamROI',
        component: MobilePlaceholder,
        meta: { title: '团队ROI' },
      },
      {
        path: 'attrition-risk',
        name: 'MobileAttritionRisk',
        component: MobilePlaceholder,
        meta: { title: '离职风险' },
      },
      {
        path: 'reviews-360',
        name: 'MobileReview360',
        component: MobilePlaceholder,
        meta: { title: '360° 环评' },
      },
      {
        path: 'calibration',
        name: 'MobileCalibration',
        component: MobilePlaceholder,
        meta: { title: '校准会' },
      },
      {
        path: 'assistant',
        name: 'MobileManagerAssistant',
        component: () => import('@/views/mobile/manager/Chat.vue'),
        meta: { title: 'AI助手' },
      },
    ],
  },
  {
    path: '/m/hr',
    component: () => import('@/layouts/MobileLayout.vue'),
    meta: { role: ['hr', 'admin'], mobile: true },
    children: [
      {
        path: '',
        name: 'MobileHRDashboard',
        component: () => import('@/views/mobile/hr/Dashboard.vue'),
        meta: { title: 'HR复核' },
      },
      {
        path: 'audit/:id',
        name: 'MobileHRAuditDetail',
        component: MobilePlaceholder,
        meta: { title: 'HR复核详情' },
      },
      {
        path: 'assistant',
        name: 'MobileHRAssistant',
        component: () => import('@/views/mobile/hr/Chat.vue'),
        meta: { title: 'AI助手' },
      },
    ],
  },
  {
    path: '/m/admin',
    component: () => import('@/layouts/MobileLayout.vue'),
    meta: { role: ['admin'], mobile: true },
    children: [
      {
        path: '',
        name: 'MobileAdminDashboard',
        // 管理端移动端首页 = 功能中心（分类卡片，逐张引导回桌面端对应功能）
        component: () => import('@/views/mobile/admin/Dashboard.vue'),
        meta: { title: '管理工作台' },
      },
      {
        path: 'chat',
        name: 'MobileAdminAssistant',
        component: () => import('@/views/mobile/admin/Chat.vue'),
        meta: { title: 'AI助手' },
      },
    ],
  },
  {
    // /m 根：按登录角色落到对应移动端首页；未登录落登录页
    path: '/m',
    redirect: () => {
      const auth = useAuthStore()
      if (!auth.isLoggedIn) return '/m/login'
      const home = { employee: '/m/employee', manager: '/m/manager', hr: '/m/hr', admin: '/m/admin' }
      return home[auth.role] || '/m/login'
    },
  },
]
