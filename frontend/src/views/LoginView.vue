<template>
  <div class="login-page">
    <!-- 动态背景装饰 -->
    <div class="bg-decoration">
      <div class="bg-circle bg-circle--1"></div>
      <div class="bg-circle bg-circle--2"></div>
      <div class="bg-circle bg-circle--3"></div>
    </div>

    <el-card class="login-card av-scale-in" shadow="always">
      <template #header>
        <div class="login-header">
          <div class="login-mark">
            <BrandLogo :show-text="false" :size="56" />
          </div>
          <h1 class="login-title">AgentValue</h1>
          <p class="login-subtitle">AI 驱动员工价值量化与成长系统</p>
        </div>
      </template>

      <el-tabs v-model="activeTab" class="login-tabs">
        <el-tab-pane label="账号登录" name="jwt">
          <el-form
            ref="jwtFormRef"
            :model="jwtForm"
            :rules="jwtRules"
            label-position="top"
            class="login-form"
            @submit.prevent="handleJwtLogin"
          >
            <el-form-item label="邮箱" prop="email">
              <el-input
                v-model="jwtForm.email"
                placeholder="请输入邮箱"
                type="email"
                :prefix-icon="Message"
                clearable
              />
            </el-form-item>
            <el-form-item label="密码" prop="password">
              <el-input
                v-model="jwtForm.password"
                type="password"
                placeholder="请输入密码"
                show-password
                :prefix-icon="Lock"
                @keyup.enter="handleJwtLogin"
              />
            </el-form-item>
            <el-form-item>
              <el-button
                type="primary"
                class="login-btn"
                :loading="loading"
                @click="handleJwtLogin"
              >
                <span v-if="!loading">登 录</span>
                <span v-else>登录中...</span>
              </el-button>
            </el-form-item>
            <div v-if="demoEnabled" class="demo-tip">
              <el-icon><InfoFilled /></el-icon>
              <span>演示账号：employee@agentvalue.ai / agentvalue123</span>
              <el-button link type="primary" @click="seedDemoUsers">初始化演示账号</el-button>
            </div>
          </el-form>
        </el-tab-pane>

        <el-tab-pane v-if="demoEnabled" label="演示模式" name="demo">
          <el-form label-position="top" class="login-form">
            <el-form-item label="选择角色（演示模式，无需密码）">
              <el-select v-model="selectedRole" placeholder="请选择角色" style="width: 100%">
                <el-option label="员工" value="employee" />
                <el-option label="主管" value="manager" />
                <el-option label="HR" value="hr" />
                <el-option label="管理员" value="admin" />
              </el-select>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" class="login-btn" @click="handleDemoLogin">
                进入系统
              </el-button>
            </el-form-item>
            <div class="demo-tip">
              <el-icon><WarningFilled /></el-icon>
              <span>演示模式通过 header 传递角色，仅适用于本地开发，生产环境应禁用。</span>
            </div>
          </el-form>
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<script setup>
import { ref, reactive, computed } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Message, Lock, InfoFilled, WarningFilled } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { authApi } from '@/api/client'
import { isDemoAuthEnabled } from '@/utils/auth'
import { roleHome } from '@/router'
import BrandLogo from '@/components/BrandLogo.vue'

const router = useRouter()
const auth = useAuthStore()
const activeTab = ref('jwt')
const loading = ref(false)
const jwtFormRef = ref(null)

const jwtForm = reactive({
  // 仅开发环境预填演示账号,生产构建中不泄露凭据
  email: import.meta.env.DEV ? 'employee@agentvalue.ai' : '',
  password: import.meta.env.DEV ? 'agentvalue123' : '',
})

const jwtRules = {
  email: [
    { required: true, message: '请输入邮箱', trigger: 'blur' },
    { type: 'email', message: '邮箱格式不正确', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, message: '密码至少 6 位', trigger: 'blur' },
  ],
}

const selectedRole = ref('employee')
const demoEnabled = computed(() => isDemoAuthEnabled())

async function handleJwtLogin() {
  if (!jwtFormRef.value) return
  try {
    await jwtFormRef.value.validate()
  } catch {
    return
  }
  loading.value = true
  try {
    const res = await authApi.login({ email: jwtForm.email, password: jwtForm.password })
    auth.loginWithToken(res.access_token, {
      user_id: res.user_id,
      name: res.name,
      role: res.role,
    })
    ElMessage.success(`欢迎，${res.name}`)
    router.push(roleHome(res.role))
  } catch (err) {
    ElMessage.error(err.message || '登录失败')
  } finally {
    loading.value = false
  }
}

function handleDemoLogin() {
  if (!demoEnabled.value) {
    ElMessage.error('演示模式未启用')
    return
  }
  auth.loginDemo(selectedRole.value)
  router.push(roleHome(selectedRole.value))
}

