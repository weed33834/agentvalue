/**
 * 认证流程共享状态与工具函数
 * 集中管理模块级状态，避免 client.js / router / auth store 循环依赖。
 */

export const authFlowState = {
  hasRedirected: false,
  isRefreshing: false,
  refreshPromise: null,
  authChecked: false,
}

export function resetApiAuthState() {
  authFlowState.hasRedirected = false
  authFlowState.isRefreshing = false
  authFlowState.refreshPromise = null
}

export function resetRouteAuthChecked() {
  authFlowState.authChecked = false
}

/**
 * 判断 JWT 是否过期（仅客户端解析 exp，不验证签名）
 */
export function isTokenExpired(token) {
  if (!token) return true
  try {
    const payload = JSON.parse(atob(token.split('.')[1]))
    if (!payload || typeof payload.exp !== 'number') return false
    return payload.exp * 1000 < Date.now()
  } catch {
    return true
  }
}

/**
 * 演示模式是否启用：仅在开发构建或显式开启 env 时可用
 */
export function isDemoAuthEnabled() {
  return import.meta.env.DEV || import.meta.env.VITE_ENABLE_DEMO_AUTH === 'true'
}
