"""add metadata column to chat_messages (for feedback etc.)

Revision ID: h9i0j1k2l3m4
Revises: g8h9i0j1k2l3
Create Date: 2026-07-20 00:00:00.000000

为 chat_messages 表添加 metadata JSON 列（映射到 ORM 的 metadata_ 属性），
用于存储消息级扩展信息（如点赞/点踩 feedback）。

幂等: 用 inspector 检查列是否存在再 ADD COLUMN，兼容已通过 create_all 建表的环境。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "h9i0j1k2l3m4"
down_revision: Union[str, Sequence[str], None] = "g8h9i0j1k2l3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    try:
        return column_name in [c["name"] for c in inspector.get_columns(table_name)]
    except Exception:
        return False


def upgrade() -> None:
    """Upgrade schema: 为 chat_messages 添加 metadata JSON 列。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_column(inspector, "chat_messages", "metadata"):
        op.add_column(
            "chat_messages",
            sa.Column("metadata", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    """Downgrade schema: 移除 chat_messages.metadata 列。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_column(inspector, "chat_messages", "metadata"):
        op.drop_column("chat_messages", "metadata")
