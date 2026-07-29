<template>
  <div class="m-chat">
    <div ref="scrollEl" class="m-chat__msgs">
      <div v-if="chat.messages.length === 0 && !bootError" class="m-empty">
        和 AI 助手开始对话吧 👋
      </div>
      <div v-if="bootError" class="m-chat__boot-error">{{ bootError }}</div>

      <div
        v-for="msg in chat.messages"
        :key="msg.id"
        class="m-msg"
        :class="msg.role === 'user' ? 'm-msg--user' : 'm-msg--ai'"
      >
        <div class="m-msg__bubble">
          <!-- AI 回复：多 part（文本 / 思考 / 工具调用） -->
          <template v-if="msg.role === 'assistant'">
            <template v-for="(part, i) in msg.parts" :key="i">
              <details v-if="part.type === 'reasoning'" class="m-reasoning" open>
                <summary>💡 思考过程</summary>
                <div class="m-reasoning__body">{{ part.text }}</div>
              </details>
              <div v-else-if="part.type === 'text'" class="m-md">
                <MarkdownText :text="part.text" />
              </div>
              <div v-else-if="part.type === 'tool'" class="m-tool">
                <span class="m-tool__icon">🔧</span>
                <span class="m-tool__name">{{ part.name }}</span>
                <span class="m-tool__state" :class="'is-' + (part.state || 'completed')">
                  {{ toolStateLabel(part.state) }}
                </span>
              </div>
            </template>
            <div v-if="msg.streaming" class="m-typing" aria-label="正在生成">
              <span></span><span></span><span></span>
            </div>
            <div v-if="msg.error" class="m-msg__error">⚠️ {{ msg.error }}</div>
          </template>

          <!-- 用户消息 -->
          <template v-else>
            <div v-for="(part, i) in msg.parts" :key="i">
              <div v-if="part.type === 'text'" class="m-user-text">{{ part.text }}</div>
              <div v-else-if="part.type === 'image'" class="m-user-attach">🖼️ {{ part.metadata?.name || '图片' }}</div>
              <div v-else-if="part.type === 'file'" class="m-user-attach">📎 {{ part.metadata?.name || '文件' }}</div>
            </div>
          </template>
        </div>
      </div>
    </div>

    <!-- 输入栏（固定在内容区底部，底栏之上） -->
    <div class="m-chat__input">
      <textarea
        v-model="draft"
        class="m-chat__textarea"
        rows="1"
        placeholder="输入消息，Enter 发送"
        @keydown.enter.exact.prevent="send"
      ></textarea>
      <button
        v-if="chat.isStreaming"
        class="m-chat__btn m-chat__btn--stop"
        aria-label="停止生成"
        @click="chat.stopGeneration()"
      >
        ■
      </button>
      <button
        v-else
        class="m-chat__btn m-chat__btn--send"
        :disabled="!draft.trim()"
        aria-label="发送"
        @click="send"
      >
        ↑
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, nextTick, watch } from 'vue'
import { useChatStore } from '@/stores/chat'
import MarkdownText from '@/views/mobile/components/MarkdownText.vue'

defineProps({
  role: { type: String, default: '' },
})

const chat = useChatStore()
const draft = ref('')
const scrollEl = ref(null)
const bootError = ref('')

function toolStateLabel(state) {
  return { running: '执行中', completed: '完成', error: '失败' }[state] || state || '完成'
}

async function ensureSession() {
  try {
    await chat.loadSessions()
    if (chat.sessions.length) {
      await chat.selectSession(chat.sessions[0].id)
    } else {
      await chat.createSession({ title: '移动端对话' })
    }
  } catch (e) {
    bootError.value = '会话初始化失败：' + (e?.message || '请确认后端服务已启动')
  }
}

async function send() {
  const text = draft.value.trim()
  if (!text || chat.isStreaming) return
  draft.value = ''
  try {
    await chat.sendMessage(text)
  } catch (e) {
    bootError.value = '发送失败：' + (e?.message || e)
  }
}

function scrollToBottom() {
  nextTick(() => {
    if (scrollEl.value) scrollEl.value.scrollTop = scrollEl.value.scrollHeight
  })
}

