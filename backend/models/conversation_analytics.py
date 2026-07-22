"""
会话分析数据模型

对标 Langfuse Token 分析 / Dashboard：记录每次会话/LLM 调用的 token 用量、成本、
延迟与状态，供多维聚合统计（趋势/分位/错误率/成本分解）与异常用量检测使用。

多租户隔离: 所有模型包含 tenant_id 字段，未显式指定时落 DEFAULT_TENANT_ID。
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base
from models.models import DEFAULT_TENANT_ID, now_utc


class ConversationMetrics(Base):
    """会话/LLM 调用度量记录

    每次会话或 LLM 调用落一行，记录 token 用量、成本、延迟、状态与错误信息。
    供 AnalyticsServiceV2 做趋势/分位/错误率/成本/异常分析。
    """

    __tablename__ = "conversation_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # 会话 ID（关联 ChatSession 等）
    conversation_id: Mapped[str] = mapped_column(
        String(128), index=True, nullable=False
    )
    # 发起用户 ID
    user_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    # 处理该会话的 Agent ID
    agent_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    # 调用的模型名（如 gpt-4o-mini）
    model: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    # 输入 token 数
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 输出 token 数
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 总 token 数（input + output）
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 本次成本（美元）
    cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # 端到端延迟（毫秒）
    latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # 调用状态: success / error / timeout 等
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="success")
    # 错误信息（status != success 时填充）
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 度量时间点（UTC）
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now_utc
    )

    __table_args__ = (
        # 按租户 + 时间检索（趋势查询主索引）
        Index("ix_conv_metrics_tenant_time", "tenant_id", "timestamp"),
        # 按租户 + 用户检索
        Index("ix_conv_metrics_tenant_user", "tenant_id", "user_id"),
        # 按租户 + Agent 检索
        Index("ix_conv_metrics_tenant_agent", "tenant_id", "agent_id"),
        # 按租户 + 模型检索
        Index("ix_conv_metrics_tenant_model", "tenant_id", "model"),
    )
