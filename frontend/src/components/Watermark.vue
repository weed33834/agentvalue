<template>
  <!--
    全屏水印覆盖层：安全合规防截图溯源（Phase 9.3 增强）。
    - pointer-events: none 不阻挡页面交互
    - aria-hidden="true" 对辅助技术隐藏，不影响 WCAG 可访问性
    - 动态水印：内容随分钟变化（时间戳 + 轮换 nonce），防固定截图去标识
    - 位置随机抖动：每次刷新随机偏移背景位置，防固定位置裁剪
    - 监听 visibilitychange：切后台可能是截图工具，上报审计日志
    - 密度可配置：通过 density（sparse/normal/dense）控制平铺间距
  -->
  <div
    class="watermark-overlay"
    :style="{
      backgroundImage: `url(${watermarkUrl})`,
      backgroundPosition: `${jitterX}px ${jitterY}px`,
    }"
    aria-hidden="true"
  ></div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { useAuthStore } from '@/stores/auth'
import api from '@/api/client'

const props = defineProps({
  // 水印文字，默认取当前用户 ID（无 ID 时取姓名）
  text: {
    type: String,
    default: '',
  },
  // 水印不透明度（0-1）
  opacity: {
    type: Number,
    default: 0.08,
  },
  // 水印旋转角度（度）
  rotate: {
    type: Number,
    default: -22,
  },
  // 水印平铺间距（像素）；设置 density 时由 density 推导覆盖
  gap: {
    type: Number,
    default: 200,
  },
  // 水印密度：sparse / normal / dense，通过 settings 配置，覆盖 gap
  density: {
    type: String,
    default: 'normal',
  },
  // 是否向后端上报水印状态（默认开启，关闭则只本地渲染）
  report: {
    type: Boolean,
    default: true,
  },
})

const auth = useAuthStore()

// 密度 → 平铺间距映射，密度越高间距越小
const DENSITY_GAP = { sparse: 280, normal: 200, dense: 140 }

// 实际生效的平铺间距：density 优先，未匹配回退 gap
const effectiveGap = computed(() => DENSITY_GAP[props.density] ?? props.gap)

// 水印文字：优先使用传入 text，否则回退到当前用户 ID / 姓名
const watermarkText = computed(() => props.text || auth.userId || auth.name || 'AgentValue-AI')

const watermarkUrl = ref('')
// 背景位置随机抖动偏移，每次刷新重新生成，防固定位置裁剪
const jitterX = ref(0)
const jitterY = ref(0)
let timer = null

// 数字补零，用于格式化时间
function pad(n) {
  return String(n).padStart(2, '0')
}

// 分钟级轮换 nonce：用当前分钟拼一个短串，让水印内容随时间变化，
// 防止攻击者用固定截图去水印（同一分钟内一致，跨分钟变化）
function minuteNonce(now) {
  const seed = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}${pad(now.getHours())}${pad(now.getMinutes())}`
  // 简单哈希取后 4 位，作为可见 nonce 后缀
  let h = 0
  for (let i = 0; i < seed.length; i++) {
    h = (h * 31 + seed.charCodeAt(i)) >>> 0
  }
  return (h % 10000).toString().padStart(4, '0')
}

// 用 canvas 生成平铺水印纹理：用户标识 + 当前时间 + 轮换 nonce
function generate() {
  if (typeof document === 'undefined') return
  const now = new Date()
  const timeStr = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`
  const nonce = minuteNonce(now)
  const lines = [watermarkText.value, timeStr, `#${nonce}`]
  const canvas = document.createElement('canvas')
  const ctx = canvas.getContext('2d')
  // 单块画布尺寸：留出旋转与多行文本空间
  const size = Math.max(effectiveGap.value, 180)
  canvas.width = size
  canvas.height = size
  ctx.clearRect(0, 0, size, size)
  ctx.font = '14px Arial, "PingFang SC", "Microsoft YaHei", sans-serif'
  ctx.fillStyle = `rgba(20, 20, 20, ${props.opacity})`
  ctx.textAlign = 'center'
  ctx.textBaseline = 'middle'
  ctx.translate(size / 2, size / 2)
  ctx.rotate((props.rotate * Math.PI) / 180)
  // 多行文本垂直居中绘制
  const lineHeight = 20
  const startY = -((lines.length - 1) * lineHeight) / 2
  lines.forEach((line, i) => {
    ctx.fillText(line, 0, startY + i * lineHeight)
  })
  watermarkUrl.value = canvas.toDataURL()

  // 随机抖动背景位置：在 0 ~ gap 范围内偏移，防固定网格裁剪
  jitterX.value = Math.floor(Math.random() * effectiveGap.value)
  jitterY.value = Math.floor(Math.random() * effectiveGap.value)
}

// 向后端上报水印状态，失败静默不影响页面
async function reportStatus({ visible, visibilityEvent = null } = {}) {
  if (!props.report) return
  try {
    await api.post('/watermark/verify', {
      visible: visible,
      density: props.density,
      visibility_event: visibilityEvent,
    })
  } catch {
    // 上报失败不阻断业务，水印仍正常渲染
  }
}

// visibilitychange：切到后台可能是截图工具，上报审计日志
// 页面隐藏时停止心跳避免后台持续上报；恢复可见时立即刷新并重启心跳
function handleVisibilityChange() {
  if (typeof document === 'undefined') return
  if (document.hidden) {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
    reportStatus({ visible: false, visibilityEvent: 'hidden' })
  } else {
    generate()
    reportStatus({ visible: true })
    timer = setInterval(() => {
      generate()
      reportStatus({ visible: true })
    }, 60 * 1000)
  }
}

onMounted(() => {
  generate()
  // 启动即上报一次心跳
  reportStatus({ visible: true })
  // 每 60 秒刷新一次水印（含时间与 nonce）并上报心跳
  timer = setInterval(() => {
    generate()
    reportStatus({ visible: true })
  }, 60 * 1000)
  document.addEventListener('visibilitychange', handleVisibilityChange)
})

onBeforeUnmount(() => {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
  if (typeof document !== 'undefined') {
    document.removeEventListener('visibilitychange', handleVisibilityChange)
  }
})

// 文字或视觉参数变化时重新生成水印纹理
watch(
  [watermarkText, effectiveGap, () => props.opacity, () => props.rotate, () => props.density],
  generate,
)
</script>

<style scoped>
.watermark-overlay {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  left: 0;
  z-index: 9999;
  pointer-events: none;
  background-repeat: repeat;
}
</style>
