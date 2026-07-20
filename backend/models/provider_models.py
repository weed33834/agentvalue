"""
P2 深水区 - Provider CRUD 数据模型

对标 Dify Model Provider 管理 (https://github.com/langgenius/dify/blob/main/api/models/provider.py)

8 张表:
- ProviderTemplate: Provider 模板(静态注册,seed 数据)
- TenantProvider: 租户 Provider 绑定 + 激活凭证指针
- TenantProviderCredential: 多凭证存储(支持负载均衡)
- TenantProviderModel: 模型启用表
- TenantProviderModelCredential: 模型级多凭证(customizable-model + LB)
- TenantDefaultModel: 默认模型(每 tenant 每 model_type 唯一)
- ModelTemplate: 模型能力声明(预定义模型,seed 数据)
- ProviderHealthCheck: 健康检查记录
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from core.database import Base
from models.models import DEFAULT_TENANT_ID, now_utc


class ProviderTemplate(Base):
    """Provider 模板(静态注册,对标 Dify ProviderEntity)。

    每个 provider(openai/anthropic/gemini/ollama)对应一行,声明:
    - 支持的模型类型(llm/embedding/rerank/vision)
    - 配置方式(predefined-model / customizable-model / fetch-from-remote)
    - 凭证表单 schema(provider_credential_schema)
    - 模型凭证表单 schema(model_credential_schema, customizable 才有)

    seed 数据由 core/providers/seed.py 初始化。
    """

    __tablename__ = "provider_templates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    label: Mapped[dict] = mapped_column(JSON, nullable=False)
    description: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    icon_small: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    icon_large: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    background: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    supported_model_types: Mapped[list] = mapped_column(JSON, nullable=False)
    configurate_methods: Mapped[list] = mapped_column(JSON, nullable=False)
    provider_credential_schema: Mapped[dict] = mapped_column(JSON, nullable=False)
    model_credential_schema: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class TenantProvider(Base):
    """租户 Provider 绑定 + 激活凭证指针(对标 Dify Provider 表)。

    一个 tenant 可以绑定多个 provider,每个 provider 有:
    - provider_type: custom(自建凭证) | system(管理员共享)
    - active_credential_id: 当前激活的凭证(多凭证切换用)
    - preferred_type: 优先 custom 还是 system
    - is_valid: 凭证是否通过校验
    """

    __tablename__ = "tenant_providers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(16), nullable=False, default="custom")
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    active_credential_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    preferred_type: Mapped[str] = mapped_column(String(16), nullable=False, default="custom")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider", "provider_type", name="uix_tenant_provider_type"
        ),
        Index("ix_tenant_providers_tid_provider", "tenant_id", "provider"),
    )


class TenantProviderCredential(Base):
    """多凭证存储(对标 Dify provider_credentials 表 v1.8.0)。

    一个 (tenant, provider) 可以有多行凭证,支持:
    - 负载均衡:多个 API Key 轮询
    - 冷却熔断:失败后 cooldown_until 期间跳过
    - 切换激活:通过 tenant_providers.active_credential_id 指针

    encrypted_config: AES-256-GCM 加密的 JSON 凭证(明文不入库)
    """

    __tablename__ = "tenant_provider_credentials"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_name: Mapped[str] = mapped_column(String(128), nullable=False)
    encrypted_config: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="team")
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_validated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cooldown_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        Index("ix_tpc_tid_provider", "tenant_id", "provider"),
    )


class TenantProviderModel(Base):
    """模型启用表(对标 Dify provider_models 表)。

    一个 (tenant, provider, model_name, model_type) 对应一行,声明:
    - enabled: 是否启用该模型
    - load_balancing_enabled: 是否开启负载均衡
    - active_credential_id: 模型级激活凭证(覆盖 provider 级)
    """

    __tablename__ = "tenant_provider_models"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_type: Mapped[str] = mapped_column(String(32), nullable=False)
    active_credential_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    load_balancing_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "provider",
            "model_name",
            "model_type",
            name="uix_tenant_provider_model",
        ),
    )


class TenantProviderModelCredential(Base):
    """模型级多凭证(对标 Dify provider_model_credentials 表)。

    用于 customizable-model 场景:同一 provider 下不同模型可能有不同凭证。
    也用于负载均衡:一个模型多组凭证轮询。
    """

    __tablename__ = "tenant_provider_model_credentials"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_type: Mapped[str] = mapped_column(String(32), nullable=False)
    credential_name: Mapped[str] = mapped_column(String(128), nullable=False)
    encrypted_config: Mapped[str] = mapped_column(Text, nullable=False)
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cooldown_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        Index(
            "ix_tpmc_tid_provider_model",
            "tenant_id",
            "provider",
            "model_name",
            "model_type",
        ),
    )


class TenantDefaultModel(Base):
    """默认模型(每 tenant 每 model_type 唯一,对标 Dify tenant_default_models)。

    用于应用层选择默认模型:不指定 model 时用 default。
    """

    __tablename__ = "tenant_default_models"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    model_type: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "model_type", name="uix_tenant_default_model"),
    )


class ModelTemplate(Base):
    """模型能力声明(对标 Dify AIModelEntity)。

    每个 (provider, model, model_type) 对应一行,声明:
    - features: ['chat', 'vision', 'function_calling', 'stream_tool_call']
    - model_properties: {mode, context_size, max_tokens}
    - parameter_rules: 推理参数 schema(temperature/top_p 等)
    - pricing: {input_per_1k, output_per_1k, currency}

    seed 数据由 core/providers/seed.py 初始化。
    """

    __tablename__ = "model_templates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    label: Mapped[dict] = mapped_column(JSON, nullable=False)
    model_type: Mapped[str] = mapped_column(String(32), nullable=False)
    features: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    model_properties: Mapped[dict] = mapped_column(JSON, nullable=False)
    parameter_rules: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    pricing: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )

    __table_args__ = (
        UniqueConstraint(
            "provider", "model", "model_type", name="uix_model_template"
        ),
    )


class ProviderHealthCheck(Base):
    """健康检查记录(对标 Dify provider_health_checks)。

    主动 ping 历史记录,用于:
    - 卡片上显示绿/黄/红圆点
    - 详情页查看历史检查结果
    - 聚合状态: healthy / degraded / down
    """

    __tablename__ = "provider_health_checks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    model_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )

    __table_args__ = (
        Index(
            "ix_phc_tid_provider_checked",
            "tenant_id",
            "provider",
        ),
    )
