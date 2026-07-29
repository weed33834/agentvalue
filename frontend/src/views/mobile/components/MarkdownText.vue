<template>
  <div class="md-text" v-html="html"></div>
</template>

<script setup>
import { computed } from 'vue'
import { renderMarkdown } from '@/utils/markdown'

const props = defineProps({
  text: { type: String, default: '' },
})

// 复用全局 renderMarkdown（已集成 marked + DOMPurify  sanitize）
const html = computed(() => renderMarkdown(props.text || ''))
</script>

<style scoped>
.md-text {
  line-height: 1.65;
  word-break: break-word;
  font-size: 15px;
}
.md-text :deep(p) {
  margin: 0 0 8px;
}
.md-text :deep(p:last-child) {
  margin-bottom: 0;
}
.md-text :deep(pre) {
  background: #0f172a;
  color: #e2e8f0;
  padding: 12px;
  border-radius: 8px;
  overflow-x: auto;
  font-size: 13px;
}
.md-text :deep(code) {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px;
}
.md-text :deep(:not(pre) > code) {
  background: var(--el-fill-color-light);
  padding: 1px 5px;
  border-radius: 4px;
}
.md-text :deep(ul),
.md-text :deep(ol) {
  padding-left: 20px;
  margin: 0 0 8px;
}
.md-text :deep(h1),
.md-text :deep(h2),
.md-text :deep(h3) {
  margin: 12px 0 6px;
  font-size: 15px;
}
.md-text :deep(table) {
  border-collapse: collapse;
  width: 100%;
  font-size: 12px;
}
.md-text :deep(th),
.md-text :deep(td) {
  border: 1px solid var(--el-border-color-lighter);
  padding: 4px 6px;
  text-align: left;
}
</style>
