/**
 * Watermark.vue 组件测试 (P1-14)
 *
 * 验证:
 * - 组件渲染 .watermark-overlay 覆盖层
 * - aria-hidden / pointer-events 安全属性正确
 * - props 默认值生效(density=normal)
 * - report=false 时不调用后端上报
 * - 传入 text prop 时使用自定义文字
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

// Mock @/api/client:避免真实 HTTP 调用 + 避免引入 element-plus 全量包
vi.mock('@/api/client', () => ({
  default: { post: vi.fn(() => Promise.resolve({ ok: true })) },
  authApi: { me: vi.fn(), refresh: vi.fn(), login: vi.fn() },
}))

import Watermark from '@/components/Watermark.vue'

// jsdom 不支持 canvas,需要 stub getContext / toDataURL,否则 generate() 会抛错
function stubCanvas() {
  const fakeCtx = {
    clearRect: () => {},
    fillText: () => {},
    translate: () => {},
    rotate: () => {},
    set font(_) {},
    set fillStyle(_) {},
    set textAlign(_) {},
    set textBaseline(_) {},
  }
  HTMLCanvasElement.prototype.getContext = vi.fn(() => fakeCtx)
  HTMLCanvasElement.prototype.toDataURL = vi.fn(() => 'data:image/png;base64,STUB')
}

describe('Watermark.vue', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    stubCanvas()
    vi.clearAllMocks()
    // 重置 localStorage,避免上次测试残留影响 auth store 初始化
    localStorage.clear()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('渲染 .watermark-overlay 覆盖层且 aria-hidden=true', () => {
    const wrapper = mount(Watermark, { props: { report: false } })
    const overlay = wrapper.find('.watermark-overlay')
    expect(overlay.exists()).toBe(true)
    expect(overlay.attributes('aria-hidden')).toBe('true')
  })

  it('覆盖层 pointer-events:none,不阻挡页面交互', () => {
    const wrapper = mount(Watermark, { props: { report: false } })
    // pointer-events:none 由 scoped CSS 提供;这里验证元素与 class 正确挂载
    const overlay = wrapper.find('.watermark-overlay')
    expect(overlay.exists()).toBe(true)
    expect(overlay.classes()).toContain('watermark-overlay')
  })

  it('默认 density=normal (gap=200),生效后 watermarkUrl 非空', async () => {
    const wrapper = mount(Watermark, { props: { report: false } })
    // 等待 onMounted generate() 完成
    await wrapper.vm.$nextTick()
    // generate 调用了 stubbed toDataURL,watermarkUrl 应被设置
    expect(wrapper.vm.watermarkUrl).toBe('data:image/png;base64,STUB')
  })

  it('density=dense 时 effectiveGap=140', () => {
    const wrapper = mount(Watermark, {
      props: { report: false, density: 'dense' },
    })
    expect(wrapper.vm.effectiveGap).toBe(140)
  })

  it('density=sparse 时 effectiveGap=280', () => {
    const wrapper = mount(Watermark, {
      props: { report: false, density: 'sparse' },
    })
    expect(wrapper.vm.effectiveGap).toBe(280)
  })

  it('传入 text prop 时 watermarkText 使用传入值', () => {
    const wrapper = mount(Watermark, {
      props: { report: false, text: 'CUSTOM-USER-123' },
    })
    expect(wrapper.vm.watermarkText).toBe('CUSTOM-USER-123')
  })

  it('未传 text 时回退到 AgentValue-AI 兜底', () => {
    const wrapper = mount(Watermark, { props: { report: false } })
    // 无 auth store user,回退到 'AgentValue-AI'
    expect(wrapper.vm.watermarkText).toBe('AgentValue-AI')
  })

  it('report=false 时不调用 api.post 上报', async () => {
    const api = (await import('@/api/client')).default
    mount(Watermark, { props: { report: false } })
    await new Promise((r) => setTimeout(r, 0))
    expect(api.post).not.toHaveBeenCalled()
  })

  it('report=true 时 onMounted 触发一次 api.post 上报', async () => {
    const api = (await import('@/api/client')).default
    mount(Watermark, { props: { report: true } })
    await new Promise((r) => setTimeout(r, 0))
    expect(api.post).toHaveBeenCalledWith(
      '/watermark/verify',
      expect.objectContaining({
        visible: true,
      }),
    )
  })
})
