/**
 * 通用 SSE 流式客户端
 *
 * 抽取自 AdminPlayground.vue 的 fetchEventSource 调用，供 chat store 与 playground 共用。
 * 移植自 opencode 的 /event SSE 订阅模式（packages/opencode/src/server/routes/instance/httpapi/handlers/event.ts）
 *
 * 特性：
 * - 自动附加 JWT Bearer token 或演示模式 header（复用 auth store）
 * - 支持 AbortController 中断
 * - onEvent 回调按事件名分发
 * - 自动心跳过滤（ping 事件忽略）
 */

import { fetchEventSource } from '@microsoft/fetch-event-source'
import { useAuthStore } from '@/stores/auth'
import { isDemoAuthEnabled } from '@/utils/auth'

const API_BASE = import.meta.env.VITE_API_BASE_URL || '/api/v1'

/**
 * 发起 SSE 流式请求
 * @param {Object} options
 * @param {string} options.url - 请求路径（如 /chat/sessions/xxx/messages），会拼接 API_BASE
 * @param {Object} options.body - POST 请求体
 * @param {Function} options.onEvent - (eventName, data) => void
 * @param {Function} [options.onClose] - 流正常关闭回调
 * @param {Function} [options.onError] - 错误回调
 * @param {AbortSignal} [options.signal] - 外部传入的 AbortSignal
 * @returns {{ ctrl: AbortController, promise: Promise<void> }}
 */
export function streamSSE({ url, body, onEvent, onClose, onError, signal }) {
  const auth = useAuthStore()
  const ctrl = signal ? null : new AbortController()
  const abortSignal = signal || ctrl.signal

  const fullUrl = url.startsWith('http') ? url : `${API_BASE}${url}`
  const headers = { 'Content-Type': 'application/json' }

  // 鉴权：JWT 模式附加 Bearer token，演示模式附加 header
  if (auth.useJwt && auth.token) {
    headers['Authorization'] = `Bearer ${auth.token}`
  } else if (isDemoAuthEnabled()) {
    if (auth.role) headers['x-user-role'] = auth.role
    if (auth.userId) headers['x-user-id'] = auth.userId
  }

  const promise = fetchEventSource(fullUrl, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
    signal: abortSignal,
    openWhenHidden: true,
    onopen: async (resp) => {
      if (!resp.ok) {
        const text = await resp.text().catch(() => '')
        throw new Error(`HTTP ${resp.status}: ${text || resp.statusText}`)
      }
    },
    onmessage: (ev) => {
      // ping 心跳事件忽略
      if (ev.event === 'ping' || !ev.event) return
      let data
      try {
        data = ev.data ? JSON.parse(ev.data) : {}
      } catch {
        data = { raw: ev.data }
      }
      if (onEvent) onEvent(ev.event, data)
    },
    onerror: (err) => {
      if (onError) onError(err)
      // 抛出错误以阻止 fetchEventSource 自动重连
      throw err
    },
    onclose: () => {
      if (onClose) onClose()
    },
  })

  return {
    ctrl: ctrl || { abort: () => {} },
    promise,
  }
}
