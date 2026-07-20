import { defineStore } from 'pinia'
import { ref } from 'vue'
import { ElNotification } from 'element-plus'
import { managerApi, hrApi } from '@/api/client'

// 审批待办轮询间隔:审批非秒级敏感,45s 平衡响应性与服务器负载
const POLL_INTERVAL = 45000

let pollTimer = null

export const useNotificationStore = defineStore('notification', () => {
  const pendingCount = ref(0)

  // 按角色拉取待审批计数:manager 看直属下属 pending,hr 看 audit 队列,admin 两者都看
  async function fetchPendingCount(role) {
    let count = 0
    if (role === 'manager' || role === 'admin') {
      const data = await managerApi.dashboard()
      count += data.pending_count || 0
    }
    if (role === 'hr' || role === 'admin') {
      const data = await hrApi.auditQueue()
      count += (data.pending || []).length
    }
    return count
  }

  function startPolling(role) {
    stopPolling()
    if (!['manager', 'hr', 'admin'].includes(role)) {
      return
    }
    // 立即拉一次,不等首个间隔
    pollOnce(role)
    pollTimer = setInterval(() => pollOnce(role), POLL_INTERVAL)
  }

  async function pollOnce(role) {
    try {
      const next = await fetchPendingCount(role)
      // 0 → 非 0 时弹一次通知,让主管/HR 主动感知有新待办
      if (pendingCount.value === 0 && next > 0) {
        ElNotification({
          title: '新的待审批',
          message: `你有 ${next} 项评估待处理`,
          type: 'warning',
          duration: 6000,
        })
      }
      pendingCount.value = next
    } catch {
      // 轮询失败静默,下个周期重试,不打断用户
    }
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
    pendingCount.value = 0
  }

  return { pendingCount, startPolling, stopPolling }
})
