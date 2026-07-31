"""add chat_artifacts table (对标 Claude Artifacts / ChatGPT Canvas)

Revision ID: l3m4n5o6p7q8
Revises: k2l3m4n5o6p7
Create Date: 2026-07-20 03:00:00.000000

Artifacts 可视化:
- chat_artifacts: 对话中生成的可交互产物 (HTML/SVG/Mermaid/Markdown/React/Code/JSON)
  - session_id / message_id 关联会话与消息
  - artifact_type: html/svg/mermaid/markdown/code/react/json
  - version: 版本号, 更新时 +1
  - metadata: 元数据 (fork 来源等扩展信息)

幂等: 用 inspector 检查表是否存在再 CREATE, 兼容已通过 create_all 建表的环境。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "l3m4n5o6p7q8"
down_revision: Union[str, Sequence[str], None] = "k2l3m4n5o6p7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ARTIFACTS = "chat_artifacts"


def _has_table(inspector, name: str) -> bool:
    try:
        return name in inspector.get_table_names()
    except Exception:
        return False


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    try:
        return index_name in [i["name"] for i in inspector.get_indexes(table_name)]
    except Exception:
        return False


def upgrade() -> None:
    """Upgrade schema: 创建 chat_artifacts 表."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ---- chat_artifacts 表 ----
    if not _has_table(inspector, _ARTIFACTS):
        op.create_table(
            _ARTIFACTS,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "session_id",
                sa.String(length=64),
                sa.ForeignKey("chat_sessions.id"),
                nullable=False,
            ),
            sa.Column(
                "message_id",
                sa.String(length=64),
                sa.ForeignKey("chat_messages.id"),
                nullable=True,
            ),
            sa.Column("name", sa.String(length=256), nullable=True),
            sa.Column("artifact_type", sa.String(length=32), nullable=False),
            sa.Column("language", sa.String(length=32), nullable=True),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("metadata", sa.JSON(), nullable=True),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        if not _has_index(inspector, _ARTIFACTS, op.f("ix_chat_artifacts_id")):
            op.create_index(op.f("ix_chat_artifacts_id"), _ARTIFACTS, ["id"])
        if not _has_index(inspector, _ARTIFACTS, op.f("ix_chat_artifacts_session_id")):
            op.create_index(
                op.f("ix_chat_artifacts_session_id"), _ARTIFACTS, ["session_id"]
            )
        if not _has_index(inspector, _ARTIFACTS, op.f("ix_chat_artifacts_message_id")):
            op.create_index(
                op.f("ix_chat_artifacts_message_id"),
                _ARTIFACTS,
                ["message_id"],
            )
        if not _has_index(
            inspector, _ARTIFACTS, op.f("ix_chat_artifacts_artifact_type")
        ):
            op.create_index(
                op.f("ix_chat_artifacts_artifact_type"),
                _ARTIFACTS,
                ["artifact_type"],
            )


def downgrade() -> None:
    """Downgrade schema: 删除 chat_artifacts 表."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, _ARTIFACTS):
        op.drop_table(_ARTIFACTS)
