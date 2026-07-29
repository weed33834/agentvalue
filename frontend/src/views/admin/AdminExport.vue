<template>
  <div class="admin-export">
    <!-- 顶部说明 -->
    <el-alert type="info" :closable="false" show-icon class="mb-16">
      <template #title>
        数据导出 —— 支持评估数据、审计日志、分析数据、通知数据四个模块的离线导出。
        每个模块可选择导出格式 (CSV / Excel / JSON) 与日期范围，导出文件将自动下载。
        该模块仅 admin / hr 角色可访问。
      </template>
    </el-alert>

    <!-- 顶部工具栏 -->
    <div class="toolbar mb-16">
      <span class="section-title">
        <el-icon><Download /></el-icon>
        数据导出
      </span>
    </div>

    <!-- 导出模块卡片网格 -->
    <div class="export-grid">
      <el-card
        v-for="mod in modules"
        :key="mod.key"
        class="export-card"
        shadow="hover"
      >
        <template #header>
          <div class="card-header">
            <span class="card-title">
              <el-icon :class="['card-icon', `card-icon--${mod.key}`]">
                <component :is="mod.icon" />
              </el-icon>
              {{ mod.label }}
            </span>
          </div>
        </template>

        <p class="card-desc">{{ mod.desc }}</p>

        <el-form label-position="top" class="export-form">
          <el-form-item label="导出格式">
            <el-radio-group v-model="state[mod.key].format">
              <el-radio-button label="csv">CSV</el-radio-button>
              <el-radio-button label="excel">Excel</el-radio-button>
              <el-radio-button label="json">JSON</el-radio-button>
            </el-radio-group>
          </el-form-item>

          <el-form-item label="日期范围 (可选)">
            <el-date-picker
              v-model="state[mod.key].dateRange"
              type="daterange"
              range-separator="至"
              start-placeholder="开始日期"
              end-placeholder="结束日期"
              format="YYYY-MM-DD"
              value-format="YYYY-MM-DD"
              style="width: 100%"
              :clearable="true"
            />
            <span class="field-hint">不选择日期范围则导出全部数据。</span>
          </el-form-item>
        </el-form>

        <div class="card-footer">
          <el-button
            type="primary"
            :loading="state[mod.key].loading"
            @click="handleExport(mod)"
          >
            <el-icon><Download /></el-icon>
            导出 {{ formatLabel(state[mod.key].format) }}
          </el-button>
        </div>
      </el-card>
    </div>

    <!-- 导出历史提示 (空状态) -->
    <el-card v-if="!exporting" class="tip-card mt-16" shadow="never">
      <el-empty description="选择上方任意模块并点击导出按钮，文件将自动下载至浏览器默认下载目录" :image-size="80" />
    </el-card>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { exportApi } from '@/api/client'

// ============================================================
// 模块配置
// ============================================================
const modules = [
  {
    key: 'evaluations',
    label: '评估数据',
    icon: 'Document',
    desc: '导出全部评估记录，含评分、状态、维度明细等字段。',
    apiFn: (params) => exportApi.evaluations(params),
    filename: 'evaluations',
  },
  {
    key: 'auditLogs',
    label: '审计日志',
    icon: 'List',
    desc: '导出系统审计日志，含操作人、动作、时间、详情等字段。',
    apiFn: (params) => exportApi.auditLogs(params),
    filename: 'audit-logs',
  },
  {
    key: 'analytics',
    label: '分析数据',
    icon: 'DataLine',
    desc: '导出 Token 用量、成本统计、Provider 分布等分析数据。',
    apiFn: (params) => exportApi.analytics(params),
    filename: 'analytics',
  },
  {
    key: 'notifications',
    label: '通知数据',
    icon: 'Bell',
    desc: '导出通知历史记录，含类型、渠道、已读状态、时间等字段。',
    apiFn: (params) => exportApi.notifications(params),
    filename: 'notifications',
  },
]

// 每个模块独立的表单状态
const state = reactive({
  evaluations: { format: 'csv', dateRange: null, loading: false },
  auditLogs: { format: 'csv', dateRange: null, loading: false },
  analytics: { format: 'csv', dateRange: null, loading: false },
  notifications: { format: 'csv', dateRange: null, loading: false },
})

// 是否有任一模块正在导出 (用于空状态展示控制)
const exporting = ref(false)

// 格式扩展名与 MIME 映射
const FORMAT_EXT = { csv: 'csv', excel: 'xlsx', json: 'json' }
const FORMAT_MIME = {
  csv: 'text/csv',
  excel: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  json: 'application/json',
}

function formatLabel(fmt) {
  const map = { csv: 'CSV', excel: 'Excel', json: 'JSON' }
  return map[fmt] || fmt
}

// ============================================================
// 导出处理
// ============================================================
async function handleExport(mod) {
  const formState = state[mod.key]
  if (formState.loading) return

  formState.loading = true
  exporting.value = true
  try {
    const params = {
      format: formState.format,
    }
    if (formState.dateRange && formState.dateRange.length === 2) {
      params.start_date = formState.dateRange[0]
      params.end_date = formState.dateRange[1]
    }

    const response = await mod.apiFn(params)

    // exportApi 对 json 格式返回解析后的对象, 对 csv/excel 返回 Blob
    // 统一转换为 Blob 触发下载
    let blob
    if (response instanceof Blob) {
      blob = response
    } else {
      // JSON 对象: 序列化为字符串后再封装
      const jsonStr =
        typeof response === 'string' ? response : JSON.stringify(response, null, 2)
      blob = new Blob([jsonStr], { type: FORMAT_MIME.json })
    }

    const ext = FORMAT_EXT[formState.format] || 'txt'
    const filename = `${mod.filename}-${buildDateStamp()}.${ext}`
    triggerDownload(blob, filename)

    ElMessage.success(`${mod.label}导出成功`)
  } catch (err) {
    console.error(`${mod.label}导出失败:`, err)
    ElMessage.error(err.message || `${mod.label}导出失败`)
  } finally {
    formState.loading = false
    exporting.value = false
  }
}

// ============================================================
// 文件下载工具
// ============================================================
function triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  // 释放对象 URL, 避免内存泄漏
  URL.revokeObjectURL(url)
}

function buildDateStamp() {
  const now = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  return `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}${pad(
    now.getHours(),
  )}${pad(now.getMinutes())}`
}
</script>

<style scoped>
.admin-export {
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
.section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  font-size: 16px;
}
.export-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  gap: 16px;
}
.export-card {
  display: flex;
  flex-direction: column;
}
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  font-size: 15px;
  color: var(--el-text-color-primary);
}
.card-icon {
  font-size: 18px;
}
.card-icon--evaluations {
  color: var(--el-color-primary);
}
.card-icon--auditLogs {
  color: var(--el-color-warning);
}
.card-icon--analytics {
  color: var(--el-color-success);
}
.card-icon--notifications {
  color: var(--el-color-danger);
}
.card-desc {
  margin: 0 0 16px 0;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
  min-height: 42px;
}
.export-form {
  margin-bottom: 8px;
}
.field-hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  display: block;
  margin-top: 4px;
}
.card-footer {
  margin-top: auto;
  padding-top: 8px;
}
.tip-card {
  border: none;
}
/* 响应式: 小屏单列 */
@media (max-width: 768px) {
  .export-grid {
    grid-template-columns: 1fr;
  }
}
</style>
