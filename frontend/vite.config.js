import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'
import { VitePWA } from 'vite-plugin-pwa'

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
    // P1-45: PWA 支持 — Service Worker 自动注册 + 离线缓存 + 可安装
    // vite-plugin-pwa 基于 Workbox,构建时生成 sw.js + manifest.webmanifest
    // 开发模式(dev)不注入 SW(避免热更新冲突),生产模式(build)自动注入
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['favicon.svg', 'robots.txt'],
      injectRegister: false,
      manifest: {
        name: 'AgentValue-AI',
        short_name: 'AgentValue',
        description: 'AI 驱动的员工价值量化与成长反馈系统',
        theme_color: '#2563eb',
        background_color: '#ffffff',
        display: 'standalone',
        orientation: 'portrait-primary',
        scope: '/',
        start_url: '/',
        lang: 'zh-CN',
        icons: [
          {
            src: 'pwa-192x192.png',
            sizes: '192x192',
            type: 'image/png',
          },
          {
            src: 'pwa-512x512.png',
            sizes: '512x512',
            type: 'image/png',
          },
          {
            src: 'pwa-512x512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'any maskable',
          },
        ],
      },
      workbox: {
        // 预缓存应用 shell(index.html + CSS/JS chunks)
        globPatterns: ['**/*.{js,css,html,svg,png,woff2}'],
        // 静态资源缓存策略: 7天过期,最多 100 个条目
        maximumFileSizeToCacheInBytes: 5 * 1024 * 1024,
        runtimeCaching: [
          {
            // API 请求: NetworkFirst(优先网络,离线降级到缓存)
            urlPattern: /^https?:\/\/.*\/api\/.*/i,
            handler: 'NetworkFirst',
            options: {
              cacheName: 'api-cache',
              expiration: {
                maxEntries: 50,
                maxAgeSeconds: 60 * 5, // 5 分钟
              },
              cacheableResponse: {
                statuses: [0, 200],
              },
            },
          },
          {
            // 静态资源(字体/图片): CacheFirst(优先缓存,减少请求)
            urlPattern: /\.(?:woff2?|png|jpg|jpeg|svg|gif)$/i,
            handler: 'CacheFirst',
            options: {
              cacheName: 'asset-cache',
              expiration: {
                maxEntries: 100,
                maxAgeSeconds: 60 * 60 * 24 * 30, // 30 天
              },
            },
          },
        ],
      },
      devOptions: {
        enabled: false,
      },
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
