/**
 * 移动端关键页面冒烟测试：补全构建的两个阻断文件
 *   - admin/Dashboard.vue（管理端移动工作台 / 功能中心）
 *   - PlaceholderView.vue（非核心桌面路由承接页）
 *
 * 验证：
 * - admin Dashboard 渲染状态卡 + 功能中心，并调用 adminApi.modelStatus
 * - PlaceholderView 显示桌面端引导，点击「前往桌面端」生成 ?desktop=1 等价路径
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { setActivePinia, createPinia } from 'pinia'

const modelStatus = vi.fn(async () => ({ online: true, model: 'gpt-4o', uptime: 3600 }))
vi.mock('@/api/client', () => ({
  default: { post: vi.fn() },
  authApi: { me: vi.fn(), login: vi.fn(), refresh: vi.fn() },
  adminApi: { modelStatus: (...args) => modelStatus(...args) },
}))

import AdminDashboard from '@/views/mobile/admin/Dashboard.vue'
import PlaceholderView from '@/views/mobile/PlaceholderView.vue'

describe('mobile views smoke', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    modelStatus.mockClear()
  })

  it('admin Dashboard 渲染状态卡 + 功能中心', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/:pathMatch(.*)*', component: { template: '<div/>' } }],
    })
    const wrapper = mount(AdminDashboard, { global: { plugins: [router] } })
    await flushPromises()
    expect(wrapper.find('.m-card').exists()).toBe(true)
    expect(wrapper.text()).toContain('系统状态')
    expect(wrapper.text()).toContain('模型管理')
    expect(modelStatus).toHaveBeenCalled()
  })

  it('PlaceholderView 引导回桌面端并带 ?desktop=1', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/:pathMatch(.*)*', component: PlaceholderView }],
    })
    router.push('/m/manager/roi')
    await router.isReady()
    const wrapper = mount(PlaceholderView, { global: { plugins: [router] } })
    await flushPromises()
    expect(wrapper.text()).toContain('桌面端')
    await wrapper.find('.m-btn').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/manager/roi')
    expect(router.currentRoute.value.query.desktop).toBe('1')
  })
})
