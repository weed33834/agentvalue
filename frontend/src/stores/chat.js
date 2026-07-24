/**
 * Chat Store
 *
 * 管理聊天会话与流式消息状态，移植 opencode session 的前端状态管理。
 * 对应后端 api/chat.py 的 SSE 事件流。
 *
 * 增强（P0）：重新生成 / 编辑消息 / 错误重试 / 反馈 / 自动标题 / 会话搜索 /
 * 思考过程展示 / Token 用量 / 延迟统计。
 */

import { defineStore } from 'pinia'
import { ref, computed, reactive } from 'vue'
import { ElMessage } from 'element-plus'
import { chatApi } from '@/api/client'
import { streamSSE } from '@/utils/sse'

export const useChatStore = defineStore('chat', () => {
  // 会话列表
  const sessions = ref([])
  // 当前会话
  const currentSession = ref(null)
  // 当前会话的消息列表 [{id, role, parts: [...], streaming, usage, error, latency, feedback}]
  const messages = ref([])
  // 是否正在流式生成
  const isStreaming = ref(false)
  // 加载状态
  const loading = ref(false)
  // 搜索关键词
  const searchQuery = ref('')
  // 搜索结果
  const searchResults = ref([])

  let currentCtrl = null
  // 流式起始时间戳（用于计算 latency）
  let streamStartTime = null

  // ============================================================
  // Session 操作
  // ============================================================

  async function loadSessions() {
    try {
      loading.value = true
      sessions.value = await chatApi.listSessions()
    } catch (e) {
      console.error('加载会话列表失败:', e)
      sessions.value = []
    } finally {
      loading.value = false
    }
  }

  async function selectSession(id) {
    try {
      loading.value = true
      currentSession.value = await chatApi.getSession(id)
      const msgs = await chatApi.listMessages(id)
      // 转换后端格式为前端消息结构
      messages.value = msgs.map((m) => ({
        id: m.id,
        role: m.role,
        parts: (m.parts || []).map((p) => ({
          id: p.id,
          type: p.type,
          text: p.text || '',
          name: p.tool_name,
          input: (p.tool_state && p.tool_state.input) || '',
          output: p.tool_state && p.tool_state.output,
          state: (p.tool_state && p.tool_state.status) || 'completed',
          error: p.tool_state && p.tool_state.error,
          stepIndex: p.step_index,
          // 保留 metadata：image/file part 的 dataUrl 与文件元信息存于此
          metadata: p.metadata || null,
        })),
        streaming: false,
        usage: m.tokens,
        error: m.error
          ? typeof m.error === 'string'
            ? m.error
            : m.error.message || JSON.stringify(m.error)
          : null,
        // 从 metadata_ 加载反馈与延迟
        feedback: m.metadata?.feedback?.rating || null,
        latency: m.metadata?.latency || null,
        createdAt: m.created_at,
      }))
    } catch (e) {
      console.error('加载会话失败:', e)
    } finally {
      loading.value = false
    }
  }

  async function createSession(payload = {}) {
    try {
      const s = await chatApi.createSession(payload)
      sessions.value.unshift(s)
      await selectSession(s.id)
      return s
    } catch (e) {
      console.error('创建会话失败:', e)
      throw e
    }
  }

  async function deleteSession(id) {
    try {
      await chatApi.deleteSession(id)
      sessions.value = sessions.value.filter((s) => s.id !== id)
      if (currentSession.value && currentSession.value.id === id) {
        currentSession.value = null
        messages.value = []
      }
    } catch (e) {
      console.error('删除会话失败:', e)
      throw e
    }
  }

  // ============================================================
  // 发送消息（SSE 流式）
  // ============================================================

  async function sendMessage(content, attachments) {
    if (!currentSession.value || isStreaming.value) return

    // 1. 乐观插入 user message（含图片附件用于本地回显）
    const userParts = [{ type: 'text', text: content }]
    if (Array.isArray(attachments) && attachments.length > 0) {
      for (const a of attachments) {
        if (a.type && a.type.startsWith('image/') && a.dataUrl) {
          userParts.push({
            type: 'image',
            text: '',
            metadata: { name: a.name, size: a.size, mime: a.type, dataUrl: a.dataUrl },
          })
        } else {
          userParts.push({
            type: 'file',
            text: '',
            metadata: { name: a.name, size: a.size, mime: a.type },
          })
        }
      }
    }
    messages.value.push({
      id: 'local-' + Date.now(),
      role: 'user',
      parts: userParts,
      streaming: false,
    })

    // 2. 插入占位 assistant message（用 reactive 确保 SSE 回调修改属性时触发 UI 更新）
    const assistantMsg = reactive({
      id: 'pending-' + Date.now(),
      role: 'assistant',
      parts: [],
      streaming: true,
    })
    messages.value.push(assistantMsg)
    isStreaming.value = true
    streamStartTime = Date.now()

    // 3. 发起 SSE 流式请求（携带 attachments 供后端做 vision 调用）
    const { ctrl } = streamSSE({
      url: `/chat/sessions/${currentSession.value.id}/messages`,
      body: { content, attachments: attachments || [] },
      onEvent: (event, data) => handleEvent(event, data, assistantMsg),
      onClose: () => {
        // 兜底：若流关闭时仍在 streaming 且无内容/错误，说明异常结束
        if (assistantMsg.streaming) {
          assistantMsg.streaming = false
          const hasText = assistantMsg.parts.some((p) => p.type === 'text' && p.text)
          if (!hasText && !assistantMsg.error) {
            assistantMsg.error = '对话连接已结束（未收到完整响应）'
          }
        }
        // 记录延迟
        if (streamStartTime) {
          assistantMsg.latency = Date.now() - streamStartTime
          streamStartTime = null
        }
        isStreaming.value = false
        currentCtrl = null
        // 首次对话后自动生成标题
        _maybeAutoTitle()
      },
      onError: (err) => {
        console.error('SSE 错误:', err)
        assistantMsg.streaming = false
        assistantMsg.error = (err && err.message) || String(err)
        isStreaming.value = false
        currentCtrl = null
        streamStartTime = null
      },
    })
    currentCtrl = ctrl
  }

  function handleEvent(event, data, assistantMsg) {
    switch (event) {
      case 'step-start':
        assistantMsg.parts.push({ type: 'step-start', stepIndex: data.index })
        break
      case 'text-start':
        assistantMsg.parts.push({ type: 'text', id: data.id, text: '' })
        break
      case 'text-delta': {
        const textPart = assistantMsg.parts.find((p) => p.id === data.id)
        if (textPart) textPart.text += data.text
        break
      }
      case 'text-end':
        break
      // ---- 思考过程（reasoning / thinking）----
      case 'reasoning-start':
      case 'thinking-start':
        assistantMsg.parts.push({ type: 'reasoning', id: data.id, text: '' })
        break
      case 'reasoning-delta':
      case 'thinking-delta': {
        const rPart = assistantMsg.parts.find((p) => p.type === 'reasoning' && p.id === data.id)
        if (rPart) rPart.text += data.text
        break
      }
      case 'reasoning-end':
      case 'thinking-end':
        break
      case 'tool-input-start':
        assistantMsg.parts.push({
          type: 'tool',
          id: data.id,
          name: data.name,
          input: '',
          output: null,
          state: 'running',
        })
        break
      case 'tool-input-delta': {
        const tp = assistantMsg.parts.find((p) => p.id === data.id)
        if (tp) tp.input += data.text
        break
      }
      case 'tool-call': {
        const tc = assistantMsg.parts.find((p) => p.id === data.id)
        if (tc) {
          tc.input =
            typeof data.input === 'string' ? data.input : JSON.stringify(data.input, null, 2)
          tc.state = 'running'
        }
        break
      }
      case 'tool-result': {
        const tr = assistantMsg.parts.find((p) => p.id === data.id)
        if (tr) {
          tr.output = data.output
          tr.state = 'completed'
        }
        break
      }
      case 'tool-error': {
        const te = assistantMsg.parts.find((p) => p.id === data.id)
        if (te) {
          te.error = data.message
          te.state = 'error'
        }
        break
      }
      case 'step-finish':
        if (data.usage) assistantMsg.usage = data.usage
        break
      case 'finish':
        assistantMsg.streaming = false
        if (streamStartTime) {
          assistantMsg.latency = Date.now() - streamStartTime
          streamStartTime = null
        }
        isStreaming.value = false
        break
      case 'provider-error':
        assistantMsg.error = data.message
        assistantMsg.streaming = false
        isStreaming.value = false
        break
      case 'error':
        assistantMsg.error = data.message
        assistantMsg.streaming = false
        isStreaming.value = false
        break
    }
  }

  function stopStreaming() {
    if (currentCtrl) {
      currentCtrl.abort()
      currentCtrl = null
    }
    isStreaming.value = false
    streamStartTime = null
    // 标记最后一条消息为非流式
    const last = messages.value[messages.value.length - 1]
    if (last && last.streaming) {
      last.streaming = false
    }
  }

  // ============================================================
  // 停止生成：通知后端 + 中断前端 SSE 监听
  // ============================================================
  async function stopGeneration() {
    if (!currentSession.value) return
    try {
      await chatApi.stop(currentSession.value.id)
    } catch (e) {
      console.error('停止生成请求失败:', e)
    }
    // 中断 SSE 监听（复用 stopStreaming 关闭 currentCtrl）
    stopStreaming()
  }

  // ============================================================
  // P0-2: 重新生成（Regenerate）
  // ============================================================

  async function regenerate() {
    if (!currentSession.value || isStreaming.value) return

    // 移除最后一条 assistant 消息（前端乐观删除，后端也会删）
    const lastMsg = messages.value[messages.value.length - 1]
    if (!lastMsg || lastMsg.role !== 'assistant') {
      ElMessage.warning('没有可重新生成的回复')
      return
    }
    messages.value.pop()

    // 插入占位 assistant message
    const assistantMsg = reactive({
      id: 'pending-' + Date.now(),
      role: 'assistant',
      parts: [],
      streaming: true,
    })
    messages.value.push(assistantMsg)
    isStreaming.value = true
    streamStartTime = Date.now()

    const { ctrl } = streamSSE({
      url: `/chat/sessions/${currentSession.value.id}/regenerate`,
      body: {},
      onEvent: (event, data) => handleEvent(event, data, assistantMsg),
      onClose: () => {
        if (assistantMsg.streaming) {
          assistantMsg.streaming = false
        }
        if (streamStartTime) {
          assistantMsg.latency = Date.now() - streamStartTime
          streamStartTime = null
        }
        isStreaming.value = false
        currentCtrl = null
      },
      onError: (err) => {
        console.error('重新生成错误:', err)
        assistantMsg.streaming = false
        assistantMsg.error = (err && err.message) || String(err)
        isStreaming.value = false
        currentCtrl = null
        streamStartTime = null
      },
    })
    currentCtrl = ctrl
  }

  // ============================================================
  // P0-3: 编辑用户消息（删除后续消息后重新发送）
  // ============================================================

  async function editMessage(message, newText) {
    if (!currentSession.value || isStreaming.value) return

    // 找到被编辑消息的索引
    const idx = messages.value.findIndex((m) => m.id === message.id)
    if (idx === -1) return

    // 删除该消息及其后所有消息（前端乐观更新）
    const removed = messages.value.splice(idx)

    // 尝试删除后端对应消息（best-effort，失败不阻塞）
    try {
      for (const m of removed) {
        if (m.id && !m.id.startsWith('local-') && !m.id.startsWith('pending-')) {
          await chatApi.deleteMessage(currentSession.value.id, m.id)
        }
      }
    } catch (e) {
      console.warn('删除后端消息失败（不阻塞）:', e)
    }

    // 重新发送编辑后的内容
    await sendMessage(newText)
  }

  // ============================================================
  // P0-6: 错误重试（删除错误消息后重新生成）
  // ============================================================

  async function retry() {
    if (!currentSession.value || isStreaming.value) return

    const lastMsg = messages.value[messages.value.length - 1]
    if (!lastMsg || !lastMsg.error) {
      ElMessage.warning('没有可重试的消息')
      return
    }

    // 删除错误消息
    messages.value.pop()

    // 如果最后一条是 assistant 错误消息，直接重新生成
    // 如果最后一条是 user 消息（assistant 从未创建），重新发送
    const prevMsg = messages.value[messages.value.length - 1]
    if (prevMsg && prevMsg.role === 'user') {
      // 取 user 消息文本重新发送
      const textPart = prevMsg.parts?.find((p) => p.type === 'text')
      const text = textPart?.text || ''
      if (text) {
        messages.value.pop() // 移除 user 消息（sendMessage 会重新加）
        await sendMessage(text)
      }
    } else {
      await regenerate()
    }
  }

  // ============================================================
  // P0-8: 点赞/点踩反馈
  // ============================================================

  async function sendFeedback(message, rating) {
    if (!currentSession.value) return
    try {
      await chatApi.sendFeedback(currentSession.value.id, message.id, { rating })
      // 更新本地状态
      const msg = messages.value.find((m) => m.id === message.id)
      if (msg) msg.feedback = rating
      ElMessage.success(rating === 'like' ? '已点赞' : '已点踩')
    } catch (e) {
      console.error('反馈失败:', e)
      ElMessage.error('反馈提交失败')
    }
  }

  // ============================================================
  // P0-5: 自动生成标题
  // ============================================================

  async function _maybeAutoTitle() {
    if (!currentSession.value) return
    // 仅当标题是默认值"新对话"且消息数 >= 2 时触发
    if (currentSession.value.title !== '新对话') return
    if (messages.value.length < 2) return
    try {
      const result = await chatApi.autoTitle(currentSession.value.id)
      if (result && result.title) {
        currentSession.value.title = result.title
        // 同步更新会话列表
        const s = sessions.value.find((s) => s.id === currentSession.value.id)
        if (s) s.title = result.title
      }
    } catch (e) {
      console.warn('自动标题生成失败:', e)
    }
  }

  async function renameSession(id, title) {
    try {
      const updated = await chatApi.updateSession(id, { title })
      const s = sessions.value.find((s) => s.id === id)
      if (s) s.title = updated.title || title
      if (currentSession.value && currentSession.value.id === id) {
        currentSession.value.title = updated.title || title
      }
      ElMessage.success('已重命名')
    } catch (e) {
      console.error('重命名失败:', e)
      ElMessage.error('重命名失败')
    }
  }

  // ============================================================
  // P0-10: 会话搜索
  // ============================================================

  async function searchSessions(query) {
    if (!query || !query.trim()) {
      searchResults.value = []
      return
    }
    try {
      searchResults.value = await chatApi.searchSessions(query.trim())
    } catch (e) {
      console.error('搜索失败:', e)
      searchResults.value = []
    }
  }

  // ============================================================
  // P0-10: 导出会话
  // ============================================================

  function exportSession() {
    if (!currentSession.value || messages.value.length === 0) {
      ElMessage.warning('没有可导出的对话')
      return
    }
    const lines = []
    lines.push(`# ${currentSession.value.title || '对话导出'}`)
    lines.push('')
    lines.push(`> 导出时间：${new Date().toLocaleString()}`)
    lines.push(`> 会话 ID：${currentSession.value.id}`)
    lines.push('')
    for (const m of messages.value) {
      const role = m.role === 'user' ? '👤 用户' : '🤖 AI 助手'
      lines.push(`## ${role}`)
      lines.push('')
      const textPart = m.parts?.find((p) => p.type === 'text')
      if (textPart?.text) {
        lines.push(textPart.text)
      }
      if (m.error) {
        lines.push(`> ⚠️ 错误：${m.error}`)
      }
      if (m.usage) {
        lines.push(`> 📊 Token：${m.usage.total_tokens || 0}`)
      }
      lines.push('')
    }
    const content = lines.join('\n')
    const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${currentSession.value.title || '对话'}.md`
    a.click()
    URL.revokeObjectURL(url)
    ElMessage.success('已导出为 Markdown')
  }

  // ============================================================
  // 计算属性
  // ============================================================

  const hasSession = computed(() => currentSession.value !== null)
  const messageCount = computed(() => messages.value.length)
  // 显示的会话列表（搜索时本地过滤，即时响应无需网络请求）
  const displaySessions = computed(() => {
    const q = searchQuery.value.trim().toLowerCase()
    if (!q) return sessions.value
    return sessions.value.filter((s) => (s.title || '').toLowerCase().includes(q))
  })

  return {
    sessions,
    currentSession,
    messages,
    isStreaming,
    loading,
    hasSession,
    messageCount,
    searchQuery,
    searchResults,
    displaySessions,
    loadSessions,
    selectSession,
    createSession,
    deleteSession,
    sendMessage,
    stopStreaming,
    stopGeneration,
    // P0 新增
    regenerate,
    editMessage,
    retry,
    sendFeedback,
    renameSession,
    searchSessions,
    exportSession,
  }
})
