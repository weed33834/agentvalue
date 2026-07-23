"""LLM-as-a-Judge 自动评测数据模型

对标 Langfuse LLM-as-a-Judge + Dify 日志回放:
- EvaluationTask: 评测任务 (关联数据集, judge 模型/提示词/维度, 进度, 汇总)
- EvaluationResult: 单条评测结果 (Agent 输出, Judge 评分, 反馈, 通过状态)

多租户隔离: 所有模型包含 tenant_id 字段, 查询时按 tenant_id 过滤。
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
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


class EvaluationTask(Base):
    """LLM-as-a-Judge 评测任务

    对标 Langfuse LLM-as-a-Judge:
    - 关联一个 EvaluationDataset, 遍历其中条目进行评测
    - judge_model: 评判用的模型档位 (L0/L1/L2/L3)
    - judge_prompt_template: 评判提示词模板 (支持 {input}/{expected}/{output} 占位符)
    - metrics: 评测维度列表 ["accuracy", "relevance", "completeness", "fluency"]
    - status: pending / running / completed / failed
    - progress / total_items / completed_items: 进度跟踪
    - results_summary: 评测汇总 (平均分/通过率/各维度得分)
    """

    __tablename__ = "evaluation_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 多租户归属
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # 任务名称
    name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    # 关联数据集 ID
    dataset_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("evaluation_datasets.id"), nullable=False, index=True
    )
    # 评判模型档位
    judge_model: Mapped[str] = mapped_column(String(32), nullable=False, default="L0")
    # 评判提示词模板
    judge_prompt_template: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 评测维度
    metrics: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
    # 任务状态: pending / running / completed / failed
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    # 进度百分比 (0-100)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 总条目数
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 已完成条目数
    completed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 评测汇总结果
    results_summary: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )
    # 创建人
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_eval_task_tenant_dataset", "tenant_id", "dataset_id"),
        Index("ix_eval_task_tenant_status", "tenant_id", "status"),
    )


class EvaluationResult(Base):
    """单条评测结果

    对标 Langfuse Score + Dify 日志回放:
    - agent_output: Agent 生成的输出 (被评判的对象)
    - judge_scores: 各维度评分 JSON {"accuracy": 85, "relevance": 90, ...}
    - judge_feedback: 评判反馈文本
    - passed: 是否通过 (综合评分是否达标)
    - latency_ms: 单条评测耗时 (毫秒)
    """

    __tablename__ = "evaluation_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 多租户归属
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # 所属评测任务 ID
    task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("evaluation_tasks.id"), nullable=False, index=True
    )
    # 对应的数据集条目 ID
    dataset_item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("dataset_items.id"), nullable=False, index=True
    )
    # Agent 生成的输出
    agent_output: Mapped[str] = mapped_column(Text, nullable=False)
    # 评判评分 (各维度)
    judge_scores: Mapped[Dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    # 评判反馈
    judge_feedback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 是否通过
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 评测耗时 (毫秒)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )

    __table_args__ = (
        Index("ix_eval_result_tenant_task", "tenant_id", "task_id"),
        Index("ix_eval_result_tenant_item", "tenant_id", "dataset_item_id"),
    )
