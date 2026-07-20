"""add provider crud tables (p2 deep dive)

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-07-12 23:30:00.000000

P2 深水区 - Provider CRUD:对标 Dify Model Provider 管理 (https://github.com/langgenius/dify)

新增 8 张表:
- provider_templates: Provider 模板(静态注册,seed 数据)
- tenant_providers: 租户 Provider 绑定 + 激活凭证指针
- tenant_provider_credentials: 多凭证存储(支持负载均衡)
- tenant_provider_models: 模型启用表
- tenant_provider_model_credentials: 模型级多凭证(customizable-model + LB)
- tenant_default_models: 默认模型(每 tenant 每 model_type 唯一)
- model_templates: 模型能力声明(预定义模型,seed 数据)
- provider_health_checks: 健康检查记录

幂等: 用 inspector 检查表是否存在再 CREATE,兼容已通过 create_all 建表的环境。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, Sequence[str], None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NEW_TABLES = [
    "provider_templates",
    "tenant_providers",
    "tenant_provider_credentials",
    "tenant_provider_models",
    "tenant_provider_model_credentials",
    "tenant_default_models",
    "model_templates",
    "provider_health_checks",
]


def _has_table(inspector, name: str) -> bool:
    try:
        return name in inspector.get_table_names()
    except Exception:
        return False


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1) provider_templates: Provider 模板(静态注册)
    if not _has_table(inspector, "provider_templates"):
        op.create_table(
            "provider_templates",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("provider", sa.String(64), nullable=False, unique=True),
            sa.Column("label", sa.JSON, nullable=False),
            sa.Column("description", sa.JSON, nullable=True),
            sa.Column("icon_small", sa.String(255), nullable=True),
            sa.Column("icon_large", sa.String(255), nullable=True),
            sa.Column("background", sa.String(16), nullable=True),
            sa.Column(
                "supported_model_types",
                sa.JSON,
                nullable=False,
            ),
            sa.Column("configurate_methods", sa.JSON, nullable=False),
            sa.Column("provider_credential_schema", sa.JSON, nullable=False),
            sa.Column("model_credential_schema", sa.JSON, nullable=True),
            sa.Column(
                "is_builtin", sa.Boolean, nullable=False, server_default=sa.text("1")
            ),
            sa.Column(
                "enabled", sa.Boolean, nullable=False, server_default=sa.text("1")
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_provider_templates_provider", "provider_templates", ["provider"]
        )

    # 2) tenant_providers: 租户 Provider 绑定 + 激活凭证指针
    if not _has_table(inspector, "tenant_providers"):
        op.create_table(
            "tenant_providers",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
            sa.Column("provider", sa.String(64), nullable=False),
            sa.Column(
                "provider_type",
                sa.String(16),
                nullable=False,
                server_default="custom",
            ),
            sa.Column(
                "is_valid", sa.Boolean, nullable=False, server_default=sa.text("0")
            ),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("active_credential_id", sa.String(64), nullable=True),
            sa.Column(
                "preferred_type",
                sa.String(16),
                nullable=False,
                server_default="custom",
            ),
            sa.Column(
                "enabled", sa.Boolean, nullable=False, server_default=sa.text("1")
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "provider",
                "provider_type",
                name="uix_tenant_provider_type",
            ),
        )
        op.create_index(
            "ix_tenant_providers_tid_provider", "tenant_providers", ["tenant_id", "provider"]
        )

    # 3) tenant_provider_credentials: 多凭证存储
    if not _has_table(inspector, "tenant_provider_credentials"):
        op.create_table(
            "tenant_provider_credentials",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
            sa.Column("provider", sa.String(64), nullable=False),
            sa.Column("credential_name", sa.String(128), nullable=False),
            sa.Column("encrypted_config", sa.Text, nullable=False),
            sa.Column("user_id", sa.String(64), nullable=True),
            sa.Column(
                "visibility", sa.String(32), nullable=False, server_default="team"
            ),
            sa.Column(
                "is_valid", sa.Boolean, nullable=False, server_default=sa.text("0")
            ),
            sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "failure_count", sa.Integer, nullable=False, server_default="0"
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_tpc_tid_provider",
            "tenant_provider_credentials",
            ["tenant_id", "provider"],
        )

    # 4) tenant_provider_models: 模型启用表
    if not _has_table(inspector, "tenant_provider_models"):
        op.create_table(
            "tenant_provider_models",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
            sa.Column("provider", sa.String(64), nullable=False),
            sa.Column("model_name", sa.String(128), nullable=False),
            sa.Column("model_type", sa.String(32), nullable=False),
            sa.Column("active_credential_id", sa.String(64), nullable=True),
            sa.Column(
                "is_valid", sa.Boolean, nullable=False, server_default=sa.text("0")
            ),
            sa.Column(
                "enabled", sa.Boolean, nullable=False, server_default=sa.text("1")
            ),
            sa.Column(
                "load_balancing_enabled",
                sa.Boolean,
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "provider",
                "model_name",
                "model_type",
                name="uix_tenant_provider_model",
            ),
        )

    # 5) tenant_provider_model_credentials: 模型级多凭证
    if not _has_table(inspector, "tenant_provider_model_credentials"):
        op.create_table(
            "tenant_provider_model_credentials",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
            sa.Column("provider", sa.String(64), nullable=False),
            sa.Column("model_name", sa.String(128), nullable=False),
            sa.Column("model_type", sa.String(32), nullable=False),
            sa.Column("credential_name", sa.String(128), nullable=False),
            sa.Column("encrypted_config", sa.Text, nullable=False),
            sa.Column(
                "is_valid", sa.Boolean, nullable=False, server_default=sa.text("0")
            ),
            sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "failure_count", sa.Integer, nullable=False, server_default="0"
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_tpmc_tid_provider_model",
            "tenant_provider_model_credentials",
            ["tenant_id", "provider", "model_name", "model_type"],
        )

    # 6) tenant_default_models: 默认模型
    if not _has_table(inspector, "tenant_default_models"):
        op.create_table(
            "tenant_default_models",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
            sa.Column("model_type", sa.String(32), nullable=False),
            sa.Column("provider", sa.String(64), nullable=False),
            sa.Column("model_name", sa.String(128), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "tenant_id", "model_type", name="uix_tenant_default_model"
            ),
        )

    # 7) model_templates: 模型能力声明
    if not _has_table(inspector, "model_templates"):
        op.create_table(
            "model_templates",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("provider", sa.String(64), nullable=False, index=True),
            sa.Column("model", sa.String(128), nullable=False),
            sa.Column("label", sa.JSON, nullable=False),
            sa.Column("model_type", sa.String(32), nullable=False),
            sa.Column("features", sa.JSON, nullable=True),
            sa.Column("model_properties", sa.JSON, nullable=False),
            sa.Column("parameter_rules", sa.JSON, nullable=True),
            sa.Column("pricing", sa.JSON, nullable=True),
            sa.Column(
                "enabled", sa.Boolean, nullable=False, server_default=sa.text("1")
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "provider", "model", "model_type", name="uix_model_template"
            ),
        )

    # 8) provider_health_checks: 健康检查记录
    if not _has_table(inspector, "provider_health_checks"):
        op.create_table(
            "provider_health_checks",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
            sa.Column("provider", sa.String(64), nullable=False),
            sa.Column("credential_id", sa.String(64), nullable=True),
            sa.Column("model_name", sa.String(128), nullable=True),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("latency_ms", sa.Integer, nullable=True),
            sa.Column("error_message", sa.Text, nullable=True),
            sa.Column(
                "checked_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_phc_tid_provider_checked",
            "provider_health_checks",
            ["tenant_id", "provider", sa.text("checked_at DESC")],
        )


def downgrade() -> None:
    """Downgrade schema."""
    for table in reversed(_NEW_TABLES):
        op.drop_table(table)
