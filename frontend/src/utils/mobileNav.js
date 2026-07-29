/**
 * 移动端导航配置（独立于桌面侧边栏）。
 * 桌面端用 el-menu 侧边栏，移动端用底部 Tab 栏 + 独立 /m 路由，
 * 两者共用同一套 store / api，仅 UI 层彻底分家。
 */

// 角色 → 移动端底部 Tab（path 指向 /m 路由树）
export const MOBILE_TABS = {
  employee: [
    { label: '看板', icon: '📊', path: '/m/employee' },
    { label: '录入', icon: '📝', path: '/m/employee/input' },
    { label: '历史', icon: '🕘', path: '/m/employee/history' },
    { label: 'AI助手', icon: '🤖', path: '/m/employee/assistant' },
  ],
  manager: [
    { label: '团队', icon: '👥', path: '/m/manager' },
    { label: '分析', icon: '📈', path: '/m/manager/team' },
    { label: 'AI助手', icon: '🤖', path: '/m/manager/assistant' },
  ],
  hr: [
    { label: '复核', icon: '🔍', path: '/m/hr' },
    { label: 'AI助手', icon: '🤖', path: '/m/hr/assistant' },
  ],
  admin: [
    { label: '工作台', icon: '🧰', path: '/m/admin' },
    { label: 'AI助手', icon: '🤖', path: '/m/admin/assistant' },
  ],
}

const ROLE_PREFIX = {
  employee: '/employee',
  manager: '/manager',
  hr: '/hr',
  admin: '/admin',
}

/**
 * 把桌面端路径映射到对应的 /m 移动端路径。
 * 例：/manager/team → /m/manager/team；/login → /m/login。
 * 无法识别的路径原样返回（避免误重定向）。
 * @param {string} path
 * @returns {string}
 */
export function desktopToMobilePath(path) {
  if (path === '/login') return '/m/login'
  for (const role of Object.keys(ROLE_PREFIX)) {
    const prefix = ROLE_PREFIX[role]
    if (path === prefix || path.startsWith(prefix + '/')) {
      return '/m' + path
    }
  }
  return path
}

/**
 * 根据角色取底部 Tab 配置，未匹配返回空数组。
 * @param {string} role
 * @returns {Array<{label:string, icon:string, path:string}>}
 */
export function tabsForRole(role) {
  return MOBILE_TABS[role] || []
}
