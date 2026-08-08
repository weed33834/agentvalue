"""
原生 Trace / Span 数据模型（WS-1 可观测性）

对标 Langfuse / LangSmith / Arize Phoenix：在自托管部署下不依赖任何外部 SaaS，
把一次业务链路（trace）与其内部的每一步（span）完整落库，供瀑布图、成本分析、
错误归因与回放使用。

与 core/tracing.py 的关系：
- core/tracing.py 是 Langfuse / OpenTelemetry 的**外发**适配层，无 key 时降级为 NoOp；
- 本模块 + core/observe.py 是**本地存储**层，无论有无外部 SaaS 都会落库，
  两者互不替代，可同时开启。

命名注意：SQLAlchemy Declarative 保留了 `metadata` 属性名，因此 trace 的自由扩展
字段命名为 `trace_metadata`，span 侧命名为 `attributes`（对齐 OpenTelemetry 语义）。

多租户隔离: 所有模型包含 tenant_id 字段，未显式指定时落 DEFAULT_TENANT_ID。
"""

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base
from models.models import DEFAULT_TENANT_ID, now_utc

# ---------------------------------------------------------------------------
# 枚举取值（仅作文档与校验参考，DB 层不加 CheckConstraint，
# 避免新增 kind 时必须走迁移，可观测性字段应对新枚举保持前向兼容）
# ---------------------------------------------------------------------------

# trace 类型：一次对话 / 一次评估 / 一次工作流 / 一次 Agent 运行
TRACE_KINDS = ("chat", "evaluation", "workflow", "agent", "rag", "task")

# span 类型：对齐 OpenTelemetry GenAI + Langfuse observation type
SPAN_KINDS = (
    "llm",
    "tool",
    "retriever",
    "agent",
    "chain",
    "http",
    "embedding",
    "rerank",
)

# 运行状态
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_ERROR = "error"
TRACE_STATUSES = (STATUS_RUNNING, STATUS_SUCCESS, STATUS_ERROR)


class TraceRecord(Base):
    """链路追踪主记录（一次完整业务调用）

    一次 `trace_context()` 落一行。汇总字段（total_spans / total_*_tokens /
    total_cost）由 core/observe.py 在 trace 结束时按其 span 汇总回填，
    使列表页与成本看板无需 join span 表即可聚合。
    """

    __tablename__ = "trace_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # 业务级 trace 标识（uuid4 hex），跨进程传递用此字段而非自增主键
    trace_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    # 租户 ID；后台任务等无租户上下文的场景允许为空
    tenant_id: Mapped[Optional[str]] = mapped_column(
        String(64), index=True, nullable=True, default=DEFAULT_TENANT_ID
    )
    # 链路名称（如 evaluate_employee / chat_completion）
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    # 链路类型: chat / evaluation / workflow / agent / rag / task
    kind: Mapped[str] = mapped_column(
        String(32), index=True, nullable=False, default="chat"
    )
    # 状态: running / success / error
    status: Mapped[str] = mapped_column(
        String(16), index=True, nullable=False, default=STATUS_RUNNING
    )
    # 开始时间（UTC）
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False, default=now_utc
    )
    # 结束时间（running 状态为空）
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 端到端耗时（毫秒）
    duration_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # 发起用户 ID
    user_id: Mapped[Optional[str]] = mapped_column(
        String(64), index=True, nullable=True
    )
    # 会话 ID（同一会话的多次调用可串联）
    session_id: Mapped[Optional[str]] = mapped_column(
        String(128), index=True, nullable=True
    )
    # span 总数（结束时回填）
    total_spans: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 全链路 prompt token 汇总
    total_prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 全链路 completion token 汇总
    total_completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    # 全链路成本汇总（美元）
    total_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # 错误摘要（status=error 时填充）
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 标签列表（如 ["prod", "batch"]），供过滤与分组
    tags: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    # 自由扩展元数据（不可命名为 metadata：SQLAlchemy Declarative 保留字）
    trace_metadata: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        # 按租户 + 时间检索（列表页主索引，newest first）
        Index("ix_trace_records_tenant_started", "tenant_id", "started_at"),
        # 按租户 + 类型检索
        Index("ix_trace_records_tenant_kind", "tenant_id", "kind"),
        # 按租户 + 状态检索（错误率统计）
        Index("ix_trace_records_tenant_status", "tenant_id", "status"),
    )


class SpanRecord(Base):
    """链路中的单个执行片段

    通过 `trace_id` + `parent_span_id` 组成树形结构（软引用，不建 DB 外键：
    span 可能先于 trace 落库，且批量写入下外键会显著拖慢插入）。
    """

    __tablename__ = "span_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # 业务级 span 标识（uuid4 hex）
    span_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # 所属 trace（软引用 trace_records.trace_id，无 DB 外键）
    trace_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # 父 span（根 span 为空）
    parent_span_id: Mapped[Optional[str]] = mapped_column(
        String(64), index=True, nullable=True
    )
    # 租户 ID
    tenant_id: Mapped[Optional[str]] = mapped_column(
        String(64), index=True, nullable=True, default=DEFAULT_TENANT_ID
    )
    # span 名称（如 chat_completion / retrieve_context）
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    # span 类型: llm / tool / retriever / agent / chain / http / embedding / rerank
    kind: Mapped[str] = mapped_column(
        String(32), index=True, nullable=False, default="chain"
    )
    # 状态: running / success / error
    status: Mapped[str] = mapped_column(
        String(16), index=True, nullable=False, default=STATUS_RUNNING
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False, default=now_utc
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # 输入（消息列表 / 工具入参等，JSON 存储；过大内容由采集侧截断）
    input: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    # 输出（补全内容 / 工具返回等）
    output: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    # 模型名（kind=llm/embedding/rerank 时填充）
    model: Mapped[Optional[str]] = mapped_column(
        String(128), index=True, nullable=True
    )
    # Provider 名（openai / anthropic / deepseek 等）
    provider: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 本 span 成本（美元），由 core/pricing.calculate_cost 计算
    cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # 错误信息（status=error 时填充）
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 自由扩展属性（对齐 OpenTelemetry span attributes）
    attributes: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        # 按租户 + 时间检索
        Index("ix_span_records_tenant_started", "tenant_id", "started_at"),
        # 瀑布图主索引：拉取某 trace 的全部 span 并按开始时间排序
        Index("ix_span_records_trace_started", "trace_id", "started_at"),
        # 按租户 + 模型检索（成本按模型分组）
        Index("ix_span_records_tenant_model", "tenant_id", "model"),
    )


__all__ = [
    "TraceRecord",
    "SpanRecord",
    "TRACE_KINDS",
    "SPAN_KINDS",
    "TRACE_STATUSES",
    "STATUS_RUNNING",
    "STATUS_SUCCESS",
    "STATUS_ERROR",
]
