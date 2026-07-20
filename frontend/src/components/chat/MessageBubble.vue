<script setup>
import { ref, computed, onMounted, onBeforeUnmount, nextTick, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { renderMarkdown, renderMermaid } from '@/utils/markdown'
import { voiceApi, artifactApi } from '@/api/client'
import { useChatStore } from '@/stores/chat'
import ToolCallCard from './ToolCallCard.vue'
import ArtifactRenderer from './ArtifactRenderer.vue'

const props = defineProps({
  message: { type: Object, required: true },
  isLast: { type: Boolean, default: false },
  isStreaming: { type: Boolean, default: false },
})

const emit = defineEmits([
  'regenerate',
  'edit',
  'retry',
  'feedback',
  'fork',
])

// ---- 编辑模式 ----
const isEditing = ref(false)
const editText = ref('')

function startEdit() {
  const textPart = props.message.parts?.find((p) => p.type === 'text')
  editText.value = textPart?.text || ''
  isEditing.value = true
}

function saveEdit() {
  if (!editText.value.trim()) return
  emit('edit', props.message, editText.value.trim())
  isEditing.value = false
}

function cancelEdit() {
  isEditing.value = false
}

// ---- 复制 ----
const copied = ref(false)
async function copyMessage() {
  const textPart = props.message.parts?.find((p) => p.type === 'text')
  const text = textPart?.text || ''
  try {
    await navigator.clipboard.writeText(text)
    copied.value = true
    setTimeout(() => (copied.value = false), 2000)
  } catch {
    ElMessage.warning('复制失败')
  }
}

// ---- 代码块复制（通过 DOM 事件委托） ----
function onContentClick(e) {
  const btn = e.target.closest('.code-copy-btn')
  if (!btn) return
  const pre = btn.closest('pre')
  const code = pre?.querySelector('code')
  if (code) {
    navigator.clipboard.writeText(code.textContent).then(() => {
      btn.textContent = '已复制'
      setTimeout(() => (btn.textContent = '复制'), 2000)
    })
  }
}

// ---- 渲染 ----
const renderedText = computed(() => {
  const textPart = props.message.parts?.find((p) => p.type === 'text')
  if (!textPart?.text) return ''
  return renderMarkdown(textPart.text)
})

const reasoningText = computed(() => {
  const parts = props.message.parts?.filter((p) => p.type === 'reasoning')
  if (!parts?.length) return ''
  return parts.map((p) => p.text).join('')
})

const toolParts = computed(() => {
  return props.message.parts?.filter((p) => p.type === 'tool') || []
})

const imageParts = computed(() => {
  return props.message.parts?.filter((p) => p.type === 'image') || []
})

const fileParts = computed(() => {
  return props.message.parts?.filter((p) => p.type === 'file') || []
})

const hasText = computed(() => {
  return props.message.parts?.some((p) => p.type === 'text' && p.text) || false
})

const showThinking = ref(false)

// ---- Token 用量 ----
const usage = computed(() => {
  return props.message.usage || null
})

const latency = computed(() => {
  if (!props.message.latency) return null
  const ms = props.message.latency
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
})

// ---- 反馈 ----
const feedback = computed(() => props.message.feedback || null)

function onFeedback(rating) {
  const newRating = feedback.value === rating ? null : rating
  emit('feedback', props.message, newRating)
}

// ---- 从此分叉 ----
function onFork() {
  // 仅本地消息（前缀 local-/pending-）无后端 ID，无法分叉
  const id = props.message.id
  if (!id || typeof id !== 'string' || id.startsWith('local-') || id.startsWith('pending-')) {
    return
  }
  emit('fork', props.message)
}

// ---- 功能2: 语音输出 (TTS) ----
// 优先使用浏览器 Web Speech API，不支持则降级调用后端 /voice/tts
const isSpeaking = ref(false)
let currentUtterance = null
let fallbackAudio = null

const ttsSupported = computed(
  () => typeof window !== 'undefined' && 'speechSynthesis' in window,
)

function getMessageText() {
  const textPart = props.message.parts?.find((p) => p.type === 'text')
  return textPart?.text || ''
}

function toggleSpeech() {
  if (isSpeaking.value) {
    stopSpeaking()
    return
  }
  const content = getMessageText()
  if (!content) {
    ElMessage.warning('没有可朗读的内容')
    return
  }
  if (ttsSupported.value) {
    speakWithBrowser(content)
  } else {
    speakWithBackend(content)
  }
}

function speakWithBrowser(content) {
  window.speechSynthesis.cancel()
  const utterance = new SpeechSynthesisUtterance(content)
  // 支持中文语音
  utterance.lang = 'zh-CN'
  utterance.onend = () => {
    isSpeaking.value = false
    currentUtterance = null
  }
  utterance.onerror = () => {
    isSpeaking.value = false
    currentUtterance = null
  }
  currentUtterance = utterance
  window.speechSynthesis.speak(utterance)
  isSpeaking.value = true
}

async function speakWithBackend(content) {
  try {
    const res = await voiceApi.tts(content)
    const audioUrl = res?.url || res?.audio_url
    if (audioUrl) {
      fallbackAudio = new Audio(audioUrl)
    } else if (res?.audio) {
      // base64 音频
      fallbackAudio = new Audio('data:audio/mp3;base64,' + res.audio)
    } else {
      ElMessage.warning('语音合成未返回音频')
      return
    }
    fallbackAudio.onended = () => {
      isSpeaking.value = false
      fallbackAudio = null
    }
    fallbackAudio.onerror = () => {
      isSpeaking.value = false
      fallbackAudio = null
    }
    await fallbackAudio.play()
    isSpeaking.value = true
  } catch (e) {
    console.error('TTS 失败:', e)
    ElMessage.error('语音合成失败')
    isSpeaking.value = false
  }
}

function stopSpeaking() {
  if (ttsSupported.value) {
    window.speechSynthesis.cancel()
  }
  if (fallbackAudio) {
    fallbackAudio.pause()
    fallbackAudio = null
  }
  isSpeaking.value = false
  currentUtterance = null
}

onBeforeUnmount(() => {
  // 组件卸载时停止朗读，避免页面切换后仍在播放
  if (isSpeaking.value) {
    stopSpeaking()
  }
})

// ---- Artifacts 可视化 (对标 Claude Artifacts / ChatGPT Canvas) ----
const chatStore = useChatStore()

// 从消息文本中提取代码块作为 artifacts (客户端正则提取, 无需后端)
const artifacts = ref([])
const CODE_FENCE_RE = /```([a-zA-Z0-9_+\-]*)\n([\s\S]*?)```/g

function detectArtifactType(lang, content) {
  const l = (lang || '').toLowerCase()
  const head = content.replace(/^\s+/, '').toLowerCase()
  if (l === 'mermaid') return { type: 'mermaid', language: 'mermaid' }
  if (l === 'markdown' || l === 'md') return { type: 'markdown', language: 'markdown' }
  if (l === 'json') return { type: 'json', language: 'json' }
  if (l === 'react' || l === 'jsx' || l === 'tsx') return { type: 'react', language: l }
  if (l === 'svg') return { type: 'svg', language: 'svg' }
  if (l === 'html') return { type: 'html', language: 'html' }
  if (head.startsWith('<svg') || content.slice(0, 200).includes('<svg'))
    return { type: 'svg', language: l || 'svg' }
  if (
    head.startsWith('<!doctype html') ||
    head.startsWith('<html') ||
    content.slice(0, 200).includes('<html') ||
    content.slice(0, 200).includes('<body')
  )
    return { type: 'html', language: l || 'html' }
  return { type: 'code', language: l || null }
}

function extractArtifactsFromText(text) {
  const items = []
  CODE_FENCE_RE.lastIndex = 0
  let m
  let idx = 0
  while ((m = CODE_FENCE_RE.exec(text)) !== null) {
    const lang = m[1] || ''
    const content = m[2]
    const { type, language } = detectArtifactType(lang, content)
    items.push({
      name: `${type}-${idx + 1}`,
      artifact_type: type,
      language,
      content,
      version: 1,
    })
    idx++
  }
  return items
}

// 消息文本变化时重新提取 (流式时持续更新, 流式结束后稳定可编辑)
watch(
  () => getMessageText(),
  (text) => {
    if (props.message.role !== 'assistant' || !text) {
      artifacts.value = []
      return
    }
    artifacts.value = extractArtifactsFromText(text)
  },
  { immediate: true },
)

// el-collapse 默认展开
const artifactCollapseNames = ref([])
watch(
  artifacts,
  (vals) => {
    if (vals.length > 0 && artifactCollapseNames.value.length === 0) {
      artifactCollapseNames.value = ['art-' + (props.message.id || 'local')]
    }
  },
  { immediate: true },
)

// 编辑 artifact 后更新本地内容 (仅前端预览)
function onArtifactUpdate(updatedArt) {
  const idx = artifacts.value.findIndex((a) => a.name === updatedArt.name)
  if (idx >= 0) {
    artifacts.value[idx] = { ...artifacts.value[idx], ...updatedArt }
  }
}

// 保存为 Artifact (调用后端 API 持久化)
const savingArtifacts = ref(false)
async function saveArtifactsToBackend() {
  if (artifacts.value.length === 0) return
  const sessionId = chatStore.currentSession?.id
  if (!sessionId) {
    ElMessage.warning('无法保存：缺少会话上下文')
    return
  }
  savingArtifacts.value = true
  let ok = 0
  let fail = 0
  for (const art of artifacts.value) {
    try {
      await artifactApi.create({
        session_id: sessionId,
        message_id:
          props.message.id && !String(props.message.id).startsWith('local-')
            ? props.message.id
            : null,
        name: art.name,
        artifact_type: art.artifact_type,
        language: art.language,
        content: art.content,
      })
      ok++
    } catch (e) {
      fail++
      console.warn('保存 artifact 失败:', e)
    }
  }
  savingArtifacts.value = false
  if (ok > 0) {
    ElMessage.success(`已保存 ${ok} 个 Artifact` + (fail > 0 ? `, ${fail} 个失败` : ''))
  } else {
    ElMessage.error('保存失败')
  }
}

// ---- 是否可以重新生成 ----
const canRegenerate = computed(() => {
  return props.isLast && !props.isStreaming && props.message.role === 'assistant' && hasText.value
})

const canRetry = computed(() => {
  return props.isLast && !props.isStreaming && props.message.error
})

// ---- 自动滚动到底部（流式时） ----
const contentRef = ref(null)
watch(
  () => props.message.parts?.length,
  () => {
    nextTick(() => {
      if (contentRef.value) {
        // 触发代码块按钮注入
        injectCopyButtons()
      }
    })
  },
)

function injectCopyButtons() {
  if (!contentRef.value) return
  const pres = contentRef.value.querySelectorAll('pre:not(.has-copy-btn)')
  pres.forEach((pre) => {
    pre.classList.add('has-copy-btn')
    pre.style.position = 'relative'
    const btn = document.createElement('button')
    btn.className = 'code-copy-btn'
    btn.textContent = '复制'
    btn.style.cssText = `
      position: absolute; top: 4px; right: 4px; z-index: 1;
      padding: 2px 8px; font-size: 11px; border-radius: 4px;
      background: rgba(255,255,255,0.1); color: inherit;
      border: 1px solid rgba(255,255,255,0.2); cursor: pointer;
      transition: background 0.2s;
    `
    btn.onmouseenter = () => (btn.style.background = 'rgba(255,255,255,0.2)')
    btn.onmouseleave = () => (btn.style.background = 'rgba(255,255,255,0.1)')
    pre.appendChild(btn)
  })
  // 渲染 Mermaid 图表
  renderMermaid(contentRef.value)
}

onMounted(() => {
  injectCopyButtons()
})
</script>

<template>
  <div class="msg-bubble" :class="message.role">
    <!-- 头像 -->
    <div class="avatar">
      <el-icon v-if="message.role === 'user'"><User /></el-icon>
      <el-icon v-else><ChatRound /></el-icon>
    </div>

    <div class="msg-body">
      <!-- 角色名 + 时间戳 -->
      <div class="msg-header">
        <span class="role-name">{{ message.role === 'user' ? '我' : 'AI 助手' }}</span>
        <span v-if="message.createdAt" class="msg-time">{{ message.createdAt }}</span>
      </div>

      <!-- 思考过程（可折叠） -->
      <div v-if="reasoningText" class="reasoning-block">
        <div class="reasoning-toggle" @click="showThinking = !showThinking">
          <el-icon><CaretRight v-if="!showThinking" /><CaretBottom v-else /></el-icon>
          <span>{{ showThinking ? '收起思考过程' : '展开思考过程' }}</span>
        </div>
        <div v-if="showThinking" class="reasoning-content" v-html="renderMarkdown(reasoningText)"></div>
      </div>

      <!-- 编辑模式 -->
      <div v-if="isEditing" class="edit-area">
        <el-input v-model="editText" type="textarea" :rows="3" autofocus />
        <div class="edit-actions">
          <el-button size="small" @click="cancelEdit">取消</el-button>
          <el-button size="small" type="primary" @click="saveEdit">保存并重新生成</el-button>
        </div>
      </div>

      <!-- 消息内容 -->
      <div
        v-else
        ref="contentRef"
        class="msg-content"
        :class="{ streaming: message.streaming, error: message.error }"
        @click="onContentClick"
      >
        <div v-if="hasText" class="markdown-body" v-html="renderedText"></div>

        <!-- 图片附件缩略图 -->
        <div v-if="imageParts.length > 0" class="image-parts">
          <el-image
            v-for="(img, idx) in imageParts"
            :key="(img.id || 'img-' + idx)"
            class="image-thumb"
            :src="img.metadata?.dataUrl"
            :alt="img.metadata?.name || '图片附件'"
            :preview-src-list="imageParts.map((p) => p.metadata?.dataUrl).filter(Boolean)"
            :initial-index="idx"
            fit="cover"
            hide-on-click-modal
          />
        </div>

        <!-- 非图片附件列表 -->
        <div v-if="fileParts.length > 0" class="file-parts">
          <div
            v-for="(f, idx) in fileParts"
            :key="(f.id || 'file-' + idx)"
            class="file-chip"
          >
            <el-icon><Document /></el-icon>
            <span class="file-name">{{ f.metadata?.name || '附件' }}</span>
          </div>
        </div>

        <!-- 流式光标 -->
        <span v-if="message.streaming && !hasText" class="thinking-dots">
          <span></span><span></span><span></span>
        </span>
        <span v-if="message.streaming && hasText" class="stream-cursor">▍</span>

        <!-- 工具调用 -->
        <ToolCallCard
          v-for="tp in toolParts"
          :key="tp.id || tp.tool_call_id"
          :tool="tp"
        />

        <!-- 错误 -->
        <div v-if="message.error" class="error-block">
          <el-icon><WarningFilled /></el-icon>
          <span>{{ message.error }}</span>
          <el-button v-if="canRetry" size="small" type="primary" plain @click="$emit('retry')">
            <el-icon><RefreshRight /></el-icon>
            重试
          </el-button>
        </div>
      </div>

      <!-- Artifacts 可视化区域 (对标 Claude Artifacts / ChatGPT Canvas) -->
      <div v-if="artifacts.length > 0 && !message.streaming" class="artifacts-block">
        <el-collapse v-model="artifactCollapseNames">
          <el-collapse-item :name="'art-' + (message.id || 'local')">
            <template #title>
              <div class="artifacts-title">
                <el-icon><Files /></el-icon>
                <span>Artifacts ({{ artifacts.length }})</span>
              </div>
            </template>
            <ArtifactRenderer
              v-for="(art, idx) in artifacts"
              :key="idx"
              :artifact="art"
              @update="onArtifactUpdate"
            />
          </el-collapse-item>
        </el-collapse>
      </div>

      <!-- Token 用量 + 延迟 -->
      <div v-if="usage || latency" class="msg-meta">
        <span v-if="usage" class="meta-item">
          <el-icon><Coin /></el-icon>
          {{ usage.total_tokens || (usage.prompt_tokens || 0) + (usage.completion_tokens || 0) }} tokens
          <template v-if="usage.prompt_tokens">
            ({{ usage.prompt_tokens }} → {{ usage.completion_tokens }})
          </template>
        </span>
        <span v-if="latency" class="meta-item">
          <el-icon><Timer /></el-icon>
          {{ latency }}
        </span>
      </div>

      <!-- 操作栏（hover 显示） -->
      <div v-if="!isEditing && !message.streaming" class="msg-actions">
        <el-button
          v-if="message.role === 'assistant' && hasText"
          size="small" text
          @click="copyMessage"
        >
          <el-icon><CopyDocument /></el-icon>
          {{ copied ? '已复制' : '复制' }}
        </el-button>

        <!-- 保存为 Artifact (对标 Claude Artifacts 持久化) -->
        <el-button
          v-if="message.role === 'assistant' && artifacts.length > 0 && !message.streaming"
          size="small"
          text
          :loading="savingArtifacts"
          @click="saveArtifactsToBackend"
        >
          <el-icon><CollectionTag /></el-icon>
          保存为Artifact
        </el-button>

        <!-- 功能2: 语音输出 (TTS) 朗读按钮 -->
        <el-button
          v-if="message.role === 'assistant' && hasText"
          size="small"
          text
          :type="isSpeaking ? 'danger' : ''"
          @click="toggleSpeech"
        >
          <el-icon><VideoPause v-if="isSpeaking" /><Headset v-else /></el-icon>
          {{ isSpeaking ? '停止' : '朗读' }}
        </el-button>

        <el-button v-if="message.role === 'user'" size="small" text @click="startEdit">
          <el-icon><Edit /></el-icon>
          编辑
        </el-button>

        <el-tooltip content="从此消息分叉出新会话" placement="top">
          <el-button size="small" text @click="onFork">
            <el-icon><Switch /></el-icon>
            从此分叉
          </el-button>
        </el-tooltip>

        <el-button v-if="canRegenerate" size="small" text @click="$emit('regenerate')">
          <el-icon><Refresh /></el-icon>
          重新生成
        </el-button>

        <template v-if="message.role === 'assistant' && hasText">
          <el-button
            size="small" text
            :type="feedback === 'like' ? 'success' : ''"
            @click="onFeedback('like')"
          >
            <el-icon><Top /></el-icon>
          </el-button>
          <el-button
            size="small" text
            :type="feedback === 'dislike' ? 'danger' : ''"
            @click="onFeedback('dislike')"
          >
            <el-icon><Bottom /></el-icon>
          </el-button>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.msg-bubble {
  display: flex;
  gap: 12px;
  padding: 16px 0;
}
.avatar {
  width: 32px; height: 32px;
  border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
  font-size: 16px;
}
.msg-bubble.user .avatar {
  background: var(--el-color-primary-light-7);
  color: var(--el-color-primary);
}
.msg-bubble.assistant .avatar {
  background: var(--el-color-success-light-7);
  color: var(--el-color-success);
}
.msg-body {
  flex: 1;
  min-width: 0;
}
.msg-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}
.role-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}
.msg-time {
  font-size: 11px;
  color: var(--el-text-color-secondary);
}
.msg-content {
  font-size: 14px;
  line-height: 1.7;
  color: var(--el-text-color-primary);
  word-break: break-word;
}
.msg-content.error {
  color: var(--el-color-danger);
}
.msg-content.streaming {
  min-height: 24px;
}

