"""add skills table (reusable skill modules, Claude/Trae Skills style)

Revision ID: m4n5o6p7q8r9
Revises: l3m4n5o6p7q8
Create Date: 2026-07-20 03:00:00.000000

Skill 模型 (对标 Claude Skills / Trae Skills):
- skills: 可复用的技能模块 = 系统提示词 + 工具配置 + 输入/输出schema
  - name: 技能名称(唯一)
  - system_prompt: 系统提示词
  - input_schema / output_schema: JSON, 输入参数与输出格式 schema
  - required_tools: JSON, 执行所需工具列表
  - model_tier / temperature: 推荐模型档位与温度
  - is_builtin / is_public / is_active: 内置/公开/激活标志
  - use_count: 使用次数(市场分发指标)
  - tags / config: 标签与额外配置

幂等: 用 inspector 检查表是否存在再 CREATE, 兼容已通过 create_all 建表的环境。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "m4n5o6p7q8r9"
down_revision: Union[str, Sequence[str], None] = "l3m4n5o6p7q8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SKILLS = "skills"


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
    """Upgrade schema: 创建 skills 表."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ---- skills 表 ----
    if not _has_table(inspector, _SKILLS):
        op.create_table(
            _SKILLS,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("display_name", sa.String(length=256), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column(
                "category",
                sa.String(length=64),
                nullable=False,
                server_default="general",
            ),
            sa.Column(
                "version",
                sa.String(length=32),
                nullable=False,
                server_default="1.0.0",
            ),
            sa.Column("system_prompt", sa.Text(), nullable=False),
            sa.Column("input_schema", sa.JSON(), nullable=True),
            sa.Column("output_schema", sa.JSON(), nullable=True),
            sa.Column("required_tools", sa.JSON(), nullable=True),
            sa.Column(
                "model_tier",
                sa.String(length=10),
                nullable=False,
                server_default="L1",
            ),
            sa.Column("temperature", sa.Integer(), nullable=False, server_default="70"),
            sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("is_public", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("tags", sa.JSON(), nullable=True),
            sa.Column("config", sa.JSON(), nullable=True),
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
            sa.UniqueConstraint("name", name=op.f("uq_skills_name")),
        )
        op.create_index(op.f("ix_skills_id"), _SKILLS, ["id"])
        op.create_index(op.f("ix_skills_name"), _SKILLS, ["name"], unique=True)
        op.create_index(op.f("ix_skills_category"), _SKILLS, ["category"])
        op.create_index(op.f("ix_skills_is_public"), _SKILLS, ["is_public"])
        op.create_index(op.f("ix_skills_is_builtin"), _SKILLS, ["is_builtin"])
        op.create_index(op.f("ix_skills_is_active"), _SKILLS, ["is_active"])


def downgrade() -> None:
    """Downgrade schema: 删除 skills 表."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, _SKILLS):
        op.drop_table(_SKILLS)
