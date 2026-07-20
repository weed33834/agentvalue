<template>
  <div class="admin-feature-flags">
    <!-- 顶部说明 -->
    <el-alert type="info" :closable="false" show-icon class="mb-16">
      <template #title>
        功能开关 —— 对标 Langfuse Feature Flag。支持按 tenant / user / 百分比分流，
        运行时动态切换功能行为（启用 rerank、启用新 prompt 灰度、启用多 Agent 模式等）。
      </template>
    </el-alert>

    <!-- 工具栏 -->
    <div class="toolbar mb-16">
      <el-select
        v-model="filterCategory"
        placeholder="按分类过滤"
        clearable
        style="width: 200px"
        @change="loadFlags"
      >
        <el-option label="全部分类" :value="''" />
        <el-option
          v-for="cat in categories"
          :key="cat.value"
          :label="cat.label"
          :value="cat.value"
        />
      </el-select>
      <el-button :loading="loading" @click="loadFlags">
        <el-icon><RefreshLeft /></el-icon>
        刷新
      </el-button>
      <el-button type="primary" @click="openCreateDialog">
        <el-icon><Plus /></el-icon>
        新建 Flag
      </el-button>
    </div>

    <!-- 列表表格 -->
    <el-card v-loading="loading">
      <el-table :data="flags" style="width: 100%" empty-text="暂无 Feature Flag">
        <el-table-column prop="key" label="Key" min-width="180" show-overflow-tooltip />
        <el-table-column prop="description" label="描述" min-width="200" show-overflow-tooltip />
        <el-table-column label="分类" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="categoryTagType(row.category)">
              {{ categoryLabel(row.category) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="启用" width="100">
          <template #default="{ row }">
            <el-switch
              :model-value="!!row.enabled"
              :loading="togglingKey === row.key"
              @change="(val) => handleToggle(row, val)"
            />
          </template>
        </el-table-column>
        <el-table-column label="灰度%" width="120">
          <template #default="{ row }">
            <span :class="{ 'pct-full': row.rollout_percentage === 100 }">
              {{ row.rollout_percentage }}%
            </span>
          </template>
        </el-table-column>
        <el-table-column label="精确受众" min-width="240">
          <template #default="{ row }">
            <div class="target-cell">
              <div v-if="row.target_tenant_ids && row.target_tenant_ids.length">
                <span class="target-label">租户:</span>
                <el-tag
                  v-for="t in row.target_tenant_ids"
                  :key="t"
                  size="small"
                  type="warning"
                  class="target-tag"
                >{{ t }}</el-tag>
              </div>
              <div v-if="row.target_user_ids && row.target_user_ids.length">
                <span class="target-label">用户:</span>
                <el-tag
                  v-for="u in row.target_user_ids"
                  :key="u"
                  size="small"
                  type="success"
                  class="target-tag"
                >{{ u }}</el-tag>
              </div>
              <span
                v-if="(!row.target_tenant_ids || !row.target_tenant_ids.length) &&
                  (!row.target_user_ids || !row.target_user_ids.length)"
                class="meta-empty"
              >—</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="280" fixed="right">
          <template #default="{ row }">
            <el-button size="small" link @click="openEditDialog(row)">编辑</el-button>
            <el-button size="small" link type="primary" @click="openCheckDialog(row)">
              测试
            </el-button>
            <el-button
              size="small"
              link
              type="danger"
              @click="handleDelete(row)"
            >删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 创建/编辑 Dialog -->
    <el-dialog
      v-model="formDialogVisible"
      :title="formTitle"
      width="640px"
      @closed="resetForm"
    >
      <el-form
        ref="formRef"
        :model="form"
        :rules="formRules"
        label-position="top"
        v-loading="formSubmitting"
      >
        <el-form-item label="Key (业务标识,创建后不可改)" prop="key">
          <el-input
            v-model="form.key"
            placeholder="如 use_rerank_v2 / enable_multi_agent"
            :disabled="isEdit"
          />
        </el-form-item>
        <el-form-item label="描述" prop="description">
          <el-input
            v-model="form.description"
            type="textarea"
            :rows="2"
            placeholder="说明此 flag 的用途"
          />
        </el-form-item>
        <el-form-item label="分类">
          <el-select v-model="form.category" style="width: 100%">
            <el-option
              v-for="cat in categories"
              :key="cat.value"
              :label="cat.label"
              :value="cat.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="全局开关">
          <el-switch v-model="form.enabled" />
          <span class="form-hint">关闭时直接返回 False, 跳过灰度判断</span>
        </el-form-item>
        <el-form-item :label="`灰度百分比: ${form.rollout_percentage}%`">
          <el-slider
            v-model="form.rollout_percentage"
            :min="0"
            :max="100"
            :step="1"
            show-input
          />
          <span class="form-hint">hash(user_id 或 tenant_id) % 100 &lt; 百分比 → 命中</span>
        </el-form-item>
        <el-form-item label="精确受众租户 (逗号或空格分隔)">
          <el-input
            v-model="targetTenantInput"
            placeholder="tenant_a, tenant_b"
          />
        </el-form-item>
        <el-form-item label="精确受众用户 (逗号或空格分隔)">
          <el-input
            v-model="targetUserInput"
            placeholder="user_1, user_2"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="formDialogVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="formSubmitting"
          @click="handleSubmitForm"
        >保存</el-button>
      </template>
    </el-dialog>

    <!-- 测试 Dialog -->
    <el-dialog
      v-model="checkDialogVisible"
      :title="`测试 Flag: ${checkTarget?.key || ''}`"
      width="540px"
    >
      <el-form label-position="top">
        <el-form-item label="租户 ID (可选)">
          <el-input v-model="checkForm.tenant_id" placeholder="留空表示不传" />
        </el-form-item>
        <el-form-item label="用户 ID (可选)">
          <el-input v-model="checkForm.user_id" placeholder="留空表示不传" />
        </el-form-item>
      </el-form>
      <div v-if="checkResult" class="check-result">
        <el-divider content-position="left">结果</el-divider>
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="启用状态">
            <el-tag :type="checkResult.enabled ? 'success' : 'info'">
              {{ checkResult.enabled ? '已启用' : '未启用' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="命中原因">
            {{ reasonLabel(checkResult.reason) }}
          </el-descriptions-item>
          <el-descriptions-item v-if="checkResult.bucket !== undefined && checkResult.bucket !== null" label="Hash 桶号">
            {{ checkResult.bucket }} / 100
          </el-descriptions-item>
          <el-descriptions-item v-if="checkResult.percentage !== undefined && checkResult.percentage !== null" label="灰度百分比">
            {{ checkResult.percentage }}%
          </el-descriptions-item>
        </el-descriptions>
      </div>
      <template #footer>
        <el-button @click="checkDialogVisible = false">关闭</el-button>
        <el-button
          type="primary"
          :loading="checkLoading"
          @click="handleRunCheck"
        >检查</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { featureFlagAdminApi } from '@/api/client'

const loading = ref(false)
const flags = ref([])
const filterCategory = ref('')
const togglingKey = ref('')

const categories = [
  { value: 'general', label: '通用 (general)' },
  { value: 'model', label: '模型 (model)' },
  { value: 'agent', label: 'Agent' },
  { value: 'feature', label: '功能 (feature)' },
]

function categoryLabel(value) {
  const item = categories.find((c) => c.value === value)
  return item ? item.label : value
}

function categoryTagType(value) {
  const map = { general: '', model: 'warning', agent: 'success', feature: 'info' }
  return map[value] || ''
}

// ============================================================
// 列表加载
// ============================================================

async function loadFlags() {
  loading.value = true
  try {
    const params = {}
    if (filterCategory.value) params.category = filterCategory.value
    const data = await featureFlagAdminApi.list(params)
    flags.value = data.items || []
  } catch (err) {
    ElMessage.error(err.message || '加载 Feature Flag 列表失败')
  } finally {
    loading.value = false
  }
}

// ============================================================
// 创建/编辑
// ============================================================

const formDialogVisible = ref(false)
const formSubmitting = ref(false)
const isEdit = ref(false)
const formRef = ref(null)

const form = reactive({
  key: '',
  description: '',
  enabled: false,
  rollout_percentage: 0,
  category: 'general',
})

const targetTenantInput = ref('')
const targetUserInput = ref('')

const formRules = {
  key: [
    { required: true, message: '请输入 key', trigger: 'blur' },
    { min: 1, max: 64, message: '长度 1-64 字符', trigger: 'blur' },
  ],
}

const formTitle = computed(() => (isEdit.value ? '编辑 Feature Flag' : '新建 Feature Flag'))

function parseTags(input) {
  if (!input) return []
  return input
    .split(/[,\s]+/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0)
}

function tagsToString(list) {
  if (!list || !list.length) return ''
  return list.join(', ')
}

function openCreateDialog() {
  isEdit.value = false
  resetForm()
  formDialogVisible.value = true
}

function openEditDialog(row) {
  isEdit.value = true
  form.key = row.key
  form.description = row.description || ''
  form.enabled = !!row.enabled
  form.rollout_percentage = row.rollout_percentage || 0
  form.category = row.category || 'general'
  targetTenantInput.value = tagsToString(row.target_tenant_ids)
  targetUserInput.value = tagsToString(row.target_user_ids)
  formDialogVisible.value = true
}

function resetForm() {
  form.key = ''
  form.description = ''
  form.enabled = false
  form.rollout_percentage = 0
  form.category = 'general'
  targetTenantInput.value = ''
  targetUserInput.value = ''
  formRef.value?.clearValidate?.()
}

async function handleSubmitForm() {
  if (!formRef.value) return
  try {
    await formRef.value.validate()
  } catch {
    return
  }
  formSubmitting.value = true
  const payload = {
    description: form.description,
    enabled: form.enabled,
    rollout_percentage: form.rollout_percentage,
    category: form.category,
    target_tenant_ids: parseTags(targetTenantInput.value),
    target_user_ids: parseTags(targetUserInput.value),
  }
  try {
    if (isEdit.value) {
      await featureFlagAdminApi.update(form.key, payload)
      ElMessage.success('更新成功')
    } else {
      payload.key = form.key
      await featureFlagAdminApi.create(payload)
      ElMessage.success('创建成功')
    }
    formDialogVisible.value = false
    await loadFlags()
  } catch (err) {
    ElMessage.error(err.message || '保存失败')
  } finally {
    formSubmitting.value = false
  }
}

// ============================================================
// 启用/禁用切换
// ============================================================

async function handleToggle(row, enabled) {
  togglingKey.value = row.key
  try {
    await featureFlagAdminApi.toggle(row.key, enabled)
    row.enabled = enabled
    ElMessage.success(`${enabled ? '启用' : '禁用'} ${row.key} 成功`)
  } catch (err) {
    ElMessage.error(err.message || '切换失败')
  } finally {
    togglingKey.value = ''
  }
}

// ============================================================
// 删除
// ============================================================

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(
      `确认删除 Feature Flag "${row.key}"? 此操作不可恢复。`,
      '删除确认',
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await featureFlagAdminApi.delete(row.key)
    ElMessage.success('删除成功')
    await loadFlags()
  } catch (err) {
    ElMessage.error(err.message || '删除失败')
  }
}

// ============================================================
// 测试 (检查 flag 状态)
// ============================================================

const checkDialogVisible = ref(false)
const checkLoading = ref(false)
const checkTarget = ref(null)
const checkResult = ref(null)
const checkForm = reactive({
  tenant_id: '',
  user_id: '',
})

function openCheckDialog(row) {
  checkTarget.value = row
  checkForm.tenant_id = ''
  checkForm.user_id = ''
  checkResult.value = null
  checkDialogVisible.value = true
}

async function handleRunCheck() {
  if (!checkTarget.value) return
  checkLoading.value = true
  try {
    const params = {}
    if (checkForm.tenant_id.trim()) params.tenant_id = checkForm.tenant_id.trim()
    if (checkForm.user_id.trim()) params.user_id = checkForm.user_id.trim()
    const data = await featureFlagAdminApi.check(checkTarget.value.key, params)
    checkResult.value = data
  } catch (err) {
    ElMessage.error(err.message || '检查失败')
  } finally {
    checkLoading.value = false
  }
}

const REASON_LABELS = {
  flag_not_found: 'Flag 不存在',
  flag_disabled: 'Flag 已禁用 (enabled=false)',
  target_user_hit: '命中精确用户 (target_user_ids)',
  target_tenant_hit: '命中精确租户 (target_tenant_ids)',
  rollout_percentage_hit: '命中百分比灰度',
  rollout_percentage_miss: '未命中百分比灰度',
  default_off: '默认关闭 (无 target 也无命中)',
}

function reasonLabel(reason) {
  return REASON_LABELS[reason] || reason
}

onMounted(() => {
  loadFlags()
})
</script>

<style scoped>
.admin-feature-flags {
  padding: 0;
}
.mb-16 {
  margin-bottom: 16px;
}
.toolbar {
  display: flex;
  gap: 12px;
  align-items: center;
}
.target-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.target-label {
  font-size: 12px;
  color: #909399;
  margin-right: 4px;
}
.target-tag {
  margin-right: 4px;
  margin-bottom: 2px;
}
.meta-empty {
  color: #c0c4cc;
}
.pct-full {
  color: #67c23a;
  font-weight: 600;
}
.form-hint {
  margin-left: 8px;
  font-size: 12px;
  color: #909399;
}
.check-result {
  margin-top: 8px;
}
</style>
