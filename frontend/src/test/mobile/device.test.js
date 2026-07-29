/**
 * 设备检测单测：isMobile / getDeviceType（纯函数，零依赖）
 *
 * 覆盖：
 * - 桌面 UA → 非移动端
 * - 手机 UA（iPhone / Android）→ 移动端
 * - iPadOS 13+ 伪装 Mac 的兜底识别（Mac + Touch）
 * - 空 UA 安全返回
 * - getDeviceType 分类映射
 */
import { describe, it, expect } from 'vitest'
import { isMobile, getDeviceType } from '@/utils/device'

const DESKTOP_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36'
const IPHONE_UA =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1'
const ANDROID_UA =
  'Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Mobile Safari/537.36'
const MAC_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15'
const IPAD_OS_UA = MAC_UA + ' Touch'

describe('device detection', () => {
  it('桌面 UA → 非移动端', () => {
    expect(isMobile(DESKTOP_UA)).toBe(false)
  })

  it('iPhone / Android UA → 移动端', () => {
    expect(isMobile(IPHONE_UA)).toBe(true)
    expect(isMobile(ANDROID_UA)).toBe(true)
  })

  it('iPadOS 13+ 伪装 Mac（带 Touch）→ 兜底识别为移动端', () => {
    expect(isMobile(MAC_UA)).toBe(false) // 纯 Mac，不含 Touch
    expect(isMobile(IPAD_OS_UA)).toBe(true) // Mac + Touch
  })

  it('空 UA / 未传 → 安全返回 false', () => {
    expect(isMobile('')).toBe(false)
    expect(isMobile()).toBe(false)
  })

  it('getDeviceType 正确分类', () => {
    expect(getDeviceType(IPHONE_UA)).toBe('mobile')
    expect(getDeviceType(DESKTOP_UA)).toBe('desktop')
  })
})
