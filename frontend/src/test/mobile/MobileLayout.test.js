/**
 * MobileLayout 组件单测：布局壳挂载、底栏 Tab、导航与退出
 *
 * 用 mock 隔离 auth / theme / Watermark，仅验证布局与交互：
 * - 顶栏 + 底栏 Tab 正常渲染（角色感知）
 * - 点击底栏 Tab 触发路由跳转
 * - 退出按钮触发 logout
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { setActivePinia, createPinia } from 'pinia'

const authStore = { role: 'employee', logout: vi.fn(async () => {}) }
vi.mock('@/stores/auth', () => ({ useAuthStore: vi.fn(() => authStore) }))
vi.mock('@/stores/theme', () => ({
  useThemeStore: vi.fn(() => ({ isDark: false, toggle: vi.fn() })),
}))
vi.mock('@/components/Watermark.vue', () => ({ default: { template: '<div class="wm-stub"></div>' } }))

import MobileLayout from '@/layouts/MobileLayout.vue'

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/:pathMatch(.*)*', component: { template: '<div/>' } }],
  })
}

describe('MobileLayout', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    authStore.role = 'employee'
    authStore.logout.mockClear()
  })

  it('渲染顶栏与角色对应的底栏 Tab（employee 4 项）', () => {
    const router = makeRouter()
    const wrapper = mount(MobileLayout, { global: { plugins: [router] } })
    expect(wrapper.find('.m-topbar').exists()).toBe(true)
    expect(wrapper.findAll('.m-tab').length).toBe(4)
  })

  it('点击底栏 Tab 触发路由跳转', async () => {
    const router = makeRouter()
    const wrapper = mount(MobileLayout, { global: { plugins: [router] } })
    const tabs = wrapper.findAll('.m-tab')
    await tabs[1].trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/m/employee/input')
  })

  it('退出按钮触发 logout', async () => {
    const router = makeRouter()
    const wrapper = mount(MobileLayout, { global: { plugins: [router] } })
    await wrapper.find('[aria-label="退出登录"]').trigger('click')
    expect(authStore.logout).toHaveBeenCalled()
  })
})
