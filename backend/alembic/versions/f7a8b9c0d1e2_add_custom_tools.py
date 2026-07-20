"""add custom_tools table (p3-1 custom tool upload via openapi schema)

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-07-13 00:30:00.000000

P3-1 自定义工具上传 (OpenAPI Schema 导入,对标 Dify Custom Tool):
- custom_tools: 用户粘贴 OpenAPI JSON/YAML → 解析 paths → 每个 operation 生成一个 LangChain Tool

幂等: 用 inspector 检查表是否存在再 CREATE,兼容已通过 create_all 建表的环境。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, Sequence[str], None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "custom_tools"


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
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column(
                "description",
                sa.String(length=512),
                nullable=False,
                server_default="",
            ),
            sa.Column("openapi_schema", sa.JSON(), nullable=False),
            sa.Column("base_url", sa.String(length=512), nullable=False),
            sa.Column(
                "auth_type",
                sa.String(length=32),
                nullable=False,
                server_default="none",
            ),
            sa.Column("auth_credentials", sa.String(length=512), nullable=True),
            sa.Column(
                "enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("1"),
            ),
            sa.Column(
                "tenant_id",
                sa.String(length=64),
                nullable=False,
                server_default="default",
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
            # 按租户内 name 唯一,避免同租户工具重名
            sa.UniqueConstraint(
                "tenant_id", "name", name="uix_custom_tools_tenant_name"
            ),
        )
        # name 索引: 列表搜索/排序常用
        op.create_index("ix_custom_tools_name", _TABLE, ["name"])
        # tenant_id 索引: 多租户过滤
        op.create_index("ix_custom_tools_tenant", _TABLE, ["tenant_id"])


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, _TABLE):
        op.drop_table(_TABLE)
