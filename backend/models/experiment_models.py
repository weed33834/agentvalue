"""实验对比数据模型 (WS-2 评估体系升级)

对标 Braintrust Experiments / LangSmith Experiments:
- Experiment:        一次评测实验的定义 (数据集 + 任务类型 + 指标集合 + 配置)
- ExperimentRun:     实验下的一次运行 (variant = 被测对象, 含指标汇总与成本统计)
- ExperimentRunItem: 运行下的逐样本结果 (输入/期望/实际/上下文/逐指标得分)

审计背景: v2.3 只有 `agent_version_service.compare_versions` 与
`prompts.diff_versions` 两处"配置 diff", `EvaluationTask` 行与行之间没有任何关联,
无法回答 "Run B 在这 12 条样本上比 Run A 差了 4%"。这三张表补齐 run 之间的
可比性 (同一 experiment 下按 sample_id 对齐), 支撑逐样本回归清单与 CI 门禁。

多租户隔离: 所有模型包含 tenant_id 字段, 查询时按 tenant_id 过滤。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

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
from models.constants import now_utc as _now_utc
from models.models import DEFAULT_TENANT_ID

# 实验任务类型
EXPERIMENT_TASK_TYPES = frozenset({"rag", "agent", "prompt", "judge"})

# 实验状态
EXPERIMENT_STATUSES = frozenset({"draft", "active", "archived"})

# 运行状态
RUN_STATUS_PENDING = "pending"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"

RUN_STATUSES = frozenset(
    {
        RUN_STATUS_PENDING,
        RUN_STATUS_RUNNING,
        RUN_STATUS_COMPLETED,
        RUN_STATUS_FAILED,
        RUN_STATUS_CANCELLED,
    }
)

# 逐样本结果状态
ITEM_STATUS_SUCCESS = "success"
ITEM_STATUS_FAILED = "failed"
ITEM_STATUS_SKIPPED = "skipped"

ITEM_STATUSES = frozenset(
    {ITEM_STATUS_SUCCESS, ITEM_STATUS_FAILED, ITEM_STATUS_SKIPPED}
)


class Experiment(Base):
    """评测实验定义

    对标 Braintrust Experiment:
    - dataset_id: 关联 evaluation_datasets 的评测集
    - task_type: rag / agent / prompt / judge, 决定 run 执行时走哪条评测链路
    - metrics: 参与本实验的指标名列表 (RAGAS 指标 + judge 维度)
    - config: 实验级默认配置 (并发度 / judge 模型 / 指标阈值等)
    """

    __tablename__ = "experiments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 多租户归属
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # 实验名称
    name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    # 实验描述
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 关联评测数据集 ID (evaluation_datasets.id)
    dataset_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # 任务类型: rag / agent / prompt / judge
    task_type: Mapped[str] = mapped_column(String(16), nullable=False, default="rag")
    # 参与评测的指标名列表 ["faithfulness", "answer_relevancy", ...]
    metrics: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
    # 实验级配置 (并发度 / judge 模型 / 阈值等)
    config: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    # 实验状态: draft / active / archived
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", index=True
    )
    # 创建人 ID
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, onupdate=_now_utc
    )

    __table_args__ = (
        Index("ix_experiment_tenant_status", "tenant_id", "status"),
        Index("ix_experiment_tenant_dataset", "tenant_id", "dataset_id"),
    )


class ExperimentRun(Base):
    """实验运行 (一次被测变体的完整评测)

    variant 描述"被测的东西": 模型 / prompt 版本 / agent 版本 / 检索参数,
    是 Run A vs Run B 对比时归因差异的依据。
    metric_summary 形如 {"faithfulness": {"mean":0.82,"std":0.11,"min":0.5,"max":1.0,"n":48}}。
    """

    __tablename__ = "experiment_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 所属实验 ID
    experiment_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("experiments.id"), nullable=False, index=True
    )
    # 多租户归属
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # 运行名称 (如 "gpt-4o-mini + prompt v1.1")
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    # 被测变体: {"model": "...", "prompt_version": "...", "agent_version_id": 1,
    #            "retriever": {"top_k": 5, "rerank": true}}
    variant: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    # 运行状态: pending / running / completed / failed / cancelled
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RUN_STATUS_PENDING, index=True
    )
    # 进度百分比 0-100
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 样本总数
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 已完成样本数
    completed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 失败样本数
    failed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 指标汇总 {metric: {"mean","std","min","max","n"}}
    metric_summary: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )
    # 累计成本 (美元)
    total_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # 累计 token 数
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 运行总耗时 (毫秒)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 运行级错误信息 (整体失败时填充)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, onupdate=_now_utc
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_experiment_run_tenant_status", "tenant_id", "status"),
        Index("ix_experiment_run_tenant_experiment", "tenant_id", "experiment_id"),
    )


class ExperimentRunItem(Base):
    """实验运行的逐样本结果

    sample_id 是跨 run 对齐的关键: Run A 与 Run B 用同一数据集时,
    相同 sample_id 的两行才可以做 delta 比较 (compare_runs 的 join key)。
    scores 形如 {"faithfulness": 0.83, "answer_relevancy": 0.91}。
    """

    __tablename__ = "experiment_run_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 所属运行 ID
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("experiment_runs.id"), nullable=False, index=True
    )
    # 冗余所属实验 ID, 便于跨 run 直接按实验聚合
    experiment_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # 多租户归属
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # 跨 run 对齐用的样本标识 (通常等于 dataset_item_id 的字符串形式)
    sample_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # 来源数据集条目 ID (dataset_items.id)
    dataset_item_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 样本输入
    input: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    # 期望输出 (ground truth)
    expected: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    # 实际输出
    actual: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    # 检索上下文 ["chunk1", "chunk2"]
    contexts: Mapped[Optional[List[Any]]] = mapped_column(JSON, nullable=True)
    # 逐指标得分 {metric: float}; 不可用的指标不写入 (不填 0 冒充)
    scores: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    # 单样本耗时 (毫秒)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 单样本成本 (美元)
    cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # 单样本 token 数
    tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 状态: success / failed / skipped
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ITEM_STATUS_SUCCESS, index=True
    )
    # 单样本错误信息
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )

    __table_args__ = (
        # compare_runs 的核心 join: 按 run 取全部样本后按 sample_id 对齐
        Index("ix_experiment_run_item_run_sample", "run_id", "sample_id"),
        Index("ix_experiment_run_item_tenant_run", "tenant_id", "run_id"),
    )


__all__ = [
    "EXPERIMENT_STATUSES",
    "EXPERIMENT_TASK_TYPES",
    "Experiment",
    "ExperimentRun",
    "ExperimentRunItem",
    "ITEM_STATUSES",
    "ITEM_STATUS_FAILED",
    "ITEM_STATUS_SKIPPED",
    "ITEM_STATUS_SUCCESS",
    "RUN_STATUSES",
    "RUN_STATUS_CANCELLED",
    "RUN_STATUS_COMPLETED",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_PENDING",
    "RUN_STATUS_RUNNING",
]