async function seedDemoUsers() {
  loading.value = true
  try {
    const res = await authApi.seedDemoUsers()
    ElMessage.success(`演示账号已就绪：${res?.created?.length || 0} 个新建，默认密码 agentvalue123`)
  } catch (err) {
    ElMessage.error(err.message || '初始化失败')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #0f172a 0%, #1e293b 50%, #1f2937 100%);
  position: relative;
  overflow: hidden;
}

/* 动态背景装饰 */
.bg-decoration {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  overflow: hidden;
  pointer-events: none;
}
.bg-circle {
  position: absolute;
  border-radius: 50%;
  filter: blur(80px);
  opacity: 0.15;
  animation: float 8s ease-in-out infinite;
}
.bg-circle--1 {
  width: 400px;
  height: 400px;
  background: #3b82f6;
  top: -100px;
  left: -100px;
  animation-delay: 0s;
}
.bg-circle--2 {
  width: 300px;
  height: 300px;
  background: #8b5cf6;
  bottom: -50px;
  right: -50px;
  animation-delay: 2s;
}
.bg-circle--3 {
  width: 250px;
  height: 250px;
  background: #06b6d4;
  top: 50%;
  left: 60%;
  animation-delay: 4s;
  opacity: 0.1;
}

@keyframes float {
  0%, 100% { transform: translate(0, 0) scale(1); }
  33% { transform: translate(30px, -30px) scale(1.05); }
  66% { transform: translate(-20px, 20px) scale(0.95); }
}

/* 登录卡片 */
.login-card {
  width: 420px;
  max-width: calc(100vw - 32px);
  border-radius: var(--av-radius-lg) !important;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3), 0 8px 24px rgba(0, 0, 0, 0.15) !important;
  border: 1px solid rgba(255, 255, 255, 0.1) !important;
  backdrop-filter: blur(20px);
}
.login-card :deep(.el-card__header) {
  padding: 32px 32px 16px !important;
  border-bottom: none !important;
}
.login-card :deep(.el-card__body) {
  padding: 0 32px 32px !important;
}

/* 头部 */
.login-header {
  text-align: center;
}
.login-mark {
  display: flex;
  justify-content: center;
  margin-bottom: 16px;
}
.login-title {
  font-size: 24px;
  font-weight: bold;
  margin: 0 0 8px;
  color: #1f2937;
  letter-spacing: 0.5px;
}
.login-subtitle {
  font-size: 13px;
  color: #6b7280;
  margin: 0;
}

/* 表单 */
.login-form {
  margin-top: 16px;
}
.login-form :deep(.el-input__wrapper) {
  padding: 4px 12px;
  border-radius: var(--av-radius-sm) !important;
}

/* 登录按钮 */
.login-btn {
  width: 100%;
  height: 44px;
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 2px;
  border-radius: var(--av-radius-sm) !important;
  background: linear-gradient(135deg, #2563eb 0%, #3b82f6 100%) !important;
  border: none !important;
  transition: all var(--av-transition-base) var(--av-ease-smooth) !important;
  box-shadow: 0 4px 12px rgba(37, 99, 235, 0.3);
}
.login-btn:hover {
  transform: translateY(-2px) !important;
  box-shadow: 0 8px 20px rgba(37, 99, 235, 0.4) !important;
}
.login-btn:active {
  transform: translateY(0) !important;
}

/* 标签页 */
.login-tabs {
  margin-top: 8px;
}
.login-tabs :deep(.el-tabs__header) {
  margin-bottom: 20px;
}
.login-tabs :deep(.el-tabs__item) {
  font-size: 14px;
  font-weight: 500;
  padding: 0 0 12px;
  margin-right: 24px;
}

/* 提示信息 */
.demo-tip {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 12px;
  color: #4b5563;
  text-align: center;
  line-height: 1.6;
  padding: 10px 12px;
  border-radius: var(--av-radius-sm);
  background: var(--el-fill-color-lighter);
  margin-top: 4px;
}
.demo-tip .el-icon {
  flex-shrink: 0;
  color: var(--el-color-info);
}

/* ==================== 移动端适配 ==================== */
@media (max-width: 480px) {
  .login-card {
    width: 100%;
    margin: 16px;
    border-radius: var(--av-radius-md) !important;
  }
  .login-card :deep(.el-card__header) {
    padding: 24px 20px 12px !important;
  }
  .login-card :deep(.el-card__body) {
    padding: 0 20px 24px !important;
  }
  .login-mark {
    margin-bottom: 12px;
  }
  .login-title {
    font-size: 20px;
  }
  .login-subtitle {
    font-size: 12px;
  }
  .login-btn {
    height: 42px;
    font-size: 14px;
  }
}

/* ==================== 暗色模式适配 ==================== */
html.dark .login-card {
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5), 0 8px 24px rgba(0, 0, 0, 0.3) !important;
}
html.dark .login-title {
  color: var(--el-text-color-primary);
}
</style>
