/**
 * Theme Store
 *
 * 管理暗色/亮色主题切换，对齐 Element Plus 官方 dark mode 用法：
 *  - 在 <html> 标签上切换 `dark` class
 *  - 状态持久化到 localStorage
 *
 * Element Plus 的 dark mode 通过 main.js 中引入的
 * `element-plus/theme-chalk/dark/css-vars.css` 生效，该 CSS 仅在 html.dark
 * 时应用暗色变量覆盖，因此切换 class 即可立即生效无需刷新页面。
 */

import { defineStore } from 'pinia'
import { ref, watch } from 'vue'

const STORAGE_KEY = 'agentvalue_theme'
const DARK_CLASS = 'dark'

function readInitialTheme() {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored === 'dark') return true
    if (stored === 'light') return false
    // 首次访问：跟随系统偏好
    if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
      return true
    }
  } catch {
    // localStorage 不可用时忽略，回退到亮色
  }
  return false
}

function applyTheme(isDark) {
  const root = document.documentElement
  if (isDark) {
    root.classList.add(DARK_CLASS)
  } else {
    root.classList.remove(DARK_CLASS)
  }
}

export const useThemeStore = defineStore('theme', () => {
  const isDark = ref(readInitialTheme())

  // 初始化时立即同步一次 DOM（避免 SSR 或延迟导致初始闪烁）
  if (typeof document !== 'undefined') {
    applyTheme(isDark.value)
  }

  function toggle() {
    isDark.value = !isDark.value
  }

  function setDark(value) {
    isDark.value = !!value
  }

  // 监听变化同步 DOM 与 localStorage
  watch(
    isDark,
    (val) => {
      applyTheme(val)
      try {
        localStorage.setItem(STORAGE_KEY, val ? 'dark' : 'light')
      } catch {
        // 写入失败仅忽略
      }
    },
    { immediate: true },
  )

  return {
    isDark,
    toggle,
    setDark,
  }
})
