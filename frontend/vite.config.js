import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [
    vue(),
    // Element Plus 按需引入：自动导入模板中使用的 el-* 组件及其样式
    // ElMessage/ElMessageBox 等命令式 API 由 AutoImport 自动导入，
    // 显式 import { ElMessage } from 'element-plus' 仍可保留(tree-shaking 生效)
    AutoImport({
      resolvers: [ElementPlusResolver()],
    }),
    Components({
      resolvers: [ElementPlusResolver()],
    }),
  ],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/metrics': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    // 代码分割：将体积较大的第三方库拆分为独立 chunk，避免主包过大
    // vite 8 起 rolldown 要求 manualChunks 为函数形式
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('node_modules')) {
            // Vue 核心运行时：vue + 路由 + 状态管理，独立成可长期缓存的 vendor chunk
            if (/[\\/]node_modules[\\/](vue|vue-router|pinia)[\\/]/.test(id)) {
              return 'vue-core'
            }
            // ECharts 图表库 + Vue 封装层，体积较大单独拆分
            if (/[\\/]node_modules[\\/](echarts|vue-echarts)[\\/]/.test(id)) {
              return 'echarts'
            }
          }
        },
      },
    },
    // 拆分后单 chunk 仍超 500KB 时才告警，避免噪音
    chunkSizeWarningLimit: 600,
  },
})
