"""
配额、预算与计费相关数据模型

包含四类模型:
- TenantQuota:   租户配额配置（日请求数 / 日 token 数 / API Key 数上限 + 当前用量）
- QuotaUsageLog: 配额使用日志（按天聚合，供统计与图表展示）
- BudgetAlert:   成本预算告警（月度/日度预算，阈值触发通知）
- BillingRecord: API 计费记录（按请求粒度，供账单汇总与导出）

多租户隔离: 所有模型包含 tenant_id 字段，未显式指定时落 DEFAULT_TENANT_ID。
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base
from models.models import DEFAULT_TENANT_ID, now_utc

# 默认配额: 与 QuotaService 中 _DEFAULT_QUOTA 常量保持一致
DEFAULT_MAX_REQUESTS_PER_DAY = 1000
DEFAULT_MAX_TOKENS_PER_DAY = 500000
DEFAULT_MAX_API_KEYS = 10


class TenantQuota(Base):
    """租户配额配置

    每个租户一行，记录日请求/token 上限与当前累计用量。
    current_requests_today / current_tokens_today 在 reset_daily_usage 时清零。
    """

    __tablename__ = "tenant_quotas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # 租户 ID，全局唯一，一个租户仅一条配额记录
    tenant_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    # 日最大请求数
    max_requests_per_day: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_MAX_REQUESTS_PER_DAY
    )
    # 日最大 token 数
    max_tokens_per_day: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_MAX_TOKENS_PER_DAY
    )
    # 最大 API Key 数量
    max_api_keys: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_MAX_API_KEYS
    )
    # 今日已用请求数（reset_daily_usage 清零）
    current_requests_today: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    # 今日已用 token 数（reset_daily_usage 清零）
    current_tokens_today: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    # 配额重置时间点（上次重置时间，供判断是否需要再次重置）
    quota_reset_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 是否启用配额限制（禁用时不做检查，放行所有请求）
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        CheckConstraint("max_requests_per_day >= 0", name="ck_quota_max_requests"),
        CheckConstraint("max_tokens_per_day >= 0", name="ck_quota_max_tokens"),
        CheckConstraint("max_api_keys >= 0", name="ck_quota_max_api_keys"),
        CheckConstraint(
            "current_requests_today >= 0", name="ck_quota_current_requests"
        ),
        CheckConstraint("current_tokens_today >= 0", name="ck_quota_current_tokens"),
    )


class QuotaUsageLog(Base):
    """配额使用日志（按天聚合）

    每个租户每天一条记录，记录当日请求次数、token 用量与成本。
    供统计图表与趋势分析使用。
    """

    __tablename__ = "quota_usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # 使用日期（UTC 日期，格式 YYYY-MM-DD，仅用于按天聚合）
    usage_date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    # 当日请求次数
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 当日 token 用量
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 当日成本（美元）
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        # 同一租户同一天仅一条聚合记录
        Index("uix_quota_usage_tenant_date", "tenant_id", "usage_date", unique=True),
    )


class BudgetAlert(Base):
    """成本预算告警

    支持月度/日度预算，当 current_usage >= budget_limit * alert_threshold 时
    触发告警通知（通过 NotificationService 发送），并将 alerted 置为 True。
    """

    __tablename__ = "budget_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # 预算类型: monthly（月度）/ daily（日度）
    budget_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="monthly"
    )
    # 预算上限（美元）
    budget_limit: Mapped[float] = mapped_column(Float, nullable=False)
    # 当前已使用预算（美元）
    current_usage: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # 告警阈值（0-1，默认 0.8，即使用 80% 时触发告警）
    alert_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.8)
    # 是否已触发告警（避免重复通知，周期重置时清零）
    alerted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 预算周期开始时间
    period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now_utc
    )
    # 预算周期结束时间
    period_end: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        CheckConstraint("budget_limit >= 0", name="ck_budget_limit_positive"),
        CheckConstraint("current_usage >= 0", name="ck_budget_usage_positive"),
        CheckConstraint(
            "alert_threshold > 0 AND alert_threshold <= 1",
            name="ck_budget_threshold_range",
        ),
        Index("ix_budget_tenant_type", "tenant_id", "budget_type"),
    )


class BillingRecord(Base):
    """API 计费记录

    按请求粒度记录每次 API 调用的计费信息，包括端点、方法、token 用量与成本。
    供账单汇总、按用户/端点聚合与导出使用。
    """

    __tablename__ = "billing_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # 调用方用户 ID（可能为 API Key 关联的 user_id 或 JWT sub）
    user_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # API 端点路径（如 /api/v1/evaluations）
    api_endpoint: Mapped[str] = mapped_column(String(512), nullable=False)
    # HTTP 方法（GET/POST/PUT/DELETE 等）
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    # 本次请求消耗的 token 数
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 本次请求成本（美元）
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # 计费时间点
    billed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now_utc
    )
    # 账单周期（如 2026-07 表示 2026 年 7 月账单）
    invoice_period: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    __table_args__ = (
        Index("ix_billing_tenant_billed", "tenant_id", "billed_at"),
        Index("ix_billing_tenant_user", "tenant_id", "user_id"),
        Index("ix_billing_tenant_endpoint", "tenant_id", "api_endpoint"),
    )
