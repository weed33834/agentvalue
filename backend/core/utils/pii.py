"""PII 脱敏工具。

用于审计日志/错误日志中可能含 PII 的字段脱敏,避免敏感数据落库或泄漏到日志聚合系统。

支持的 PII 类型(简化正则,适配中国大陆主流格式):
- 手机号:1[3-9]xxxxxxxxx
- 邮箱:标准 RFC 5322 简化版
- 身份证号:18 位(最后一位可为 X)
- 银行卡号:16~19 位连续数字

设计取舍:
- 正则简化优先可读性,可能漏检边缘场景(如带空格/连字符的卡片号),如有需要可扩展。
- 脱敏保留首尾若干字符,便于人工识别模式但无法还原原值。
- 不依赖外部库,可在任何环境直接 import。
"""

import re
from typing import Any, Dict, List, Optional, Union

# PII 模式注册表：集中定义各类 PII 的正则模式,作为单一来源供本模块
# (掩码脱敏,用于日志/审计)与 core.guards.output_guard(占位符脱敏,用于展示视图)
# 共享,避免两处各自维护正则。每项 {"name": 类型名, "pattern": 正则字符串}。
# 顺序敏感：身份证号必须排在银行卡号之前(18 位纯数字身份证会被 16~19 位
# 银行卡正则误吞,先命中身份证即可避免)。
PII_PATTERNS = [
    {"name": "手机号", "pattern": r"(?<!\d)1[3-9]\d{9}(?!\d)"},
    # 支持 3-4-4 分组的手机号(带空格/连字符)
    {"name": "手机号", "pattern": r"(?<!\d)1[3-9]\d[\s\-]?\d{4}[\s\-]?\d{4}(?!\d)"},
    {"name": "邮箱", "pattern": r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"},
    {"name": "身份证号", "pattern": r"(?<!\d)\d{17}[\dXx](?!\d)"},
    {"name": "银行卡号", "pattern": r"(?<!\d)\d{16,19}(?!\d)"},
]


def _mask_phone(match: re.Match) -> str:
    s = match.group()
    return s[:3] + "****" + s[-4:]


def _mask_email(match: re.Match) -> str:
    s = match.group()
    return s[:2] + "***@" + s.split("@")[1]


def _mask_idcard(match: re.Match) -> str:
    s = match.group()
    return s[:6] + "********" + s[-4:]


def _mask_bankcard(match: re.Match) -> str:
    s = match.group()
    return s[:4] + "********" + s[-4:]


# 各 PII 类型对应的掩码替换函数(保留首尾少量字符,用于日志/审计)
_PII_MASKERS = {
    "手机号": _mask_phone,
    "邮箱": _mask_email,
    "身份证号": _mask_idcard,
    "银行卡号": _mask_bankcard,
}

# 预编译:按注册表顺序(身份证号先于银行卡号)成 (编译正则, 类型名) 列表
_COMPILED_PII_PATTERNS = [
    (re.compile(entry["pattern"]), entry["name"]) for entry in PII_PATTERNS
]


def redact_pii(text: Optional[str]) -> Optional[str]:
    """对文本中的 PII 做掩码脱敏,保留首尾少量字符。

    - 手机号:前 3 + **** + 后 4  -> 138****1234
    - 邮箱:前 2 + ***@ + 域名   -> ab***@example.com
    - 身份证号:前 6 + ******** + 后 4 -> 110101********1234
    - 银行卡号:前 4 + ******** + 后 4 -> 6228**********1234

    Args:
        text: 待脱敏文本,None 或空字符串原样返回。

    Returns:
        脱敏后的文本;若输入为 None/空,原样返回。
    """
    if not text:
        return text

    for pattern, name in _COMPILED_PII_PATTERNS:
        text = pattern.sub(_PII_MASKERS[name], text)
    return text


def redact_dict(d: Any) -> Any:
    """递归脱敏 dict / list 中的所有字符串值。

    用于审计日志 details 这类嵌套结构:遍历 dict 的 value 与 list 元素,
    仅对 str 类型调用 redact_pii,其他类型(int / float / bool / None)原样保留。

    - dict: 返回新 dict(原 dict 不变),value 递归处理。
    - list: 返回新 list(原 list 不变),元素递归处理。
    - str: 返回 redact_pii(s)。
    - 其他:原样返回。

    Args:
        d: 任意可序列化数据结构。

    Returns:
        结构相同、字符串值已脱敏的新对象;输入 None 时返回 None。
    """
    if d is None:
        return None
    if isinstance(d, str):
        return redact_pii(d)
    if isinstance(d, dict):
        return {k: redact_dict(v) for k, v in d.items()}
    if isinstance(d, list):
        return [redact_dict(item) for item in d]
    if isinstance(d, tuple):
        return tuple(redact_dict(item) for item in d)
    return d


def redact_audit_details(details: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """便捷封装:对审计日志 details 字段做整体脱敏。

    与 redact_dict 等价,显式签名表明意图,便于在 AuditService.log 中调用:
        details = redact_audit_details(details)
    """
    return redact_dict(details)
