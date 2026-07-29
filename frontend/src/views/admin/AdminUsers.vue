<template>
  <div class="admin-users">
    <!-- 顶部说明 -->
    <el-alert type="info" :closable="false" show-icon class="mb-16">
      <template #title>
        用户管理 —— 对标企业 IAM 用户中心。支持创建/编辑/禁用启用/删除用户，
        支持按角色与部门筛选、关键字搜索，以及 CSV 批量导入（含导入结果反馈）。
        该模块仅 admin 角色可访问。
      </template>
    </el-alert>

    <!-- 顶部工具栏: 标题 + 操作按钮 -->
    <div class="toolbar mb-16">
      <span class="section-title">
        <el-icon><User /></el-icon>
        用户管理
      </span>
      <div class="toolbar-actions">
        <el-button type="primary" @click="openCreateDialog">
          <el-icon><Plus /></el-icon>
          创建用户
        </el-button>
        <el-button @click="importDialogVisible = true">
          <el-icon><Upload /></el-icon>
          批量导入
        </el-button>
        <el-button :loading="loading" @click="loadUsers">
          <el-icon><RefreshLeft /></el-icon>
          刷新
        </el-button>
      </div>
    </div>

    <!-- 筛选区 -->
    <div class="filter-bar mb-16">
      <el-input
        v-model="filters.keyword"
        placeholder="搜索 user_id / 姓名 / 邮箱"
        clearable
        class="filter-input"
        @keyup.enter="handleSearch"
        @clear="handleSearch"
      >
        <template #prefix>
          <el-icon><Search /></el-icon>
        </template>
      </el-input>
      <el-select
        v-model="filters.role"
        placeholder="按角色筛选"
        clearable
        class="filter-select"
        @change="handleSearch"
      >
        <el-option label="全部角色" :value="''" />
        <el-option v-for="r in roleOptions" :key="r.value" :label="r.label" :value="r.value" />
      </el-select>
      <el-input
        v-model="filters.department"
        placeholder="按部门筛选"
        clearable
        class="filter-select"
        @keyup.enter="handleSearch"
        @clear="handleSearch"
      />
      <el-button type="primary" plain @click="handleSearch">
        <el-icon><Search /></el-icon>
        查询
      </el-button>
      <el-button @click="handleReset">重置</el-button>
    </div>

    <!-- 列表表格 -->
    <el-card v-loading="loading" :aria-busy="loading">
      <el-table :data="users" stripe style="width: 100%" empty-text="暂无用户数据">
        <el-table-column label="User ID" min-width="140" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="mono-text">{{ row.user_id || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="姓名" min-width="120">
          <template #default="{ row }">
            <span class="user-name">{{ row.name || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="邮箱" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <span>{{ row.email || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="角色" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="roleTagType(row.role)">
              {{ roleLabel(row.role) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="部门" min-width="140">
          <template #default="{ row }">
            <span v-if="row.department">{{ row.department }}</span>
            <span v-else class="meta-empty">—</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag v-if="row.disabled" size="small" type="info">已禁用</el-tag>
            <el-tag v-else size="small" type="success">启用中</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="180">
          <template #default="{ row }">
            {{ formatTime(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="240" fixed="right">
          <template #default="{ row }">
            <el-button size="small" link @click="openEditDialog(row)">
              <el-icon><Edit /></el-icon>
              编辑
            </el-button>
            <el-button
              size="small"
              link
              :type="row.disabled ? 'success' : 'warning'"
              :loading="togglingId === row.user_id"
              @click="handleToggleDisable(row)"
            >
              <el-icon><Lock v-if="!row.disabled" /><Unlock v-else /></el-icon>
              {{ row.disabled ? '启用' : '禁用' }}
            </el-button>
            <el-button size="small" link type="danger" @click="handleDelete(row)">
              <el-icon><Delete /></el-icon>
              删除
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
          @current-change="loadUsers"
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
        <el-form-item label="User ID" prop="user_id">
          <el-input
            v-model="form.user_id"
            placeholder="如：E1001"
            maxlength="64"
            :disabled="isEdit"
          />
          <span class="field-hint">唯一用户标识，创建后不可修改。</span>
        </el-form-item>
        <el-form-item label="姓名" prop="name">
          <el-input
            v-model="form.name"
            placeholder="请输入姓名"
            maxlength="64"
            show-word-limit
          />
        </el-form-item>
        <el-form-item label="邮箱" prop="email">
          <el-input
            v-model="form.email"
            placeholder="请输入邮箱"
            :disabled="isEdit"
          />
          <span class="field-hint">创建后邮箱不可修改。</span>
        </el-form-item>
        <el-form-item label="角色" prop="role">
          <el-select v-model="form.role" placeholder="请选择角色" style="width: 100%">
            <el-option v-for="r in roleOptions" :key="r.value" :label="r.label" :value="r.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="部门" prop="department">
          <el-input v-model="form.department" placeholder="如：产品研发部" maxlength="64" />
        </el-form-item>
        <el-form-item v-if="!isEdit" label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="请输入初始密码"
            show-password
          />
          <span class="field-hint">仅创建时需要，编辑用户不在此处修改密码。</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="formDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="formSubmitting" @click="handleSubmitForm">
          {{ isEdit ? '保存' : '创建' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- ============ 批量导入 对话框 ============ -->
    <el-dialog
      v-model="importDialogVisible"
      title="批量导入用户 (CSV)"
      width="600px"
      :close-on-click-modal="false"
      @closed="resetImport"
    >
      <el-alert type="info" :closable="false" show-icon class="mb-16">
        <template #title>
          CSV 文件需包含表头：user_id, name, email, role, department, password。
          每行一个用户，导入后将在下方显示成功与失败详情。
        </template>
      </el-alert>
      <el-upload
        ref="uploadRef"
        drag
        :auto-upload="false"
        :limit="1"
        accept=".csv"
        :on-change="handleFileChange"
        :on-exceed="handleExceed"
        :file-list="fileList"
      >
        <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
        <div class="el-upload__text">
          将 CSV 文件拖到此处，或 <em>点击上传</em>
        </div>
        <template #tip>
          <div class="el-upload__tip">仅支持 .csv 文件，单次上传一个文件。</div>
        </template>
      </el-upload>

      <!-- 导入结果 -->
      <div v-if="importResult" class="import-result mt-16">
        <el-descriptions :column="2" border size="small" class="mb-16">
          <el-descriptions-item label="总计">
            {{ importResult.total ?? 0 }}
          </el-descriptions-item>
          <el-descriptions-item label="成功">
            <span class="success-text">{{ importResult.success ?? 0 }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="失败">
            <span class="danger-text">{{ importResult.failed ?? 0 }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="消息">
            {{ importResult.message || '—' }}
          </el-descriptions-item>
        </el-descriptions>
        <div v-if="importErrors.length" class="usage-section-title">失败详情</div>
        <pre v-if="importErrors.length" class="config-pre">{{ importErrorsText }}</pre>
      </div>

      <template #footer>
        <el-button @click="importDialogVisible = false">关闭</el-button>
        <el-button
          type="primary"
          :loading="importing"
          :disabled="!selectedFile"
          @click="handleImport"
        >
          开始导入
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { userAdminApi } from '@/api/client'

// ============================================================
// 角色配置
// ============================================================
const roleOptions = [
  { value: 'admin', label: '管理员 (admin)' },
  { value: 'hr', label: 'HR (hr)' },
  { value: 'manager', label: '主管 (manager)' },
  { value: 'employee', label: '员工 (employee)' },
]

function roleLabel(value) {
  const item = roleOptions.find((r) => r.value === value)
  return item ? item.label.split(' ')[0] : value || '—'
}

function roleTagType(value) {
  const map = { admin: 'danger', hr: 'warning', manager: 'success', employee: '' }
  return map[value] || 'info'
}

// ============================================================
// 列表加载 (服务端分页 + 筛选 + 搜索)
// ============================================================
const loading = ref(false)
const users = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)

const filters = reactive({
  keyword: '',
  role: '',
  department: '',
})

async function loadUsers() {
  loading.value = true
  try {
    const params = {
      page: page.value,
      page_size: pageSize.value,
    }
    if (filters.keyword) params.keyword = filters.keyword
    if (filters.role) params.role = filters.role
    if (filters.department) params.department = filters.department
    const data = await userAdminApi.list(params)
    users.value = data.items || []
    total.value = data.total || 0
  } catch (err) {
    console.error('加载用户列表失败:', err)
    ElMessage.error(err.message || '加载用户列表失败')
  } finally {
    loading.value = false
  }
}

function handleSearch() {
  page.value = 1
  loadUsers()
}

function handleReset() {
  filters.keyword = ''
  filters.role = ''
  filters.department = ''
  page.value = 1
  loadUsers()
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
  user_id: '',
  name: '',
  email: '',
  role: 'employee',
  department: '',
  password: '',
})

const formRules = {
  user_id: [
    { required: true, message: '请输入 User ID', trigger: 'blur' },
    { min: 1, max: 64, message: '长度 1-64 字符', trigger: 'blur' },
  ],
  name: [
    { required: true, message: '请输入姓名', trigger: 'blur' },
    { min: 1, max: 64, message: '长度 1-64 字符', trigger: 'blur' },
  ],
  email: [
    { required: true, message: '请输入邮箱', trigger: 'blur' },
    { type: 'email', message: '邮箱格式不正确', trigger: 'blur' },
  ],
  role: [{ required: true, message: '请选择角色', trigger: 'change' }],
  password: [
    {
      validator: (_rule, value, cb) => {
        // 仅创建时校验密码
        if (isEdit.value) {
          cb()
          return
        }
        if (!value) {
          cb(new Error('请输入初始密码'))
        } else if (value.length < 6) {
          cb(new Error('密码长度至少 6 位'))
        } else {
          cb()
        }
      },
      trigger: 'blur',
    },
  ],
}

const formTitle = computed(() => (isEdit.value ? '编辑用户' : '创建用户'))

function resetForm() {
  form.user_id = ''
  form.name = ''
  form.email = ''
  form.role = 'employee'
  form.department = ''
  form.password = ''
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
  editingId.value = row.user_id
  form.user_id = row.user_id || ''
  form.name = row.name || ''
  form.email = row.email || ''
  form.role = row.role || 'employee'
  form.department = row.department || ''
  form.password = ''
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
      // 编辑: 仅修改 name / role / department, 不改密码
      const payload = {
        name: form.name,
        role: form.role,
        department: form.department,
      }
      await userAdminApi.update(editingId.value, payload)
      ElMessage.success('更新成功')
      formDialogVisible.value = false
      await loadUsers()
    } else {
      const payload = {
        user_id: form.user_id,
        name: form.name,
        email: form.email,
        role: form.role,
        department: form.department,
        password: form.password,
      }
      await userAdminApi.create(payload)
      ElMessage.success('创建成功')
      formDialogVisible.value = false
      await loadUsers()
    }
  } catch (err) {
    console.error('保存用户失败:', err)
    ElMessage.error(err.message || '保存失败')
  } finally {
    formSubmitting.value = false
  }
}

// ============================================================
// 禁用/启用 (toggle disable)
// ============================================================
const togglingId = ref(null)

async function handleToggleDisable(row) {
  const nextDisabled = !row.disabled
  const action = nextDisabled ? '禁用' : '启用'
  try {
    await ElMessageBox.confirm(
      `确认${action}用户 "${row.name || row.user_id}"?`,
      `${action}确认`,
      { type: 'warning', confirmButtonText: `确认${action}`, cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  togglingId.value = row.user_id
  try {
    await userAdminApi.toggleDisable(row.user_id, nextDisabled)
    ElMessage.success(`已${action}`)
    await loadUsers()
  } catch (err) {
    console.error(`${action}用户失败:`, err)
    ElMessage.error(err.message || `${action}失败`)
  } finally {
    togglingId.value = null
  }
}

// ============================================================
// 删除 (二次确认)
// ============================================================
async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(
      `确认删除用户 "${row.name || row.user_id}"? 删除后该用户将无法登录，此操作不可恢复。`,
      '删除确认',
      { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await userAdminApi.delete(row.user_id)
    ElMessage.success('删除成功')
    await loadUsers()
  } catch (err) {
    console.error('删除用户失败:', err)
    ElMessage.error(err.message || '删除失败')
  }
}

// ============================================================
// 批量导入 (CSV 上传, 显示导入结果)
// ============================================================
const importDialogVisible = ref(false)
const importing = ref(false)
const uploadRef = ref(null)
const fileList = ref([])
const selectedFile = ref(null)
const importResult = ref(null)

const importErrors = computed(() => {
  if (!importResult.value) return []
  const errs = importResult.value.errors || importResult.value.failures || []
  return Array.isArray(errs) ? errs : []
})

const importErrorsText = computed(() => {
  if (!importErrors.value.length) return ''
  try {
    return importErrors.value
      .map((e, i) => `${i + 1}. ${typeof e === 'string' ? e : JSON.stringify(e)}`)
      .join('\n')
  } catch {
    return String(importErrors.value)
  }
})

function handleFileChange(file) {
  if (!file) {
    selectedFile.value = null
    return
  }
  selectedFile.value = file.raw || null
  // 保持单文件
  fileList.value = [file]
}

function handleExceed(files) {
  ElMessage.warning('仅支持上传一个 CSV 文件，已替换为新文件')
  uploadRef.value?.clearFiles?.()
  const file = files[0]
  uploadRef.value?.handleStart?.(file)
  selectedFile.value = file
  fileList.value = [{ name: file.name, raw: file }]
}

function resetImport() {
  selectedFile.value = null
  fileList.value = []
  importResult.value = null
  uploadRef.value?.clearFiles?.()
}

async function handleImport() {
  if (!selectedFile.value) {
    ElMessage.warning('请先选择 CSV 文件')
    return
  }
  importing.value = true
  importResult.value = null
  try {
    const formData = new FormData()
    formData.append('file', selectedFile.value)
    const data = await userAdminApi.batchImport(formData)
    importResult.value = data || { total: 0, success: 0, failed: 0 }
    const success = data?.success ?? 0
    const failed = data?.failed ?? 0
    if (failed > 0) {
      ElMessage.warning(`导入完成：成功 ${success} 条，失败 ${failed} 条`)
    } else {
      ElMessage.success(`导入成功 ${success} 条`)
    }
    // 导入后刷新列表
    await loadUsers()
  } catch (err) {
    console.error('批量导入失败:', err)
    ElMessage.error(err.message || '批量导入失败')
  } finally {
    importing.value = false
  }
}

// ============================================================
// 辅助函数
// ============================================================
function formatTime(ts) {
  if (!ts) return '—'
  try {
    return new Date(ts).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return String(ts)
  }
}

// ============================================================
// 初始化
// ============================================================
onMounted(() => {
  loadUsers()
})
</script>

<style scoped>
.admin-users {
  padding: 0;
}
.mb-16 {
  margin-bottom: 16px;
}
.mt-16 {
  margin-top: 16px;
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
  flex-wrap: wrap;
}
.section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  font-size: 16px;
}
.filter-bar {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
}
.filter-input {
  width: 280px;
}
.filter-select {
  width: 200px;
}
.user-name {
  font-weight: 600;
  color: var(--el-text-color-primary);
}
.mono-text {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px;
  color: var(--el-text-color-primary);
}
.meta-empty {
  color: var(--el-text-color-placeholder);
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
/* 导入结果 */
.import-result {
  width: 100%;
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
  max-height: 240px;
  overflow: auto;
  margin: 0;
}
.success-text {
  color: var(--el-color-success);
  font-weight: 600;
}
.danger-text {
  color: var(--el-color-danger);
  font-weight: 600;
}
</style>
