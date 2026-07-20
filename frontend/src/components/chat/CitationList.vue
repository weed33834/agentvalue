<script setup>
import { ref, watchEffect } from 'vue'
import { evidenceApi } from '@/api/client'

const props = defineProps({
  evaluationId: { type: String, default: '' },
})

const groups = ref([])
const loading = ref(false)

watchEffect(async () => {
  if (!props.evaluationId) return
  loading.value = true
  try {
    groups.value = await evidenceApi.list(props.evaluationId)
  } catch (e) {
    console.error('加载引用失败:', e)
    groups.value = []
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="citation-list" v-loading="loading">
    <div v-if="groups.length === 0 && !loading" class="empty">
      <el-icon><Document /></el-icon>
      <span>暂无引用数据</span>
    </div>
    <div v-for="g in groups" :key="g.dimension" class="citation-group">
      <div class="group-header">
        <el-tag type="primary" size="small">{{ g.dimension }}</el-tag>
        <span v-if="g.score !== null && g.score !== undefined" class="score">
          得分: {{ g.score }}
        </span>
      </div>
      <div v-for="(item, i) in g.items" :key="i" class="citation-item">
        <el-tag size="small" type="info">[{{ i + 1 }}]</el-tag>
        <span class="evidence-text">{{ item.evidence_text }}</span>
        <div v-if="item.raw_input" class="raw-source">
          <el-icon><Link /></el-icon>
          <span>来源: {{ item.raw_input.source || '未知' }}</span>
          <span v-if="item.raw_input.period">| 周期: {{ item.raw_input.period }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.citation-list {
  padding: 12px;
}
.empty {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--el-text-color-secondary);
  justify-content: center;
  padding: 24px;
}
.citation-group {
  margin-bottom: 16px;
  padding: 12px;
  background: var(--el-fill-color-light);
  border-radius: 8px;
}
.group-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.score {
  font-size: 13px;
  color: var(--el-color-success);
  font-weight: 600;
}
.citation-item {
  padding: 8px 0;
  border-top: 1px dashed var(--el-border-color-lighter);
}
.citation-item:first-of-type {
  border-top: none;
}
.evidence-text {
  margin-left: 8px;
  font-size: 14px;
  line-height: 1.6;
}
.raw-source {
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  display: flex;
  align-items: center;
  gap: 4px;
}
</style>
