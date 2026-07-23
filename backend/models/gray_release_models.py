"""灰度发布 / 蓝绿部署数据模型

对标 Bisheng / Langfuse 的 Canary 发布能力:
- GrayRelease: Agent / 工作流版本的灰度发布策略记录
  - canary (金丝雀): 按 traffic_percentage 概率将流量导入新版本
  - blue_green (蓝绿): 根据 config.current 在 blue / green 两个版本间整体切换
  - rolling (滚动): 逐步增加流量百分比

状态机: draft (草稿) → active (灰度中) → paused (暂停) → completed (完成 100% 切换)
                                          ↘ rolled_back (已回滚)

表: gray_releases
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class GrayRelease(Base):
    """灰度发布实体 (按 tenant + agent 隔离)

    一个 Agent 同时只能有一个 active 状态的灰度发布 (业务层保证)。
    config 示例:
      - blue_green: {"blue_version": 1, "green_version": 2, "current": "blue"}
      - rolling:    {"step": 25, "steps": [0, 25, 50, 75, 100]}
      - canary:     {"baseline_version": 1}  (基准版本, 未命中灰度的流量走此版本)
    """

    __tablename__ = "gray_releases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 多租户归属
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default="default"
    )
    # 灰度发布名称 (前端展示)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 关联的 Agent 预设 ID (软关联 AgentPreset.id)
    agent_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agent_presets.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 新版本 ID (关联 AgentVersion.id, 灰度目标版本)
    version_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agent_versions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 发布类型: canary / blue_green / rolling
    release_type: Mapped[str] = mapped_column(String(16), nullable=False, default="canary")
    # 灰度流量百分比 (0-100)
    traffic_percentage: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # 发布状态: draft / active / paused / completed / rolled_back
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default="draft"
    )
    # 灰度配置 (JSON, 随发布类型不同而结构不同)
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default="{}")
    # 备注 / 描述
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 启动时间 (status → active 时写入)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 完成时间 (status → completed / rolled_back 时写入)
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
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
            "traffic_percentage >= 0 AND traffic_percentage <= 100",
            name="ck_gray_release_traffic_range",
        ),
        # 索引: tenant_id + status (按状态过滤列表)
        Index("ix_gray_release_tenant_status", "tenant_id", "status"),
        # 索引: tenant_id + agent_id (获取 Agent 当前灰度)
        Index("ix_gray_release_tenant_agent", "tenant_id", "agent_id"),
    )
