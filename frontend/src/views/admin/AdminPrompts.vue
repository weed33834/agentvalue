<template>
  <div class="admin-prompts">
    <el-alert type="info" :closable="false" show-icon class="mb-16">
      <template #title>
        Prompt 管理中心 —— 对标 Langfuse Prompt Management，支持版本不可变历史、 Label
        指针（production/staging/prod-a/prod-b/canary-Npct）、Diff 对比、一键回滚、 A/B
        测试与灰度发布。所有变更记入审计日志。
      </template>
    </el-alert>

    <!-- 顶部操作条 -->
    <div class="toolbar">
      <el-input
        v-model="search"
        placeholder="按模板名搜索"
        clearable
        class="search-input"
        @keyup.enter="loadTemplates(1)"
        @clear="loadTemplates(1)"
      >
        <template #prefix
          ><el-icon><Search /></el-icon
        ></template>
      </el-input>
      <el-button type="primary" @click="openCreateDialog">
        <el-icon><Plus /></el-icon>
        新建 Prompt 模板
      </el-button>
      <el-button @click="loadTemplates()">
        <el-icon><RefreshLeft /></el-icon>
        刷新
      </el-button>
    </div>

    <!-- 模板列表 -->
    <el-card v-loading="loading" :aria-busy="loading">
      <el-table :data="templates" stripe>
        <el-table-column prop="name" label="模板名" min-width="160">
          <template #default="{ row }">
            <el-link type="primary" @click="openDetail(row.name)">{{ row.name }}</el-link>
          </template>
        </el-table-column>
        <el-table-column prop="type" label="类型" width="80">
          <template #default="{ row }">
            <el-tag size="small" :type="row.type === 'chat' ? 'success' : 'info'">
              {{ row.type }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="description" label="描述" min-width="200" show-overflow-tooltip />
        <el-table-column prop="version_count" label="版本数" width="90" align="center" />
        <el-table-column prop="label_count" label="Label 数" width="90" align="center" />
        <el-table-column prop="updated_at" label="更新时间" width="180">
          <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="240" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="openDetail(row.name)">详情</el-button>
            <el-button size="small" type="warning" @click="openVersionDialog(row.name)">
              新版本
            </el-button>
            <el-button
              size="small"
              type="danger"
              :disabled="row.label_count > 0"
              @click="confirmDelete(row.name)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination-wrap">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next, jumper"
          @current-change="loadTemplates()"
          @size-change="loadTemplates(1)"
        />
      </div>
    </el-card>

    <!-- 新建模板对话框 -->
    <el-dialog
      v-model="createDialogVisible"
      title="新建 Prompt 模板"
      width="720px"
      :close-on-click-modal="false"
    >
      <el-form ref="createFormRef" :model="createForm" label-position="top" :rules="createRules">
        <el-form-item label="模板名 (同租户唯一)" prop="name">
          <el-input v-model="createForm.name" placeholder="如 daily_evaluation / weekly_summary" />
        </el-form-item>
        <el-form-item label="类型">
          <el-radio-group v-model="createForm.type">
            <el-radio value="text">text 纯文本</el-radio>
            <el-radio value="chat">chat 对话消息</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="createForm.description"
            type="textarea"
            :rows="2"
            placeholder="模板用途说明,便于团队理解"
          />
        </el-form-item>
        <el-form-item label="Prompt 正文 (支持 {{ var }} 与 {{ var }} 模板变量)" prop="content">
          <el-input
            v-model="createForm.content"
            type="textarea"
            :rows="10"
            placeholder="你是员工评估助手...员工 ID: {{ employee_id }}\n周期: {{ period }}"
          />
        </el-form-item>
        <el-form-item label="模型配置 (JSON,可选)">
          <el-input
            v-model="configText"
            type="textarea"
            :rows="4"
            placeholder='{"model": "gpt-4o-mini", "temperature": 0.1, "max_tokens": 4096}'
          />
        </el-form-item>
        <el-form-item label="初始 Label (逗号分隔)">
          <el-input v-model="labelsText" placeholder="production,latest" />
          <span class="field-hint">默认 latest 自动维护,production 受保护</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>

    <!-- 新版本对话框 -->
    <el-dialog
      v-model="versionDialogVisible"
      :title="`为 ${versionForm.name} 新建版本`"
      width="720px"
      :close-on-click-modal="false"
    >
      <el-form ref="versionFormRef" :model="versionForm" label-position="top">
        <el-form-item label="新版本内容" prop="content">
          <el-input
            v-model="versionForm.content"
            type="textarea"
            :rows="12"
            placeholder="粘贴或编辑新版本 Prompt 正文"
          />
        </el-form-item>
        <el-form-item label="模型配置 (JSON,可选)">
          <el-input
            v-model="versionForm.configText"
            type="textarea"
            :rows="4"
            placeholder='{"model": "gpt-4o-mini", "temperature": 0.1}'
          />
        </el-form-item>
        <el-form-item label="分配 Label (逗号分隔)">
          <el-input v-model="versionForm.labelsText" placeholder="staging,latest" />
          <span class="field-hint">
            覆盖同名旧 label。如需上线,用 production。 A/B 用 prod-a/prod-b,灰度用 canary-Npct。
          </span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="versionDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="creatingVersion" @click="submitCreateVersion">
          创建版本
        </el-button>
      </template>
    </el-dialog>

    <!-- 模板详情抽屉 -->
    <el-drawer
      v-model="detailVisible"
      :title="`Prompt 模板: ${detail?.template?.name || ''}`"
      direction="rtl"
      size="60%"
    >
      <div v-if="detail" v-loading="detailLoading" class="detail-content">
        <!-- 模板元信息 -->
        <el-descriptions :column="2" border size="small" class="mb-16">
          <el-descriptions-item label="模板名">{{ detail.template.name }}</el-descriptions-item>
          <el-descriptions-item label="类型">{{ detail.template.type }}</el-descriptions-item>
          <el-descriptions-item label="描述" :span="2">
            {{ detail.template.description || '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="版本数">{{
            detail.template.version_count
          }}</el-descriptions-item>
          <el-descriptions-item label="Label 数">{{
            detail.template.label_count
          }}</el-descriptions-item>
          <el-descriptions-item label="创建时间">
            {{ formatTime(detail.template.created_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="更新时间">
            {{ formatTime(detail.template.updated_at) }}
          </el-descriptions-item>
        </el-descriptions>

        <!-- Label 指针列表 -->
        <el-card class="section-card">
          <template #header>
            <div class="card-header-row">
              <span class="section-title">
                <el-icon><Collection /></el-icon>
                Label 指针
              </span>
              <el-button size="small" type="primary" @click="openAssignLabelDialog">
                <el-icon><Plus /></el-icon>
                分配 Label
              </el-button>
            </div>
          </template>
          <el-table :data="detail.labels" size="small" stripe>
            <el-table-column prop="label" label="Label" min-width="140">
              <template #default="{ row }">
                <el-tag :type="labelTagType(row.label)" size="small">{{ row.label }}</el-tag>
                <el-icon v-if="row.protected" class="protected-icon"><Lock /></el-icon>
              </template>
            </el-table-column>
            <el-table-column prop="version" label="指向版本" width="100">
              <template #default="{ row }">v{{ row.version }}</template>
            </el-table-column>
            <el-table-column
              prop="updated_by"
              label="更新人"
              min-width="120"
              show-overflow-tooltip
            />
            <el-table-column prop="updated_at" label="更新时间" width="160">
              <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="100">
              <template #default="{ row }">
                <el-button
                  size="small"
                  type="danger"
                  :icon="Delete"
                  :disabled="row.label === 'latest'"
                  @click="confirmRemoveLabel(row.label)"
                />
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <!-- 版本历史 -->
        <el-card class="section-card">
          <template #header>
            <div class="card-header-row">
              <span class="section-title">
                <el-icon><Clock /></el-icon>
                版本历史
              </span>
              <div class="header-actions">
                <el-button size="small" @click="openDiffDialog">Diff 对比</el-button>
                <el-button size="small" type="warning" @click="openRollbackDialog">
                  一键回滚
                </el-button>
                <el-button size="small" type="success" @click="openAbTestDialog">
                  A/B 测试
                </el-button>
                <el-button size="small" @click="openCanaryDialog">灰度发布</el-button>
              </div>
            </div>
          </template>
          <el-table :data="detail.versions" size="small" stripe>
            <el-table-column prop="version" label="版本" width="80">
              <template #default="{ row }">v{{ row.version }}</template>
            </el-table-column>
            <el-table-column label="Label" min-width="160">
              <template #default="{ row }">
                <el-tag
                  v-for="lb in row.labels"
                  :key="lb"
                  :type="labelTagType(lb)"
                  size="small"
                  class="label-chip"
                >
                  {{ lb }}
                </el-tag>
                <span v-if="!row.labels?.length" class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column
              prop="content_preview"
              label="内容预览"
              min-width="280"
              show-overflow-tooltip
            />
            <el-table-column prop="created_at" label="创建时间" width="160">
              <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="200" fixed="right">
              <template #default="{ row }">
                <el-button size="small" link @click="viewVersionContent(row)">查看</el-button>
                <el-button size="small" link @click="openPreviewDialog(row.version)"
                  >预览</el-button
                >
                <el-button size="small" link type="primary" @click="assignProduction(row.version)">
                  上线
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </div>
    </el-drawer>

    <!-- 版本内容查看对话框 -->
    <el-dialog
      v-model="contentDialogVisible"
      :title="`版本 v${currentVersion?.version} 内容`"
      width="800px"
    >
      <pre v-if="currentVersion" class="content-pre">{{ currentVersion.content }}</pre>
      <template #footer>
        <el-button @click="contentDialogVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- Diff 对比对话框 -->
    <el-dialog v-model="diffDialogVisible" title="版本 Diff 对比" width="800px">
      <el-form :inline="true" class="diff-form">
        <el-form-item label="起始版本">
          <el-select v-model="diffFrom" placeholder="v1" style="width: 120px">
            <el-option
              v-for="v in detail?.versions || []"
              :key="v.version"
              :label="`v${v.version}`"
              :value="v.version"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="目标版本">
          <el-select v-model="diffTo" placeholder="v2" style="width: 120px">
            <el-option
              v-for="v in detail?.versions || []"
              :key="v.version"
              :label="`v${v.version}`"
              :value="v.version"
            />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="diffing" @click="loadDiff">对比</el-button>
        </el-form-item>
      </el-form>
      <div v-if="diffResult" class="diff-result">
        <el-alert
          v-if="!diffResult.has_content_change && !diffResult.has_config_change"
          type="success"
          :closable="false"
          show-icon
        >
          两版本内容与配置完全一致,无差异。
        </el-alert>
        <template v-else>
          <div v-if="diffResult.has_content_change" class="diff-section">
            <div class="diff-section-title">内容差异</div>
            <pre class="diff-pre">{{ diffResult.diff || '(无文本差异)' }}</pre>
          </div>
          <div v-if="diffResult.has_config_change" class="diff-section">
            <div class="diff-section-title">配置差异</div>
            <pre class="diff-pre">{{ diffResult.config_diff || '(无配置差异)' }}</pre>
          </div>
        </template>
      </div>
      <template #footer>
        <el-button @click="diffDialogVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 回滚对话框 -->
    <el-dialog v-model="rollbackDialogVisible" title="一键回滚 production" width="480px">
      <el-alert type="warning" :closable="false" show-icon class="mb-16">
        回滚会把 production label 指向选定版本,线上请求立即生效。 版本本身不会被删除,可随时再切回。
      </el-alert>
      <el-form label-position="top">
        <el-form-item label="回滚到版本">
          <el-select v-model="rollbackTo" placeholder="选择目标版本" style="width: 100%">
            <el-option
              v-for="v in detail?.versions || []"
              :key="v.version"
              :label="`v${v.version}`"
              :value="v.version"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="rollbackDialogVisible = false">取消</el-button>
        <el-button type="warning" :loading="rollingBack" @click="submitRollback">
          确认回滚
        </el-button>
      </template>
    </el-dialog>

    <!-- A/B 测试对话框 -->
    <el-dialog v-model="abTestDialogVisible" title="配置 A/B 测试" width="520px">
      <el-alert type="info" :closable="false" show-icon class="mb-16">
        DbPromptLoader 按 hash(employee_id) % 100 分流,同一员工稳定走同一版本。 prod-a / prod-b
        label 分别指向两个版本。
      </el-alert>
      <el-form label-position="top">
        <el-form-item label="prod-a 指向版本">
          <el-select v-model="abTestForm.version_a" style="width: 100%">
            <el-option
              v-for="v in detail?.versions || []"
              :key="v.version"
              :label="`v${v.version}`"
              :value="v.version"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="prod-b 指向版本">
          <el-select v-model="abTestForm.version_b" style="width: 100%">
            <el-option
              v-for="v in detail?.versions || []"
              :key="v.version"
              :label="`v${v.version}`"
              :value="v.version"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="流量分配 (prod-a 百分比)">
          <el-slider v-model="abTestForm.traffic_split" :min="1" :max="99" show-input />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="abTestDialogVisible = false">取消</el-button>
        <el-button type="success" :loading="settingAb" @click="submitAbTest">配置</el-button>
      </template>
    </el-dialog>

    <!-- 灰度发布对话框 -->
    <el-dialog v-model="canaryDialogVisible" title="配置灰度发布" width="520px">
      <el-alert type="info" :closable="false" show-icon class="mb-16">
        创建 canary-Npct label 指向新版本。 hash(employee_id) % 100 &lt; N 走灰度版本,否则走
        production。 逐步扩大:5pct → 25pct → 50pct → 全量(把 production 指向新版本)。
      </el-alert>
      <el-form label-position="top">
        <el-form-item label="灰度版本">
          <el-select v-model="canaryForm.version" style="width: 100%">
            <el-option
              v-for="v in detail?.versions || []"
              :key="v.version"
              :label="`v${v.version}`"
              :value="v.version"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="灰度百分比">
          <el-slider
            v-model="canaryForm.percentage"
            :min="1"
            :max="100"
            :marks="canaryMarks"
            show-input
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="canaryDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="settingCanary" @click="submitCanary">配置</el-button>
      </template>
    </el-dialog>

    <!-- 分配 Label 对话框 -->
    <el-dialog v-model="assignLabelDialogVisible" title="分配 Label 指针" width="480px">
      <el-form label-position="top">
        <el-form-item label="目标版本">
          <el-select v-model="assignLabelForm.version" style="width: 100%">
            <el-option
              v-for="v in detail?.versions || []"
              :key="v.version"
              :label="`v${v.version}`"
              :value="v.version"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="Label 名">
          <el-input
            v-model="assignLabelForm.label"
            placeholder="production / staging / prod-a / canary-Npct"
          />
          <span class="field-hint">latest 由系统维护,不可手动指定</span>
        </el-form-item>
        <el-form-item label="是否受保护">
          <el-switch v-model="assignLabelForm.protected" />
          <span class="field-hint">production 默认受保护</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="assignLabelDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="assigningLabel" @click="submitAssignLabel">
          分配
        </el-button>
      </template>
    </el-dialog>

    <!-- 渲染预览对话框 -->
    <el-dialog v-model="previewDialogVisible" :title="`渲染预览 v${previewVersion}`" width="800px">
      <el-form label-position="top">
        <el-form-item label="变量 JSON (可选)">
          <el-input
            v-model="previewVariablesText"
            type="textarea"
            :rows="4"
            placeholder='{"employee_id": "u001", "period": "2025-W01", "raw_inputs": []}'
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="previewing" @click="submitPreview">渲染</el-button>
        </el-form-item>
      </el-form>
      <div v-if="previewResult" class="preview-result">
        <div class="diff-section-title">渲染结果</div>
        <pre class="content-pre">{{ previewResult.rendered }}</pre>
        <div class="field-hint">
          使用变量: {{ previewResult.variables_used?.join(', ') || '无' }}
        </div>
      </div>
      <template #footer>
        <el-button @click="previewDialogVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete } from '@element-plus/icons-vue'
import { promptAdminApi } from '@/api/client'

// ====== 列表状态 ======
const loading = ref(false)
const templates = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const search = ref('')

async function loadTemplates(targetPage) {
  if (targetPage) page.value = targetPage
  loading.value = true
  try {
    const data = await promptAdminApi.listTemplates({
      page: page.value,
      page_size: pageSize.value,
      search: search.value || undefined,
    })
    templates.value = data.items || []
    total.value = data.total || 0
  } catch (err) {
    ElMessage.error('加载模板列表失败: ' + err.message)
  } finally {
    loading.value = false
  }
}

// ====== 创建模板 ======
const createDialogVisible = ref(false)
const creating = ref(false)
const createFormRef = ref(null)
const configText = ref('')
const labelsText = ref('production,latest')
const createForm = reactive({
  name: '',
  type: 'text',
  description: '',
  content: '',
})
const createRules = {
  name: [{ required: true, message: '请输入模板名', trigger: 'blur' }],
  content: [{ required: true, message: '请输入 Prompt 正文', trigger: 'blur' }],
}

function openCreateDialog() {
  createForm.name = ''
  createForm.type = 'text'
  createForm.description = ''
  createForm.content = ''
  configText.value = ''
  labelsText.value = 'production,latest'
  createDialogVisible.value = true
}

async function submitCreate() {
  if (!createFormRef.value) return
  await createFormRef.value.validate(async (valid) => {
    if (!valid) return
    let config = null
    if (configText.value.trim()) {
      try {
        config = JSON.parse(configText.value)
      } catch {
        ElMessage.error('模型配置 JSON 格式错误')
        return
      }
    }
    creating.value = true
    try {
      const payload = {
        name: createForm.name,
        type: createForm.type,
        description: createForm.description || null,
        content: createForm.content,
        config,
        labels: labelsText.value
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean),
        protected_labels: ['production'],
      }
      await promptAdminApi.createTemplate(payload)
      ElMessage.success('模板创建成功')
      createDialogVisible.value = false
      await loadTemplates()
    } catch (err) {
      ElMessage.error('创建失败: ' + err.message)
    } finally {
      creating.value = false
    }
  })
}

// ====== 删除模板 ======
async function confirmDelete(name) {
  try {
    await ElMessageBox.confirm(`确认删除模板 ${name}?此操作不可恢复。`, '删除确认', {
      confirmButtonText: '确认删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await promptAdminApi.deleteTemplate(name)
    ElMessage.success('已删除')
    await loadTemplates()
  } catch (err) {
    ElMessage.error('删除失败: ' + err.message)
  }
}

// ====== 模板详情 ======
const detailVisible = ref(false)
const detailLoading = ref(false)
const detail = ref(null)
const currentName = ref('')

async function openDetail(name) {
  currentName.value = name
  detailVisible.value = true
  await loadDetail()
}

async function loadDetail() {
  detailLoading.value = true
  try {
    detail.value = await promptAdminApi.getTemplate(currentName.value)
  } catch (err) {
    ElMessage.error('加载详情失败: ' + err.message)
  } finally {
    detailLoading.value = false
  }
}

// ====== 新建版本 ======
const versionDialogVisible = ref(false)
const creatingVersion = ref(false)
const versionForm = reactive({
  name: '',
  content: '',
  configText: '',
  labelsText: 'latest',
})

function openVersionDialog(name) {
  versionForm.name = name
  versionForm.content = ''
  versionForm.configText = ''
  versionForm.labelsText = 'latest'
  versionDialogVisible.value = true
}

async function submitCreateVersion() {
  if (!versionForm.content.trim()) {
    ElMessage.warning('请输入新版本内容')
    return
  }
  let config = null
  if (versionForm.configText.trim()) {
    try {
      config = JSON.parse(versionForm.configText)
    } catch {
      ElMessage.error('配置 JSON 格式错误')
      return
    }
  }
  creatingVersion.value = true
  try {
    await promptAdminApi.createVersion(versionForm.name, {
      content: versionForm.content,
      config,
      labels: versionForm.labelsText
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean),
    })
    ElMessage.success('新版本创建成功')
    versionDialogVisible.value = false
    await loadTemplates()
    if (detailVisible.value && currentName.value === versionForm.name) {
      await loadDetail()
    }
  } catch (err) {
    ElMessage.error('创建版本失败: ' + err.message)
  } finally {
    creatingVersion.value = false
  }
}

// ====== 版本内容查看 ======
const contentDialogVisible = ref(false)
const currentVersion = ref(null)

function viewVersionContent(version) {
  currentVersion.value = version
  contentDialogVisible.value = true
}

// ====== Diff 对比 ======
const diffDialogVisible = ref(false)
const diffFrom = ref(null)
const diffTo = ref(null)
const diffResult = ref(null)
const diffing = ref(false)

function openDiffDialog() {
  const versions = detail.value?.versions || []
  diffFrom.value = versions.length > 0 ? versions[versions.length - 1].version : null
  diffTo.value = versions.length > 0 ? versions[0].version : null
  diffResult.value = null
  diffDialogVisible.value = true
}

async function loadDiff() {
  if (!diffFrom.value || !diffTo.value) {
    ElMessage.warning('请选择两个版本')
    return
  }
  diffing.value = true
  try {
    diffResult.value = await promptAdminApi.diffVersions(
      currentName.value,
      diffFrom.value,
      diffTo.value,
    )
  } catch (err) {
    ElMessage.error('Diff 失败: ' + err.message)
  } finally {
    diffing.value = false
  }
}

// ====== 一键回滚 ======
const rollbackDialogVisible = ref(false)
const rollbackTo = ref(null)
const rollingBack = ref(false)

function openRollbackDialog() {
  rollbackTo.value = null
  rollbackDialogVisible.value = true
}

async function submitRollback() {
  if (!rollbackTo.value) {
    ElMessage.warning('请选择回滚目标版本')
    return
  }
  try {
    await ElMessageBox.confirm(
      `确认把 production 回滚到 v${rollbackTo.value}?线上请求立即生效。`,
      '回滚确认',
      { confirmButtonText: '确认回滚', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  rollingBack.value = true
  try {
    await promptAdminApi.rollback(currentName.value, rollbackTo.value)
    ElMessage.success(`已回滚到 v${rollbackTo.value}`)
    rollbackDialogVisible.value = false
    await loadDetail()
  } catch (err) {
    ElMessage.error('回滚失败: ' + err.message)
  } finally {
    rollingBack.value = false
  }
}

// ====== 直接把 production 指向某版本(快速上线) ======
async function assignProduction(version) {
  try {
    await ElMessageBox.confirm(
      `确认把 production label 指向 v${version}?线上请求立即生效。`,
      '上线确认',
      { confirmButtonText: '确认上线', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await promptAdminApi.assignLabel(currentName.value, {
      version,
      label: 'production',
      protected: true,
    })
    ElMessage.success(`production 已指向 v${version}`)
    await loadDetail()
  } catch (err) {
    ElMessage.error('上线失败: ' + err.message)
  }
}

// ====== A/B 测试 ======
const abTestDialogVisible = ref(false)
const settingAb = ref(false)
const abTestForm = reactive({
  version_a: null,
  version_b: null,
  traffic_split: 50,
})

function openAbTestDialog() {
  const versions = detail.value?.versions || []
  abTestForm.version_a = versions.length > 0 ? versions[0].version : null
  abTestForm.version_b = versions.length > 1 ? versions[1].version : null
  abTestForm.traffic_split = 50
  abTestDialogVisible.value = true
}

async function submitAbTest() {
  if (!abTestForm.version_a || !abTestForm.version_b) {
    ElMessage.warning('请选择两个版本')
    return
  }
  settingAb.value = true
  try {
    await promptAdminApi.setupAbTest(currentName.value, {
      version_a: abTestForm.version_a,
      version_b: abTestForm.version_b,
      traffic_split: abTestForm.traffic_split,
    })
    ElMessage.success('A/B 测试已配置')
    abTestDialogVisible.value = false
    await loadDetail()
  } catch (err) {
    ElMessage.error('配置失败: ' + err.message)
  } finally {
    settingAb.value = false
  }
}

// ====== 灰度发布 ======
const canaryDialogVisible = ref(false)
const settingCanary = ref(false)
const canaryForm = reactive({
  version: null,
  percentage: 5,
})
const canaryMarks = { 5: '5%', 25: '25%', 50: '50%', 100: '全量' }

function openCanaryDialog() {
  const versions = detail.value?.versions || []
  canaryForm.version = versions.length > 0 ? versions[0].version : null
  canaryForm.percentage = 5
  canaryDialogVisible.value = true
}

async function submitCanary() {
  if (!canaryForm.version) {
    ElMessage.warning('请选择灰度版本')
    return
  }
  settingCanary.value = true
  try {
    await promptAdminApi.setupCanary(currentName.value, {
      version: canaryForm.version,
      percentage: canaryForm.percentage,
    })
    ElMessage.success('灰度发布已配置')
    canaryDialogVisible.value = false
    await loadDetail()
  } catch (err) {
    ElMessage.error('配置失败: ' + err.message)
  } finally {
    settingCanary.value = false
  }
}

// ====== Label 管理 ======
const assignLabelDialogVisible = ref(false)
const assigningLabel = ref(false)
const assignLabelForm = reactive({
  version: null,
  label: '',
  protected: false,
})

function openAssignLabelDialog() {
  assignLabelForm.version = null
  assignLabelForm.label = ''
  assignLabelForm.protected = false
  assignLabelDialogVisible.value = true
}

async function submitAssignLabel() {
  if (!assignLabelForm.version || !assignLabelForm.label.trim()) {
    ElMessage.warning('请填写版本与 label')
    return
  }
  if (assignLabelForm.label.trim() === 'latest') {
    ElMessage.warning('latest 由系统自动维护,不可手动指定')
    return
  }
  assigningLabel.value = true
  try {
    await promptAdminApi.assignLabel(currentName.value, {
      version: assignLabelForm.version,
      label: assignLabelForm.label.trim(),
      protected: assignLabelForm.protected,
    })
    ElMessage.success('Label 已分配')
    assignLabelDialogVisible.value = false
    await loadDetail()
  } catch (err) {
    ElMessage.error('分配失败: ' + err.message)
  } finally {
    assigningLabel.value = false
  }
}

async function confirmRemoveLabel(label) {
  try {
    await ElMessageBox.confirm(
      `确认删除 label "${label}"?不影响版本本身,仅移除指针。`,
      '删除确认',
      { confirmButtonText: '确认删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await promptAdminApi.removeLabel(currentName.value, label)
    ElMessage.success('Label 已删除')
    await loadDetail()
  } catch (err) {
    ElMessage.error('删除失败: ' + err.message)
  }
}

// ====== 渲染预览 ======
const previewDialogVisible = ref(false)
const previewVersion = ref(null)
const previewVariablesText = ref('')
const previewResult = ref(null)
const previewing = ref(false)

function openPreviewDialog(version) {
  previewVersion.value = version
  previewVariablesText.value = ''
  previewResult.value = null
  previewDialogVisible.value = true
}

async function submitPreview() {
  let variables = {}
  if (previewVariablesText.value.trim()) {
    try {
      variables = JSON.parse(previewVariablesText.value)
    } catch {
      ElMessage.error('变量 JSON 格式错误')
      return
    }
  }
  previewing.value = true
  try {
    previewResult.value = await promptAdminApi.previewRender(currentName.value, {
      version: previewVersion.value,
      variables,
    })
  } catch (err) {
    ElMessage.error('预览失败: ' + err.message)
  } finally {
    previewing.value = false
  }
}

// ====== 工具函数 ======
function formatTime(iso) {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    return d.toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

function labelTagType(label) {
  if (label === 'production') return 'danger'
  if (label === 'latest') return 'info'
  if (label?.startsWith('prod-')) return 'success'
  if (label?.startsWith('canary-')) return 'warning'
  if (label === 'staging') return ''
  return 'info'
}

onMounted(loadTemplates)
</script>

<style scoped>
.mb-16 {
  margin-bottom: 16px;
}
.mt-16 {
  margin-top: 16px;
}
.toolbar {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
  align-items: center;
}
.search-input {
  max-width: 320px;
}
.pagination-wrap {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}
.section-card {
  margin-bottom: 16px;
}
.section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
}
.card-header-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.header-actions {
  display: flex;
  gap: 8px;
}
.field-hint {
  color: #909399;
  font-size: 12px;
  margin-left: 8px;
}
.label-chip {
  margin-right: 4px;
}
.protected-icon {
  margin-left: 4px;
  color: #f56c6c;
}
.muted {
  color: #909399;
}
.detail-content {
  padding: 0 16px 16px 0;
}
.content-pre {
  background-color: #f5f7fa;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  padding: 12px;
  font-family: ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 60vh;
  overflow: auto;
}
.diff-form {
  margin-bottom: 16px;
}
.diff-result {
  margin-top: 16px;
}
.diff-section {
  margin-bottom: 16px;
}
.diff-section-title {
  font-weight: 600;
  margin-bottom: 8px;
  color: #303133;
}
.diff-pre {
  background-color: #fafafa;
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
.preview-result {
  margin-top: 16px;
}
</style>
