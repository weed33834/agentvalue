<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import { useChatStore } from '@/stores/chat'
import { templateApi, voiceApi } from '@/api/client'

const props = defineProps({
  disabled: { type: Boolean, default: false },
})
const emit = defineEmits(['send', 'stop'])

const chatStore = useChatStore()
// 从 store 获取流式状态：AI 正在回复时显示停止按钮
const isStreaming = computed(() => chatStore.isStreaming)

const text = ref('')
const attachments = ref([])

const canSend = computed(
  () => text.value.trim().length > 0 && !props.disabled && !isStreaming.value,
)

function onSend() {
  if (!canSend.value) return
  const content = text.value.trim()
  // 将文件元信息附带返回；图片额外携带 base64 data URL 供后端 vision 调用
  const files = attachments.value.map((f) => {
    const meta = { name: f.name, size: f.size, type: f.type }
    if (f.type && f.type.startsWith('image/') && f.dataUrl) {
      meta.dataUrl = f.dataUrl
    }
    return meta
  })
  text.value = ''
  attachments.value = []
  emit('send', content, files.length > 0 ? files : undefined)
}

function onKeydown(e) {
  // 仅处理纯 Enter（无 Shift/Mod），Cmd/Ctrl+Enter 由父组件全局监听触发 submit
  if (e.key === 'Enter' && !e.shiftKey && !e.metaKey && !e.ctrlKey) {
    e.preventDefault()
    onSend()
  }
}

function onFileSelect(e) {
  const files = Array.from(e.target.files || [])
  for (const file of files) {
    if (file.size > 10 * 1024 * 1024) {
      ElMessage.warning(`${file.name} 超过 10MB 限制`)
      continue
    }
    // 图片文件读取为 base64 data URL，便于预览与后端 vision 调用
    if (file.type && file.type.startsWith('image/')) {
      const reader = new FileReader()
      reader.onload = () => {
        attachments.value.push({
          name: file.name,
          size: file.size,
          type: file.type,
          dataUrl: String(reader.result || ''),
        })
      }
      reader.onerror = () => {
        ElMessage.warning(`${file.name} 读取失败`)
      }
      reader.readAsDataURL(file)
    } else {
      attachments.value.push(file)
    }
  }
  e.target.value = '' // reset for same-file re-select
}

function removeAttachment(idx) {
  attachments.value.splice(idx, 1)
}

function formatSize(bytes) {
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB'
}

// ============================================================
// 功能1: 语音输入 (STT) - Web Speech API + 降级文件上传
// ============================================================
const isRecording = ref(false)
const speechSupported = ref(false)
let recognition = null

onMounted(() => {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition
  if (SR) {
    speechSupported.value = true
    recognition = new SR()
    recognition.lang = 'zh-CN'
    recognition.continuous = true
    recognition.interimResults = true
    recognition.onresult = (event) => {
      // 仅追加 final 结果，避免中间态频繁刷新输入框
      let finalText = ''
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const transcript = event.results[i][0].transcript
        if (event.results[i].isFinal) {
          finalText += transcript
        }
      }
      if (finalText) {
        text.value += finalText
      }
    }
    recognition.onerror = (event) => {
      console.error('语音识别错误:', event.error)
      if (event.error !== 'no-speech' && event.error !== 'aborted') {
        ElMessage.warning('语音识别失败: ' + event.error)
      }
      isRecording.value = false
    }
    recognition.onend = () => {
      isRecording.value = false
    }
  }
})

function toggleRecording() {
  if (!recognition) return
  if (isRecording.value) {
    recognition.stop()
    isRecording.value = false
  } else {
    try {
      recognition.start()
      isRecording.value = true
    } catch (e) {
      console.error('启动录音失败:', e)
      ElMessage.warning('启动录音失败，请重试')
    }
  }
}

// 降级：浏览器不支持 Web Speech API 时，上传音频文件走后端 STT
function onVoiceFileSelect(e) {
  const file = e.target.files?.[0]
  if (!file) return
  if (file.size > 25 * 1024 * 1024) {
    ElMessage.warning('音频文件不能超过 25MB')
    e.target.value = ''
    return
  }
  ElMessage.info('正在识别语音...')
  voiceApi
    .stt(file)
    .then((res) => {
      const transcript = res?.text || res?.transcript || ''
      if (transcript) {
        text.value += transcript
        ElMessage.success('语音识别完成')
      } else {
        ElMessage.warning('未识别到语音内容')
      }
    })
    .catch((err) => {
      console.error('语音识别失败:', err)
      ElMessage.error('语音识别失败')
    })
  e.target.value = ''
}

// ============================================================
// 功能3: 提示词模板选择器
// ============================================================
const showTemplatePicker = ref(false)
const templates = ref([])
const templateCategory = ref('all')
const templateLoading = ref(false)

const categories = [
  { value: 'all', label: '全部' },
  { value: 'general', label: '通用' },
  { value: 'programming', label: '编程' },
  { value: 'writing', label: '写作' },
  { value: 'analysis', label: '分析' },
  { value: 'hr', label: 'HR' },
]

