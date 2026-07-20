"""add prompt management tables (template/version/label/eval_run)

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-07-12 22:00:00.000000

P1 Prompt 管理增强: 参考 Langfuse 数据模型 (https://langfuse.com/docs/prompt-management/data-model)

新增 4 张表:
- prompt_templates: 模板逻辑实体(同名多版本)
- prompt_versions: 不可变版本历史
- prompt_labels: Label 指针(production/latest/staging/prod-a/prod-b/canary-Npct)
- prompt_eval_runs: 评估运行(关联 Langfuse trace)

幂等: 用 inspector 检查表是否存在再 CREATE,兼容已通过 create_all 建表的环境。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, Sequence[str], None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NEW_TABLES = [
    "prompt_templates",
    "prompt_versions",
    "prompt_labels",
    "prompt_eval_runs",
]


def _has_table(inspector, name: str) -> bool:
    try:
        return name in inspector.get_table_names()
    except Exception:
        return False


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1) prompt_templates
    if not _has_table(inspector, "prompt_templates"):
        op.create_table(
            "prompt_templates",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.String(length=64),
                nullable=False,
                server_default="default",
            ),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column(
                "type", sa.String(length=16), nullable=False, server_default="text"
            ),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("created_by", sa.String(length=64), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.UniqueConstraint(
                "tenant_id", "name", name="uix_tenant_prompt_name"
            ),
        )
        op.create_index(
            "ix_prompt_templates_tenant", "prompt_templates", ["tenant_id"]
        )

    # 2) prompt_versions
    if not _has_table(inspector, "prompt_versions"):
        op.create_table(
            "prompt_versions",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column(
                "template_id",
                sa.String(length=64),
                sa.ForeignKey("prompt_templates.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("config", sa.JSON(), nullable=True),
            sa.Column("variables_schema", sa.JSON(), nullable=True),
            sa.Column("created_by", sa.String(length=64), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.UniqueConstraint(
                "template_id", "version", name="uix_prompt_template_version"
            ),
        )
        op.create_index(
            "ix_prompt_version_template",
            "prompt_versions",
            ["template_id", "version"],
        )

    # 3) prompt_labels
    if not _has_table(inspector, "prompt_labels"):
        op.create_table(
            "prompt_labels",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column(
                "template_id",
                sa.String(length=64),
                sa.ForeignKey("prompt_templates.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "version_id",
                sa.String(length=64),
                sa.ForeignKey("prompt_versions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("label", sa.String(length=64), nullable=False),
            sa.Column(
                "protected", sa.Boolean(), nullable=False, server_default=sa.text("0")
            ),
            sa.Column("updated_by", sa.String(length=64), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.UniqueConstraint(
                "template_id", "label", name="uix_prompt_template_label"
            ),
        )
        op.create_index(
            "ix_prompt_labels_template", "prompt_labels", ["template_id"]
        )

    # 4) prompt_eval_runs
    if not _has_table(inspector, "prompt_eval_runs"):
        op.create_table(
            "prompt_eval_runs",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column(
                "template_id",
                sa.String(length=64),
                sa.ForeignKey("prompt_templates.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "version_id",
                sa.String(length=64),
                sa.ForeignKey("prompt_versions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("dataset_id", sa.String(length=64), nullable=True),
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("metrics", sa.JSON(), nullable=True),
            sa.Column("trace_ids", sa.JSON(), nullable=True),
            sa.Column("created_by", sa.String(length=64), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
        )
        op.create_index(
            "ix_prompt_eval_runs_template", "prompt_eval_runs", ["template_id"]
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table in reversed(_NEW_TABLES):
        if _has_table(inspector, table):
            op.drop_table(table)
