import { defineConfig } from 'vitest/config'
import { mergeConfig } from 'vite'
import viteConfig from './vite.config.js'

// P1-14: vitest 配置,通过 mergeConfig 复用 vite.config.js 的 plugins 与 resolve.alias,仅追加 test 字段
// 运行:npm test(vitest run,单次)| npm run test:watch(监听)
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: 'jsdom',
      globals: true,
      include: ['src/test/**/*.test.js'],
    },
  }),
)
