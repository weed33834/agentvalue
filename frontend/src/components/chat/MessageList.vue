<script setup>
import { ref, watch, nextTick } from 'vue'
import MessageBubble from './MessageBubble.vue'

const props = defineProps({
  messages: { type: Array, required: true },
  isStreaming: { type: Boolean, default: false },
})

defineEmits(['regenerate', 'edit', 'retry', 'feedback', 'fork'])

const container = ref(null)

// 消息变化时自动滚动到底部
watch(
  () => props.messages.length,
  async () => {
    await nextTick()
    if (container.value) {
      container.value.scrollTop = container.value.scrollHeight
    }
  },
  { flush: 'post' }
)

// 深度监听：流式内容更新时也滚动
watch(
  () => props.messages.map((m) => (m.parts ? m.parts.length : 0)).join(','),
  async () => {
    await nextTick()
    if (container.value) {
      container.value.scrollTop = container.value.scrollHeight
    }
  }
)
</script>

<template>
  <div ref="container" class="message-list">
    <div v-if="messages.length === 0" class="empty-state">
      <el-icon size="48"><ChatRound /></el-icon>
      <p>开始一段新对话</p>
      <p class="hint">在下方输入框中输入消息，与 AI 助手对话</p>
    </div>
    <MessageBubble
      v-for="(m, idx) in messages"
      :key="m.id"
      :message="m"
      :is-last="idx === messages.length - 1"
      :is-streaming="isStreaming"
      @regenerate="$emit('regenerate')"
      @edit="(msg, text) => $emit('edit', msg, text)"
      @retry="$emit('retry')"
      @feedback="(msg, rating) => $emit('feedback', msg, rating)"
      @fork="(msg) => $emit('fork', msg)"
    />
  </div>
</template>

<style scoped>
.message-list {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.empty-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: var(--el-text-color-secondary);
  text-align: center;
}
.empty-state .el-icon {
  margin-bottom: 12px;
  color: var(--el-color-info-light-5);
}
.empty-state p {
  margin: 4px 0;
}
.empty-state .hint {
  font-size: 13px;
  opacity: 0.7;
}
</style>
