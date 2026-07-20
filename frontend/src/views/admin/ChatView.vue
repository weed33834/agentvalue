<script setup>
import { onMounted, onUnmounted, ref, nextTick } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useChatStore } from '@/stores/chat'
import { chatApi } from '@/api/client'
import MessageList from '@/components/chat/MessageList.vue'
import ChatInput from '@/components/chat/ChatInput.vue'

const chat = useChatStore()
const newSessionVisible = ref(false)
const newSessionForm = ref({
  title: '新对话',
  model_name: 'DeepSeek-V4-Flash',
})

// 模型选项
const modelOptions = [
  { label: 'DeepSeek V4 Flash (快速)', value: 'DeepSeek-V4-Flash' },
  { label: 'DeepSeek V4 Pro (智能)', value: 'DeepSeek-V4-Pro' },
  { label: 'GLM 4.7', value: 'glm-4.7' },
  { label: 'GLM 5.1', value: 'glm-5.1' },
  { label: 'Qwen 3 Coder', value: 'Qwen3-Coder-Next-FP8' },
  { label: 'Kimi K2.6', value: 'Kimi-K2.6' },
  { label: 'MiniMax M3', value: 'MiniMax-M3' },
  { label: 'auto (自动路由)', value: 'auto' },
]

// ---- 搜索 ----
const searchInput = ref('')
const searchInputRef = ref(null)
const chatInputRef = ref(null)

function onSearchInput() {
  // 直接设置 searchQuery，displaySessions 会自动做本地过滤
  chat.searchQuery = searchInput.value
}

function clearSearch() {
  searchInput.value = ''
  chat.searchQuery = ''
}

// ---- 快捷键 ----
// Cmd/Ctrl + K: 聚焦搜索框
// Cmd/Ctrl + Enter: 发送消息（透传给 ChatInput，因 ChatInput 已绑定 @keydown）
// Cmd/Ctrl + Shift + N: 新建会话
// Esc: 关闭对话框 / 取消重命名
function isMod(e) {
  return e.metaKey || e.ctrlKey
}

function onGlobalKeydown(e) {
  // Cmd/Ctrl + K：聚焦搜索框
  if (isMod(e) && !e.shiftKey && (e.key === 'k' || e.key === 'K')) {
    e.preventDefault()
    if (searchInputRef.value) {
      // el-input 组件实例聚焦
      searchInputRef.value.focus?.()
    }
    return
  }
  // Cmd/Ctrl + Shift + N：新建会话
  if (isMod(e) && e.shiftKey && (e.key === 'n' || e.key === 'N')) {
    e.preventDefault()
    newSessionVisible.value = true
    return
  }
  // Cmd/Ctrl + Enter：发送消息（仅当 ChatInput 持有时触发其 send）
  if (isMod(e) && e.key === 'Enter') {
    e.preventDefault()
    chatInputRef.value?.submit?.()
    return
  }
  // Esc：关闭对话框 / 取消重命名
  if (e.key === 'Escape') {
    if (newSessionVisible.value) {
      newSessionVisible.value = false
      return
    }
    if (editingSessionId.value) {
      cancelRename()
      return
    }
  }
}

// ---- 会话重命名 ----
const editingSessionId = ref(null)
const editingTitle = ref('')
const titleInputRef = ref(null)

async function startRename(session) {
  editingSessionId.value = session.id
  editingTitle.value = session.title
  await nextTick()
  if (titleInputRef.value) {
    titleInputRef.value.focus()
    titleInputRef.value.select()
  }
}

async function saveRename() {
  if (!editingSessionId.value) return
  const title = editingTitle.value.trim()
  if (!title) {
    ElMessage.warning('标题不能为空')
    return
  }
  await chat.renameSession(editingSessionId.value, title)
  editingSessionId.value = null
}

function cancelRename() {
  editingSessionId.value = null
}

onMounted(() => {
  chat.loadSessions()
  // 注册全局快捷键监听（keydown 阶段，capture=false 避免与子组件冲突）
  window.addEventListener('keydown', onGlobalKeydown)
})

onUnmounted(() => {
  window.removeEventListener('keydown', onGlobalKeydown)
})

async function onSend(text, attachments) {
  if (!chat.currentSession) {
    // 无会话时自动创建
    await chat.createSession({
      title: text.slice(0, 20),
      model_name: newSessionForm.value.model_name,
    })
  }
  chat.sendMessage(text, attachments)
}

async function createNewSession() {
  try {
    await chat.createSession({
      title: newSessionForm.value.title || '新对话',
      model_name: newSessionForm.value.model_name,
    })
    newSessionVisible.value = false
    newSessionForm.value.title = '新对话'
  } catch {
    ElMessage.error('创建会话失败')
  }
}

async function onDeleteSession(id) {
  try {
    await ElMessageBox.confirm('确定删除该会话？所有消息将被清除。', '提示', {
      type: 'warning',
    })
    await chat.deleteSession(id)
    ElMessage.success('已删除')
  } catch {
    // 用户取消
  }
}

