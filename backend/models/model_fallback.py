"""
模型 Fallback 链数据模型

对标阿里百炼 AI 网关秒级容灾：主模型故障时按 fallback chain 依次切换备用模型，
成功即返回，触发事件写入审计日志便于复盘。

多租户隔离: 所有模型包含 tenant_id 字段，未显式指定时落 DEFAULT_TENANT_ID。

chain_config 格式示例:
[
    {"tier": "L2", "provider": "openai", "model": "gpt-4o-mini", "timeout": 30, "max_retries": 2},
    {"tier": "L0", "provider": "openai", "model": "gpt-4o", "timeout": 60, "max_retries": 1},
    {"tier": "L1", "provider": "ollama", "model": "qwen2.5:7b", "timeout": 20, "max_retries": 3}
]
"""

from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base
from models.models import DEFAULT_TENANT_ID, now_utc


class FallbackChain(Base):
    """模型 Fallback 链

    每个 (tenant_id, name) 描述一条降级链，chain_config 为有序的候选模型列表。
    enabled=False 时该链不参与路由；priority 越高越优先被选中（同一 tier 多条链时）。
    """

    __tablename__ = "model_fallback_chains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # 链名称（同租户内建议唯一，便于在路由中按名称引用）
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 链描述（可选）
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 降级链配置：有序候选模型列表，每个元素含 tier/provider/model/timeout/max_retries
    chain_config: Mapped[List[Any]] = mapped_column(JSON, nullable=False, default=list)
    # 是否启用
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 优先级（数值越大越优先，同租户多条链时按 priority 降序选取）
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        # 按租户 + 优先级检索，便于快速选取最高优先级的启用链
        Index("ix_fallback_tenant_priority", "tenant_id", "priority"),
    )
