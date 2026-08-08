"""add webhook subscription / delivery tables (WS-3 出站 Webhook + API Key 扩展)

Revision ID: p3c3webhook0
Revises: p2b2evalexp0
Create Date: 2026-08-08 00:00:00.000000

新增 2 张表, 支撑出站 Webhook 订阅与投递 (对标 Svix / Stripe Webhooks):

- models/webhook_subscription.py: webhook_subscriptions   (订阅注册表: url / events[] / secret / headers / enabled)
- models/webhook_subscription.py: webhook_deliveries      (投递日志 + 重试队列: 载荷 / 响应码 / 耗时 / 死信)

同时为 ``api_keys`` 表补齐 WS-3 的 4 个扩展列 (scopes / rate_limit / expires_at /
last_used_at) —— 模型 models/models.py 已声明, 此处用 batch_alter_table + 列存在性
检查保证幂等: 已由 n5o6p7q8r9s0 创建的环境直接跳过, 走链式 upgrade 的环境补上。

幂等: 表创建用 inspector 检查是否存在再 CREATE, 兼容已通过 create_all 建表的环境。
表创建顺序按逻辑依赖排列 (父表 webhook_subscriptions 先于子表 webhook_deliveries),
downgrade 用 reversed 顺序 drop; 两张表均无 DB 外键 (逻辑外键, 与仓库约定一致)。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "p3c3webhook0"
down_revision: Union[str, Sequence[str], None] = "p2b2evalexp0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 按逻辑依赖顺序排列（父表在前，子表在后），downgrade 用 reversed 顺序 drop
_NEW_TABLES = [
    "webhook_subscriptions",  # 订阅注册表
    "webhook_deliveries",     # 引用 webhook_subscriptions.id (逻辑外键, 无 DB FK)
]

# 本轮为 api_keys 表补齐的 WS-3 扩展列（models/models.py 已声明）
_API_KEY_EXTRA_COLUMNS = {
    "scopes": sa.Column("scopes", sa.Text(), nullable=True),
    "rate_limit": sa.Column(
        "rate_limit", sa.Integer(), nullable=True, server_default="60"
    ),
    "expires_at": sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    "last_used_at": sa.Column(
        "last_used_at", sa.DateTime(timezone=True), nullable=True
    ),
}


def _has_table(inspector, name: str) -> bool:
    try:
        return name in inspector.get_table_names()
    except Exception:
        return False


def _has_column(inspector, table: str, column: str) -> bool:
    try:
        return column in {col["name"] for col in inspector.get_columns(table)}
    except Exception:
        return False


def upgrade() -> None:
    """Upgrade schema: 新增 2 张 webhook 表 + api_keys 扩展列."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ==================================================================
    # 1. webhook_subscriptions (models/webhook_subscription.py)
    # ==================================================================
    if not _has_table(inspector, "webhook_subscriptions"):
        op.create_table(
            "webhook_subscriptions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "tenant_id",
                sa.String(64),
                nullable=False,
                index=True,
                server_default="default",
            ),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("url", sa.String(1024), nullable=False),
            sa.Column("events", sa.JSON(), nullable=False),
            sa.Column("secret", sa.String(128), nullable=False),
            sa.Column("headers", sa.JSON(), nullable=True),
            sa.Column(
                "enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("1"),
            ),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="6"),
            sa.Column(
                "timeout_seconds", sa.Integer(), nullable=False, server_default="10"
            ),
            sa.Column("created_by", sa.String(64), nullable=True),
            sa.Column(
                "last_delivery_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column("last_status", sa.String(16), nullable=True),
            sa.Column(
                "consecutive_failures",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column("disabled_reason", sa.Text(), nullable=True),
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
            sa.Index("ix_webhook_sub_tenant_enabled", "tenant_id", "enabled"),
        )

    # ==================================================================
    # 2. webhook_deliveries (models/webhook_subscription.py)
    # ==================================================================
    if not _has_table(inspector, "webhook_deliveries"):
        op.create_table(
            "webhook_deliveries",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "subscription_id", sa.Integer(), nullable=False, index=True
            ),
            sa.Column(
                "tenant_id",
                sa.String(64),
                nullable=False,
                index=True,
                server_default="default",
            ),
            sa.Column("event", sa.String(128), nullable=False, index=True),
            sa.Column("event_id", sa.String(128), nullable=True, index=True),
            sa.Column("payload", sa.JSON(), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "max_attempts", sa.Integer(), nullable=False, server_default="6"
            ),
            sa.Column(
                "next_retry_at", sa.DateTime(timezone=True), nullable=True, index=True
            ),
            sa.Column("response_code", sa.Integer(), nullable=True),
            sa.Column("response_body", sa.Text(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
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
            sa.Index(
                "ix_webhook_delivery_status_retry", "status", "next_retry_at"
            ),
            sa.Index(
                "ix_webhook_delivery_sub_created",
                "subscription_id",
                "created_at",
            ),
            sa.Index(
                "ix_webhook_delivery_tenant_status", "tenant_id", "status"
            ),
            sa.UniqueConstraint(
                "subscription_id", "event_id", name="uq_webhook_delivery_sub_event"
            ),
        )

    # ==================================================================
    # 3. api_keys 补齐 WS-3 扩展列 (幂等: 已存在则跳过)
    # ==================================================================
    if _has_table(inspector, "api_keys"):
        missing = {
            col: definition
            for col, definition in _API_KEY_EXTRA_COLUMNS.items()
            if not _has_column(inspector, "api_keys", col)
        }
        for column_name, definition in missing.items():
            with op.batch_alter_table("api_keys") as batch_op:
                batch_op.add_column(definition)
            # inspector 缓存按需刷新, 逐个列处理
            inspector = sa.inspect(bind)


def downgrade() -> None:
    """Downgrade schema: 先删 api_keys 扩展列, 再按依赖逆序删表."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, "api_keys"):
        for column_name in _API_KEY_EXTRA_COLUMNS:
            if _has_column(inspector, "api_keys", column_name):
                with op.batch_alter_table("api_keys") as batch_op:
                    batch_op.drop_column(column_name)
                inspector = sa.inspect(bind)

    for table in reversed(_NEW_TABLES):
        op.drop_table(table)
