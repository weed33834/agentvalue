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

import App from './App.vue'
import router from './router'

const app = createApp(App)

// 图标仍全局注册(体积小,且 unplugin 按需对图标支持需额外配置)
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

app.config.errorHandler = (err, instance, info) => {
  console.error('全局错误:', err, info)
}

app.use(createPinia())
app.use(router)

app.mount('#app')