/* 图片附件缩略图 */
.image-parts {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 8px 0;
}
.image-thumb {
  width: 160px;
  height: 160px;
  border-radius: 8px;
  border: 1px solid var(--el-border-color-lighter);
  cursor: pointer;
  background: var(--el-fill-color-light);
}

/* 非图片附件 chip */
.file-parts {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 8px 0;
}
.file-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  font-size: 12px;
  color: var(--el-text-color-regular);
}
.file-chip .file-name {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 200px;
}
.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3) {
  margin: 12px 0 8px;
  font-weight: 600;
}
.markdown-body :deep(p) {
  margin: 8px 0;
}
.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  padding-left: 20px;
  margin: 8px 0;
}
.markdown-body :deep(pre) {
  background: var(--el-fill-color-darker);
  border-radius: 8px;
  padding: 12px 40px 12px 12px;
  overflow-x: auto;
  margin: 8px 0;
}
.markdown-body :deep(code) {
  font-family: 'Fira Code', 'Consolas', monospace;
  font-size: 13px;
}
.markdown-body :deep(blockquote) {
  border-left: 3px solid var(--el-color-primary-light-5);
  padding-left: 12px;
  margin: 8px 0;
  color: var(--el-text-color-secondary);
}
.markdown-body :deep(table) {
  border-collapse: collapse;
  margin: 8px 0;
}
.markdown-body :deep(th),
.markdown-body :deep(td) {
  border: 1px solid var(--el-border-color);
  padding: 6px 12px;
}
.markdown-body :deep(a) {
  color: var(--el-color-primary);
  text-decoration: none;
}
.markdown-body :deep(a:hover) {
  text-decoration: underline;
}

