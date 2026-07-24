import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { authApi } from '@/api/client'
import {
  resetApiAuthState,
  resetRouteAuthChecked,
  isTokenExpired,
  isDemoAuthEnabled,
} from '@/utils/auth'

function defaultUserId(role) {
  const map = {
    employee: 'E1001',
    manager: 'M001',
    hr: 'HR001',
    admin: 'ADMIN001',
  }
  return map[role] || 'U001'
}

export const useAuthStore = defineStore('auth', () => {
  const role = ref(localStorage.getItem('agentvalue_role') || '')
  const userId = ref(localStorage.getItem('agentvalue_user_id') || '')
  const name = ref(localStorage.getItem('agentvalue_name') || '')
  const token = ref(localStorage.getItem('agentvalue_token') || '')
  // 是否使用 JWT 真实认证；false 表示演示模式（header 伪造角色）
  const useJwt = ref(!!token.value)

  // 生产环境清理残留的演示模式本地数据
  if (!token.value && !isDemoAuthEnabled()) {
    localStorage.removeItem('agentvalue_role')
    localStorage.removeItem('agentvalue_user_id')
    localStorage.removeItem('agentvalue_name')
    role.value = ''
    userId.value = ''
    name.value = ''
  }

  const isLoggedIn = computed(() => !!role.value)

  function loginWithToken(tokenValue, payload) {
    token.value = tokenValue
    role.value = payload.role
    userId.value = payload.user_id
    name.value = payload.name || ''
    useJwt.value = true
    localStorage.setItem('agentvalue_token', tokenValue)
    localStorage.setItem('agentvalue_role', payload.role)
    localStorage.setItem('agentvalue_user_id', payload.user_id)
    localStorage.setItem('agentvalue_name', name.value)
    // 登录成功后重置认证流程状态，允许重新校验与跳转
    resetApiAuthState()
    resetRouteAuthChecked()
  }

  function loginDemo(selectedRole, id = null) {
    if (!isDemoAuthEnabled()) {
      return
    }
    role.value = selectedRole
    userId.value = id || defaultUserId(selectedRole)
    name.value = ''
    token.value = ''
    useJwt.value = false
    localStorage.setItem('agentvalue_role', selectedRole)
    localStorage.setItem('agentvalue_user_id', userId.value)
    localStorage.removeItem('agentvalue_token')
    localStorage.removeItem('agentvalue_name')
    resetApiAuthState()
    resetRouteAuthChecked()
  }

  function logout() {
    role.value = ''
    userId.value = ''
    name.value = ''
    token.value = ''
    useJwt.value = false
    localStorage.removeItem('agentvalue_role')
    localStorage.removeItem('agentvalue_user_id')
    localStorage.removeItem('agentvalue_name')
    localStorage.removeItem('agentvalue_token')
    resetApiAuthState()
    resetRouteAuthChecked()
  }

  async function checkAuth() {
    if (!useJwt.value) return true
    if (isTokenExpired(token.value)) {
      logout()
      return false
    }
    try {
      const data = await authApi.me()
      if (data) {
        role.value = data.role || role.value
        userId.value = data.user_id || userId.value
        name.value = data.name || name.value
      }
      return true
    } catch {
      logout()
      return false
    }
  }

  return {
    role,
    userId,
    name,
    token,
    useJwt,
    isLoggedIn,
    loginWithToken,
    loginDemo,
    logout,
    checkAuth,
  }
})
