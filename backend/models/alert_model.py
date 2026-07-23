"""告警通知数据模型

对标 PagerDuty / Grafana Alerting:
- Alert: 告警记录 (级别 + 标题 + 消息 + 来源 + 状态 + 元数据)

告警级别 (severity): critical / warning / info
告警状态 (status): active (活跃) → acknowledged (已确认) → resolved (已解决)
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    JSON,
    DateTime,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


def _now_utc() -> datetime:
    """当前 UTC 时间"""
    return datetime.now(timezone.utc)


class Alert(Base):
    """告警记录

    记录系统/业务告警, 支持多渠道通知 (飞书群机器人 / 邮件 / Webhook)。
    状态机: active → acknowledged → resolved
    """

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 多租户归属
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default="default"
    )
    # 告警级别: critical / warning / info
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default="warning", index=True
    )
    # 告警标题
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    # 告警消息内容
    message: Mapped[str] = mapped_column(Text, nullable=False)
    # 告警来源 (如 system / agent_error / quota / sensitive_word 等)
    source: Mapped[str] = mapped_column(
        String(64), nullable=False, default="system", index=True
    )
    # 告警状态: active / acknowledged / resolved
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", index=True
    )
    # 元数据 (JSON, 存储附加信息如 agent_id / error_stack / threshold 等)
    # 注意: "metadata" 在 SQLAlchemy Declarative API 中是保留字, 用 Python 属性名 metadata_
    # 映射到数据库列名 metadata
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSON, default=dict)
    # 创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )
    # 确认时间
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 确认人 (用户 ID)
    acknowledged_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # 解决时间
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 解决人 (用户 ID)
    resolved_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_alert_status_severity", "status", "severity"),
        Index("ix_alert_source_status", "source", "status"),
        Index("ix_alert_created_at", "created_at"),
    )
