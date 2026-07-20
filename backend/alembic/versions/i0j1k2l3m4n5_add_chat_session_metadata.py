"""add metadata column to chat_sessions (for share_id / fork origin etc.)

Revision ID: i0j1k2l3m4n5
Revises: h9i0j1k2l3m4
Create Date: 2026-07-20 00:00:00.000000

为 chat_sessions 表添加 metadata JSON 列（映射到 ORM 的 metadata_ 属性），
用于存储会话级扩展信息（如对话分享 share_id、fork 来源等）。

幂等: 用 inspector 检查列是否存在再 ADD COLUMN，兼容已通过 create_all 建表的环境。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "i0j1k2l3m4n5"
down_revision: Union[str, Sequence[str], None] = "h9i0j1k2l3m4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    try:
        return column_name in [c["name"] for c in inspector.get_columns(table_name)]
    except Exception:
        return False


def upgrade() -> None:
    """Upgrade schema: 为 chat_sessions 添加 metadata JSON 列。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_column(inspector, "chat_sessions", "metadata"):
        op.add_column(
            "chat_sessions",
            sa.Column("metadata", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    """Downgrade schema: 移除 chat_sessions.metadata 列。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_column(inspector, "chat_sessions", "metadata"):
        op.drop_column("chat_sessions", "metadata")
