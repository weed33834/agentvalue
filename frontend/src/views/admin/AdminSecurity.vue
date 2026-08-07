<template>
  <div class="admin-security av-fade-in-up">
    <div class="page-header mb-16">
      <div class="page-title">
        <el-icon><Lock /></el-icon>
        <span>安全治理</span>
      </div>
    </div>

    <el-tabs v-model="activeTab" @tab-change="onTabChange">
      <!-- ============ 敏感词管理 ============ -->
      <el-tab-pane label="敏感词管理" name="sensitive">
        <div class="toolbar mb-16">
          <el-input v-model="swFilter.keyword" placeholder="搜索敏感词" clearable style="width: 220px" @change="loadWords" />
          <el-select v-model="swFilter.status" placeholder="审核状态" clearable style="width: 160px" @change="loadWords">
            <el-option label="待审核" value="pending" />
            <el-option label="已通过" value="approved" />
            <el-option label="已拒绝" value="rejected" />
          </el-select>
          <el-button :loading="swLoading" @click="loadWords"><el-icon><RefreshLeft /></el-icon>刷新</el-button>
          <el-button type="primary" @click="openWordDialog"><el-icon><Plus /></el-icon>新增</el-button>
          <el-button type="warning" @click="importDialogVisible = true"><el-icon><Upload /></el-icon>批量导入</el-button>
        </div>
        <el-card v-loading="swLoading">
          <el-table :data="words" stripe empty-text="暂无敏感词">
            <el-table-column prop="id" label="ID" width="70" align="center" />
            <el-table-column prop="term" label="敏感词" min-width="180" show-overflow-tooltip />
            <el-table-column prop="category" label="分类" width="140" />
            <el-table-column label="审核状态" width="120">
              <template #default="{ row }">
                <el-tag size="small" :type="reviewTagType(row.status)">{{ reviewLabel(row.status) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="创建时间" width="180">
              <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="240" fixed="right">
              <template #default="{ row }">
                <el-button v-if="row.status === 'pending'" size="small" link type="success" :loading="reviewingId === row.id" @click="handleReview(row, 'approved')">通过</el-button>
                <el-button v-if="row.status === 'pending'" size="small" link type="danger" :loading="reviewingId === row.id" @click="handleReview(row, 'rejected')">拒绝</el-button>
                <el-button size="small" link type="danger" @click="handleDeleteWord(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div class="pagination-wrapper">
            <el-pagination v-model:current-page="swPage" v-model:page-size="swPageSize" :total="swTotal" :page-sizes="[10, 20, 50]" layout="total, sizes, prev, pager, next, jumper" @size-change="loadWords" @current-change="loadWords" />
          </div>
        </el-card>
      </el-tab-pane>

      <!-- ============ SSO 配置 ============ -->
      <el-tab-pane label="SSO 配置" name="sso">
        <div class="toolbar mb-16">
          <el-button :loading="ssoLoading" @click="loadSso"><el-icon><RefreshLeft /></el-icon>刷新</el-button>
          <el-button type="primary" @click="openSsoDialog"><el-icon><Plus /></el-icon>新增 SSO</el-button>
          <el-button type="success" @click="openLdapDialog"><el-icon><Connection /></el-icon>LDAP 登录测试</el-button>
        </div>
        <el-card v-loading="ssoLoading">
          <el-table :data="ssoConfigs" stripe empty-text="暂无 SSO 配置">
            <el-table-column prop="id" label="ID" width="70" align="center" />
            <el-table-column prop="provider_name" label="名称" min-width="140" show-overflow-tooltip />
            <el-table-column prop="provider_type" label="协议" width="120">
              <template #default="{ row }"><el-tag size="small">{{ row.provider_type || '—' }}</el-tag></template>
            </el-table-column>
            <el-table-column label="Issuer / 地址" min-width="220" show-overflow-tooltip>
              <template #default="{ row }">{{ issuerOf(row) }}</template>
            </el-table-column>
            <el-table-column label="启用" width="90">
              <template #default="{ row }"><el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '是' : '否' }}</el-tag></template>
            </el-table-column>
            <el-table-column label="操作" width="160" fixed="right">
              <template #default="{ row }">
                <el-button size="small" link @click="openSsoDialog(row)">编辑</el-button>
                <el-button size="small" link type="danger" @click="handleDeleteSso(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <!-- 新增敏感词 -->
    <el-dialog v-model="wordDialogVisible" title="新增敏感词" width="460px">
      <el-form ref="wordFormRef" :model="wordForm" :rules="wordRules" label-position="top" v-loading="wordSubmitting">
        <el-form-item label="敏感词" prop="term"><el-input v-model="wordForm.term" /></el-form-item>
        <el-form-item label="分类"><el-input v-model="wordForm.category" placeholder="如 政治/涉黄/广告" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="wordDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="wordSubmitting" @click="handleSubmitWord">保存</el-button>
      </template>
    </el-dialog>

    <!-- 批量导入 -->
    <el-dialog v-model="importDialogVisible" title="批量导入敏感词" width="520px">
      <el-alert type="info" :closable="false" show-icon class="mb-16">每行一个敏感词，支持 # 开头注释行</el-alert>
      <el-input v-model="importText" type="textarea" :rows="10" placeholder="敏感词1&#10;敏感词2&#10;# 注释" />
      <template #footer>
        <el-button @click="importDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="importSubmitting" @click="handleImport">导入</el-button>
      </template>
    </el-dialog>

    <!-- SSO 新增/编辑 -->
    <el-dialog v-model="ssoDialogVisible" :title="ssoIsEdit ? '编辑 SSO 配置' : '新增 SSO 配置'" width="560px" @closed="resetSsoForm">
      <el-form ref="ssoFormRef" :model="ssoForm" :rules="ssoRules" label-position="top" v-loading="ssoSubmitting">
        <el-form-item label="提供商名称" prop="provider_name"><el-input v-model="ssoForm.provider_name" placeholder="如 企业微信/飞书/Okta" /></el-form-item>
        <el-form-item label="协议类型" prop="provider_type">
          <el-select v-model="ssoForm.provider_type" style="width: 100%">
            <el-option label="OAuth2" value="oauth2" />
            <el-option label="SAML" value="saml" />
            <el-option label="LDAP" value="ldap" />
          </el-select>
        </el-form-item>
        <el-form-item :label="ssoForm.provider_type === 'ldap' ? 'LDAP 服务地址 (server_url)' : 'Issuer / 服务地址'">
          <el-input v-model="ssoForm.issuer" :placeholder="ssoForm.provider_type === 'ldap' ? 'ldap://host:389' : 'https://sso.example.com'" />
        </el-form-item>
        <el-form-item label="Client ID"><el-input v-model="ssoForm.client_id" /></el-form-item>
        <el-form-item label="Client Secret / Bind Password"><el-input v-model="ssoForm.client_secret" type="password" show-password /></el-form-item>
        <el-form-item label="启用"><el-switch v-model="ssoForm.enabled" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="ssoDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="ssoSubmitting" @click="handleSubmitSso">保存</el-button>
      </template>
    </el-dialog>

    <!-- LDAP 登录测试 -->
    <el-dialog v-model="ldapDialogVisible" title="LDAP 登录测试" width="460px">
      <el-alert v-if="!ldapConfigs.length" type="warning" :closable="false" show-icon class="mb-16">
        暂无 LDAP 类型配置, 请先在「SSO 配置」中创建 provider_type=ldap 的配置。
      </el-alert>
      <el-form label-position="top" v-loading="ldapSubmitting">
        <el-form-item label="选择 LDAP 配置" required>
          <el-select v-model="ldapForm.config_id" style="width: 100%" placeholder="选择 LDAP 配置">
            <el-option v-for="c in ldapConfigs" :key="c.id" :label="`#${c.id} ${c.provider_name}`" :value="c.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="用户 DN / 用户名"><el-input v-model="ldapForm.username" placeholder="uid=test,ou=users,dc=example,dc=com" /></el-form-item>
        <el-form-item label="密码"><el-input v-model="ldapForm.password" type="password" show-password /></el-form-item>
      </el-form>
      <div v-if="ldapResult" class="ldap-result">
        <el-divider content-position="left">结果</el-divider>
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="状态"><el-tag :type="ldapResult.success ? 'success' : 'danger'">{{ ldapResult.success ? '登录成功' : '登录失败' }}</el-tag></el-descriptions-item>
          <el-descriptions-item label="消息">{{ ldapResult.message || '—' }}</el-descriptions-item>
        </el-descriptions>
      </div>
      <template #footer>
        <el-button @click="ldapDialogVisible = false">关闭</el-button>
        <el-button type="primary" :loading="ldapSubmitting" :disabled="!ldapForm.config_id" @click="handleLdapTest">测试登录</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { sensitiveWordApi, ssoApi } from '@/api/client'

const activeTab = ref('sensitive')

// ====== 敏感词 ======
const swLoading = ref(false)
const words = ref([])
const swTotal = ref(0)
const swPage = ref(1)
const swPageSize = ref(20)
const swFilter = reactive({ keyword: '', status: '' })
const reviewingId = ref(null)

async function loadWords() {
  swLoading.value = true
  try {
    const params = { page: swPage.value, page_size: swPageSize.value }
    if (swFilter.keyword) params.keyword = swFilter.keyword
    if (swFilter.status) params.status = swFilter.status
    const data = await sensitiveWordApi.list(params)
    words.value = data.items || []
    swTotal.value = data.total || 0
  } catch (err) {
    ElMessage.error('加载敏感词失败: ' + (err.message || ''))
  } finally {
    swLoading.value = false
  }
}

const wordDialogVisible = ref(false)
const wordSubmitting = ref(false)
const wordFormRef = ref(null)
const wordForm = reactive({ term: '', category: '' })
const wordRules = { term: [{ required: true, message: '请输入敏感词', trigger: 'blur' }] }

function openWordDialog() {
  wordForm.term = ''
  wordForm.category = ''
  wordDialogVisible.value = true
}

async function handleSubmitWord() {
  if (!wordFormRef.value) return
  try { await wordFormRef.value.validate() } catch { return }
  wordSubmitting.value = true
  try {
    await sensitiveWordApi.create({ term: wordForm.term, category: wordForm.category })
    ElMessage.success('新增成功')
    wordDialogVisible.value = false
    await loadWords()
  } catch (err) {
    ElMessage.error('新增失败: ' + (err.message || ''))
  } finally {
    wordSubmitting.value = false
  }
}

async function handleReview(row, action) {
  reviewingId.value = row.id
  try {
    await sensitiveWordApi.review(row.id, { action })
    ElMessage.success(action === 'approved' ? '已通过' : '已拒绝')
    await loadWords()
  } catch (err) {
    ElMessage.error('审核失败: ' + (err.message || ''))
  } finally {
    reviewingId.value = null
  }
}

async function handleDeleteWord(row) {
  try { await ElMessageBox.confirm(`确认删除敏感词 "${row.term}"?`, '删除确认', { type: 'warning' }) } catch { return }
  try {
    await sensitiveWordApi.delete(row.id)
    ElMessage.success('删除成功')
    await loadWords()
  } catch (err) {
    ElMessage.error('删除失败: ' + (err.message || ''))
  }
}

// 批量导入
const importDialogVisible = ref(false)
const importText = ref('')
const importSubmitting = ref(false)

async function handleImport() {
  const terms = importText.value.split('\n').map((s) => s.trim()).filter((s) => s && !s.startsWith('#'))
  if (!terms.length) { ElMessage.warning('未检测到有效敏感词'); return }
  importSubmitting.value = true
  try {
    const data = await sensitiveWordApi.batchImport({ terms })
    ElMessage.success(`导入完成: ${data.imported ?? terms.length} 条`)
    importDialogVisible.value = false
    importText.value = ''
    await loadWords()
  } catch (err) {
    ElMessage.error('导入失败: ' + (err.message || ''))
  } finally {
    importSubmitting.value = false
  }
}

// ====== SSO ======
const ssoLoading = ref(false)
const ssoConfigs = ref([])

async function loadSso() {
  ssoLoading.value = true
  try {
    const data = await ssoApi.listConfigs()
    ssoConfigs.value = data.items || []
  } catch (err) {
    ElMessage.error('加载 SSO 配置失败: ' + (err.message || ''))
  } finally {
    ssoLoading.value = false
  }
}

function issuerOf(row) {
  const cfg = row.config || {}
  return cfg.server_url || cfg.issuer || cfg.entity_id || '—'
}

const ldapConfigs = computed(() => ssoConfigs.value.filter((c) => c.provider_type === 'ldap'))

const ssoDialogVisible = ref(false)
const ssoSubmitting = ref(false)
const ssoIsEdit = ref(false)
const ssoFormRef = ref(null)
const ssoForm = reactive({ id: null, provider_name: '', provider_type: 'oauth2', issuer: '', client_id: '', client_secret: '', enabled: true })
const ssoRules = {
  provider_name: [{ required: true, message: '请输入提供商名称', trigger: 'blur' }],
  provider_type: [{ required: true, message: '请选择协议类型', trigger: 'change' }],
}

function openSsoDialog(row = null) {
  ssoIsEdit.value = !!row
  if (row) {
    const cfg = row.config || {}
    Object.assign(ssoForm, {
      id: row.id,
      provider_name: row.provider_name || '',
      provider_type: row.provider_type || 'oauth2',
      issuer: cfg.server_url || cfg.issuer || cfg.entity_id || '',
      client_id: cfg.client_id || '',
      client_secret: '',
      enabled: !!row.enabled,
    })
  } else {
    resetSsoForm()
  }
  ssoDialogVisible.value = true
}

function resetSsoForm() {
  Object.assign(ssoForm, { id: null, provider_name: '', provider_type: 'oauth2', issuer: '', client_id: '', client_secret: '', enabled: true })
  ssoFormRef.value?.clearValidate?.()
}

function buildSsoConfig() {
  const cfg = {}
  const isLdap = ssoForm.provider_type === 'ldap'
  if (ssoForm.client_id) cfg.client_id = ssoForm.client_id
  if (ssoForm.client_secret) cfg.client_secret = ssoForm.client_secret
  if (ssoForm.issuer) {
    if (isLdap) cfg.server_url = ssoForm.issuer
    else cfg.issuer = ssoForm.issuer
  }
  return cfg
}

async function handleSubmitSso() {
  if (!ssoFormRef.value) return
  try { await ssoFormRef.value.validate() } catch { return }
  ssoSubmitting.value = true
  const payload = {
    provider_name: ssoForm.provider_name,
    provider_type: ssoForm.provider_type,
    config: buildSsoConfig(),
    enabled: ssoForm.enabled,
  }
  try {
    if (ssoIsEdit.value) {
      await ssoApi.updateConfig(ssoForm.id, payload)
      ElMessage.success('更新成功')
    } else {
      await ssoApi.createConfig(payload)
      ElMessage.success('创建成功')
    }
    ssoDialogVisible.value = false
    await loadSso()
  } catch (err) {
    ElMessage.error('保存失败: ' + (err.message || ''))
  } finally {
    ssoSubmitting.value = false
  }
}

async function handleDeleteSso(row) {
  try { await ElMessageBox.confirm(`确认删除 SSO 配置 "${row.provider_name}"?`, '删除确认', { type: 'warning' }) } catch { return }
  try {
    await ssoApi.deleteConfig(row.id)
    ElMessage.success('删除成功')
    await loadSso()
  } catch (err) {
    ElMessage.error('删除失败: ' + (err.message || ''))
  }
}

// LDAP 测试
const ldapDialogVisible = ref(false)
const ldapSubmitting = ref(false)
const ldapResult = ref(null)
const ldapForm = reactive({ config_id: null, username: '', password: '' })

function openLdapDialog() {
  ldapForm.config_id = null
  ldapForm.username = ''
  ldapForm.password = ''
  ldapResult.value = null
  ldapDialogVisible.value = true
}

async function handleLdapTest() {
  if (!ldapForm.config_id) { ElMessage.warning('请先选择 LDAP 配置'); return }
  if (!ldapForm.username || !ldapForm.password) { ElMessage.warning('请填写用户名和密码'); return }
  ldapSubmitting.value = true
  try {
    const res = await ssoApi.ldapLogin(ldapForm.config_id, { username: ldapForm.username, password: ldapForm.password })
    const who = res?.user ? (res.user.name || res.user.user_id || '') : ''
    ldapResult.value = { success: true, message: who ? `登录成功, 欢迎 ${who}` : '登录成功' }
  } catch (err) {
    ldapResult.value = { success: false, message: err.message || '测试失败' }
  } finally {
    ldapSubmitting.value = false
  }
}

function reviewTagType(v) { return { pending: 'warning', approved: 'success', rejected: 'danger' }[v] || 'info' }
function reviewLabel(v) { return { pending: '待审核', approved: '已通过', rejected: '已拒绝' }[v] || v || '—' }
function formatTime(iso) { if (!iso) return '—'; try { return new Date(iso).toLocaleString('zh-CN', { hour12: false }) } catch { return iso } }

function onTabChange(name) {
  if (name === 'sso' && ssoConfigs.value.length === 0) loadSso()
}

onMounted(() => { loadWords() })
</script>

<style scoped>
.mb-16 { margin-bottom: 16px; }
.page-header { display: flex; justify-content: space-between; align-items: center; }
.page-title { display: inline-flex; align-items: center; gap: 8px; font-size: 18px; font-weight: 600; color: var(--el-text-color-primary); }
.toolbar { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
.pagination-wrapper { margin-top: 16px; display: flex; justify-content: flex-end; }
.ldap-result { margin-top: 8px; }
</style>
