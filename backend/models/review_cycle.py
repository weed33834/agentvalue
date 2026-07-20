"""
360° 环评数据模型

实体:
- ReviewCycle: 一次 360° 环评邀请记录 (一个 evaluation 可对应多名评估人)
  - reviewer_role: peer / manager / subordinate / external
  - status: pending / submitted
  - scores: JSON 结构, 各维度评分 (如 {"执行力": 85, "协作": 90})
  - feedback_text: 文字反馈

设计说明:
- 与现有 Evaluation 模型通过 evaluation_id (字符串业务键) 关联,
  与 ApprovalAction / Feedback 等表保持一致, 避免破坏现有外键语义。
- 多租户隔离字段 tenant_id, 与其它模型风格一致。
- 评估人 reviewer_id 关联 users.user_id (字符串业务键)。
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base
from models.models import DEFAULT_TENANT_ID, now_utc


# 评估人角色常量
REVIEWER_ROLE_PEER = "peer"
REVIEWER_ROLE_MANAGER = "manager"
REVIEWER_ROLE_SUBORDINATE = "subordinate"
REVIEWER_ROLE_EXTERNAL = "external"
REVIEWER_ROLES = frozenset(
    {
        REVIEWER_ROLE_PEER,
        REVIEWER_ROLE_MANAGER,
        REVIEWER_ROLE_SUBORDINATE,
        REVIEWER_ROLE_EXTERNAL,
    }
)

# 环评记录状态
REVIEW_STATUS_PENDING = "pending"
REVIEW_STATUS_SUBMITTED = "submitted"
REVIEW_STATUSES = frozenset({REVIEW_STATUS_PENDING, REVIEW_STATUS_SUBMITTED})


class ReviewCycle(Base):
    """360° 环评邀请记录

    一次评估 (Evaluation) 可发起多份环评邀请, 每位评估人一份。
    评估人通过 submit 端点提交 scores + feedback_text 后, status 置 submitted。
    """

    __tablename__ = "review_cycles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    review_id: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    evaluation_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("evaluations.evaluation_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # 被评估员工 ID, 冗余字段, 便于按员工聚合环评结果而无需 join
    employee_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.user_id"), index=True, nullable=False
    )
    # 评估人 ID, 关联 users.user_id
    reviewer_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.user_id"), index=True, nullable=False
    )
    # 评估人角色: peer(同事) / manager(上级) / subordinate(下属) / external(跨部门/外部)
    reviewer_role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=REVIEW_STATUS_PENDING
    )
    # 各维度评分 JSON, 如 {"执行力": 85, "协作": 90, "创新": 80}
    scores: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 综合评分 (各维度均值或加权, 由评估人 submit 时计算或前端传入)
    overall_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # 文字反馈
    feedback_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 发起人 (HR/Manager) ID
    requested_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
    submitted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "reviewer_role IN ('peer','manager','subordinate','external')",
            name="ck_review_reviewer_role_valid",
        ),
        CheckConstraint(
            "status IN ('pending','submitted')",
            name="ck_review_status_valid",
        ),
        # 同一 evaluation + reviewer 只能有一份未删除的邀请
        Index(
            "ix_review_eval_reviewer",
            "evaluation_id",
            "reviewer_id",
            unique=True,
        ),
        Index("ix_review_tenant_eval", "tenant_id", "evaluation_id"),
        Index("ix_review_tenant_reviewer", "tenant_id", "reviewer_id"),
    )
