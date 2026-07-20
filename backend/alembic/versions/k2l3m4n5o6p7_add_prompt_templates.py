"""add prompt_templates (template library) and agent_presets tables

Revision ID: k2l3m4n5o6p7
Revises: j1k2l3m4n5o6
Create Date: 2026-07-20 02:00:00.000000

提示词模板库 + Agent预设 (对标 LobeChat/Open WebUI 模板 + ChatGPT GPTs):
- prompt_templates: 用户面向的提示词模板库 (代码审查/周报/绩效面谈/数据分析/翻译润色)
  注意: prompt_templates 表名与 d5e6f7a8b9c0_add_prompt_management 迁移重名,
  那张表是 Langfuse 风格版本管理实体; 本迁移通过 inspector 幂等检查,
  已存在则跳过(不重建), 新增字段由应用层 ORM extend_existing 处理。
- agent_presets: Agent预设市场 (代码助手/HR顾问/数据分析师/文案写手/技术文档专家)

幂等: 用 inspector 检查表是否存在再 CREATE, 兼容已通过 create_all 建表的环境。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "k2l3m4n5o6p7"
down_revision: Union[str, Sequence[str], None] = "j1k2l3m4n5o6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_PROMPT_TEMPLATES = "prompt_templates"
_AGENT_PRESETS = "agent_presets"


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
    """Upgrade schema: 创建 prompt_templates(模板库) / agent_presets 表.

    注意: prompt_templates 表可能已由 d5e6f7a8b9c0_add_prompt_management 迁移创建
    (Langfuse 风格版本管理), 这里通过 inspector 检查幂等跳过, 避免冲突。
    agent_presets 是全新表, 直接创建。
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ---- prompt_templates 表 (模板库视角) ----
    # 幂等: 表已存在则跳过(兼容 d5e6f7a8b9c0 已创建的场景)
    if not _has_table(inspector, _PROMPT_TEMPLATES):
        op.create_table(
            _PROMPT_TEMPLATES,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column(
                "category",
                sa.String(length=64),
                nullable=False,
                server_default="general",
            ),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("variables", sa.JSON(), nullable=True),
            sa.Column(
                "is_builtin", sa.Boolean(), nullable=False, server_default="0"
            ),
            sa.Column(
                "is_public", sa.Boolean(), nullable=False, server_default="1"
            ),
            sa.Column("created_by", sa.Integer(), nullable=True),
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
        op.create_index(
            op.f("ix_prompt_templates_id"), _PROMPT_TEMPLATES, ["id"]
        )
        op.create_index(
            op.f("ix_prompt_templates_category"),
            _PROMPT_TEMPLATES,
            ["category"],
        )
        op.create_index(
            op.f("ix_prompt_templates_is_public"),
            _PROMPT_TEMPLATES,
            ["is_public"],
        )

    # ---- agent_presets 表 ----
    if not _has_table(inspector, _AGENT_PRESETS):
        op.create_table(
            _AGENT_PRESETS,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("avatar", sa.String(length=512), nullable=True),
            sa.Column("system_prompt", sa.Text(), nullable=False),
            sa.Column(
                "category",
                sa.String(length=64),
                nullable=False,
                server_default="general",
            ),
            sa.Column("tags", sa.JSON(), nullable=True),
            sa.Column(
                "model_tier",
                sa.String(length=10),
                nullable=False,
                server_default="L1",
            ),
            sa.Column("enabled_tools", sa.JSON(), nullable=True),
            sa.Column(
                "temperature", sa.Integer(), nullable=False, server_default="70"
            ),
            sa.Column(
                "is_builtin", sa.Boolean(), nullable=False, server_default="0"
            ),
            sa.Column(
                "is_public", sa.Boolean(), nullable=False, server_default="1"
            ),
            sa.Column(
                "use_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("created_by", sa.Integer(), nullable=True),
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
        op.create_index(
            op.f("ix_agent_presets_id"), _AGENT_PRESETS, ["id"]
        )
        op.create_index(
            op.f("ix_agent_presets_category"), _AGENT_PRESETS, ["category"]
        )
        op.create_index(
            op.f("ix_agent_presets_is_public"), _AGENT_PRESETS, ["is_public"]
        )
        op.create_index(
            op.f("ix_agent_presets_is_builtin"), _AGENT_PRESETS, ["is_builtin"]
        )


def downgrade() -> None:
    """Downgrade schema: 删除 agent_presets 表.

    注意: prompt_templates 表不删除(可能被 d5e6f7a8b9c0 迁移管理),
    仅删除本迁移创建的 agent_presets。
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, _AGENT_PRESETS):
        op.drop_table(_AGENT_PRESETS)