async function loadTemplates() {
  templateLoading.value = true
  try {
    const res = await templateApi.list(templateCategory.value)
    // 兼容多种返回结构：数组 / {items} / {templates} / {data}
    templates.value = Array.isArray(res) ? res : res?.items || res?.templates || res?.data || []
  } catch (e) {
    console.error('加载模板失败:', e)
    templates.value = []
    ElMessage.warning('模板加载失败')
  } finally {
    templateLoading.value = false
  }
}

function openTemplatePicker() {
  showTemplatePicker.value = true
  loadTemplates()
}

function onCategoryChange() {
  loadTemplates()
}

// 模板变量填写对话框
const variableDialogVisible = ref(false)
const currentTemplate = ref(null)
const variableValues = ref({})

function extractVariables(content) {
  if (!content) return []
  const matches = content.match(/\{\{(\w+)\}\}/g) || []
  return [...new Set(matches.map((m) => m.replace(/\{\{|\}\}/g, '')))]
}

function selectTemplate(tpl) {
  const content = tpl.content || tpl.template || tpl.body || ''
  const variables = extractVariables(content)
  if (variables.length > 0) {
    // 模板含变量，弹出表单让用户填入
    currentTemplate.value = tpl
    variableValues.value = {}
    variables.forEach((v) => (variableValues.value[v] = ''))
    variableDialogVisible.value = true
  } else {
    applyTemplate(tpl, {})
  }
}

function applyTemplate(tpl, variables) {
  let content = tpl.content || tpl.template || tpl.body || ''
  for (const [k, v] of Object.entries(variables)) {
    content = content.replace(new RegExp(`\\{\\{${k}\\}\\}`, 'g'), v)
  }
  text.value = content
  showTemplatePicker.value = false
  variableDialogVisible.value = false
  ElMessage.success('已应用模板')
}

function confirmVariables() {
  // 校验必填变量
  for (const [k, v] of Object.entries(variableValues.value)) {
    if (!v || !v.trim()) {
      ElMessage.warning(`请填写变量: ${k}`)
      return
    }
  }
  applyTemplate(currentTemplate.value, variableValues.value)
}

// ============================================================
// 功能4: 对话停止生成
// ============================================================
function onStop() {
  chatStore.stopGeneration()
  emit('stop')
}

onBeforeUnmount(() => {
  // 组件卸载时清理录音状态
  if (recognition && isRecording.value) {
    recognition.stop()
  }
})

// 暴露给父组件，供快捷键 Cmd/Ctrl+Enter 触发发送
defineExpose({
  submit: onSend,
})
</script>

