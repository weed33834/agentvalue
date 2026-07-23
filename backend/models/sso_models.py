"""SSO 单点登录数据模型

对标 Dify SSO / Bisheng SSO, 支持三种协议:
- OAuth2: {client_id, client_secret, authorize_url, token_url, userinfo_url, scopes, redirect_uri}
- SAML:   {entity_id, sso_url, certificate, attribute_mapping}
- LDAP:   {server_url, bind_dn, bind_password, search_base, search_filter, attribute_mapping}

安全注意: config 字段中的敏感信息 (client_secret / bind_password / certificate)
在 API 响应中必须脱敏, 由 SSOService._mask_config 统一处理。
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


def _now_utc() -> datetime:
    """当前 UTC 时间"""
    return datetime.now(timezone.utc)


class SSOConfig(Base):
    """SSO 配置 (每个租户可配置多个 SSO Provider)

    provider_type: oauth2 / saml / ldap
    config (JSON) 按 provider_type 结构不同:
      - oauth2: {client_id, client_secret, authorize_url, token_url, userinfo_url, scopes, redirect_uri}
      - saml:   {entity_id, sso_url, certificate, attribute_mapping}
      - ldap:   {server_url, bind_dn, bind_password, search_base, search_filter, attribute_mapping}
    """

    __tablename__ = "sso_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default="default"
    )
    # 提供商名称 (租户内唯一, 如 google / github / keycloak / ad)
    provider_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # 协议类型: oauth2 / saml / ldap
    provider_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # 配置 JSON (含敏感信息, API 响应需脱敏)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, onupdate=_now_utc
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "provider_name", name="uix_tenant_sso_provider"),
        Index("ix_sso_config_tenant_type", "tenant_id", "provider_type"),
    )


class SSOSession(Base):
    """SSO 会话记录 (外部用户 → 内部用户的映射关系 + token)

    用于追踪 SSO 登录会话, 支持单点登出与 token 刷新。
    """

    __tablename__ = "sso_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default="default"
    )
    provider_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # 外部系统用户 ID (如 OAuth2 sub / SAML nameid / LDAP dn)
    external_user_id: Mapped[str] = mapped_column(
        String(256), nullable=False, index=True
    )
    # 内部系统用户 ID (关联 users.user_id)
    internal_user_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    # OAuth2 access_token / SAML assertion (脱敏存储)
    access_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )

    __table_args__ = (
        Index("ix_sso_session_tenant_external", "tenant_id", "external_user_id"),
        Index("ix_sso_session_internal", "internal_user_id"),
    )
