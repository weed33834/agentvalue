<template>
  <div class="admin-providers">
    <!-- 顶部说明 -->
    <el-alert type="info" :closable="false" show-icon class="mb-16">
      <template #title>
        模型供应商管理 —— 对标 Dify model-providers。支持 OpenAI / Anthropic / Gemini / Ollama
        多 Provider 接入,凭证加密存储(AES-256-GCM)、多凭证负载均衡、被动健康检查与冷却。
      </template>
    </el-alert>

    <!-- 工具栏 -->
    <div class="toolbar mb-16">
      <el-button :loading="loading" @click="loadProviders">
        <el-icon><RefreshLeft /></el-icon>
        刷新
      </el-button>
      <el-button type="primary" plain :loading="loadingDefaults" @click="loadDefaultModels">
        <el-icon><Star /></el-icon>
        默认模型
      </el-button>
    </div>

    <!-- 卡片网格 -->
    <el-row :gutter="16" v-loading="loading">
      <el-col
        v-for="p in providers"
        :key="p.provider"
        :xs="24"
        :md="12"
        :lg="8"
      >
        <el-card class="provider-card">
          <template #header>
            <div class="card-header">
              <span class="provider-title">
                <el-icon><Box /></el-icon>
                {{ p.label || p.provider }}
                <el-tag size="small" type="info" class="provider-name-tag">{{ p.provider }}</el-tag>
              </span>
              <el-switch
                :model-value="!!p.enabled"
                :loading="togglingProvider === p.provider"
                @change="(val) => toggleProvider(p, val)"
              />
            </div>
          </template>

          <div class="provider-desc">{{ p.description || '—' }}</div>

          <div class="provider-meta">
            <div class="meta-row">
              <span class="meta-label">模型类型</span>
              <span class="meta-tags">
                <el-tag
                  v-for="t in p.supported_model_types || []"
                  :key="t"
                  size="small"
                  class="meta-tag"
                >
                  {{ t }}
                </el-tag>
                <span v-if="!(p.supported_model_types && p.supported_model_types.length)" class="meta-empty">—</span>
              </span>
            </div>

            <div class="meta-row">
              <span class="meta-label">凭证</span>
              <span class="meta-value">{{ p.credentials && p.credentials.length || 0 }} 个</span>
              <el-tag v-if="getActiveCredential(p)" type="success" size="small" class="meta-tag">
                活跃: {{ getActiveCredential(p).name || getActiveCredential(p).id }}
              </el-tag>
              <el-tag v-else-if="(p.credentials && p.credentials.length)" type="info" size="small" class="meta-tag">
                无活跃凭证
              </el-tag>
            </div>

            <div class="meta-row">
              <span class="meta-label">模型</span>
              <span class="meta-value">{{ p.models && p.models.length || 0 }} 个</span>
              <el-tag v-if="getDefaultModelName(p)" type="warning" size="small" class="meta-tag">
                默认: {{ getDefaultModelName(p) }}
              </el-tag>
            </div>

            <div class="meta-row">
              <span class="meta-label">健康状态</span>
              <el-tag :type="healthTagType(p)" size="small">{{ healthTagText(p) }}</el-tag>
            </div>
          </div>

          <div class="card-actions">
            <el-button size="small" @click="openCredentialsDialog(p)">
              <el-icon><Key /></el-icon>
              凭证管理
            </el-button>
            <el-button size="small" @click="openModelsDialog(p)">
              <el-icon><Cpu /></el-icon>
              模型管理
            </el-button>
            <el-button size="small" @click="openHealthDialog(p)">
              <el-icon><CircleCheck /></el-icon>
              健康检查
            </el-button>
            <el-button size="small" :loading="testingProvider === p.provider" @click="testConnection(p)">
              <el-icon><Connection /></el-icon>
              测试连接
            </el-button>
          </div>
        </el-card>
      </el-col>
      <el-col v-if="!loading && !providers.length" :span="24">
        <el-empty description="暂无 Provider,请检查后端配置" />
      </el-col>
    </el-row>

    <!-- 凭证管理 Dialog -->
    <el-dialog v-model="credDialogVisible" :title="`${currentProviderLabel} 凭证管理`" width="800px">
      <div class="dialog-toolbar mb-16">
        <el-button type="primary" size="small" @click="openCredFormDialog(null)">
          <el-icon><Plus /></el-icon>
          新增凭证
        </el-button>
        <el-button size="small" :loading="validatingTemp" @click="validateTempConnection">
          <el-icon><Connection /></el-icon>
          验证连接(临时表单)
        </el-button>
        <el-button size="small" link @click="loadCredentials(currentProvider)">
          <el-icon><RefreshLeft /></el-icon>
          刷新
        </el-button>
      </div>
      <el-table :data="credentials" v-loading="credLoading" size="small" stripe>
        <el-table-column prop="name" label="名称" min-width="140" />
        <el-table-column label="凭证值(脱敏)" min-width="220">
          <template #default="{ row }">
            <span v-if="row.masked_credentials">
              <span
                v-for="(val, key) in row.masked_credentials"
                :key="key"
                class="credential-code"
              >
                {{ key }}: {{ val }}
              </span>
            </span>
            <span v-else class="meta-empty">—</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag v-if="row.is_active" type="success" size="small">活跃</el-tag>
            <el-tag v-else type="info" size="small">备用</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="180">
          <template #default="{ row }">
            {{ formatTime(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="!row.is_active"
              size="small"
              type="primary"
              link
              @click="activateCredential(row)"
            >
              激活
            </el-button>
            <el-button size="small" link @click="openCredFormDialog(row)">编辑</el-button>
            <el-button
              size="small"
              type="danger"
              link
              @click="removeCredential(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>

    <!-- 凭证表单子 Dialog(动态 schema) -->
    <el-dialog
      v-model="credFormDialogVisible"
      :title="credFormTitle"
      width="600px"
      append-to-body
    >
      <el-form
        ref="credFormRef"
        :model="credForm"
        label-position="top"
        v-loading="credFormSubmitting"
      >
        <el-form-item label="凭证名称">
          <el-input v-model="credForm.name" placeholder="如:生产主凭证" />
        </el-form-item>
        <el-form-item
          v-for="schema in credentialFormSchemas"
          :key="schema.variable"
          :label="schema.label"
          :required="schema.required"
          :prop="'credentials.' + schema.variable"
          :rules="schema.required ? [{ required: true, message: `${schema.label}必填`, trigger: 'blur' }] : []"
        >
          <el-select
            v-if="schema.type === 'select'"
            v-model="credForm.credentials[schema.variable]"
            :placeholder="schema.placeholder || '请选择'"
            clearable
          >
            <el-option
              v-for="opt in schema.options || []"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>
          <el-input
            v-else-if="schema.type === 'secret-input'"
            v-model="credForm.credentials[schema.variable]"
            type="password"
            show-password
            :placeholder="schema.placeholder || ''"
          />
          <el-input
            v-else
            v-model="credForm.credentials[schema.variable]"
            :placeholder="schema.placeholder || ''"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="credFormDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="credFormSubmitting" @click="submitCredForm">保存</el-button>
      </template>
    </el-dialog>

    <!-- 模型管理 Dialog -->
    <el-dialog v-model="modelDialogVisible" :title="`${currentProviderLabel} 模型管理`" width="900px">
      <div class="dialog-toolbar mb-16">
        <el-button type="primary" size="small" @click="openModelFormDialog">
          <el-icon><Plus /></el-icon>
          新增模型
        </el-button>
        <el-button size="small" link @click="loadModels(currentProvider)">
          <el-icon><RefreshLeft /></el-icon>
          刷新
        </el-button>
      </div>

      <!-- 默认模型展示 -->
      <el-alert
        v-if="defaultModelSummary"
        type="success"
        :closable="false"
        show-icon
        class="mb-16"
      >
        <template #title>
          当前默认模型:
          <el-tag
            v-for="(modelName, modelType) in defaultModelSummary"
            :key="modelType"
            size="small"
            class="meta-tag"
          >
            {{ modelType }}: {{ modelName }}
          </el-tag>
        </template>
      </el-alert>

      <el-table :data="models" v-loading="modelLoading" size="small" stripe>
        <el-table-column prop="model_name" label="模型名" min-width="160" />
        <el-table-column label="类型" width="120">
          <template #default="{ row }">
            <el-tag size="small">{{ row.model_type }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="默认" width="90">
          <template #default="{ row }">
            <el-switch
              :model-value="isDefaultModel(row)"
              @change="(val) => toggleDefaultModel(row, val)"
            />
          </template>
        </el-table-column>
        <el-table-column label="启用" width="90">
          <template #default="{ row }">
            <el-switch
              :model-value="!!row.enabled"
              :loading="modelToggling === row.id"
              @change="(val) => toggleModel(row, val)"
            />
          </template>
        </el-table-column>
        <el-table-column label="负载均衡" width="100">
          <template #default="{ row }">
            <el-switch
              :model-value="!!row.load_balancing_enabled"
              :loading="lbToggling === row.id"
              @change="(val) => toggleLoadBalancing(row, val)"
            />
          </template>
        </el-table-column>
        <el-table-column label="凭证数" width="90" align="center">
          <template #default="{ row }">
            {{ row.credentials && row.credentials.length || 0 }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="240" fixed="right">
          <template #default="{ row }">
            <el-button size="small" link @click="openModelCredentialsDialog(row)">
              <el-icon><Key /></el-icon>
              凭证
            </el-button>
            <el-button size="small" link @click="openParamRulesDialog(row)">
              <el-icon><Setting /></el-icon>
              参数规则
            </el-button>
            <el-button size="small" type="danger" link @click="removeModel(row)">
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>

    <!-- 新增模型表单子 Dialog -->
    <el-dialog v-model="modelFormDialogVisible" title="新增模型" width="600px" append-to-body>
      <el-form
        ref="modelFormRef"
        :model="modelForm"
        label-position="top"
        v-loading="modelFormSubmitting"
      >
        <el-form-item label="模型名" required prop="model_name" :rules="[{ required: true, message: '模型名必填', trigger: 'blur' }]">
          <el-input v-model="modelForm.model_name" placeholder="如:gpt-4o" />
        </el-form-item>
        <el-form-item label="模型类型" required prop="model_type" :rules="[{ required: true, message: '请选择类型', trigger: 'change' }]">
          <el-select v-model="modelForm.model_type" placeholder="请选择" style="width: 100%">
            <el-option
              v-for="t in currentModelTypeOptions"
              :key="t"
              :label="t"
              :value="t"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="凭证选择">
          <el-select
            v-model="modelForm.credentials"
            placeholder="选择已有 Provider 凭证(可选)"
            style="width: 100%"
            clearable
          >
            <el-option
              v-for="c in credentials"
              :key="c.id"
              :label="c.name || c.id"
              :value="c.id"
            />
          </el-select>
          <span class="field-hint">不选则使用 Provider 默认活跃凭证</span>
        </el-form-item>
        <el-form-item label="模型配置 (JSON, 可选)">
          <el-input
            v-model="modelConfigText"
            type="textarea"
            :rows="4"
            placeholder="可填 JSON 模型配置(可选)"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="modelFormDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="modelFormSubmitting" @click="submitModelForm">创建</el-button>
      </template>
    </el-dialog>

    <!-- 模型凭证管理子 Dialog -->
    <el-dialog
      v-model="modelCredDialogVisible"
      :title="`模型凭证: ${currentModelName}`"
      width="800px"
      append-to-body
    >
      <div class="dialog-toolbar mb-16">
        <el-button type="primary" size="small" @click="openModelCredFormDialog(null)">
          <el-icon><Plus /></el-icon>
          新增模型凭证
        </el-button>
        <el-button size="small" :loading="validatingModelTemp" @click="validateModelTempConnection">
          <el-icon><Connection /></el-icon>
          验证连接(临时表单)
        </el-button>
        <el-button size="small" link @click="loadModelCredentials(currentModel)">
          <el-icon><RefreshLeft /></el-icon>
          刷新
        </el-button>
      </div>
      <el-table :data="modelCredentials" v-loading="modelCredLoading" size="small" stripe>
        <el-table-column prop="name" label="名称" min-width="140" />
        <el-table-column label="凭证值(脱敏)" min-width="220">
          <template #default="{ row }">
            <span v-if="row.masked_credentials">
              <span
                v-for="(val, key) in row.masked_credentials"
                :key="key"
                class="credential-code"
              >
                {{ key }}: {{ val }}
              </span>
            </span>
            <span v-else class="meta-empty">—</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag v-if="row.is_active" type="success" size="small">活跃</el-tag>
            <el-tag v-else type="info" size="small">备用</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="!row.is_active"
              size="small"
              type="primary"
              link
              @click="activateModelCredential(row)"
            >
              激活
            </el-button>
            <el-button
              size="small"
              type="danger"
              link
              @click="removeModelCredential(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>

    <!-- 模型凭证表单子 Dialog(动态 schema) -->
    <el-dialog
      v-model="modelCredFormDialogVisible"
      :title="modelCredFormTitle"
      width="600px"
      append-to-body
    >
      <el-form :model="modelCredForm" label-position="top" v-loading="modelCredFormSubmitting">
        <el-form-item label="凭证名称">
          <el-input v-model="modelCredForm.name" placeholder="如:模型主凭证" />
        </el-form-item>
        <el-form-item
          v-for="schema in credentialFormSchemas"
          :key="schema.variable"
          :label="schema.label"
          :required="schema.required"
        >
          <el-select
            v-if="schema.type === 'select'"
            v-model="modelCredForm.credentials[schema.variable]"
            :placeholder="schema.placeholder || '请选择'"
            clearable
          >
            <el-option
              v-for="opt in schema.options || []"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>
          <el-input
            v-else-if="schema.type === 'secret-input'"
            v-model="modelCredForm.credentials[schema.variable]"
            type="password"
            show-password
            :placeholder="schema.placeholder || ''"
          />
          <el-input
            v-else
            v-model="modelCredForm.credentials[schema.variable]"
            :placeholder="schema.placeholder || ''"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="modelCredFormDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="modelCredFormSubmitting" @click="submitModelCredForm">保存</el-button>
      </template>
    </el-dialog>

    <!-- 参数规则 Dialog -->
    <el-dialog
      v-model="paramRulesDialogVisible"
      :title="`参数规则: ${currentModelName}`"
      width="700px"
      append-to-body
    >
      <el-alert type="info" :closable="false" show-icon class="mb-16">
        参数规则定义模型推理参数(temperature / top_p / max_tokens 等)的取值范围与默认值。
      </el-alert>
      <pre v-loading="paramRulesLoading" class="config-pre">{{ paramRulesText }}</pre>
    </el-dialog>

    <!-- 健康检查 Dialog -->
    <el-dialog v-model="healthDialogVisible" :title="`${currentProviderLabel} 健康检查`" width="700px">
      <div class="dialog-toolbar mb-16">
        <el-button type="primary" size="small" :loading="healthTriggering" @click="triggerHealthCheck">
          <el-icon><VideoPlay /></el-icon>
          手动触发
        </el-button>
        <el-button size="small" link @click="loadHealthChecks(currentProvider)">
          <el-icon><RefreshLeft /></el-icon>
          刷新
        </el-button>
      </div>
      <el-table :data="healthChecks" v-loading="healthLoading" size="small" stripe>
        <el-table-column label="时间" width="180">
          <template #default="{ row }">
            {{ formatTime(row.checked_at || row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.status === 'success' || row.healthy ? 'success' : 'danger'" size="small">
              {{ row.status === 'success' || row.healthy ? 'success' : (row.status || 'failed') }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="延迟(ms)" width="110" align="center">
          <template #default="{ row }">
            {{ row.latency_ms != null ? row.latency_ms : (row.latency != null ? row.latency : '—') }}
          </template>
        </el-table-column>
        <el-table-column label="错误信息" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            {{ row.error || row.message || '—' }}
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!healthLoading && !healthChecks.length" description="暂无健康检查记录" />
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { providerAdminApi } from '@/api/client'

// ====== Provider 列表 ======
const loading = ref(false)
const providers = ref([])
const togglingProvider = ref('')
const testingProvider = ref('')

async function loadProviders() {
  loading.value = true
  try {
    const data = await providerAdminApi.getWorkspaceProviders()
    providers.value = Array.isArray(data) ? data : data.items || data.providers || []
  } catch (e) {
    ElMessage.error(e.message || e)
  } finally {
    loading.value = false
  }
}

async function toggleProvider(provider, val) {
  togglingProvider.value = provider.provider
  try {
    await providerAdminApi.setPreferredType(provider.provider, {
      enabled: val,
    })
    ElMessage.success(`${provider.label || provider.provider} 已${val ? '启用' : '禁用'}`)
    await loadProviders()
  } catch (e) {
    ElMessage.error(e.message || e)
  } finally {
    togglingProvider.value = ''
  }
}

async function testConnection(provider) {
  // validateCredentials 需要原始凭证明文(脱敏值不可还原),
  // 卡片级"测试连接"打开凭证管理 + 临时表单,由用户填写后点击"验证连接"
  testingProvider.value = provider.provider
  try {
    openCredentialsDialog(provider)
    await loadCredentials(provider)
    // 直接弹出新增凭证表单(临时模式),用户填写后点验证按钮
    openCredFormDialog(null)
    ElMessage.info('请在表单填写凭证后点击"验证连接(临时表单)"按钮')
  } catch (e) {
    ElMessage.error(e.message || e)
  } finally {
    testingProvider.value = ''
  }
}

// ====== 辅助:卡片展示 ======
function getActiveCredential(p) {
  return (p.credentials || []).find((c) => c.is_active)
}

function getDefaultModelName(p) {
  const dm = (p.models || []).find((m) => m.is_default)
  return dm ? dm.model_name : ''
}

function healthTagType(p) {
  if (!p.enabled) return 'info'
  if (!p.credentials || !p.credentials.length) return 'danger'
  if (!p.models || !p.models.length) return 'warning'
  return 'success'
}

function healthTagText(p) {
  if (!p.enabled) return '未启用'
  if (!p.credentials || !p.credentials.length) return '未配置凭证'
  if (!p.models || !p.models.length) return '无模型'
  return '正常'
}

function formatTime(ts) {
  if (!ts) return '—'
  try {
    return new Date(ts).toLocaleString('zh-CN')
  } catch {
    return String(ts)
  }
}

// ====== 当前上下文 Provider ======
const currentProvider = ref(null)
const currentProviderLabel = computed(() => {
  const p = currentProvider.value
  return p ? p.label || p.provider : ''
})
const currentModelTypeOptions = computed(() => {
  const p = currentProvider.value
  return (p && p.supported_model_types) || ['llm', 'text-embedding', 'speech2text', 'moderation', 'rerank']
})
const credentialFormSchemas = computed(() => {
  const p = currentProvider.value
  return (p && p.provider_credential_schema && p.provider_credential_schema.credential_form_schemas) || []
})

// ====== 凭证管理 ======
const credDialogVisible = ref(false)
const credLoading = ref(false)
const credentials = ref([])

function openCredentialsDialog(p) {
  currentProvider.value = p
  credDialogVisible.value = true
  loadCredentials(p)
}

async function loadCredentials(p) {
  if (!p) return
  credLoading.value = true
  try {
    const data = await providerAdminApi.listCredentials(p.provider)
    credentials.value = Array.isArray(data) ? data : data.items || []
  } catch (e) {
    ElMessage.error(e.message || e)
  } finally {
    credLoading.value = false
  }
}

// 凭证表单(动态 schema)
const credFormRef = ref(null)
const credFormDialogVisible = ref(false)
const credFormSubmitting = ref(false)
const credEditingId = ref(null)
const credFormTitle = computed(() => (credEditingId.value ? '编辑凭证' : '新增凭证'))
const credForm = reactive({
  name: '',
  credentials: {},
})

function resetCredForm() {
  credForm.name = ''
  credForm.credentials = {}
  // 初始化 schema 默认值
  credentialFormSchemas.value.forEach((s) => {
    if (s.default !== undefined) {
      credForm.credentials[s.variable] = s.default
    }
  })
}

function openCredFormDialog(cred) {
  resetCredForm()
  if (cred) {
    credEditingId.value = cred.id
    credForm.name = cred.name || ''
    // 编辑时凭证字段留空(脱敏值不可回填,需重新输入)
  } else {
    credEditingId.value = null
  }
  credFormDialogVisible.value = true
}

async function submitCredForm() {
  const p = currentProvider.value
  if (!p) return
  if (credFormRef.value) {
    try {
      await credFormRef.value.validate()
    } catch {
      return
    }
  }
  // 校验必填字段
  for (const s of credentialFormSchemas.value) {
    if (s.required && !credForm.credentials[s.variable]) {
      ElMessage.warning(`${s.label}必填`)
      return
    }
  }
  credFormSubmitting.value = true
  try {
    if (credEditingId.value) {
      await providerAdminApi.updateCredential(p.provider, credEditingId.value, {
        credentials: { ...credForm.credentials },
      })
      ElMessage.success('凭证已更新')
    } else {
      await providerAdminApi.createCredential(p.provider, {
        credentials: { ...credForm.credentials },
        credential_name: credForm.name || undefined,
      })
      ElMessage.success('凭证已创建')
    }
    credFormDialogVisible.value = false
    await loadCredentials(p)
    await loadProviders()
  } catch (e) {
    ElMessage.error(e.message || e)
  } finally {
    credFormSubmitting.value = false
  }
}

async function activateCredential(cred) {
  const p = currentProvider.value
  if (!p) return
  try {
    await providerAdminApi.activateCredential(p.provider, cred.id)
    ElMessage.success('凭证已激活')
    await loadCredentials(p)
    await loadProviders()
  } catch (e) {
    ElMessage.error(e.message || e)
  }
}

async function removeCredential(cred) {
  const p = currentProvider.value
  if (!p) return
  try {
    await ElMessageBox.confirm(
      `确认删除凭证 "${cred.name || cred.id}"?该操作不可恢复。`,
      '删除确认',
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await providerAdminApi.deleteCredential(p.provider, cred.id)
    ElMessage.success('凭证已删除')
    await loadCredentials(p)
    await loadProviders()
  } catch (e) {
    ElMessage.error(e.message || e)
  }
}

// 临时验证连接(用当前表单值,不入库)
const validatingTemp = ref(false)

async function validateTempConnection() {
  const p = currentProvider.value
  if (!p) return
  // 检查必填
  for (const s of credentialFormSchemas.value) {
    if (s.required && !credForm.credentials[s.variable]) {
      ElMessage.warning(`请先在凭证表单中填写 ${s.label}`)
      return
    }
  }
  // 若表单未打开,则打开
  if (!credFormDialogVisible.value) {
    openCredFormDialog(null)
    ElMessage.info('请在表单填写凭证后再次点击验证')
    return
  }
  validatingTemp.value = true
  try {
    const result = await providerAdminApi.validateCredentials(p.provider, {
      credentials: { ...credForm.credentials },
    })
    if (result && (result.valid || result.success)) {
      ElMessage.success('验证通过(未入库)')
    } else {
      ElMessage.warning(`验证未通过: ${result?.message || result?.error || '未知原因'}`)
    }
  } catch (e) {
    ElMessage.error(e.message || e)
  } finally {
    validatingTemp.value = false
  }
}

// ====== 模型管理 ======
const modelDialogVisible = ref(false)
const modelLoading = ref(false)
const models = ref([])
const modelToggling = ref('')
const lbToggling = ref('')

function openModelsDialog(p) {
  currentProvider.value = p
  modelDialogVisible.value = true
  loadModels(p)
  // 同时加载凭证供新增模型选择
  if (!credentials.value.length) {
    loadCredentials(p)
  }
  loadDefaultModels()
}

async function loadModels(p) {
  if (!p) return
  modelLoading.value = true
  try {
    const data = await providerAdminApi.listModels(p.provider)
    models.value = Array.isArray(data) ? data : data.items || []
  } catch (e) {
    ElMessage.error(e.message || e)
  } finally {
    modelLoading.value = false
  }
}

async function toggleModel(model, val) {
  const p = currentProvider.value
  if (!p) return
  modelToggling.value = model.id
  try {
    await providerAdminApi.toggleModel(p.provider, model.id, { enabled: val })
    ElMessage.success(`${model.model_name} 已${val ? '启用' : '禁用'}`)
    model.enabled = val
    await loadModels(p)
  } catch (e) {
    ElMessage.error(e.message || e)
  } finally {
    modelToggling.value = ''
  }
}

async function toggleLoadBalancing(model, val) {
  const p = currentProvider.value
  if (!p) return
  lbToggling.value = model.id
  try {
    await providerAdminApi.toggleModelLoadBalancing(p.provider, model.id, { enabled: val })
    ElMessage.success(`${model.model_name} 负载均衡已${val ? '开启' : '关闭'}`)
    model.load_balancing_enabled = val
    await loadModels(p)
  } catch (e) {
    ElMessage.error(e.message || e)
  } finally {
    lbToggling.value = ''
  }
}

// 新增模型表单
const modelFormRef = ref(null)
const modelFormDialogVisible = ref(false)
const modelFormSubmitting = ref(false)
const modelConfigText = ref('')
const modelForm = reactive({
  model_name: '',
  model_type: 'llm',
  credentials: '',
})

function openModelFormDialog() {
  modelForm.model_name = ''
  modelForm.model_type = currentModelTypeOptions.value[0] || 'llm'
  modelForm.credentials = ''
  modelConfigText.value = ''
  modelFormDialogVisible.value = true
}

async function submitModelForm() {
  const p = currentProvider.value
  if (!p) return
  if (modelFormRef.value) {
    try {
      await modelFormRef.value.validate()
    } catch {
      return
    }
  }
  let modelConfig = null
  if (modelConfigText.value.trim()) {
    try {
      modelConfig = JSON.parse(modelConfigText.value)
    } catch {
      ElMessage.error('模型配置 JSON 格式错误')
      return
    }
  }
  modelFormSubmitting.value = true
  try {
    await providerAdminApi.addModel(p.provider, {
      model_name: modelForm.model_name,
      model_type: modelForm.model_type,
      model_config: modelConfig || {},
      credentials: modelForm.credentials || undefined,
    })
    ElMessage.success('模型已添加')
    modelFormDialogVisible.value = false
    await loadModels(p)
    await loadProviders()
  } catch (e) {
    ElMessage.error(e.message || e)
  } finally {
    modelFormSubmitting.value = false
  }
}

async function removeModel(model) {
  const p = currentProvider.value
  if (!p) return
  try {
    await ElMessageBox.confirm(
      `确认删除模型 "${model.model_name}"?该操作不可恢复。`,
      '删除确认',
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await providerAdminApi.deleteModel(p.provider, model.id)
    ElMessage.success('模型已删除')
    await loadModels(p)
    await loadProviders()
  } catch (e) {
    ElMessage.error(e.message || e)
  }
}

// ====== 默认模型 ======
const loadingDefaults = ref(false)
const defaultModels = ref([]) // [{model_type, provider, model_name}]
const defaultModelSummary = computed(() => {
  const map = {}
  defaultModels.value.forEach((d) => {
    map[d.model_type] = d.model_name
  })
  return map
})

async function loadDefaultModels() {
  loadingDefaults.value = true
  try {
    const data = await providerAdminApi.listDefaultModels()
    defaultModels.value = Array.isArray(data) ? data : data.items || []
  } catch (e) {
    ElMessage.error(e.message || e)
  } finally {
    loadingDefaults.value = false
  }
}

function isDefaultModel(model) {
  const p = currentProvider.value
  if (!p) return false
  return defaultModels.value.some(
    (d) => d.model_type === model.model_type && d.model_name === model.model_name && d.provider === p.provider,
  )
}

async function toggleDefaultModel(model, val) {
  const p = currentProvider.value
  if (!p) return
  if (!val) {
    ElMessage.info('默认模型不支持取消,请设置其他模型为默认')
    return
  }
  try {
    await providerAdminApi.setDefaultModel({
      model_type: model.model_type,
      provider: p.provider,
      model_name: model.model_name,
    })
    ElMessage.success(`${model.model_name} 已设为 ${model.model_type} 默认模型`)
    await loadDefaultModels()
    await loadProviders()
  } catch (e) {
    ElMessage.error(e.message || e)
  }
}

// ====== 模型凭证管理 ======
const currentModel = ref(null)
const currentModelName = computed(() => {
  const m = currentModel.value
  return m ? m.model_name : ''
})
const modelCredDialogVisible = ref(false)
const modelCredLoading = ref(false)
const modelCredentials = ref([])

function openModelCredentialsDialog(model) {
  currentModel.value = model
  modelCredDialogVisible.value = true
  loadModelCredentials(model)
}

async function loadModelCredentials(model) {
  if (!model) return
  const p = currentProvider.value
  if (!p) return
  modelCredLoading.value = true
  try {
    const data = await providerAdminApi.listModelCredentials(p.provider, model.id)
    modelCredentials.value = Array.isArray(data) ? data : data.items || []
  } catch (e) {
    ElMessage.error(e.message || e)
  } finally {
    modelCredLoading.value = false
  }
}

// 模型凭证表单
const modelCredFormDialogVisible = ref(false)
const modelCredFormSubmitting = ref(false)
const modelCredFormTitle = computed(() => '新增模型凭证')
const modelCredForm = reactive({
  name: '',
  credentials: {},
})
const validatingModelTemp = ref(false)

function openModelCredFormDialog() {
  modelCredForm.name = ''
  modelCredForm.credentials = {}
  credentialFormSchemas.value.forEach((s) => {
    if (s.default !== undefined) {
      modelCredForm.credentials[s.variable] = s.default
    }
  })
  modelCredFormDialogVisible.value = true
}

async function submitModelCredForm() {
  const p = currentProvider.value
  const m = currentModel.value
  if (!p || !m) return
  for (const s of credentialFormSchemas.value) {
    if (s.required && !modelCredForm.credentials[s.variable]) {
      ElMessage.warning(`${s.label}必填`)
      return
    }
  }
  modelCredFormSubmitting.value = true
  try {
    await providerAdminApi.addModelCredential(p.provider, m.id, {
      credentials: { ...modelCredForm.credentials },
      credential_name: modelCredForm.name || undefined,
    })
    ElMessage.success('模型凭证已添加')
    modelCredFormDialogVisible.value = false
    await loadModelCredentials(m)
  } catch (e) {
    ElMessage.error(e.message || e)
  } finally {
    modelCredFormSubmitting.value = false
  }
}

async function activateModelCredential(cred) {
  const p = currentProvider.value
  const m = currentModel.value
  if (!p || !m) return
  try {
    await providerAdminApi.activateModelCredential(p.provider, m.id, cred.id)
    ElMessage.success('模型凭证已激活')
    await loadModelCredentials(m)
  } catch (e) {
    ElMessage.error(e.message || e)
  }
}

async function removeModelCredential(cred) {
  const p = currentProvider.value
  const m = currentModel.value
  if (!p || !m) return
  try {
    await ElMessageBox.confirm(
      `确认删除模型凭证 "${cred.name || cred.id}"?`,
      '删除确认',
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await providerAdminApi.deleteModelCredential(p.provider, m.id, cred.id)
    ElMessage.success('模型凭证已删除')
    await loadModelCredentials(m)
  } catch (e) {
    ElMessage.error(e.message || e)
  }
}

async function validateModelTempConnection() {
  const p = currentProvider.value
  const m = currentModel.value
  if (!p || !m) return
  if (!modelCredFormDialogVisible.value) {
    openModelCredFormDialog()
    ElMessage.info('请在表单填写凭证后再次点击验证')
    return
  }
  validatingModelTemp.value = true
  try {
    const result = await providerAdminApi.validateModelCredentials(p.provider, m.id, {
      credentials: { ...modelCredForm.credentials },
    })
    if (result && (result.valid || result.success)) {
      ElMessage.success('验证通过(未入库)')
    } else {
      ElMessage.warning(`验证未通过: ${result?.message || result?.error || '未知原因'}`)
    }
  } catch (e) {
    ElMessage.error(e.message || e)
  } finally {
    validatingModelTemp.value = false
  }
}

// ====== 参数规则 ======
const paramRulesDialogVisible = ref(false)
const paramRulesLoading = ref(false)
const paramRulesText = ref('')

async function openParamRulesDialog(model) {
  currentModel.value = model
  paramRulesDialogVisible.value = true
  paramRulesLoading.value = true
  paramRulesText.value = ''
  const p = currentProvider.value
  if (!p) return
  try {
    const data = await providerAdminApi.getModelParameterRules(p.provider, model.id)
    paramRulesText.value = JSON.stringify(data, null, 2)
  } catch (e) {
    paramRulesText.value = '加载失败: ' + (e.message || e)
  } finally {
    paramRulesLoading.value = false
  }
}

// ====== 健康检查 ======
const healthDialogVisible = ref(false)
const healthLoading = ref(false)
const healthTriggering = ref(false)
const healthChecks = ref([])

function openHealthDialog(p) {
  currentProvider.value = p
  healthDialogVisible.value = true
  loadHealthChecks(p)
}

async function loadHealthChecks(p) {
  if (!p) return
  healthLoading.value = true
  try {
    const data = await providerAdminApi.getHealthChecks(p.provider)
    healthChecks.value = Array.isArray(data) ? data : data.items || []
  } catch (e) {
    ElMessage.error(e.message || e)
  } finally {
    healthLoading.value = false
  }
}

async function triggerHealthCheck() {
  const p = currentProvider.value
  if (!p) return
  healthTriggering.value = true
  try {
    await providerAdminApi.triggerHealthCheck(p.provider)
    ElMessage.success('健康检查已触发')
    await loadHealthChecks(p)
  } catch (e) {
    ElMessage.error(e.message || e)
  } finally {
    healthTriggering.value = false
  }
}

// ====== 初始化 ======
onMounted(() => {
  loadProviders()
  loadDefaultModels()
})
</script>

<style scoped>
.admin-providers {
  padding: 20px;
}
.mb-16 {
  margin-bottom: 16px;
}
.toolbar {
  display: flex;
  gap: 8px;
  align-items: center;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.provider-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
}
.provider-name-tag {
  margin-left: 4px;
}
.provider-card {
  margin-bottom: 16px;
  transition: box-shadow 0.2s;
}
.provider-card:hover {
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
}
.provider-desc {
  color: #606266;
  font-size: 13px;
  line-height: 1.6;
  margin-bottom: 12px;
  min-height: 42px;
}
.provider-meta {
  font-size: 13px;
}
.meta-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.meta-label {
  color: #909399;
  min-width: 70px;
}
.meta-value {
  color: #303133;
  font-weight: 500;
}
.meta-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.meta-tag {
  margin: 0;
}
.meta-empty {
  color: #c0c4cc;
}
.card-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid #ebeef5;
}
.dialog-toolbar {
  display: flex;
  gap: 8px;
  align-items: center;
}
.credential-code {
  font-family: ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace;
  background: #f5f7fa;
  padding: 2px 6px;
  border-radius: 3px;
  margin-right: 6px;
  font-size: 12px;
  display: inline-block;
  margin-bottom: 2px;
}
.field-hint {
  color: #909399;
  font-size: 12px;
  display: block;
  margin-top: 4px;
}
.config-pre {
  background-color: #f5f7fa;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  padding: 12px;
  font-family: ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 400px;
  overflow: auto;
}
</style>
