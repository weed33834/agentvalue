/**
 * 移动端导航配置单测：MOBILE_TABS / desktopToMobilePath / tabsForRole
 *
 * 覆盖：
 * - 四端 Tab 配置齐全且均含 AI 助手入口
 * - 桌面路径 → /m 移动路径映射（login / 角色路由 / 深层路由）
 * - 未知路径原样返回（避免误重定向）
 * - tabsForRole 按角色取 Tab，未知角色返回空
 */
import { describe, it, expect } from 'vitest'
import { MOBILE_TABS, desktopToMobilePath, tabsForRole } from '@/utils/mobileNav'

describe('mobile navigation config', () => {
  it('MOBILE_TABS 覆盖四端且各端含 AI 助手入口', () => {
    expect(Object.keys(MOBILE_TABS).sort()).toEqual(['admin', 'employee', 'hr', 'manager'])
    for (const role of Object.keys(MOBILE_TABS)) {
      expect(MOBILE_TABS[role].length).toBeGreaterThan(0)
      expect(MOBILE_TABS[role].some((t) => t.path.endsWith('/assistant'))).toBe(true)
    }
  })

  it('desktopToMobilePath 映射 login', () => {
    expect(desktopToMobilePath('/login')).toBe('/m/login')
  })

  it('desktopToMobilePath 映射角色路由（含深层路由）', () => {
    expect(desktopToMobilePath('/employee')).toBe('/m/employee')
    expect(desktopToMobilePath('/manager/team')).toBe('/m/manager/team')
    expect(desktopToMobilePath('/hr/audit/123')).toBe('/m/hr/audit/123')
  })

  it('desktopToMobilePath 对未知 / 已为 /m 路径原样返回', () => {
    expect(desktopToMobilePath('/about')).toBe('/about')
    expect(desktopToMobilePath('/m/employee')).toBe('/m/employee')
  })

  it('tabsForRole 返回对应角色 Tab，未知角色返回空数组', () => {
    expect(tabsForRole('manager')).toBe(MOBILE_TABS.manager)
    expect(tabsForRole('unknown')).toEqual([])
  })
})
