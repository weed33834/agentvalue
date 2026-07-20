// 评估状态 -> 中文标签 / el-tag 类型的统一映射
// 员工 / 主管 / HR 各端共用，避免内联 map 漂移导致数值不一致

const STATUS_LABELS = {
  ai_drafted: 'AI草拟',
  manager_review: '待主管复核',
  hr_audit: '待HR复核',
  approved: '已通过',
  rejected: '已驳回',
}

const STATUS_TAG_TYPES = {
  ai_drafted: 'info',
  manager_review: 'warning',
  hr_audit: 'warning',
  approved: 'success',
  rejected: 'danger',
}

const RISK_TAG_TYPES = {
  critical: 'error',
  high: 'warning',
  medium: 'warning',
  low: 'info',
}

export function statusLabel(status) {
  return STATUS_LABELS[status] || status
}

export function statusTagType(status) {
  return STATUS_TAG_TYPES[status] || 'info'
}

export function riskTagType(level) {
  return RISK_TAG_TYPES[level] || 'info'
}
