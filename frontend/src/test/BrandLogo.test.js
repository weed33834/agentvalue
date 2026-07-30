import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import BrandLogo from '@/components/BrandLogo.vue'

describe('BrandLogo 组件', () => {
  it('默认渲染字标 + 几何 A 标记', () => {
    const w = mount(BrandLogo)
    expect(w.find('.brand-logo').exists()).toBe(true)
    expect(w.find('.brand-logo__mark').exists()).toBe(true)
    // 渐变方块内应含白色几何 A 的 path
    const paths = w.findAll('.brand-logo__mark svg path')
    expect(paths.length).toBeGreaterThan(0)
    expect(w.text()).toContain('AgentValue')
  })

  it('showText=false 时只显示图标', () => {
    const w = mount(BrandLogo, { props: { showText: false } })
    expect(w.find('.brand-logo__mark').exists()).toBe(true)
    expect(w.text().trim()).toBe('')
  })

  it('size 写入 mark 的内联尺寸', () => {
    const w = mount(BrandLogo, { props: { size: 48, showText: false } })
    const mark = w.find('.brand-logo__mark')
    const style = mark.attributes('style') || ''
    expect(style).toContain('48px')
  })

  it('自定义字标生效', () => {
    const w = mount(BrandLogo, { props: { wordmark: 'Foo' } })
    expect(w.text()).toContain('Foo')
  })
})
