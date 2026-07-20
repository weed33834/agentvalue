"""admin 路由公共工具

收口各 admin 路由文件重复的辅助函数(_gen_id / _entity_to_dict 通用 helper),
避免复制粘贴导致的签名漂移和行为不一致。
"""

import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, Optional


def gen_id(prefix: Optional[str] = None, hex_len: int = 32) -> str:
    """生成主键

    Args:
        prefix: 可选前缀(如 "wf" / "ct"),传入时返回 `{prefix}_{hex[:24]}`;
            None 时返回完整 uuid4 hex(32 字符,向后兼容历史调用)
        hex_len: hex 部分长度(prefix=None 时生效),默认 32

    Returns:
        主键字符串
    """
    h = uuid.uuid4().hex
    if prefix:
        return f"{prefix}_{h[:24]}"
    return h[:hex_len]


def entity_to_dict(
    entity: Any,
    fields: Iterable[str],
    *,
    iso_fields: Iterable[str] = (),
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """通用 ORM entity → dict 序列化器

    收口 custom_tools / workflows / feature_flags 三个路由文件中重复的
    `_entity_to_dict` 实现,统一 datetime 字段的 ISO 格式化行为。

    Args:
        entity: SQLAlchemy ORM 实例
        fields: 要提取的字段名列表
        iso_fields: fields 中需要转 ISO 格式字符串的 datetime 字段
        extra: 额外字段(如计算字段或跨表 join 结果),合并进返回 dict

    Returns:
        dict 形式的 entity 数据
    """
    iso_set = set(iso_fields)
    result: Dict[str, Any] = {}
    for f in fields:
        val = getattr(entity, f, None)
        if f in iso_set and isinstance(val, datetime):
            val = val.isoformat()
        result[f] = val
    if extra:
        result.update(extra)
    return result
