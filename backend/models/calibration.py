"""
校准会数据模型

实体:
- CalibrationSession: 校准会主记录
  - period: 校准周期 (如 2026-Q2)
  - facilitator_id: 主持人 (HR/Manager) ID
  - status: scheduled / in_progress / completed
  - participants: JSON 数组, 参与校准会的成员 ID 列表
  - notes: 会议纪要
- CalibrationItem: 校准会中的单个校准项 (一份评估的调整记录)
  - session_id: 所属校准会
  - evaluation_id: 被校准的评估
  - original_score: 原始综合得分
  - calibrated_score: 校准后得分
  - adjustment_reason: 调整原因
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base
from models.models import DEFAULT_TENANT_ID, now_utc


# 校准会状态
CALIBRATION_STATUS_SCHEDULED = "scheduled"
CALIBRATION_STATUS_IN_PROGRESS = "in_progress"
CALIBRATION_STATUS_COMPLETED = "completed"
CALIBRATION_STATUSES = frozenset(
    {
        CALIBRATION_STATUS_SCHEDULED,
        CALIBRATION_STATUS_IN_PROGRESS,
        CALIBRATION_STATUS_COMPLETED,
    }
)


class CalibrationSession(Base):
    """校准会主记录

    一个校准会汇聚同期多份评估的分数调整, 主持人组织参与者讨论后,
    逐项调整 original_score → calibrated_score, 完成后批量应用回 Evaluation。
    """

    __tablename__ = "calibration_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    session_id: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    # 校准周期, 如 2026-Q2 / 2026-W20
    period: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    # 校准会标题
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    # 主持人 ID
    facilitator_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.user_id"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CALIBRATION_STATUS_SCHEDULED
    )
    # 参与者 ID 列表 JSON, 如 ["U1","U2","U3"]
    participants: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    # 会议纪要
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('scheduled','in_progress','completed')",
            name="ck_calibration_session_status_valid",
        ),
        Index("ix_calibration_session_tenant_period", "tenant_id", "period"),
        Index("ix_calibration_session_tenant_status", "tenant_id", "status"),
    )


class CalibrationItem(Base):
    """校准项: 一份评估在校准会中的调整记录

    添加校准项时记录 original_score (从 Evaluation 快照),
    主持人讨论后调整 calibrated_score, 完成校准会时批量应用回 Evaluation.overall_score。
    """

    __tablename__ = "calibration_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    item_id: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    session_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("calibration_sessions.session_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    evaluation_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("evaluations.evaluation_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # 被校准员工 ID, 冗余字段便于按员工聚合
    employee_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.user_id"), index=True, nullable=False
    )
    original_score: Mapped[float] = mapped_column(Float, nullable=False)
    calibrated_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    adjustment_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 是否已应用回 Evaluation (完成校准会时统一应用)
    applied: Mapped[bool] = mapped_column(
        Integer, default=0, nullable=False  # SQLite 兼容: 用 Integer 0/1 存 bool
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        CheckConstraint(
            "original_score >= 0 AND original_score <= 100",
            name="ck_calibration_item_original_score_range",
        ),
        CheckConstraint(
            "(calibrated_score IS NULL) OR "
            "(calibrated_score >= 0 AND calibrated_score <= 100)",
            name="ck_calibration_item_calibrated_score_range",
        ),
        # 同一校准会 + 评估只能有一条记录
        Index(
            "ix_calibration_item_session_eval",
            "session_id",
            "evaluation_id",
            unique=True,
        ),
        Index("ix_calibration_item_tenant_session", "tenant_id", "session_id"),
    )
