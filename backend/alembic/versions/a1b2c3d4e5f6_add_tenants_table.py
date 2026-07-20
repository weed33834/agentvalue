"""add tenants table

Revision ID: a1b2c3d4e5f6
Revises: 2d49c8ec0ef7
Create Date: 2026-07-05 16:00:00.000000

P1-N2: 多租户顶层主体表。tenant_id 全局唯一,作为 users / evaluations /
raw_inputs / audit_logs / feedback 等业务表 tenant_id 外键的源头。
单租户兼容场景由 default 租户行承载,DEFAULT_TENANT_ID = "default"。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "2d49c8ec0ef7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # tenants 表:多租户隔离的顶层主体
    # 字段对齐 backend/models/models.py 的 Tenant 模型
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "tenants" not in existing_tables:
        op.create_table(
            "tenants",
            sa.Column("tenant_id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column(
                "plan", sa.String(length=32), nullable=False, server_default="free"
            ),
            sa.Column(
                "status", sa.String(length=16), nullable=False, server_default="active"
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.PrimaryKeyConstraint("tenant_id"),
        )
        op.create_index(
            op.f("ix_tenants_tenant_id"), "tenants", ["tenant_id"], unique=True
        )

        # 兼容性种子:default 租户,承载单租户历史数据
        op.execute(
            sa.text(
                "INSERT INTO tenants (tenant_id, name, plan, status, created_at) "
                "VALUES ('default', 'Default Tenant', 'free', 'active', "
                "CURRENT_TIMESTAMP)"
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "tenants" in existing_tables:
        op.drop_index(op.f("ix_tenants_tenant_id"), table_name="tenants")
        op.drop_table("tenants")
