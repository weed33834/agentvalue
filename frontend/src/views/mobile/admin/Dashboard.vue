<template>
  <div class="m-page">
    <!-- 顶部模型状态条（移动端最实用的概览信息，无需进桌面端） -->
    <div class="m-card" style="margin-bottom: 12px">
      <div class="m-card__title">系统状态</div>
      <div class="m-stat-grid" style="margin-bottom: 0">
        <div class="m-stat">
          <div class="m-stat__value" style="font-size: 18px">
            <span class="m-dot" :class="statusClass"></span>
            {{ statusText }}
          </div>
          <div class="m-stat__label">{{ modelName || '模型服务' }}</div>
        </div>
        <div class="m-stat">
          <div class="m-stat__value" style="font-size: 18px">{{ uptimeText }}</div>
          <div class="m-stat__label">运行时长</div>
        </div>
      </div>
    </div>

    <!-- 功能中心：分类卡片，逐张引导回桌面端对应功能 -->
    <template v-for="cat in categories" :key="cat.title">
      <p class="m-section-title">{{ cat.title }}</p>
      <div class="m-list">
        <div
          v-for="f in cat.items"
          :key="f.path"
          class="m-list-item"
          @click="goDesktop(f.path)"
        >
          <div style="flex: 1">
            <div class="m-list-item__title">{{ f.icon }} {{ f.label }}</div>
            <div class="m-list-item__sub">{{ f.desc }}</div>
          </div>
          <span class="m-badge m-badge--desktop">桌面端</span>
          <span class="m-list-item__arrow">›</span>
        </div>
      </div>
    </template>

    <p class="m-foot-hint">
      管理端功能以桌面端为主，移动端提供状态概览与快捷入口。
    </p>
  </div>
</template>

<script setup>
import { ref, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import { adminApi } from '@/api/client'

const router = useRouter()

// 模型状态（防御性加载，失败不影响页面其余部分）
const status = ref(null)
const statusText = computed(() => {
  if (status.value === null) return '检测中…'
  return status.value?.online ? '在线' : '离线'
})
const statusClass = computed(() => (status.value?.online ? 'm-dot--ok' : 'm-dot--off'))
const modelName = computed(() => status.value?.model || status.value?.name || '')
const uptimeText = computed(() => {
  const u = status.value?.uptime
  if (!u && u !== 0) return '—'
  const h = Math.floor(u / 3600)
  const m = Math.floor((u % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m`
})

// 功能中心：按桌面端真实路由分组，点击跳桌面端对应功能（?desktop=1 强制桌面视图）
const categories = [
  {
    title: '模型与推理',
    items: [
      { label: '模型管理', desc: '切换模型 tier / 查看在线状态', icon: '🧠', path: '/admin' },
      { label: 'LLM 配置', desc: 'API Key / base_url / 模型名', icon: '🔑', path: '/admin/llm-config' },
      { label: '模型运维', desc: '部署与灰度运维', icon: '🛠️', path: '/admin/model-ops' },
    ],
  },
  {
    title: '内容与提示词',
    items: [
      { label: 'Prompt 管理', desc: '模板/版本/灰度/回滚', icon: '📝', path: '/admin/prompts' },
      { label: '知识库', desc: '文档检索与知识运维', icon: '📚', path: '/admin/knowledge-base' },
      { label: 'Agent 模板', desc: '预设 Agent 模板管理', icon: '🤖', path: '/admin/agent-templates' },
    ],
  },
  {
    title: '工具与集成',
    items: [
      { label: '工具管理', desc: '内置/toolkit/MCP 服务器', icon: '🧩', path: '/admin/tools' },
      { label: 'API 密钥', desc: '租户级密钥管理', icon: '🔐', path: '/admin/api-keys' },
      { label: '特性开关', desc: '功能灰度开关', icon: '🎚️', path: '/admin/feature-flags' },
    ],
  },
  {
    title: '运维与合规',
    items: [
      { label: '审计日志', desc: '操作审计追溯', icon: '🧾', path: '/admin/audit-logs' },
      { label: '系统指标', desc: '关键指标看板', icon: '📈', path: '/admin/metrics' },
      { label: '告警', desc: '告警规则与通知', icon: '🚨', path: '/admin/alerts' },
      { label: '调试', desc: 'Trace 链路与排障', icon: '🐞', path: '/admin/debug' },
    ],
  },
]

function goDesktop(path) {
  router.push(path + '?desktop=1')
}

async function loadStatus() {
  try {
    const res = await adminApi.modelStatus()
    status.value = res || {}
  } catch {
    // 状态检测失败不阻断页面，其余功能入口仍可点击
    status.value = { online: false }
  }
}

onMounted(loadStatus)
</script>

<style scoped>
.m-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 6px;
  vertical-align: middle;
}
.m-dot--ok {
  background: #22c55e;
  box-shadow: 0 0 0 3px rgba(34, 197, 94, 0.18);
}
.m-dot--off {
  background: #ef4444;
  box-shadow: 0 0 0 3px rgba(239, 68, 68, 0.18);
}
.m-badge--desktop {
  background: var(--el-fill-color-light);
  color: var(--el-text-color-secondary);
}
.m-foot-hint {
  text-align: center;
  font-size: 12px;
  color: var(--el-text-color-placeholder);
  margin: 18px 8px 8px;
  line-height: 1.6;
}
</style>
