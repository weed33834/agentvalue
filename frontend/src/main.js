import { createApp } from 'vue'
import { createPinia } from 'pinia'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
// 命令式 API（ElMessage/ElMessageBox/ElNotification）的样式不会随模板按需引入自动注入，
// 此处全局兜底引入其 CSS，确保显式 import 与 AutoImport 注入都能命中样式
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import 'element-plus/es/components/notification/style/css'
// Element Plus 暗色模式 CSS 变量：仅在 <html class="dark"> 时生效，
// 由 stores/theme.js 控制 html 标签的 dark class 切换。
// 必须在亮色组件样式之后引入，使其暗色变量优先级生效。
import 'element-plus/theme-chalk/dark/css-vars.css'
// 主题色覆盖：须在 Element Plus 样式之后引入，通过 CSS 变量统一主色调
// (Element Plus 组件样式由 vite 插件 unplugin-vue-components 按需引入)
import './styles/theme.css'
// 全局样式：过渡动画、响应式布局、组件交互增强
import './styles/global.css'

import App from './App.vue'
import router from './router'

// P1-2: Sentry 前端错误监控 (优雅降级：未安装或未配置 VITE_SENTRY_DSN 时跳过)
const SENTRY_DSN = import.meta.env.VITE_SENTRY_DSN
let SentryInstance = null
if (SENTRY_DSN) {
  try {
    const Sentry = await import('@sentry/vue')
    SentryInstance = Sentry
  } catch {
    console.warn('Sentry SDK 加载失败，降级为无错误监控')
  }
}

const app = createApp(App)

// P1-2: Sentry 初始化（绑定 app + router，启用性能追踪）
if (SentryInstance) {
  SentryInstance.init({
    app,
    dsn: SENTRY_DSN,
    integrations: [SentryInstance.browserTracingIntegration({ router })],
    tracesSampleRate: import.meta.env.PROD ? 0.1 : 1.0,
    environment: import.meta.env.MODE,
  })
}

// 图标仍全局注册(体积小,且 unplugin 按需对图标支持需额外配置)
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

app.config.errorHandler = (err, instance, info) => {
  console.error('全局错误:', err, info)
  // P1-2: 同时上报到 Sentry(如果已初始化)
  if (SentryInstance) {
    SentryInstance.captureException(err, { contexts: { vue: { info } } })
  }
}

app.use(createPinia())
app.use(router)

app.mount('#app')
