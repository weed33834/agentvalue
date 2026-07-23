<script setup>
/**
 * ArtifactRenderer - 可交互产物渲染器 (对标 Claude Artifacts / ChatGPT Canvas)
 *
 * 根据 artifact.type 渲染不同预览:
 * - html: iframe (sandbox="allow-scripts") srcdoc
 * - svg:  v-html 直接渲染 SVG
 * - mermaid: 动态 import mermaid 渲染 (失败降级 <pre>)
 * - markdown: 复用 utils/markdown.js 渲染
 * - code/json/react: <pre><code> 显示 (react 无法沙箱执行, 降级为代码)
 *
 * 工具栏: 名称 / 类型标签 / 复制 / 全屏 / 编辑-预览切换
 */
import { ref, computed, watch, onMounted, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { renderMarkdown } from '@/utils/markdown'

const props = defineProps({
  artifact: { type: Object, required: true },
})

const emit = defineEmits(['update'])

// ---- 模式: 预览 / 编辑 ----
const mode = ref('preview')
const editContent = ref('')
const fullscreenVisible = ref(false)

// ---- 字段 ----
const artifactType = computed(() => props.artifact?.artifact_type || props.artifact?.type || 'code')
const content = computed(() => props.artifact?.content || '')
const name = computed(() => props.artifact?.name || 'Artifact')
const language = computed(() => props.artifact?.language || '')
const version = computed(() => props.artifact?.version || 1)

const typeLabel = computed(() => {
  const map = {
    html: 'HTML',
    svg: 'SVG',
    mermaid: 'Mermaid',
    markdown: 'Markdown',
    code: 'Code',
    react: 'React',
    json: 'JSON',
  }
  return map[artifactType.value] || artifactType.value
})

const typeColor = computed(() => {
  const map = {
    html: '#e34c26',
    svg: '#ffb13b',
    mermaid: '#ff3677',
    markdown: '#083fa1',
    code: '#701516',
    react: '#61dafb',
    json: '#cbcb41',
  }
  return map[artifactType.value] || '#909399'
})

// ---- 渲染 ----
const iframeSrcDoc = computed(() => content.value)
const renderedMarkdown = computed(() => renderMarkdown(content.value))

// ---- Mermaid 懒加载渲染 ----
const mermaidSvg = ref('')
const mermaidError = ref(false)
let _mermaidLoaded = false
let _mermaidPromise = null

function loadMermaid() {
  if (!_mermaidLoaded) {
    if (!_mermaidPromise) {
      _mermaidPromise = import('mermaid').then((mod) => {
        const mermaid = mod.default
        mermaid.initialize({
          startOnLoad: false,
          theme: 'neutral',
          securityLevel: 'loose',
        })
        _mermaidLoaded = true
        return mermaid
      })
    }
  }
  return _mermaidPromise
}

async function renderMermaidDiagram() {
  if (artifactType.value !== 'mermaid') return
  mermaidError.value = false
  if (!content.value) return
  try {
    const mermaid = await loadMermaid()
    const id = 'mmd-' + Math.random().toString(36).slice(2, 10)
    const result = await mermaid.render(id, content.value)
    mermaidSvg.value = result.svg || ''
  } catch (e) {
    console.warn('Mermaid 渲染失败:', e)
    mermaidError.value = true
    mermaidSvg.value = ''
  }
}

watch(
  [content, artifactType, mode],
  () => {
    if (mode.value === 'preview' && artifactType.value === 'mermaid') {
      renderMermaidDiagram()
    }
  },
  { immediate: false },
)

onMounted(() => {
  if (artifactType.value === 'mermaid') {
    renderMermaidDiagram()
  }
})

// ---- 复制 ----
const copied = ref(false)
async function copyContent() {
  try {
    await navigator.clipboard.writeText(content.value)
    copied.value = true
    ElMessage.success('已复制')
    setTimeout(() => (copied.value = false), 2000)
  } catch {
    ElMessage.warning('复制失败')
  }
}

// ---- 编辑 / 保存 ----
function startEdit() {
  editContent.value = content.value
  mode.value = 'edit'
}

function saveEdit() {
  emit('update', { ...props.artifact, content: editContent.value })
  mode.value = 'preview'
  ElMessage.success('已保存')
}

function cancelEdit() {
  mode.value = 'preview'
}

// ---- 全屏 ----
function openFullscreen() {
  fullscreenVisible.value = true
  if (artifactType.value === 'mermaid') {
    nextTick(() => renderMermaidDiagram())
  }
}
</script>

<template>
  <div class="artifact-card">
    <!-- 工具栏 -->
    <div class="artifact-toolbar">
      <div class="toolbar-left">
        <span class="type-tag" :style="{ background: typeColor }">{{ typeLabel }}</span>
        <span class="artifact-name" :title="name">{{ name }}</span>
        <span v-if="language" class="artifact-lang">{{ language }}</span>
        <span class="artifact-version">v{{ version }}</span>
      </div>
      <div class="toolbar-right">
        <el-button size="small" text @click="copyContent">
          <el-icon><CopyDocument /></el-icon>
          {{ copied ? '已复制' : '复制' }}
        </el-button>
        <el-button v-if="mode === 'preview'" size="small" text @click="startEdit">
          <el-icon><Edit /></el-icon>
          编辑
        </el-button>
        <template v-else>
          <el-button size="small" text type="primary" @click="saveEdit">
            <el-icon><Check /></el-icon>
            保存
          </el-button>
          <el-button size="small" text @click="cancelEdit">取消</el-button>
        </template>
        <el-button size="small" text @click="openFullscreen">
          <el-icon><FullScreen /></el-icon>
          全屏
        </el-button>
      </div>
    </div>

    <!-- 内容区 -->
    <div class="artifact-body">
      <!-- 编辑模式 -->
      <div v-if="mode === 'edit'" class="edit-area">
        <el-input
          v-model="editContent"
          type="textarea"
          :autosize="{ minRows: 6, maxRows: 24 }"
          placeholder="编辑产物内容..."
        />
      </div>

      <!-- 预览模式 -->
      <div v-else class="preview-area">
        <!-- HTML: iframe 沙箱 -->
        <iframe
          v-if="artifactType === 'html'"
          class="html-frame"
          sandbox="allow-scripts"
          :srcdoc="iframeSrcDoc"
        />

        <!-- SVG: 直接渲染 -->
        <div v-else-if="artifactType === 'svg'" class="svg-area" v-html="content" />

        <!-- Mermaid -->
        <div v-else-if="artifactType === 'mermaid'" class="mermaid-area">
          <div v-if="mermaidSvg" class="mermaid-svg" v-html="mermaidSvg" />
          <pre v-else-if="mermaidError" class="mermaid-fallback"><code>{{ content }}</code></pre>
          <div v-else class="mermaid-loading">渲染中...</div>
        </div>

        <!-- Markdown -->
        <div
          v-else-if="artifactType === 'markdown'"
          class="markdown-area markdown-body"
          v-html="renderedMarkdown"
        />

        <!-- Code / JSON / React: 代码显示 (react 无法沙箱执行, 降级) -->
        <pre v-else class="code-area"><code>{{ content }}</code></pre>
      </div>
    </div>

    <!-- 全屏对话框 -->
    <el-dialog
      v-model="fullscreenVisible"
      :title="name"
      fullscreen
      append-to-body
      class="artifact-fullscreen-dialog"
      destroy-on-close
    >
      <div class="fullscreen-body">
        <iframe
          v-if="artifactType === 'html'"
          class="html-frame full"
          sandbox="allow-scripts"
          :srcdoc="iframeSrcDoc"
        />
        <div v-else-if="artifactType === 'svg'" class="svg-area full" v-html="content" />
        <div v-else-if="artifactType === 'mermaid'" class="mermaid-area full">
          <div v-if="mermaidSvg" class="mermaid-svg" v-html="mermaidSvg" />
          <pre v-else class="mermaid-fallback"><code>{{ content }}</code></pre>
        </div>
        <div
          v-else-if="artifactType === 'markdown'"
          class="markdown-area markdown-body full"
          v-html="renderedMarkdown"
        />
        <pre v-else class="code-area full"><code>{{ content }}</code></pre>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.artifact-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  overflow: hidden;
  background: var(--el-bg-color);
}
.artifact-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 12px;
  background: var(--el-fill-color-light);
  border-bottom: 1px solid var(--el-border-color-lighter);
  gap: 8px;
}
.toolbar-left {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex: 1;
}
.type-tag {
  font-size: 10px;
  font-weight: 600;
  color: #fff;
  padding: 1px 6px;
  border-radius: 4px;
  text-transform: uppercase;
  flex-shrink: 0;
  letter-spacing: 0.5px;
}
.artifact-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--el-text-color-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.artifact-lang,
.artifact-version {
  font-size: 11px;
  color: var(--el-text-color-secondary);
  flex-shrink: 0;
}
.toolbar-right {
  display: flex;
  align-items: center;
  gap: 2px;
  flex-shrink: 0;
}
.toolbar-right :deep(.el-button) {
  padding: 2px 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.toolbar-right :deep(.el-button:hover) {
  color: var(--el-color-primary);
}
.artifact-body {
  max-height: 480px;
  overflow: auto;
}
.preview-area {
  padding: 12px;
}
.html-frame {
  width: 100%;
  min-height: 320px;
  border: none;
  border-radius: 6px;
  background: #fff;
}
.svg-area {
  display: flex;
  justify-content: center;
  align-items: center;
  padding: 12px;
}
.svg-area :deep(svg) {
  max-width: 100%;
  height: auto;
}
.mermaid-area {
  display: flex;
  justify-content: center;
  align-items: center;
  padding: 12px;
  min-height: 80px;
}
.mermaid-svg :deep(svg) {
  max-width: 100%;
  height: auto;
}
.mermaid-loading {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  padding: 24px;
}
.mermaid-fallback {
  background: var(--el-fill-color-darker);
  border-radius: 6px;
  padding: 12px;
  overflow-x: auto;
  width: 100%;
  margin: 0;
  font-family: 'Fira Code', 'Consolas', monospace;
  font-size: 12px;
}
.markdown-area {
  font-size: 13px;
  line-height: 1.6;
  color: var(--el-text-color-primary);
}
.markdown-area :deep(pre) {
  background: var(--el-fill-color-darker);
  border-radius: 6px;
  padding: 10px;
  overflow-x: auto;
}
.markdown-area :deep(code) {
  font-family: 'Fira Code', 'Consolas', monospace;
  font-size: 12px;
}
.markdown-area :deep(h1),
.markdown-area :deep(h2),
.markdown-area :deep(h3) {
  margin: 10px 0 6px;
  font-weight: 600;
}
.markdown-area :deep(p) {
  margin: 6px 0;
}
.markdown-area :deep(table) {
  border-collapse: collapse;
  margin: 6px 0;
}
.markdown-area :deep(th),
.markdown-area :deep(td) {
  border: 1px solid var(--el-border-color);
  padding: 4px 10px;
}
.code-area {
  background: var(--el-fill-color-darker);
  border-radius: 6px;
  padding: 12px;
  overflow-x: auto;
  font-family: 'Fira Code', 'Consolas', monospace;
  font-size: 13px;
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
}
.edit-area {
  padding: 12px;
}
.fullscreen-body {
  height: calc(100vh - 120px);
  overflow: auto;
  padding: 16px;
}
.html-frame.full,
.code-area.full,
.svg-area.full,
.mermaid-area.full {
  min-height: 60vh;
}
.artifact-fullscreen-dialog :deep(.el-dialog__body) {
  padding: 0 16px 16px;
}
</style>
