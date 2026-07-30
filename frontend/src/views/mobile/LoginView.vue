<template>
  <div class="m-login">
    <div class="m-login__hero">
      <div class="m-login__logo">AV</div>
      <h1 class="m-login__title">AgentValue</h1>
      <p class="m-login__sub">AI 驱动的员工价值量化与成长系统</p>
    </div>

    <div class="m-login__body">
      <!-- 演示模式：角色一键进入（移动端最简路径） -->
      <template v-if="demoEnabled">
        <p class="m-section-title">演示模式 · 选择角色进入</p>
        <div class="m-role-grid">
          <button
            v-for="r in roles"
            :key="r.value"
            class="m-role-card"
            @click="handleDemoLogin(r.value)"
          >
            <span class="m-role-card__icon">{{ r.icon }}</span>
            <span class="m-role-card__label">{{ r.label }}</span>
          </button>
        </div>
        <div class="m-divider"><span>或账号登录</span></div>
      </template>

      <!-- 账号登录（JWT） -->
      <form class="m-login__form" @submit.prevent="handleJwtLogin">
        <label class="m-field">
          <span class="m-field__label">邮箱</span>
          <input
            v-model="email"
            class="m-input"
            type="email"
            inputmode="email"
            autocomplete="email"
            placeholder="请输入邮箱"
          />
        </label>
        <label class="m-field">
          <span class="m-field__label">密码</span>
          <input
            v-model="password"
            class="m-input"
            type="password"
            autocomplete="current-password"
            placeholder="请输入密码"
            @keyup.enter="handleJwtLogin"
          />
        </label>
        <p v-if="error" class="m-login__error">{{ error }}</p>
        <button class="m-btn" type="submit" :disabled="loading">
          {{ loading ? '登录中…' : '登 录' }}
        </button>
      </form>

      <button class="m-link" @click="goDesktop">使用桌面端 →</button>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { authApi } from '@/api/client'
import { isDemoAuthEnabled } from '@/utils/auth'

const router = useRouter()
const auth = useAuthStore()

const demoEnabled = computed(() => isDemoAuthEnabled())
const loading = ref(false)
const error = ref('')

const email = ref(import.meta.env.DEV ? 'employee@agentvalue.ai' : '')
const password = ref(import.meta.env.DEV ? 'agentvalue123' : '')

// 移动端首页映射（登录后落对应 /m 页面）
const mobileHome = {
  employee: '/m/employee',
  manager: '/m/manager',
  hr: '/m/hr',
  admin: '/m/admin',
}

const roles = [
  { value: 'employee', label: '员工', icon: '🧑‍💼' },
  { value: 'manager', label: '主管', icon: '👔' },
  { value: 'hr', label: 'HR', icon: '🗂️' },
  { value: 'admin', label: '管理员', icon: '🛠️' },
]

function handleDemoLogin(role) {
  if (!demoEnabled.value) {
    error.value = '演示模式未启用'
    return
  }
  auth.loginDemo(role)
  router.push(mobileHome[role])
}

async function handleJwtLogin() {
  if (!email.value || !password.value) {
    error.value = '请输入邮箱和密码'
    return
  }
  loading.value = true
  error.value = ''
  try {
    const res = await authApi.login({ email: email.value, password: password.value })
    auth.loginWithToken(res.access_token, {
      user_id: res.user_id,
      name: res.name,
      role: res.role,
    })
    router.push(mobileHome[res.role] || '/m/login')
  } catch (err) {
    error.value = err.message || '登录失败'
  } finally {
    loading.value = false
  }
}

function goDesktop() {
  router.push('/login?desktop=1')
}
</script>

<style scoped>
.m-login {
  min-height: 100vh;
  min-height: 100dvh;
  display: flex;
  flex-direction: column;
  background: linear-gradient(160deg, #0f172a 0%, #1e293b 55%, #1f2937 100%);
  color: #fff;
  padding: env(safe-area-inset-top) 0 env(safe-area-inset-bottom);
}
.m-login__hero {
  text-align: center;
  padding: 56px 24px 32px;
}
.m-login__logo {
  width: 64px;
  height: 64px;
  margin: 0 auto 16px;
  border-radius: 18px;
  background: linear-gradient(135deg, #3b82f6, #2563eb);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;
  font-weight: 800;
  box-shadow: 0 8px 24px rgba(37, 99, 235, 0.4);
}
.m-login__title {
  font-size: 26px;
  font-weight: 800;
  margin: 0 0 8px;
}
.m-login__title .accent {
  color: #60a5fa;
}
.m-login__sub {
  font-size: 13px;
  color: rgba(255, 255, 255, 0.6);
  margin: 0;
}

.m-login__body {
  flex: 1;
  background: var(--el-bg-color);
  border-radius: 24px 24px 0 0;
  padding: 24px 20px calc(24px + env(safe-area-inset-bottom));
  color: var(--el-text-color-primary);
}

.m-role-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.m-role-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 20px 0;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: var(--av-radius-lg);
  background: var(--el-bg-color);
  cursor: pointer;
  transition: all var(--av-transition-fast) var(--av-ease-smooth);
}
.m-role-card:active {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  transform: scale(0.98);
}
.m-role-card__icon {
  font-size: 28px;
}
.m-role-card__label {
  font-size: 14px;
  font-weight: 600;
}

.m-divider {
  display: flex;
  align-items: center;
  gap: 12px;
  color: var(--el-text-color-placeholder);
  font-size: 12px;
  margin: 20px 0;
}
.m-divider::before,
.m-divider::after {
  content: '';
  flex: 1;
  height: 1px;
  background: var(--el-border-color-lighter);
}

.m-field {
  display: block;
  margin-bottom: 16px;
}
.m-field__label {
  display: block;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  margin-bottom: 6px;
}
.m-input {
  width: 100%;
  height: 46px;
  padding: 0 14px;
  border: 1px solid var(--el-border-color);
  border-radius: var(--av-radius-md);
  font-size: 16px;
  background: var(--el-bg-color-blank);
  color: var(--el-text-color-primary);
  box-sizing: border-box;
  transition: border-color var(--av-transition-fast);
}
.m-input:focus {
  outline: none;
  border-color: var(--el-color-primary);
}
.m-login__error {
  color: #ef4444;
  font-size: 13px;
  margin: 0 0 12px;
}
.m-link {
  display: block;
  width: 100%;
  margin-top: 16px;
  background: none;
  border: none;
  color: var(--el-color-primary);
  font-size: 14px;
  cursor: pointer;
}
</style>
