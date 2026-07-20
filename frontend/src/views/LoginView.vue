<template>
  <div class="login-page">
    <el-card class="login-card" shadow="hover">
      <template #header>
        <h1 class="login-header">AgentValue-AI</h1>
      </template>
      <p class="login-subtitle">AI 驱动员工价值量化与成长系统</p>

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
              <el-input v-model="jwtForm.email" placeholder="请输入邮箱" type="email" />
            </el-form-item>
            <el-form-item label="密码" prop="password">
              <el-input
                v-model="jwtForm.password"
                type="password"
                placeholder="请输入密码"
                show-password
                @keyup.enter="handleJwtLogin"
              />
            </el-form-item>
            <el-form-item>
              <el-button
                type="primary"
                style="width: 100%"
                :loading="loading"
                @click="handleJwtLogin"
              >
                登录
              </el-button>
            </el-form-item>
            <div v-if="demoEnabled" class="demo-tip">
              演示账号：employee@agentvalue.ai / agentvalue123
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
              <el-button type="primary" style="width: 100%" @click="handleDemoLogin">
                进入系统
              </el-button>
            </el-form-item>
            <div class="demo-tip">
              演示模式通过 header 传递角色，仅适用于本地开发，生产环境应禁用。
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
import { useAuthStore } from '@/stores/auth'
import { authApi } from '@/api/client'
import { isDemoAuthEnabled } from '@/utils/auth'
import { roleHome } from '@/router'

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
    const res = await authApi.login(jwtForm.email, jwtForm.password)
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
    // 后端返回 {created, note},不含 default_password；演示账号密码固定为 agentvalue123
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
  background: linear-gradient(135deg, #1f2937 0%, #374151 100%);
}
.login-card {
  width: 420px;
}
.login-header {
  text-align: center;
  font-size: 22px;
  font-weight: bold;
  margin: 0;
}
.login-subtitle {
  text-align: center;
  color: #606266;
  margin-bottom: 16px;
}
.login-tabs {
  margin-top: 8px;
}
.demo-tip {
  font-size: 12px;
  /* 无障碍：加深文字颜色，12px 小字需 ≥4.5:1 对比度（原 #6b7280 约 4.8:1 偏低，这里提升至约 7.5:1） */
  color: #4b5563;
  text-align: center;
  line-height: 1.6;
}
</style>
