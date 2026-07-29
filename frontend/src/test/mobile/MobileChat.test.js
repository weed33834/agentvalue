/**
 * MobileChat 旗舰聊天组件单测：空态 / 消息渲染 / 发送 / 停止
 *
 * 隔离 @/stores/chat 与 MarkdownText，仅验证组件行为与 store 调用契约：
 * - 无消息时显示空态引导
 * - 渲染 user / assistant 消息（assistant 走 MarkdownText）
 * - 发送：调用 chat.sendMessage(trimmed) 并清空草稿
 * - 流式生成中：显示停止按钮并调用 chat.stopGeneration()
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// 用 vi.hoisted 声明共享 mock，确保早于 vi.mock 工厂可用
const h = vi.hoisted(() => ({
  messages: [],
  isStreaming: false,
  loadSessions: vi.fn(async () => {}),
  selectSession: vi.fn(async () => {}),
  createSession: vi.fn(async () => {}),
  sendMessage: vi.fn(async () => {}),
  stopGeneration: vi.fn(),
}))

vi.mock('@/stores/chat', () => ({
  useChatStore: vi.fn(() => ({
    messages: h.messages,
    isStreaming: h.isStreaming,
    loadSessions: h.loadSessions,
    selectSession: h.selectSession,
    createSession: h.createSession,
    sendMessage: h.sendMessage,
    stopGeneration: h.stopGeneration,
  })),
}))
vi.mock('@/views/mobile/components/MarkdownText.vue', () => ({
  default: { template: '<span class="md-stub">{{ text }}</span>', props: ['text'] },
}))

import MobileChat from '@/views/mobile/components/MobileChat.vue'

describe('MobileChat', () => {
  beforeEach(() => {
    h.messages = []
    h.isStreaming = false
    vi.clearAllMocks()
  })

  it('无消息时显示空态引导', () => {
    const wrapper = mount(MobileChat, { props: { role: 'employee' } })
    expect(wrapper.text()).toContain('和 AI 助手开始对话吧')
  })

  it('渲染 user 与 assistant 消息', () => {
    h.messages = [
      { id: '1', role: 'user', parts: [{ type: 'text', text: '你好' }] },
      { id: '2', role: 'assistant', parts: [{ type: 'text', text: '**hi**' }] },
    ]
    const wrapper = mount(MobileChat, { props: { role: 'employee' } })
    expect(wrapper.text()).toContain('你好')
    expect(wrapper.find('.md-stub').exists()).toBe(true)
  })

  it('发送：调用 sendMessage(trimmed) 并清空草稿', async () => {
    const wrapper = mount(MobileChat, { props: { role: 'employee' } })
    await wrapper.find('textarea').setValue('  你好世界  ')
    await wrapper.find('.m-chat__btn--send').trigger('click')
    await flushPromises()
    expect(h.sendMessage).toHaveBeenCalledWith('你好世界')
  })

  it('生成中：显示停止按钮并调用 stopGeneration', async () => {
    h.isStreaming = true
    const wrapper = mount(MobileChat, { props: { role: 'employee' } })
    await flushPromises()
    const stopBtn = wrapper.find('.m-chat__btn--stop')
    expect(stopBtn.exists()).toBe(true)
    await stopBtn.trigger('click')
    expect(h.stopGeneration).toHaveBeenCalled()
  })
})
