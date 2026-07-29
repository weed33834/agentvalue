<template>
  <div class="admin-api-keys">
    <!-- 顶部说明 -->
    <el-alert type="info" :closable="false" show-icon class="mb-16">
      <template #title>
        API Key 管理 —— 对标 Langfuse API Keys。支持创建/编辑/吊销/轮换与用量统计， 明文 Key
        仅在创建或轮换时返回一次，请立即妥善保存；支持按 Scope 授权与速率限制。
      </template>
    </el-alert>

    <!-- 顶部工具栏: 标题 + 操作按钮 -->
    <div class="toolbar mb-16">
      <span class="section-title">
        <el-icon><Key /></el-icon>
        API Key 管理
      </span>
      <div class="toolbar-actions">
        <el-button type="primary" @click="openCreateDialog">
          <el-icon><Plus /></el-icon>
          创建 API Key
        </el-button>
        <el-button :loading="loading" @click="loadKeys">
          <el-icon><RefreshLeft /></el-icon>
          刷新
        </el-button>
      </div>
    </div>

    <!-- 列表表格 -->
    <el-card v-loading="loading" :aria-busy="loading">
      <el-table :data="keys" stripe style="width: 100%" empty-text="暂无 API Key">
        <el-table-column label="名称" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="key-name">{{ row.name || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="Key 前缀 (前12位)" min-width="200">
          <template #default="{ row }">
            <code class="key-prefix-code">{{ keyPrefixDisplay(row) }}</code>
          </template>
        </el-table-column>
        <el-table-column label="Scopes" min-width="200">
          <template #default="{ row }">
            <template v-if="row.scopes && row.scopes.length">
              <el-tag
                v-for="scope in row.scopes"
                :key="scope"
                size="small"
                type="warning"
                class="scope-tag"
              >
                {{ scope }}
              </el-tag>
            </template>
            <span v-else class="meta-empty">—</span>
          </template>
        </el-table-column>
        <el-table-column label="速率限制" width="120" align="center">
          <template #default="{ row }">
            <span class="rate-limit-value">{{
              row.rate_limit != null ? row.rate_limit : '—'
            }}</span>
            <span v-if="row.rate_limit != null" class="rate-limit-unit">/min</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag v-if="!row.is_active" size="small" type="info">已停用</el-tag>
            <el-tag v-else-if="isExpired(row)" size="small" type="warning">已过期</el-tag>
            <el-tag v-else size="small" type="success">活跃</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="180">
          <template #default="{ row }">
            {{ formatTime(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column label="过期时间" width="180">
          <template #default="{ row }">
            <span :class="{ 'expired-text': isExpired(row) }">
              {{ formatTime(row.expires_at) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="280" fixed="right">
          <template #default="{ row }">
            <el-button size="small" link @click="openEditDialog(row)">
              <el-icon><Edit /></el-icon>
              编辑
            </el-button>
            <el-button size="small" link type="primary" @click="openUsageDialog(row)">
              <el-icon><DataLine /></el-icon>
              用量
            </el-button>
            <el-button
              size="small"
              link
              type="warning"
              :disabled="!row.is_active"
              @click="handleRotate(row)"
            >
              <el-icon><Refresh /></el-icon>
              轮换
            </el-button>
            <el-button
              size="small"
              link
              type="danger"
              :disabled="!row.is_active"
              @click="handleRevoke(row)"
            >
              <el-icon><Delete /></el-icon>
              吊销
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- 分页 -->
      <div class="pagination-wrapper">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next, jumper"
          background
          @size-change="handleSearch"
          @current-change="loadKeys"
        />
      </div>
    </el-card>

    <!-- ============ 创建/编辑 对话框 ============ -->
    <el-dialog
      v-model="formDialogVisible"
      :title="formTitle"
      width="560px"
      :close-on-click-modal="false"
      @closed="resetForm"
    >
      <el-form
        ref="formRef"
        :model="form"
        :rules="formRules"
        label-position="top"
        v-loading="formSubmitting"
      >
        <el-form-item label="名称" prop="name">
          <el-input
            v-model="form.name"
            placeholder="如：生产环境调用密钥"
            maxlength="64"
            show-word-limit
          />
        </el-form-item>
        <el-form-item label="Scopes (逗号分隔)" prop="scopes">
          <el-input v-model="form.scopes" placeholder="如：evaluations:read, evaluations:write" />
          <span class="field-hint">多个权限范围以逗号分隔，留空表示不限定 Scope。</span>
        </el-form-item>
        <el-form-item label="速率限制 (次/分钟)" prop="rate_limit">
          <el-input-number
            v-model="form.rate_limit"
            :min="0"
            :step="10"
            controls-position="right"
            style="width: 100%"
          />
          <span class="field-hint">0 表示不限制。</span>
        </el-form-item>
        <el-form-item v-if="!isEdit" label="过期时间 (可选)" prop="expires_at">
          <el-date-picker
            v-model="form.expires_at"
            type="datetime"
            placeholder="选择过期时间，留空表示永不过期"
            format="YYYY-MM-DD HH:mm:ss"
            value-format="YYYY-MM-DDTHH:mm:ss"
            style="width: 100%"
          />
          <span class="field-hint">留空则该 Key 永不过期；创建后过期时间不可在此处修改。</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="formDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="formSubmitting" @click="handleSubmitForm">
          {{ isEdit ? '保存' : '创建' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- ============ Key 明文展示对话框 (创建/轮换后) ============ -->
    <el-dialog
      v-model="keyDialogVisible"
      title="API Key 已生成"
      width="620px"
      :close-on-click-modal="false"
      :show-close="false"
    >
      <el-alert type="warning" :closable="false" show-icon class="mb-16">
        <template #title>
          请立即复制并妥善保存此 API Key 明文。关闭对话框后将无法再次查看，
          一旦丢失只能通过「轮换」重新生成。
        </template>
      </el-alert>
      <el-form label-position="top">
        <el-form-item label="API Key (明文，仅显示一次)">
          <el-input :model-value="revealedKey" readonly>
            <template #append>
              <el-button @click="copyKey">
                <el-icon><DocumentCopy /></el-icon>
                复制
              </el-button>
            </template>
          </el-input>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button type="primary" @click="keyDialogVisible = false"> 我已保存 </el-button>
      </template>
    </el-dialog>

    <!-- ============ 用量统计对话框 ============ -->
    <el-dialog
      v-model="usageDialogVisible"
      :title="`用量统计: ${usageTarget?.name || ''}`"
      width="680px"
    >
      <div v-loading="usageLoading" class="usage-body">
        <template v-if="usageData">
          <el-descriptions
            v-if="usageScalarEntries.length"
            :column="2"
            border
            size="small"
            class="mb-16"
          >
            <el-descriptions-item
              v-for="[k, v] in usageScalarEntries"
              :key="k"
              :label="usageFieldLabel(k)"
            >
              {{ formatUsageValue(k, v) }}
            </el-descriptions-item>
          </el-descriptions>
          <div class="usage-section-title">完整统计 (JSON)</div>
          <pre class="config-pre">{{ usageJsonText }}</pre>
        </template>
        <el-empty v-else-if="!usageLoading" description="暂无用量数据" />
      </div>
      <template #footer>
        <el-button @click="usageDialogVisible = false">关闭</el-button>
        <el-button :loading="usageLoading" @click="loadUsage(usageTarget)">
          <el-icon><RefreshLeft /></el-icon>
          刷新
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { apiKeyAdminApi } from '@/api/client'

// ============================================================
// 列表加载 (服务端分页)
// ============================================================
const loading = ref(false)
const keys = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)

async function loadKeys() {
  loading.value = true
  try {
    const data = await apiKeyAdminApi.list({
      page: page.value,
      page_size: pageSize.value,
    })
    keys.value = data.items || []
    total.value = data.total || 0
  } catch (err) {
    ElMessage.error(err.message || '加载 API Key 列表失败')
  } finally {
    loading.value = false
  }
}

function handleSearch() {
  page.value = 1
  loadKeys()
}

// ============================================================
// 创建/编辑
// ============================================================
const formDialogVisible = ref(false)
const formSubmitting = ref(false)
const isEdit = ref(false)
const editingId = ref(null)
const formRef = ref(null)

const form = reactive({
  name: '',
  scopes: '',
  rate_limit: 60,
  expires_at: null,
})

const formRules = {
  name: [
    { required: true, message: '请输入名称', trigger: 'blur' },
    { min: 1, max: 64, message: '长度 1-64 字符', trigger: 'blur' },
  ],
  rate_limit: [
    { required: true, message: '请输入速率限制', trigger: 'blur' },
    {
      validator: (_rule, value, cb) => {
        if (value === null || value === undefined || Number.isNaN(Number(value))) {
          cb(new Error('请输入有效的数字'))
        } else if (Number(value) < 0) {
          cb(new Error('速率限制不能为负数'))
        } else {
          cb()
        }
      },
      trigger: 'blur',
    },
  ],
}

const formTitle = computed(() => (isEdit.value ? '编辑 API Key' : '创建 API Key'))

function parseScopes(input) {
  if (!input) return []
  return input
    .split(/[,\s]+/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0)
}

function scopesToString(list) {
  if (!list || !list.length) return ''
  return list.join(', ')
}

function resetForm() {
  form.name = ''
  form.scopes = ''
  form.rate_limit = 60
  form.expires_at = null
  editingId.value = null
  formRef.value?.clearValidate?.()
}

function openCreateDialog() {
  isEdit.value = false
  resetForm()
  formDialogVisible.value = true
}

function openEditDialog(row) {
  isEdit.value = true
  editingId.value = row.id
  form.name = row.name || ''
  form.scopes = scopesToString(row.scopes)
  form.rate_limit = row.rate_limit != null ? row.rate_limit : 60
  form.expires_at = null
  formDialogVisible.value = true
}

async function handleSubmitForm() {
  if (!formRef.value) return
  try {
    await formRef.value.validate()
  } catch {
    return
  }
  formSubmitting.value = true
  try {
    if (isEdit.value) {
      const payload = {
        name: form.name,
        scopes: parseScopes(form.scopes),
        rate_limit: form.rate_limit,
      }
      await apiKeyAdminApi.update(editingId.value, payload)
      ElMessage.success('更新成功')
      formDialogVisible.value = false
      await loadKeys()
    } else {
      const payload = {
        name: form.name,
        scopes: parseScopes(form.scopes),
        rate_limit: form.rate_limit,
        expires_at: form.expires_at || null,
      }
      const data = await apiKeyAdminApi.create(payload)
      ElMessage.success('创建成功')
      formDialogVisible.value = false
      await loadKeys()
      // 明文 Key 仅返回一次，弹出展示对话框
      if (data && data.key) {
        showRevealedKey(data.key)
      }
    }
  } catch (err) {
    ElMessage.error(err.message || '保存失败')
  } finally {
    formSubmitting.value = false
  }
}

// ============================================================
// 吊销 (soft delete)
// ============================================================
async function handleRevoke(row) {
  try {
    await ElMessageBox.confirm(
      `确认吊销 API Key "${row.name || row.key_prefix}"? 吊销后该 Key 将立即失效，此操作不可恢复。`,
      '吊销确认',
      { type: 'warning', confirmButtonText: '确认吊销', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await apiKeyAdminApi.delete(row.id)
    ElMessage.success('已吊销')
    await loadKeys()
  } catch (err) {
    ElMessage.error(err.message || '吊销失败')
  }
}

// ============================================================
// 轮换 (生成新 Key, 旧 Key 吊销)
// ============================================================
async function handleRotate(row) {
  try {
    await ElMessageBox.confirm(
      `确认轮换 API Key "${row.name || row.key_prefix}"? 将生成新 Key 并立即吊销旧 Key，使用旧 Key 的服务需及时更新。`,
      '轮换确认',
      { type: 'warning', confirmButtonText: '确认轮换', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const data = await apiKeyAdminApi.rotate(row.id)
    ElMessage.success('轮换成功')
    await loadKeys()
    // 新明文 Key 仅返回一次，弹出展示对话框
    if (data && data.key) {
      showRevealedKey(data.key)
    }
  } catch (err) {
    ElMessage.error(err.message || '轮换失败')
  }
}

// ============================================================
// Key 明文展示
// ============================================================
const keyDialogVisible = ref(false)
const revealedKey = ref('')

function showRevealedKey(plaintext) {
  revealedKey.value = plaintext
  keyDialogVisible.value = true
}

async function copyKey() {
  if (!revealedKey.value) return
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(revealedKey.value)
      ElMessage.success('已复制到剪贴板')
      return
    }
  } catch {
    // 降级到 execCommand 兜底
  }
  // 兜底复制方案
  try {
    const ta = document.createElement('textarea')
    ta.value = revealedKey.value
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.focus()
    ta.select()
    document.execCommand('copy')
    document.body.removeChild(ta)
    ElMessage.success('已复制到剪贴板')
  } catch {
    ElMessage.error('复制失败，请手动选择文本复制')
  }
}

// ============================================================
// 用量统计
// ============================================================
const usageDialogVisible = ref(false)
const usageLoading = ref(false)
const usageTarget = ref(null)
const usageData = ref(null)

function openUsageDialog(row) {
  usageTarget.value = row
  usageDialogVisible.value = true
  loadUsage(row)
}

async function loadUsage(row) {
  if (!row) return
  usageLoading.value = true
  usageData.value = null
  try {
    const data = await apiKeyAdminApi.usage(row.id)
    usageData.value = data
  } catch (err) {
    ElMessage.error(err.message || '加载用量统计失败')
  } finally {
    usageLoading.value = false
  }
}

// 标量字段用于 el-descriptions 展示，嵌套结构走完整 JSON
const usageScalarEntries = computed(() => {
  if (!usageData.value || typeof usageData.value !== 'object') return []
  return Object.entries(usageData.value).filter(
    ([, v]) => v === null || v === undefined || typeof v !== 'object',
  )
})

const usageJsonText = computed(() => {
  if (!usageData.value) return ''
  try {
    return JSON.stringify(usageData.value, null, 2)
  } catch {
    return String(usageData.value)
  }
})

const USAGE_FIELD_LABELS = {
  total_requests: '总请求数',
  request_count: '请求数',
  requests: '请求数',
  total_tokens: 'Token 总量',
  tokens: 'Token 总量',
  last_used_at: '最近使用',
  last_used: '最近使用',
  created_at: '创建时间',
  error_count: '错误数',
  rate_limited_count: '限流次数',
  period: '统计周期',
}

function usageFieldLabel(key) {
  return USAGE_FIELD_LABELS[key] || key
}

function formatUsageValue(key, value) {
  if (value === null || value === undefined) return '—'
  if (key === 'last_used_at' || key === 'last_used' || key === 'created_at') {
    return formatTime(value)
  }
  return String(value)
}

// ============================================================
// 辅助函数
// ============================================================
function keyPrefixDisplay(row) {
  const prefix = row.key_prefix || ''
  return prefix.slice(0, 12)
}

function isExpired(row) {
  if (!row.expires_at) return false
  const ts = new Date(row.expires_at).getTime()
  return !Number.isNaN(ts) && ts < Date.now()
}

function formatTime(ts) {
  if (!ts) return '—'
  try {
    return new Date(ts).toLocaleString('zh-CN')
  } catch {
    return String(ts)
  }
}

// ============================================================
// 初始化
// ============================================================
onMounted(() => {
  loadKeys()
})
</script>

<style scoped>
.admin-api-keys {
  padding: 0;
}
.mb-16 {
  margin-bottom: 16px;
}
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}
.toolbar-actions {
  display: flex;
  gap: 8px;
}
.section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  font-size: 16px;
}
.key-name {
  font-weight: 600;
  color: var(--el-text-color-primary);
}
.key-prefix-code {
  font-family: ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace;
  background: var(--el-fill-color-light);
  padding: 2px 8px;
  border-radius: 3px;
  font-size: 12px;
  color: var(--el-color-success);
  letter-spacing: 0.5px;
}
.scope-tag {
  margin-right: 4px;
  margin-bottom: 2px;
}
.rate-limit-value {
  font-weight: 600;
  color: var(--el-text-color-primary);
}
.rate-limit-unit {
  margin-left: 2px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.meta-empty {
  color: var(--el-text-color-placeholder);
}
.expired-text {
  color: var(--el-color-warning);
}
.pagination-wrapper {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}
/* 表单辅助 */
.field-hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  display: block;
  margin-top: 4px;
}
/* 用量统计 */
.usage-body {
  min-height: 120px;
}
.usage-section-title {
  font-weight: 600;
  color: var(--el-text-color-primary);
  margin-bottom: 6px;
  font-size: 13px;
}
.config-pre {
  background-color: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
  padding: 12px;
  font-family: ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 320px;
  overflow: auto;
  margin: 0;
}
</style>
