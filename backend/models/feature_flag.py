"""
Feature Flag 数据模型 (P3-2: 应用级功能开关, 对标 Langfuse Feature Flag)

用途:
- 启用/禁用某些 Provider
- 启用 rerank
- 启用新 prompt 版本灰度
- 启用多 Agent 模式等

表: feature_flags
- key: 主键 (如 "use_rerank_v2" / "enable_multi_agent", 业务层自命名)
- description: 用途描述
- enabled: 全局开关 (False 时直接返回 False, 不进灰度判断)
- rollout_percentage: 灰度百分比 0-100
- target_tenant_ids: 精确受众租户列表 (空表示所有租户)
- target_user_ids: 精确受众用户列表 (空表示所有用户)
- category: 分类 (general / model / agent / feature)
- created_at / updated_at: 时间戳

判定规则 (FeatureFlagService.is_enabled):
1. flag 不存在或 enabled=False → False
2. user_id 在 target_user_ids → True
3. tenant_id 在 target_tenant_ids → True
4. rollout_percentage > 0 → hash(user_id or tenant_id) % 100 < percentage → True
5. 默认 False
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class FeatureFlag(Base):
    """Feature Flag 实体 (按 key 全局唯一,跨租户共享配置)"""

    __tablename__ = "feature_flags"

    # 主键: 业务 key (如 use_rerank_v2), 不可改
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 用途描述 (前端展示)
    description: Mapped[str] = mapped_column(
        String(256), nullable=False, default="", server_default=""
    )
    # 全局开关 (False 时直接返回 False, 跳过灰度判断)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=func.text("0")
    )
    # 灰度百分比 0-100 (0 表示无灰度, 100 表示全量启用)
    rollout_percentage: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # 精确受众租户列表 (空表示所有租户)
    target_tenant_ids: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    # 精确受众用户列表 (空表示所有用户)
    target_user_ids: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    # 分类: general / model / agent / feature
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, default="general", server_default="general"
    )
    # 创建时间 (数据库 server_default, 避免应用时区不一致)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 更新时间 (onupdate 触发, UPDATE 时自动刷新)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        # 百分比范围约束
        CheckConstraint(
            "rollout_percentage >= 0 AND rollout_percentage <= 100",
            name="ck_feature_flag_rollout_range",
        ),
        # category 索引: 列表按分类过滤常用
        Index("ix_feature_flags_category", "category"),
        # enabled 索引: 列表过滤常用
        Index("ix_feature_flags_enabled", "enabled"),
    )