/* 流式光标 */
.stream-cursor {
  display: inline-block;
  animation: blink 1s infinite;
  color: var(--el-color-primary);
}
@keyframes blink {
  0%, 50% { opacity: 1; }
  51%, 100% { opacity: 0; }
}

/* 思考中三点 */
.thinking-dots {
  display: inline-flex;
  gap: 4px;
  align-items: center;
  padding: 4px 0;
}
.thinking-dots span {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--el-text-color-secondary);
  animation: bounce 1.4s infinite ease-in-out;
}
.thinking-dots span:nth-child(2) { animation-delay: 0.2s; }
.thinking-dots span:nth-child(3) { animation-delay: 0.4s; }
@keyframes bounce {
  0%, 80%, 100% { transform: scale(0.6); opacity: 0.5; }
  40% { transform: scale(1); opacity: 1; }
}

/* 思考过程 */
.reasoning-block {
  background: var(--el-fill-color-light);
  border-radius: 8px;
  margin-bottom: 8px;
  overflow: hidden;
}
.reasoning-toggle {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 12px;
  cursor: pointer;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  user-select: none;
}
.reasoning-toggle:hover {
  color: var(--el-color-primary);
}
.reasoning-content {
  padding: 8px 12px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  border-top: 1px solid var(--el-border-color-lighter);
}

