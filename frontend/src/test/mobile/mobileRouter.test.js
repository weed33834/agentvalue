/**
 * 移动端路由树单测：/m 命名空间注册 + 根路由角色重定向
 *
 * 覆盖：
 * - mobileRoutes 注册四端命名空间 + 登录 + 根路由
 * - 各角色父路由使用 MobileLayout 且含 AI 助手子路由
 * - /m 根路由按登录态与角色落到对应移动端首页 / 登录页
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { mobileRoutes } from '@/router/mobile'
import { useAuthStore } from '@/stores/auth'

describe('mobile route tree', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
  })

  it('注册四端命名空间 + 登录 + 根路由', () => {
    const paths = mobileRoutes.map((r) => r.path)
    expect(paths).toEqual(
      expect.arrayContaining(['/m/login', '/m/employee', '/m/manager', '/m/hr', '/m/admin', '/m']),
    )
  })

  it('各角色父路由使用 MobileLayout 且含 AI 助手子路由', () => {
    for (const role of ['/m/employee', '/m/manager', '/m/hr', '/m/admin']) {
      const r = mobileRoutes.find((x) => x.path === role)
      expect(r).toBeTruthy()
      expect(r.component.toString().includes('MobileLayout')).toBe(true)
      expect(r.children.length).toBeGreaterThan(0)
      // 各角色 AI 助手入口：employee/manager/hr 为 assistant，admin 为 chat
      expect(r.children.some((c) => /Chat\.vue/.test(c.component.toString()))).toBe(true)
    }
  })

  it('/m 根路由：未登录 → /m/login', () => {
    const auth = useAuthStore()
    expect(auth.isLoggedIn).toBe(false)
    const redirect = mobileRoutes.find((r) => r.path === '/m').redirect
    expect(redirect()).toBe('/m/login')
  })

  it('/m 根路由：已登录 → 按角色落到 /m/{role}', () => {
    const auth = useAuthStore()
    auth.loginDemo('manager')
    expect(auth.isLoggedIn).toBe(true)
    const redirect = mobileRoutes.find((r) => r.path === '/m').redirect
    expect(redirect()).toBe('/m/manager')
  })
})