watch(() => chat.messages.length, scrollToBottom)
watch(
  () => chat.messages.map((m) => m.streaming),
  scrollToBottom,
  { deep: true },
)

onMounted(async () => {
  await ensureSession()
  scrollToBottom()
})
</script>

<style scoped>
.m-chat {
  display: flex;
  flex-direction: column;
  height: 100%;
  box-sizing: border-box;
  /* 抵消 MobileLayout .m-content 的 padding，使聊天区铺满 */
  margin: -12px;
  padding: 12px 12px 0;
}
.m-chat__msgs {
  flex: 1 1 auto;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  min-height: 0;
  padding-bottom: 8px;
}
.m-msg {
  display: flex;
  margin-bottom: 12px;
}
.m-msg--user {
  justify-content: flex-end;
}
.m-msg--ai {
  justify-content: flex-start;
}
.m-msg__bubble {
  max-width: 85%;
  padding: 10px 14px;
  border-radius: 16px;
  font-size: 15px;
  line-height: 1.6;
  word-break: break-word;
}
.m-msg--user .m-msg__bubble {
  background: var(--el-color-primary);
  color: #fff;
  border-bottom-right-radius: 4px;
}
.m-msg--ai .m-msg__bubble {
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-bottom-left-radius: 4px;
}
.m-user-text {
  white-space: pre-wrap;
}
.m-user-attach {
  font-size: 13px;
  opacity: 0.9;
}
.m-reasoning {
  margin-bottom: 8px;
  border-left: 3px solid var(--el-color-primary-light-5);
  padding-left: 8px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.m-reasoning summary {
  cursor: pointer;
  user-select: none;
}
.m-reasoning__body {
  margin-top: 4px;
  white-space: pre-wrap;
}
.m-tool {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
  padding: 6px 10px;
  background: var(--el-fill-color-light);
  border-radius: 8px;
  font-size: 12px;
}
.m-tool__state {
  margin-left: auto;
  color: var(--el-text-color-placeholder);
}
.m-tool__state.is-running {
  color: var(--el-color-primary);
}
.m-tool__state.is-error {
  color: #ef4444;
}
.m-msg__error {
  color: #ef4444;
  font-size: 13px;
  margin-top: 4px;
}
.m-chat__boot-error {
  color: #ef4444;
  font-size: 14px;
  text-align: center;
  padding: 24px 16px;
}
.m-typing {
  display: inline-flex;
  gap: 4px;
  padding: 4px 0;
}
.m-typing span {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--el-text-color-placeholder);
  animation: m-bounce 1.2s infinite ease-in-out;
}
.m-typing span:nth-child(2) {
  animation-delay: 0.2s;
}
.m-typing span:nth-child(3) {
  animation-delay: 0.4s;
}
@keyframes m-bounce {
  0%, 60%, 100% { transform: translateY(0); opacity: 0.4; }
  30% { transform: translateY(-4px); opacity: 1; }
}

.m-chat__input {
  flex: 0 0 auto;
  display: flex;
  align-items: flex-end;
  gap: 8px;
  padding: 10px 0 calc(10px + env(safe-area-inset-bottom));
  border-top: 1px solid var(--el-border-color-lighter);
  background: var(--el-bg-color);
}
.m-chat__textarea {
  flex: 1;
  max-height: 120px;
  resize: none;
  padding: 10px 12px;
  border: 1px solid var(--el-border-color);
  border-radius: 18px;
  font-size: 15px;
  font-family: inherit;
  line-height: 1.4;
  background: var(--el-bg-color-blank);
  color: var(--el-text-color-primary);
  box-sizing: border-box;
}
.m-chat__textarea:focus {
  outline: none;
  border-color: var(--el-color-primary);
}
.m-chat__btn {
  flex: 0 0 auto;
  width: 40px;
  height: 40px;
  border: none;
  border-radius: 50%;
  font-size: 18px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}
.m-chat__btn--send {
  background: var(--el-color-primary);
  color: #fff;
}
.m-chat__btn--send:disabled {
  background: var(--el-border-color);
  color: var(--el-text-color-placeholder);
  cursor: not-allowed;
}
.m-chat__btn--stop {
  background: var(--el-fill-color);
  color: var(--el-text-color-regular);
}
</style>
