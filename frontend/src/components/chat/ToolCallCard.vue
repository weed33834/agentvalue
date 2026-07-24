<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'

const props = defineProps({
  tool: { type: Object, required: true },
})

const inputExpanded = ref(true)
const outputExpanded = ref(true)
const fullOutput = ref(false)

const statusInfo = computed(() => {
  const map = {
    running: { type: 'warning', text: '执行中', spin: true },
    completed: { type: 'success', text: '完成', spin: false },
    error: { type: 'danger', text: '失败', spin: false },
  }
  return map[props.tool.state] || { type: 'info', text: props.tool.state, spin: false }
})

const formattedInput = computed(() => {
  const input = props.tool.input
  if (!input) return ''
  if (typeof input === 'string') {
    try {
      const parsed = JSON.parse(input)
      return JSON.stringify(parsed, null, 2)
    } catch {
      return input
    }
  }
  try {
    return JSON.stringify(input, null, 2)
  } catch {
    return String(input)
  }
})

const formattedOutput = computed(() => {
  const output = props.tool.output
  if (!output) return ''
  if (typeof output === 'string') return output
  try {
    return JSON.stringify(output, null, 2)
  } catch {
    return String(output)
  }
})

const truncatedOutput = computed(() => {
  const text = formattedOutput.value
  if (!fullOutput.value && text.length > 2000) {
    return text.slice(0, 2000) + '\n... [点击展开查看更多]'
  }
  return text
})

const isJsonOutput = computed(() => {
  const text = formattedOutput.value
  try {
    JSON.parse(text)
    return true
  } catch {
    return false
  }
})

function copyInput() {
  navigator.clipboard.writeText(formattedInput.value)
  ElMessage.success('已复制参数')
}

function copyOutput() {
  navigator.clipboard.writeText(formattedOutput.value)
  ElMessage.success('已复制结果')
}

function toggleFullOutput() {
  fullOutput.value = !fullOutput.value
}
</script>

<template>
  <div class="tool-card" :class="`state-${tool.state}`">
    <!-- Tool header -->
    <div class="tool-header" @click="outputExpanded = !outputExpanded">
      <div class="tool-badge" :class="`badge-${statusInfo.type}`">
        <el-icon :class="{ spinning: statusInfo.spin }">
          <Loading v-if="statusInfo.spin" />
          <CircleCheck v-else-if="tool.state === 'completed'" />
          <CircleClose v-else-if="tool.state === 'error'" />
          <Tools v-else />
        </el-icon>
      </div>
      <el-tooltip :content="tool.name + ' 工具'" placement="top">
        <span class="tool-name">{{ tool.name }}</span>
      </el-tooltip>
      <el-tag :type="statusInfo.type" size="small" effect="light" round>
        {{ statusInfo.text }}
      </el-tag>
      <el-icon class="toggle-icon" :class="{ rotated: outputExpanded }">
        <ArrowDown />
      </el-icon>
    </div>

    <!-- Input section -->
    <div v-if="formattedInput" v-show="inputExpanded" class="tool-section">
      <div class="section-label">
        <span>参数</span>
        <el-button size="small" text @click.stop="copyInput">
          <el-icon><CopyDocument /></el-icon>
        </el-button>
      </div>
      <pre class="code-block input-block">{{ formattedInput }}</pre>
    </div>

    <!-- Output section -->
    <div v-if="formattedOutput || tool.error" v-show="outputExpanded" class="tool-section">
      <div class="section-label">
        <span>{{ tool.error ? '错误' : '结果' }}</span>
        <div class="section-actions">
          <el-button
            v-if="formattedOutput.length > 2000"
            size="small"
            text
            @click.stop="toggleFullOutput"
          >
            {{ fullOutput ? '收起' : '展开全部' }}
          </el-button>
          <el-button v-if="formattedOutput" size="small" text @click.stop="copyOutput">
            <el-icon><CopyDocument /></el-icon>
          </el-button>
        </div>
      </div>
      <pre
        v-if="formattedOutput"
        class="code-block output-block"
        :class="{ 'json-output': isJsonOutput }"
        >{{ truncatedOutput }}</pre>
      <div v-if="tool.error" class="tool-error">
        <el-icon><WarningFilled /></el-icon>
        <span>{{ tool.error }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tool-card {
  margin: 8px 0;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;
  overflow: hidden;
  background: var(--el-fill-color-lighter);
  transition: border-color 0.2s;
}
.tool-card:hover {
  border-color: var(--el-color-primary-light-5);
}
.tool-card.state-error {
  border-color: var(--el-color-danger-light-5);
}
.tool-card.state-running {
  border-color: var(--el-color-warning-light-5);
}

.tool-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  cursor: pointer;
  user-select: none;
  background: var(--el-fill-color);
  transition: background 0.2s;
}
.tool-header:hover {
  background: var(--el-fill-color-dark);
}
.tool-badge {
  width: 24px;
  height: 24px;
  border-radius: 6px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  font-size: 14px;
}
.badge-success {
  background: var(--el-color-success-light-9);
  color: var(--el-color-success);
}
.badge-warning {
  background: var(--el-color-warning-light-9);
  color: var(--el-color-warning);
}
.badge-danger {
  background: var(--el-color-danger-light-9);
  color: var(--el-color-danger);
}
.badge-info {
  background: var(--el-color-info-light-9);
  color: var(--el-color-info);
}

.spinning {
  animation: spin 1s linear infinite;
}
@keyframes spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

.tool-name {
  font-weight: 600;
  font-size: 13px;
  color: var(--el-text-color-primary);
}

.toggle-icon {
  margin-left: auto;
  transition: transform 0.2s;
  color: var(--el-text-color-secondary);
}
.toggle-icon.rotated {
  transform: rotate(180deg);
}

.tool-section {
  padding: 0 12px 8px;
}
.section-label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 11px;
  color: var(--el-text-color-secondary);
  margin-bottom: 4px;
  margin-top: 4px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.section-actions {
  display: flex;
  gap: 4px;
}

.code-block {
  background: #1e1e2e;
  color: #cdd6f4;
  padding: 10px 14px;
  border-radius: 8px;
  font-size: 12px;
  font-family: 'JetBrains Mono', 'Fira Code', 'Consolas', 'Monaco', monospace;
  overflow-x: auto;
  margin: 0;
  max-height: 300px;
  overflow-y: auto;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
}
.output-block {
  background: #0d1117;
  color: #7ee787;
  border: 1px solid #21262d;
}
.json-output {
  color: #79c0ff;
}

.tool-error {
  color: var(--el-color-danger);
  font-size: 12px;
  margin-top: 4px;
  display: flex;
  align-items: flex-start;
  gap: 4px;
  padding: 8px 12px;
  background: var(--el-color-danger-light-9);
  border-radius: 6px;
}
</style>
