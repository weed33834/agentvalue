"""
API 健康监控数据模型

对标 Langfuse 延迟监控 / 告警系统：记录端点级请求度量（状态码/响应时间），
并支持 SLO 定义（目标延迟/成功率/窗口），供健康状态与 SLO 达成分析使用。

多租户隔离: 所有模型包含 tenant_id 字段，未显式指定时落 DEFAULT_TENANT_ID。
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base
from models.models import DEFAULT_TENANT_ID, now_utc


class ApiHealthMetric(Base):
    """API 端点请求度量记录

    每次请求落一行，记录端点、方法、状态码与响应时间，供端点统计与 SLO 计算使用。
    """

    __tablename__ = "api_health_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # API 端点路径（如 /api/v1/evaluations）
    endpoint: Mapped[str] = mapped_column(String(512), index=True, nullable=False)
    # HTTP 方法（GET/POST/PUT/DELETE 等）
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    # HTTP 状态码
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    # 响应时间（毫秒）
    response_time_ms: Mapped[float] = mapped_column(Float, nullable=False)
    # 度量时间点（UTC）
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now_utc
    )

    __table_args__ = (
        # 按租户 + 端点 + 时间检索（端点统计与 SLO 计算主索引）
        Index("ix_api_health_tenant_endpoint_time", "tenant_id", "endpoint", "timestamp"),
    )


class SloDefinition(Base):
    """SLO 定义

    每条 SLO 描述某端点的目标延迟与目标成功率，及统计窗口（分钟）。
    enabled=False 时该 SLO 不参与达成状态计算。
    """

    __tablename__ = "slo_definitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # SLO 名称（同租户内建议唯一）
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 目标端点路径
    endpoint: Mapped[str] = mapped_column(String(512), index=True, nullable=False)
    # 目标延迟上限（毫秒），P95 应低于此值
    target_latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    # 目标成功率（0-1，如 0.99 表示 99% 请求成功）
    target_success_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.99)
    # 统计窗口（分钟）
    window_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    # 是否启用
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        # 按租户 + 端点检索，便于按端点聚合 SLO
        Index("ix_slo_tenant_endpoint", "tenant_id", "endpoint"),
    )
