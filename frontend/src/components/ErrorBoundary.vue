<template>
  <div v-if="error" class="error-boundary">
    <el-result icon="error" title="页面出错了" sub-title="抱歉，页面发生了异常">
      <template #extra>
        <p class="error-detail">错误信息: {{ error.message || '未知错误' }}</p>
        <div class="error-actions">
          <el-button type="primary" @click="handleRetry">重试</el-button>
          <el-button @click="handleGoHome">返回首页</el-button>
        </div>
      </template>
    </el-result>
  </div>
  <slot v-else />
</template>

<script setup>
/**
 * P1: Error Boundary 组件
 *
 * 捕获子组件树中的未处理异常，防止整个页面白屏。
 * 参考 React Error Boundary 模式，在 Vue 中通过 onErrorCaptured 实现。
 *
 * 使用方式:
 *   <ErrorBoundary>
 *     <ComplexComponent />
 *   </ErrorBoundary>
 */
import { ref, onErrorCaptured } from 'vue'
import { useRouter } from 'vue-router'

const error = ref(null)
const router = useRouter()

onErrorCaptured((err) => {
  error.value = err
  // 记录到全局错误处理(可对接 Sentry/Datadog RUM)
  console.error('[ErrorBoundary]', err)
  // 阻止错误继续向上传播
  return false
})

function handleRetry() {
  error.value = null
  // 触发组件重新渲染
  window.location.reload()
}

function handleGoHome() {
  error.value = null
  router.push('/')
}
</script>

<style scoped>
.error-boundary {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 400px;
  padding: 24px;
}

.error-detail {
  color: var(--el-text-color-secondary);
  font-size: 14px;
  margin-bottom: 16px;
  word-break: break-all;
}

.error-actions {
  display: flex;
  gap: 12px;
  justify-content: center;
}
</style>
