"""
模型负载均衡数据模型

对标阿里百炼 AI 网关 GPU 感知负载均衡：
- ModelInstance: 模型实例（多 provider/多实例），含权重、并发限制、健康状态、延迟
- LoadBalancerConfig: 负载均衡策略配置（round_robin/weighted/least_connections/latency_aware）

多租户隔离: 所有模型包含 tenant_id 字段，未显式指定时落 DEFAULT_TENANT_ID。
api_key_ref 只存储引用名称（如 "env:OPENAI_API_KEY"），不存明文密钥。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base
from models.models import DEFAULT_TENANT_ID, now_utc


class ModelInstance(Base):
    """模型实例

    描述一个可用的模型实例（OpenAI/本地/Azure/Anthropic），含权重、最大并发、
    当前并发数、健康状态与平均延迟。select_instance 根据策略选择最优实例。

    api_key_ref 只存储引用名称（如 "env:OPENAI_API_KEY"），不存明文密钥，
    运行时通过 credential_service 解析为真实密钥。
    """

    __tablename__ = "model_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # 实例名称（同租户内建议唯一）
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Provider 类型: openai | local | azure | anthropic
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    # 模型名称
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # API base URL
    base_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # API Key 引用（如 "env:OPENAI_API_KEY"），不存明文
    api_key_ref: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    # 权重（默认 1，越大被选中概率越高）
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 最大并发数（默认 10）
    max_concurrent: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    # 当前并发数（默认 0，由 acquire/release 维护）
    current_load: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 健康状态: healthy | unhealthy | degraded
    health_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="healthy"
    )
    # 上次健康检查时间
    last_health_check: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 平均延迟（毫秒）
    avg_latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # 是否启用
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        # 按租户 + 启用状态检索（选择实例时过滤）
        Index("ix_model_instance_tenant_enabled", "tenant_id", "enabled"),
        # 按租户 + provider 检索（按 provider 分组管理）
        Index("ix_model_instance_tenant_provider", "tenant_id", "provider"),
    )


class LoadBalancerConfig(Base):
    """负载均衡配置

    描述一组实例的负载均衡策略，instances 为 instance_id 列表 + 权重。
    select_instance 根据策略从关联实例中选择最优实例。

    strategy:
    - round_robin:      轮询
    - weighted:         按权重随机
    - least_connections: 最少连接数
    - latency_aware:    最低延迟优先
    """

    __tablename__ = "load_balancer_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # 配置名称（同租户内建议唯一）
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 负载均衡策略
    strategy: Mapped[str] = mapped_column(
        String(32), nullable=False, default="round_robin"
    )
    # instances JSON: [{"instance_id": 1, "weight": 2}, ...]
    instances: Mapped[List[Dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    # 是否启用
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        # 按租户 + 名称检索
        Index("ix_lb_config_tenant_name", "tenant_id", "name"),
    )
