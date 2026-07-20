"""add feature_flags table (p3-2 feature flag system)

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-07-13 00:45:00.000000

P3-2 Feature Flag 系统 (对标 Langfuse Feature Flag):
- feature_flags: 应用级功能开关, 按 key 全局唯一, 支持 tenant/user/百分比分流

幂等: 用 inspector 检查表是否存在再 CREATE, 兼容已通过 create_all 建表的环境。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8b9c0d1e2f3"
down_revision: Union[str, Sequence[str], None] = "f7a8b9c0d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "feature_flags"


def _has_table(inspector, name: str) -> bool:
    try:
        return name in inspector.get_table_names()
    except Exception:
        return False


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_table(inspector, _TABLE):
        op.create_table(
            _TABLE,
            sa.Column("key", sa.String(length=64), primary_key=True),
            sa.Column(
                "description",
                sa.String(length=256),
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "rollout_percentage",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "target_tenant_ids",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
            sa.Column(
                "target_user_ids",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
            sa.Column(
                "category",
                sa.String(length=32),
                nullable=False,
                server_default="general",
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
            sa.CheckConstraint(
                "rollout_percentage >= 0 AND rollout_percentage <= 100",
                name="ck_feature_flag_rollout_range",
            ),
        )
        # category 索引: 列表按分类过滤常用
        op.create_index("ix_feature_flags_category", _TABLE, ["category"])
        # enabled 索引: 列表过滤常用
        op.create_index("ix_feature_flags_enabled", _TABLE, ["enabled"])


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, _TABLE):
        op.drop_table(_TABLE)