/* 编辑区域 */
.edit-area {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.edit-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}

/* 错误块 */
.error-block {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: var(--el-color-danger-light-9);
  border-radius: 8px;
  color: var(--el-color-danger);
  font-size: 13px;
  margin-top: 8px;
}

/* Token / 延迟 */
.msg-meta {
  display: flex;
  gap: 12px;
  margin-top: 6px;
  font-size: 11px;
  color: var(--el-text-color-secondary);
}
.meta-item {
  display: flex;
  align-items: center;
  gap: 2px;
}

/* 操作栏 */
.msg-actions {
  display: flex;
  gap: 2px;
  margin-top: 4px;
  opacity: 0;
  transition: opacity 0.2s;
}
.msg-bubble:hover .msg-actions {
  opacity: 1;
}
.msg-actions :deep(.el-button) {
  padding: 2px 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.msg-actions :deep(.el-button:hover) {
  color: var(--el-color-primary);
}

/* Artifacts 区域 */
.artifacts-block {
  margin-top: 8px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  overflow: hidden;
}
.artifacts-block :deep(.el-collapse) {
  border: none;
}
.artifacts-block :deep(.el-collapse-item__header) {
  padding: 0 12px;
  font-size: 13px;
  height: 36px;
  line-height: 36px;
  background: var(--el-fill-color-light);
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.artifacts-block :deep(.el-collapse-item__content) {
  padding: 8px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.artifacts-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--el-text-color-regular);
}
</style>