function formatTime(ts) {
  if (!ts) return ''
  const d = new Date(ts)
  return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours()}:${String(d.getMinutes()).padStart(2, '0')}`
}

// 切换模型
async function onModelChange(modelName) {
  if (!chat.currentSession) return
  try {
    // 通过更新会话来保存模型选择（后端可能需要添加 model_name 更新支持）
    const s = chat.sessions.find((s) => s.id === chat.currentSession.id)
    if (s) s.model_name = modelName
    ElMessage.success(`已切换到 ${modelName}`)
  } catch (e) {
    console.error('切换模型失败:', e)
  }
}

// ---- 分享会话 ----
const shareVisible = ref(false)
const shareUrl = ref('')
const shareId = ref('')

async function onShareSession() {
  if (!chat.currentSession) return
  try {
    const data = await chatApi.shareSession(chat.currentSession.id)
    shareUrl.value = data.share_url || ''
    shareId.value = data.share_id || ''
    shareVisible.value = true
  } catch (e) {
    console.error('生成分享链接失败:', e)
    ElMessage.error('生成分享链接失败')
  }
}

async function copyShareUrl() {
  if (!shareUrl.value) return
  try {
    await navigator.clipboard.writeText(shareUrl.value)
    ElMessage.success('链接已复制')
  } catch {
    ElMessage.warning('复制失败，请手动复制')
  }
}

// ---- 从消息分叉 ----
async function onForkMessage(message) {
  if (!chat.currentSession || !message?.id) return
  try {
    await ElMessageBox.confirm(
      `将从此消息（含）分叉出一个新会话，复制到此为止的全部历史。是否继续？`,
      '分叉会话',
      { type: 'info', confirmButtonText: '分叉', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户取消
  }
  try {
    const newSession = await chatApi.forkSession(
      chat.currentSession.id,
      message.id,
    )
    if (newSession) {
      // 将新会话插入到列表头部并切换
      chat.sessions.unshift(newSession)
      await chat.selectSession(newSession.id)
      ElMessage.success('已分叉出新会话')
    }
  } catch (e) {
    console.error('分叉会话失败:', e)
    ElMessage.error('分叉会话失败')
  }
}
</script>

<template>
  <div class="chat-view">
    <!-- 左侧会话列表 -->
    <div class="sidebar">
      <div class="sidebar-header">
        <el-button type="primary" class="new-btn" @click="newSessionVisible = true">
          <el-icon><Plus /></el-icon>
          新对话
        </el-button>
      </div>

      <!-- P0-10: 搜索框 -->
      <div class="search-box">
        <el-input
          ref="searchInputRef"
          v-model="searchInput"
          placeholder="搜索会话... (Ctrl+K)"
          :prefix-icon="'Search'"
          clearable
          size="small"
          @input="onSearchInput"
          @clear="clearSearch"
        />
      </div>

      <div class="session-list">
        <div
          v-for="s in chat.displaySessions"
          :key="s.id"
          class="session-item"
          :class="{ active: chat.currentSession && chat.currentSession.id === s.id }"
          @click="chat.selectSession(s.id)"
        >
          <el-icon class="session-icon"><ChatDotRound /></el-icon>
          <div class="session-info">
            <!-- 重命名模式 -->
            <el-input
              v-if="editingSessionId === s.id"
              ref="titleInputRef"
              v-model="editingTitle"
              size="small"
              @click.stop
              @keyup.enter="saveRename"
              @keyup.esc="cancelRename"
              @blur="saveRename"
            />
            <template v-else>
              <div
                class="session-title"
                @dblclick.stop="startRename(s)"
              >
                {{ s.title }}
              </div>
              <div class="session-time">{{ formatTime(s.updated_at || s.created_at) }}</div>
            </template>
          </div>
          <el-icon
            v-if="editingSessionId !== s.id"
            class="rename-btn"
            @click.stop="startRename(s)"
          >
            <EditPen />
          </el-icon>
          <el-icon
            v-if="editingSessionId !== s.id"
            class="delete-btn"
            @click.stop="onDeleteSession(s.id)"
          >
            <Delete />
          </el-icon>
        </div>
        <div v-if="chat.displaySessions.length === 0" class="empty-sessions">
          <el-icon size="32"><ChatRound /></el-icon>
          <p>{{ searchInput ? '未找到匹配的会话' : '暂无会话' }}</p>
        </div>
      </div>
    </div>

    <!-- 右侧对话区 -->
    <div class="main">
      <div v-if="chat.currentSession" class="chat-header">
        <span class="title">{{ chat.currentSession.title }}</span>
        <!-- 模型切换下拉 -->
        <el-select
          v-model="chat.currentSession.model_name"
          size="small"
          style="width: 200px"
          @change="onModelChange"
        >
          <el-option
            v-for="opt in modelOptions"
            :key="opt.value"
            :label="opt.label"
            :value="opt.value"
          />
        </el-select>
        <div class="header-actions">
          <el-tooltip content="重命名会话" placement="bottom">
            <el-button size="small" text @click="startRename(chat.currentSession)">
              <el-icon><EditPen /></el-icon>
            </el-button>
          </el-tooltip>
          <el-tooltip content="分享会话" placement="bottom">
            <el-button size="small" text @click="onShareSession">
              <el-icon><Share /></el-icon>
            </el-button>
          </el-tooltip>
          <el-tooltip content="导出为 Markdown" placement="bottom">
            <el-button size="small" text @click="chat.exportSession()">
              <el-icon><Download /></el-icon>
            </el-button>
          </el-tooltip>
        </div>
      </div>
      <MessageList
        v-if="chat.currentSession"
        :messages="chat.messages"
        :is-streaming="chat.isStreaming"
        @regenerate="chat.regenerate"
        @edit="chat.editMessage"
        @retry="chat.retry"
        @feedback="chat.sendFeedback"
        @fork="onForkMessage"
      />
      <div v-else class="welcome">
        <el-icon size="64"><ChatRound /></el-icon>
        <h2>AgentValue AI 助手</h2>
        <p>您的智能员工价值评估助手</p>
        <p class="welcome-hint">点击左侧"新对话"开始</p>
      </div>
      <ChatInput
        v-if="chat.currentSession"
        ref="chatInputRef"
        :disabled="chat.isStreaming"
        @send="onSend"
        @stop="chat.stopStreaming"
      />
    </div>

    <!-- 新建会话弹窗 -->
    <el-dialog v-model="newSessionVisible" title="新建对话" width="420px">
      <el-form :model="newSessionForm" label-width="80px">
        <el-form-item label="标题">
          <el-input v-model="newSessionForm.title" placeholder="新对话" />
        </el-form-item>
        <el-form-item label="模型">
          <el-select v-model="newSessionForm.model_name" placeholder="选择模型">
            <el-option
              v-for="opt in modelOptions"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="newSessionVisible = false">取消</el-button>
        <el-button type="primary" @click="createNewSession">创建</el-button>
      </template>
    </el-dialog>

    <!-- 分享会话弹窗 -->
    <el-dialog v-model="shareVisible" title="分享会话" width="520px">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="任何人通过此链接均可只读访问本会话内容"
        style="margin-bottom: 12px"
      />
      <el-form label-width="80px">
        <el-form-item label="分享链接">
          <el-input v-model="shareUrl" readonly>
            <template #append>
              <el-button @click="copyShareUrl">复制</el-button>
            </template>
          </el-input>
        </el-form-item>
        <el-form-item label="Share ID">
          <el-input v-model="shareId" readonly />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button type="primary" @click="copyShareUrl">复制链接</el-button>
        <el-button @click="shareVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.chat-view {
  display: flex;
  height: 100%;
  background: var(--el-bg-color);
}
.sidebar {
  width: 260px;
  border-right: 1px solid var(--el-border-color-lighter);
  display: flex;
  flex-direction: column;
  background: var(--el-fill-color-blank);
}
.sidebar-header {
  padding: 12px;
}
.new-btn {
  width: 100%;
}
.search-box {
  padding: 0 12px 8px;
}
.session-list {
  flex: 1;
  overflow-y: auto;
  padding: 0 8px;
}
.session-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border-radius: 8px;
  cursor: pointer;
  margin-bottom: 4px;
  transition: background 0.2s;
}
.session-item:hover {
  background: var(--el-fill-color-light);
}
.session-item.active {
  background: var(--el-color-primary-light-9);
}
.session-icon {
  color: var(--el-text-color-secondary);
  flex-shrink: 0;
}
.session-info {
  flex: 1;
  min-width: 0;
}
.session-title {
  font-size: 14px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.session-time {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.rename-btn,
.delete-btn {
  opacity: 0;
  color: var(--el-text-color-secondary);
  transition: opacity 0.2s, color 0.2s;
  flex-shrink: 0;
}
.rename-btn:hover {
  color: var(--el-color-primary);
}
.delete-btn {
  color: var(--el-color-danger);
}
.session-item:hover .rename-btn,
.session-item:hover .delete-btn {
  opacity: 1;
}
.empty-sessions {
  text-align: center;
  padding: 40px 0;
  color: var(--el-text-color-secondary);
}
.empty-sessions p {
  font-size: 13px;
  margin-top: 8px;
}
.main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.chat-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.chat-header .title {
  font-size: 16px;
  font-weight: 600;
}
.header-actions {
  margin-left: auto;
  display: flex;
  gap: 4px;
}
.welcome {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: var(--el-text-color-secondary);
}
.welcome .el-icon {
  color: var(--el-color-primary-light-5);
  margin-bottom: 16px;
}
.welcome h2 {
  margin: 0 0 8px;
  color: var(--el-text-color-primary);
}
.welcome-hint {
  font-size: 13px;
  opacity: 0.7;
}
</style>
