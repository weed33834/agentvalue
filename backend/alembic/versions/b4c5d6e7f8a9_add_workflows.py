"""add workflows + workflow_runs tables (p4-2 workflow visual orchestration)

Revision ID: b4c5d6e7f8a9
Revises: a8b9c0d1e2f3
Create Date: 2026-07-13 01:00:00.000000

P4-2 工作流可视化编排 (对标 Dify Workflow / Coze Bot 编排):
- workflows: 工作流定义 (DAG 图 + 输入变量 schema + 启用状态 + 版本)
- workflow_runs: 工作流运行实例 (状态 + 输入/输出 + 节点级执行状态)

幂等: 用 inspector 检查表是否存在再 CREATE, 兼容已通过 create_all 建表的环境。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4c5d6e7f8a9"
down_revision: Union[str, Sequence[str], None] = "a8b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_WORKFLOWS = "workflows"
_WORKFLOW_RUNS = "workflow_runs"


def _has_table(inspector, name: str) -> bool:
    try:
        return name in inspector.get_table_names()
    except Exception:
        return False


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ---- workflows 表 ----
    if not _has_table(inspector, _WORKFLOWS):
        op.create_table(
            _WORKFLOWS,
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column(
                "description",
                sa.String(length=512),
                nullable=False,
                server_default="",
            ),
            sa.Column("graph", sa.JSON(), nullable=False),
            # SQLite 不支持 server_default 'json({})', 由应用层 default dict 兜底
            sa.Column("input_schema", sa.JSON(), nullable=False),
            sa.Column(
                "enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("1"),
            ),
            sa.Column(
                "version",
                sa.Integer(),
                nullable=False,
                server_default="1",
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
        )
        op.create_index("ix_workflows_name", _WORKFLOWS, ["name"])
        op.create_index("ix_workflows_tenant", _WORKFLOWS, ["tenant_id"])
        op.create_index("ix_workflows_enabled", _WORKFLOWS, ["enabled"])

    # ---- workflow_runs 表 ----
    if not _has_table(inspector, _WORKFLOW_RUNS):
        op.create_table(
            _WORKFLOW_RUNS,
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column(
                "workflow_id", sa.String(length=64), nullable=False, index=True
            ),
            sa.Column(
                "thread_id", sa.String(length=64), nullable=False, index=True
            ),
            sa.Column(
                "status",
                sa.String(length=32),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("inputs", sa.JSON(), nullable=False),
            sa.Column("outputs", sa.JSON(), nullable=False),
            sa.Column("node_states", sa.JSON(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "completed_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
        # workflow_id + created_at: 工作流运行历史按时间倒序
        op.create_index(
            "ix_workflow_run_workflow_created",
            _WORKFLOW_RUNS,
            ["workflow_id", "created_at"],
        )
        op.create_index("ix_workflow_run_status", _WORKFLOW_RUNS, ["status"])


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, _WORKFLOW_RUNS):
        op.drop_index("ix_workflow_run_status", table_name=_WORKFLOW_RUNS)
        op.drop_index(
            "ix_workflow_run_workflow_created", table_name=_WORKFLOW_RUNS
        )
        op.drop_table(_WORKFLOW_RUNS)

    if _has_table(inspector, _WORKFLOWS):
        op.drop_index("ix_workflows_enabled", table_name=_WORKFLOWS)
        op.drop_index("ix_workflows_tenant", table_name=_WORKFLOWS)
        op.drop_index("ix_workflows_name", table_name=_WORKFLOWS)
        op.drop_table(_WORKFLOWS)
