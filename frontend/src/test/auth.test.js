/**
 * auth store 测试 (P1-14)
 *
 * 验证 useAuthStore 的核心行为:
 * - loginDemo / loginWithToken / logout 状态切换
 * - isLoggedIn 计算属性
 * - 演示模式受 isDemoAuthEnabled 控制
 * - checkAuth 在 JWT 过期时登出
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

// Mock @/api/client:authApi.me 由 store.checkAuth 调用
const meMock = vi.fn()
vi.mock('@/api/client', () => ({
  default: { post: vi.fn(), get: vi.fn() },
  authApi: {
    me: (...args) => meMock(...args),
    refresh: vi.fn(),
    login: vi.fn(),
  },
}))

import { useAuthStore } from '@/stores/auth'
import { isDemoAuthEnabled, isTokenExpired } from '@/utils/auth'

// 生成一个未过期的 JWT payload(exp 设为 1 小时后)
function makeJwt(payload = {}) {
  const header = Buffer.from(JSON.stringify({ alg: 'HS256', typ: 'JWT' })).toString('base64url')
  const body = {
    role: 'employee',
    user_id: 'E1001',
    name: '张三',
    exp: Math.floor(Date.now() / 1000) + 3600,
    ...payload,
  }
  const bodyB64 = Buffer.from(JSON.stringify(body)).toString('base64url')
  return `${header}.${bodyB64}.signature`
}

// 生成一个已过期的 JWT
function makeExpiredJwt(payload = {}) {
  const header = Buffer.from(JSON.stringify({ alg: 'HS256', typ: 'JWT' })).toString('base64url')
  const body = {
    role: 'employee',
    user_id: 'E1001',
    exp: Math.floor(Date.now() / 1000) - 3600, // 1 小时前过期
    ...payload,
  }
  const bodyB64 = Buffer.from(JSON.stringify(body)).toString('base64url')
  return `${header}.${bodyB64}.signature`
}

describe('auth store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    meMock.mockReset()
    localStorage.clear()
  })

  it('初始状态:未登录', () => {
    const auth = useAuthStore()
    expect(auth.role).toBe('')
    expect(auth.userId).toBe('')
    expect(auth.isLoggedIn).toBe(false)
    expect(auth.useJwt).toBe(false)
  })

  it('loginDemo 设置 role/userId 并持久化到 localStorage', () => {
    // vitest 默认 DEV=true,isDemoAuthEnabled() 返回 true
    expect(isDemoAuthEnabled()).toBe(true)
    const auth = useAuthStore()
    auth.loginDemo('manager', 'M001')
    expect(auth.role).toBe('manager')
    expect(auth.userId).toBe('M001')
    expect(auth.isLoggedIn).toBe(true)
    expect(auth.useJwt).toBe(false)
    expect(localStorage.getItem('agentvalue_role')).toBe('manager')
    expect(localStorage.getItem('agentvalue_user_id')).toBe('M001')
    // demo 模式不应写 token
    expect(localStorage.getItem('agentvalue_token')).toBeNull()
  })

  it('loginDemo 默认 userId 按 role 映射', () => {
    const auth = useAuthStore()
    auth.loginDemo('employee')
    expect(auth.userId).toBe('E1001')
    auth.loginDemo('hr')
    expect(auth.userId).toBe('HR001')
    auth.loginDemo('admin')
    expect(auth.userId).toBe('ADMIN001')
  })

  it('loginWithToken 写入 token 并开启 useJwt', () => {
    const auth = useAuthStore()
    const token = makeJwt({ role: 'hr', user_id: 'HR001', name: 'HR小李' })
    auth.loginWithToken(token, { role: 'hr', user_id: 'HR001', name: 'HR小李' })
    expect(auth.token).toBe(token)
    expect(auth.role).toBe('hr')
    expect(auth.userId).toBe('HR001')
    expect(auth.name).toBe('HR小李')
    expect(auth.useJwt).toBe(true)
    expect(auth.isLoggedIn).toBe(true)
    expect(localStorage.getItem('agentvalue_token')).toBe(token)
  })

  it('logout 清空所有状态与 localStorage', () => {
    const auth = useAuthStore()
    auth.loginWithToken(makeJwt(), { role: 'employee', user_id: 'E1001' })
    expect(auth.isLoggedIn).toBe(true)
    auth.logout()
    expect(auth.role).toBe('')
    expect(auth.userId).toBe('')
    expect(auth.token).toBe('')
    expect(auth.useJwt).toBe(false)
    expect(auth.isLoggedIn).toBe(false)
    expect(localStorage.getItem('agentvalue_role')).toBeNull()
    expect(localStorage.getItem('agentvalue_token')).toBeNull()
  })

  it('checkAuth: 非 JWT 模式直接返回 true,不发请求', async () => {
    const auth = useAuthStore()
    auth.loginDemo('employee')
    const ok = await auth.checkAuth()
    expect(ok).toBe(true)
    expect(meMock).not.toHaveBeenCalled()
  })

  it('checkAuth: JWT 已过期时登出并返回 false', async () => {
    const auth = useAuthStore()
    const expired = makeExpiredJwt({ role: 'employee', user_id: 'E1001' })
    auth.loginWithToken(expired, { role: 'employee', user_id: 'E1001' })
    expect(isTokenExpired(expired)).toBe(true)
    const ok = await auth.checkAuth()
    expect(ok).toBe(false)
    expect(meMock).not.toHaveBeenCalled()
    // 过期后应被登出
    expect(auth.token).toBe('')
    expect(auth.isLoggedIn).toBe(false)
  })

  it('checkAuth: JWT 未过期 + me 成功时刷新用户信息并返回 true', async () => {
    const auth = useAuthStore()
    const token = makeJwt({ role: 'employee', user_id: 'E1001' })
    auth.loginWithToken(token, { role: 'employee', user_id: 'E1001', name: '张三' })
    meMock.mockResolvedValue({ role: 'employee', user_id: 'E1001', name: '张三更新' })
    const ok = await auth.checkAuth()
    expect(ok).toBe(true)
    expect(meMock).toHaveBeenCalledTimes(1)
    expect(auth.name).toBe('张三更新')
  })

  it('checkAuth: me 抛异常时登出并返回 false', async () => {
    const auth = useAuthStore()
    const token = makeJwt({ role: 'employee', user_id: 'E1001' })
    auth.loginWithToken(token, { role: 'employee', user_id: 'E1001' })
    meMock.mockRejectedValue(new Error('401'))
    const ok = await auth.checkAuth()
    expect(ok).toBe(false)
    expect(auth.isLoggedIn).toBe(false)
    expect(auth.token).toBe('')
  })

  it('isTokenExpired: 空 token 视为过期', () => {
    expect(isTokenExpired('')).toBe(true)
    expect(isTokenExpired(null)).toBe(true)
  })

  it('isTokenExpired: 非法 token 视为过期', () => {
    expect(isTokenExpired('not-a-jwt')).toBe(true)
    expect(isTokenExpired('a.b')).toBe(true)
  })
})