<template>
  <div class="chat-input">
    <!-- 附件预览 -->
    <div v-if="attachments.length > 0" class="attachments-bar">
      <div
        v-for="(file, idx) in attachments"
        :key="idx"
        class="attachment-chip"
        :class="{ 'is-image': file.type && file.type.startsWith('image/') }"
      >
        <img
          v-if="file.type && file.type.startsWith('image/') && file.dataUrl"
          :src="file.dataUrl"
          :alt="file.name"
          class="file-thumb"
        />
        <el-icon v-else><Document /></el-icon>
        <div class="file-meta">
          <span class="file-name">{{ file.name }}</span>
          <span class="file-size">{{ formatSize(file.size) }}</span>
        </div>
        <el-icon class="remove-btn" @click="removeAttachment(idx)"><Close /></el-icon>
      </div>
    </div>

    <div class="input-row">
      <!-- 文件上传按钮 -->
      <label class="upload-btn" :class="{ disabled }">
        <input
          type="file"
          multiple
          :disabled="disabled"
          @change="onFileSelect"
          style="display: none"
        />
        <el-icon><Paperclip /></el-icon>
      </label>

      <!-- 功能3: 提示词模板选择器 -->
      <el-popover
        v-model:visible="showTemplatePicker"
        placement="top-start"
        :width="420"
        trigger="click"
      >
        <template #reference>
          <el-tooltip content="提示词模板" placement="top">
            <button class="icon-action-btn" type="button" @click="openTemplatePicker">
              <el-icon><Document /></el-icon>
            </button>
          </el-tooltip>
        </template>
        <div class="template-picker">
          <div class="template-header">提示词模板</div>
          <el-radio-group
            v-model="templateCategory"
            size="small"
            class="category-group"
            @change="onCategoryChange"
          >
            <el-radio-button v-for="c in categories" :key="c.value" :value="c.value">
              {{ c.label }}
            </el-radio-button>
          </el-radio-group>
          <div v-loading="templateLoading" class="template-list">
            <div v-if="!templateLoading && templates.length === 0" class="empty-tip">暂无模板</div>
            <div
              v-for="tpl in templates"
              :key="tpl.id || tpl.name"
              class="template-item"
              @click="selectTemplate(tpl)"
            >
              <div class="tpl-row">
                <span class="tpl-name">{{ tpl.name || tpl.title }}</span>
                <el-tag v-if="tpl.category" size="small" type="info">
                  {{ tpl.category }}
                </el-tag>
              </div>
              <div v-if="tpl.description" class="tpl-desc">{{ tpl.description }}</div>
            </div>
          </div>
        </div>
      </el-popover>

      <!-- 功能1: 语音输入 (STT) - 浏览器原生 -->
      <el-tooltip v-if="speechSupported" content="语音输入" placement="top">
        <button
          class="icon-action-btn mic-btn"
          :class="{ recording: isRecording }"
          type="button"
          @click="toggleRecording"
        >
          <el-icon><Microphone /></el-icon>
        </button>
      </el-tooltip>
      <!-- 降级：浏览器不支持 Web Speech API，上传音频文件走后端 STT -->
      <el-tooltip v-else content="上传语音文件识别" placement="top">
        <label class="upload-btn">
          <input type="file" accept="audio/*" @change="onVoiceFileSelect" style="display: none" />
          <el-icon><Microphone /></el-icon>
        </label>
      </el-tooltip>

      <div class="input-wrapper">
        <el-input
          v-model="text"
          type="textarea"
          :rows="2"
          :disabled="disabled || isStreaming"
          resize="none"
          placeholder="输入消息，Enter 发送，Shift+Enter 换行，Cmd/Ctrl+Enter 也可发送"
          @keydown="onKeydown"
        />
      </div>

      <!-- 功能4: 发送 / 停止生成按钮 -->
      <el-button v-if="isStreaming" type="danger" @click="onStop">
        <el-icon><VideoPause /></el-icon>
        停止
      </el-button>
      <el-button v-else type="primary" :disabled="!canSend" @click="onSend">
        <el-icon><Promotion /></el-icon>
        发送
      </el-button>
    </div>

    <!-- 模板变量填写对话框 -->
    <el-dialog v-model="variableDialogVisible" title="填写模板变量" width="480px" append-to-body>
      <el-form v-if="currentTemplate" label-position="top">
        <el-form-item v-for="(_, key) in variableValues" :key="key" :label="key">
          <el-input v-model="variableValues[key]" :placeholder="`请输入 ${key}`" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="variableDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="confirmVariables">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.chat-input {
  padding: 8px 16px 12px;
  border-top: 1px solid var(--el-border-color-lighter);
  background: var(--el-bg-color);
}
.attachments-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
  padding: 4px 0;
}
.attachment-chip {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  font-size: 12px;
  max-width: 240px;
}
.attachment-chip.is-image {
  padding: 4px;
  max-width: 200px;
}
.file-thumb {
  width: 36px;
  height: 36px;
  object-fit: cover;
  border-radius: 4px;
  flex-shrink: 0;
}
.file-meta {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.file-name {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 140px;
}
.file-size {
  color: var(--el-text-color-secondary);
  font-size: 11px;
}
.remove-btn {
  cursor: pointer;
  color: var(--el-color-danger);
  flex-shrink: 0;
}
.input-row {
  display: flex;
  gap: 8px;
  align-items: flex-end;
}
.upload-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: 6px;
  cursor: pointer;
  color: var(--el-text-color-secondary);
  transition: all 0.2s;
  flex-shrink: 0;
}
.upload-btn:hover:not(.disabled) {
  background: var(--el-fill-color-light);
  color: var(--el-color-primary);
}
.upload-btn.disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.input-wrapper {
  flex: 1;
}
.chat-input :deep(.el-textarea__inner) {
  resize: none;
  font-family: inherit;
}

/* 图标操作按钮（模板/麦克风）通用样式 */
.icon-action-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: 6px;
  border: none;
  background: transparent;
  cursor: pointer;
  color: var(--el-text-color-secondary);
  transition: all 0.2s;
  flex-shrink: 0;
  padding: 0;
}
.icon-action-btn:hover {
  background: var(--el-fill-color-light);
  color: var(--el-color-primary);
}

/* 麦克风录音状态：红色 + 脉冲动画 */
.mic-btn.recording {
  background: var(--el-color-danger);
  color: #fff;
  animation: mic-pulse 1.5s infinite;
}
.mic-btn.recording:hover {
  background: var(--el-color-danger);
  color: #fff;
}
@keyframes mic-pulse {
  0% {
    box-shadow: 0 0 0 0 rgba(245, 108, 108, 0.7);
  }
  70% {
    box-shadow: 0 0 0 10px rgba(245, 108, 108, 0);
  }
  100% {
    box-shadow: 0 0 0 0 rgba(245, 108, 108, 0);
  }
}

/* 提示词模板选择器 */
.template-picker {
  max-height: 400px;
  display: flex;
  flex-direction: column;
}
.template-header {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 8px;
  color: var(--el-text-color-primary);
}
.category-group {
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.template-list {
  flex: 1;
  overflow-y: auto;
  max-height: 280px;
}
.template-item {
  padding: 8px 10px;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.2s;
  border: 1px solid var(--el-border-color-lighter);
  margin-bottom: 6px;
}
.template-item:hover {
  background: var(--el-fill-color-light);
}
.tpl-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.tpl-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--el-text-color-primary);
}
.tpl-desc {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 4px;
  line-height: 1.5;
}
.empty-tip {
  text-align: center;
  color: var(--el-text-color-secondary);
  padding: 24px 0;
  font-size: 13px;
}
</style>
