/**
 * 设备类型检测（纯函数，便于单测）。
 * 移动端适配与桌面端完全分离：本文件只回答「当前是不是手机」，
 * 真正的路由分流在 router 层做（移动 UA 且未显式 ?desktop=1 时跳 /m）。
 */
const MOBILE_UA_RE =
  /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini|Mobile|Windows Phone/i

/**
 * iPadOS 13+ 的 Safari 在 UA 里伪装成 Macintosh，但带 touch 与平板尺寸，
 * 这里用「Mac + 多点触控能力」兜底识别为移动端。
 */
const IPAD_OS_RE = /Macintosh/i
const TOUCH_CAPABLE_RE = /Macintosh.*Touch/i

/**
 * 判断给定 UA 是否为移动设备。
 * @param {string} [ua] 可选 UA；默认取 navigator.userAgent（无 DOM 环境返回 false）。
 * @returns {boolean}
 */
export function isMobile(ua) {
  const agent = ua ?? (typeof navigator !== 'undefined' ? navigator.userAgent : '')
  if (!agent) return false
  if (MOBILE_UA_RE.test(agent)) return true
  // iPadOS 13+ 兜底：Mac UA 但带 Touch 标识
  if (IPAD_OS_RE.test(agent) && TOUCH_CAPABLE_RE.test(agent)) return true
  return false
}

/**
 * 返回设备分类：'mobile' | 'desktop'。
 * @param {string} [ua]
 * @returns {'mobile' | 'desktop'}
 */
export function getDeviceType(ua) {
  return isMobile(ua) ? 'mobile' : 'desktop'
}
