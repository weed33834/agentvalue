"""人工标注工具数据模型

对标 Langfuse Human-in-the-loop:
- AnnotationTask: 标注任务 (来源类型, 待标注内容, 分配状态, 优先级)
- Annotation: 标注结果 (标注人, 标签, 评分, 反馈, 元数据)

多租户隔离: 所有模型包含 tenant_id 字段, 查询时按 tenant_id 过滤。
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import (
    JSON,
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
from models.models import DEFAULT_TENANT_ID


def _now_utc() -> datetime:
    """当前 UTC 时间"""
    return datetime.now(timezone.utc)


class AnnotationTask(Base):
    """标注任务

    对标 Langfuse Human-in-the-loop:
    - source_type: 数据来源 evaluation_result / chat_message / agent_output
    - source_id: 来源记录 ID (软关联, 不加外键)
    - content: 待标注的内容文本
    - status: pending / in_progress / completed
    - assigned_to: 分配给的标注人 ID
    - priority: 优先级 (数值越大越优先)
    """

    __tablename__ = "annotation_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 多租户归属
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # 任务名称
    name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    # 任务描述
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 来源类型: evaluation_result / chat_message / agent_output
    source_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="agent_output"
    )
    # 来源记录 ID (软关联)
    source_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # 待标注内容
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 任务状态: pending / in_progress / completed
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    # 分配给的标注人 ID
    assigned_to: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # 优先级 (数值越大越优先)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_annotation_task_tenant_status", "tenant_id", "status"),
        Index("ix_annotation_task_tenant_assignee", "tenant_id", "assigned_to"),
    )


class Annotation(Base):
    """标注结果

    每条记录一次标注提交:
    - annotator_id: 标注人 ID
    - label: 标签 (如 "good" / "bad" / "needs_improvement")
    - score: 评分 (0-100)
    - feedback: 反馈文本
    - metadata: 附加元数据 (JSON)
    """

    __tablename__ = "annotations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 多租户归属
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # 所属标注任务 ID
    task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("annotation_tasks.id"), nullable=False, index=True
    )
    # 标注人 ID
    annotator_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 标签
    label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # 评分 (0-100)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # 反馈文本
    feedback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 附加元数据 (数据库列名: metadata)
    metadata_: Mapped[Dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )

    __table_args__ = (
        Index("ix_annotation_tenant_task", "tenant_id", "task_id"),
        Index("ix_annotation_tenant_annotator", "tenant_id", "annotator_id"),
    )
