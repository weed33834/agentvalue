"""数据集管理数据模型

对标 Langfuse 数据集管理 + 阿里百炼训练集/评测集:
- EvaluationDataset: 评测数据集 (test/train/eval 类型, 标签, 条目计数)
- DatasetItem: 数据集条目 (输入, 期望输出, 元数据, 标注状态)

多租户隔离: 所有模型包含 tenant_id 字段, 查询时按 tenant_id 过滤。
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    JSON,
    DateTime,
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


class EvaluationDataset(Base):
    """评测数据集

    对标 Langfuse Dataset + 阿里百炼训练集/评测集:
    - dataset_type: test (测试集) / train (训练集) / eval (评测集)
    - tags: 自定义标签数组, 便于分类筛选
    - item_count: 冗余字段, 添加/删除条目时同步更新, 避免列表页 COUNT 查询
    """

    __tablename__ = "evaluation_datasets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 多租户归属
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # 数据集名称
    name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    # 数据集描述
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 数据集类型: test / train / eval
    dataset_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="test", index=True
    )
    # 自定义标签
    tags: Mapped[List[Any]] = mapped_column(JSON, nullable=False, default=list)
    # 条目总数 (冗余字段, 添加/删除时同步更新)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 创建人
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, onupdate=_now_utc
    )

    __table_args__ = (
        Index("ix_eval_dataset_tenant_type", "tenant_id", "dataset_type"),
    )


class DatasetItem(Base):
    """数据集条目

    每条记录一个测试用例:
    - input: 输入内容 (JSON, 支持复杂结构)
    - expected_output: 期望输出 (JSON, 用于对比评测)
    - metadata: 附加元数据 (JSON, 如来源/难度等)
    - label: 标签 (如分类标签)
    - status: 标注状态 pending / labeled / reviewed
    """

    __tablename__ = "dataset_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 多租户归属
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # 所属数据集 ID
    dataset_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("evaluation_datasets.id"), nullable=False, index=True
    )
    # 输入内容
    input: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)
    # 期望输出
    expected_output: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )
    # 附加元数据 (数据库列名: metadata)
    metadata_: Mapped[Dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )
    # 标签
    label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # 标注状态: pending / labeled / reviewed
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )

    __table_args__ = (
        # 按租户 + 数据集查询条目
        Index("ix_dataset_item_tenant_dataset", "tenant_id", "dataset_id"),
        # 按租户 + 状态筛选
        Index("ix_dataset_item_tenant_status", "tenant_id", "status"),
    )
