import { defineStore } from 'pinia'
import { ref } from 'vue'
import { ElNotification } from 'element-plus'
import { managerApi, hrApi, notificationApi } from '@/api/client'

// 审批待办轮询间隔:审批非秒级敏感,45s 平衡响应性与服务器负载
const POLL_INTERVAL = 45000

let pollTimer = null

export const useNotificationStore = defineStore('notification', () => {
  const pendingCount = ref(0)
  const unreadCount = ref(0)
  const notifications = ref([])
  const notificationDrawerVisible = ref(false)

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

  // 从后端通知系统获取未读通知数
  async function fetchUnreadCount() {
    try {
      const data = await notificationApi.unreadCount()
      unreadCount.value = data.unread_count || 0
    } catch {
      // 静默失败，不影响审批待办轮询
    }
  }

  // 获取通知列表
  async function fetchNotifications(params = {}) {
    try {
      const data = await notificationApi.list(params)
      notifications.value = data.items || []
      return data
    } catch {
      notifications.value = []
      return { items: [], total: 0 }
    }
  }

  // 标记单条已读
  async function markAsRead(notificationId) {
    try {
      await notificationApi.markRead(notificationId)
      const item = notifications.value.find((n) => n.notification_id === notificationId)
      if (item) item.is_read = true
      if (unreadCount.value > 0) unreadCount.value--
    } catch {
      // 静默
    }
  }

  // 全部标记已读
  async function markAllAsRead() {
    try {
      await notificationApi.markAllRead()
      notifications.value.forEach((n) => (n.is_read = true))
      unreadCount.value = 0
    } catch {
      // 静默
    }
  }

  function startPolling(role) {
    stopPolling()
    if (!['manager', 'hr', 'admin'].includes(role)) {
      // 非管理角色仍轮询通知未读数
      fetchUnreadCount()
      pollTimer = setInterval(() => fetchUnreadCount(), POLL_INTERVAL)
      return
    }
    // 立即拉一次,不等首个间隔
    pollOnce(role)
    pollTimer = setInterval(() => pollOnce(role), POLL_INTERVAL)
  }

  async function pollOnce(role) {
    try {
      // 并发获取审批待办 + 通知未读数
      const tasks = [fetchUnreadCount()]
      if (['manager', 'hr', 'admin'].includes(role)) {
        tasks.push(fetchPendingCount(role))
      }
      const results = await Promise.allSettled(tasks)
      // 审批待办（如果请求了）
      if (results.length > 1 && results[1].status === 'fulfilled') {
        const next = results[1].value
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
      }
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
    unreadCount.value = 0
  }

  function openNotificationDrawer() {
    notificationDrawerVisible.value = true
    fetchNotifications({ page: 1, page_size: 20 })
  }

  return {
    pendingCount,
    unreadCount,
    notifications,
    notificationDrawerVisible,
    fetchUnreadCount,
    fetchNotifications,
    markAsRead,
    markAllAsRead,
    startPolling,
    stopPolling,
    openNotificationDrawer,
  }
})
