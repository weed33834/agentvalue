"""add chat tables (session/message/part for streaming chat agent)

Revision ID: g8h9i0j1k2l3
Revises: f7a8b9c0d1e2
Create Date: 2026-07-19 00:00:00.000000

移植 opencode Session/Message/Part 三层数据模型：
- chat_sessions: 聊天会话（含 title / model / agent 配置）
- chat_messages: 一条消息（user / assistant）
- chat_parts: 消息内的 part（text / reasoning / tool / step-* / file）

幂等: 用 inspector 检查表是否存在再 CREATE，兼容已通过 create_all 建表的环境。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "g8h9i0j1k2l3"
down_revision: Union[str, Sequence[str], None] = "b4c5d6e7f8a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLES = ["chat_sessions", "chat_messages", "chat_parts"]


def _has_table(inspector, name: str) -> bool:
    try:
        return name in inspector.get_table_names()
    except Exception:
        return False


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. chat_sessions
    if not _has_table(inspector, "chat_sessions"):
        op.create_table(
            "chat_sessions",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.String(length=64),
                nullable=False,
                server_default="default",
            ),
            sa.Column("user_id", sa.String(length=64), nullable=False),
            sa.Column(
                "title", sa.String(length=256), nullable=False, server_default="新对话"
            ),
            sa.Column(
                "model_name",
                sa.String(length=128),
                nullable=False,
                server_default="gpt-4o-mini",
            ),
            sa.Column("provider", sa.String(length=64), nullable=True),
            sa.Column(
                "agent_name",
                sa.String(length=64),
                nullable=False,
                server_default="assistant",
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
            sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        )
        op.create_index(
            "ix_chat_session_tenant_user", "chat_sessions", ["tenant_id", "user_id"]
        )
        op.create_index("ix_chat_sessions_tenant_id", "chat_sessions", ["tenant_id"])

    # 2. chat_messages
    if not _has_table(inspector, "chat_messages"):
        op.create_table(
            "chat_messages",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("session_id", sa.String(length=64), nullable=False),
            sa.Column("role", sa.String(length=16), nullable=False),
            sa.Column("parent_id", sa.String(length=64), nullable=True),
            sa.Column("model_id", sa.String(length=128), nullable=True),
            sa.Column("provider_id", sa.String(length=64), nullable=True),
            sa.Column("tokens", sa.JSON(), nullable=True),
            sa.Column("cost", sa.Float(), nullable=False, server_default="0"),
            sa.Column("finish_reason", sa.String(length=32), nullable=True),
            sa.Column("error", sa.JSON(), nullable=True),
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
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["session_id"], ["chat_sessions.id"], ondelete="CASCADE"
            ),
        )
        op.create_index(
            "ix_chat_msg_session_created", "chat_messages", ["session_id", "created_at"]
        )
        op.create_index("ix_chat_messages_tenant_id", "chat_messages", ["tenant_id"])
        op.create_index("ix_chat_messages_session_id", "chat_messages", ["session_id"])

    # 3. chat_parts
    if not _has_table(inspector, "chat_parts"):
        op.create_table(
            "chat_parts",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("message_id", sa.String(length=64), nullable=False),
            sa.Column("session_id", sa.String(length=64), nullable=False),
            sa.Column("type", sa.String(length=32), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("text", sa.Text(), nullable=True),
            sa.Column("tool_name", sa.String(length=128), nullable=True),
            sa.Column("tool_call_id", sa.String(length=64), nullable=True),
            sa.Column("tool_state", sa.JSON(), nullable=True),
            sa.Column("step_index", sa.Integer(), nullable=True),
            sa.Column("metadata", sa.JSON(), nullable=True),
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
            sa.ForeignKeyConstraint(
                ["message_id"], ["chat_messages.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["session_id"], ["chat_sessions.id"], ondelete="CASCADE"
            ),
        )
        op.create_index(
            "ix_chat_part_msg_seq", "chat_parts", ["message_id", "sequence"]
        )
        op.create_index("ix_chat_parts_message_id", "chat_parts", ["message_id"])
        op.create_index("ix_chat_parts_session_id", "chat_parts", ["session_id"])
        op.create_index("ix_chat_parts_tool_call_id", "chat_parts", ["tool_call_id"])
        op.create_index("ix_chat_parts_tenant_id", "chat_parts", ["tenant_id"])


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table in reversed(_TABLES):
        if _has_table(inspector, table):
            op.drop_table(table)
