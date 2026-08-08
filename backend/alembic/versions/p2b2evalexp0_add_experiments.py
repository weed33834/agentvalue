"""add experiment tables (WS-2 实验对比 + RAGAS)

Revision ID: p2b2evalexp0
Revises: p1a1trace000
Create Date: 2026-08-08 00:00:00.000000

新增 3 张表, 支撑 Run A vs Run B 的结果对比 (对标 Braintrust Experiments):

- models/experiment_models.py: experiments          (实验定义: 数据集 + 指标 + 配置)
- models/experiment_models.py: experiment_runs      (一次运行: 被测变体 + 指标汇总)
- models/experiment_models.py: experiment_run_items (逐样本结果: 跨 run 按 sample_id 对齐)

幂等: 用 inspector 检查表是否存在再 CREATE，兼容已通过 create_all 建表的环境。
表创建顺序按逻辑外键依赖排列（父表先于子表），downgrade 用 reversed 顺序 drop。
注: experiment_runs.experiment_id / experiment_run_items.run_id 在 model 中声明了
ForeignKey，这里同样建出，SQLite 亦兼容（建表时内联 FK）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "p2b2evalexp0"
down_revision: Union[str, Sequence[str], None] = "p1a1trace000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 按逻辑外键依赖顺序排列（父表在前，子表在后），downgrade 用 reversed 顺序 drop
_NEW_TABLES = [
    "experiments",  # 实验主表
    "experiment_runs",  # 引用 experiments.id
    "experiment_run_items",  # 引用 experiment_runs.id
]


def _has_table(inspector, name: str) -> bool:
    try:
        return name in inspector.get_table_names()
    except Exception:
        return False


def upgrade() -> None:
    """Upgrade schema: 新增 3 张实验对比表."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ==================================================================
    # 1. experiments (models/experiment_models.py: Experiment)
    # ==================================================================
    if not _has_table(inspector, "experiments"):
        op.create_table(
            "experiments",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "tenant_id", sa.String(64), nullable=False, server_default="default"
            ),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("dataset_id", sa.Integer(), nullable=False),
            sa.Column("task_type", sa.String(16), nullable=False, server_default="rag"),
            sa.Column("metrics", sa.JSON(), nullable=False),
            sa.Column("config", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="active"),
            sa.Column("created_by", sa.String(64), nullable=True),
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
        op.create_index("ix_experiments_tenant_id", "experiments", ["tenant_id"])
        op.create_index("ix_experiments_name", "experiments", ["name"])
        op.create_index("ix_experiments_dataset_id", "experiments", ["dataset_id"])
        op.create_index("ix_experiments_status", "experiments", ["status"])
        op.create_index(
            "ix_experiment_tenant_status", "experiments", ["tenant_id", "status"]
        )
        op.create_index(
            "ix_experiment_tenant_dataset", "experiments", ["tenant_id", "dataset_id"]
        )

    # ==================================================================
    # 2. experiment_runs (models/experiment_models.py: ExperimentRun)
    # ==================================================================
    if not _has_table(inspector, "experiment_runs"):
        op.create_table(
            "experiment_runs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("experiment_id", sa.Integer(), nullable=False),
            sa.Column(
                "tenant_id", sa.String(64), nullable=False, server_default="default"
            ),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("variant", sa.JSON(), nullable=False),
            sa.Column(
                "status", sa.String(16), nullable=False, server_default="pending"
            ),
            sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_items", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "completed_items", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("failed_items", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("metric_summary", sa.JSON(), nullable=True),
            sa.Column("total_cost", sa.Float(), nullable=False, server_default="0"),
            sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error", sa.Text(), nullable=True),
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
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["experiment_id"],
                ["experiments.id"],
                name="fk_experiment_runs_experiment_id",
            ),
        )
        op.create_index(
            "ix_experiment_runs_experiment_id", "experiment_runs", ["experiment_id"]
        )
        op.create_index(
            "ix_experiment_runs_tenant_id", "experiment_runs", ["tenant_id"]
        )
        op.create_index("ix_experiment_runs_status", "experiment_runs", ["status"])
        op.create_index(
            "ix_experiment_run_tenant_status",
            "experiment_runs",
            ["tenant_id", "status"],
        )
        op.create_index(
            "ix_experiment_run_tenant_experiment",
            "experiment_runs",
            ["tenant_id", "experiment_id"],
        )

    # ==================================================================
    # 3. experiment_run_items (models/experiment_models.py: ExperimentRunItem)
    # ==================================================================
    if not _has_table(inspector, "experiment_run_items"):
        op.create_table(
            "experiment_run_items",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("run_id", sa.Integer(), nullable=False),
            sa.Column("experiment_id", sa.Integer(), nullable=False),
            sa.Column(
                "tenant_id", sa.String(64), nullable=False, server_default="default"
            ),
            sa.Column("sample_id", sa.String(128), nullable=False),
            sa.Column("dataset_item_id", sa.Integer(), nullable=True),
            sa.Column("input", sa.JSON(), nullable=True),
            sa.Column("expected", sa.JSON(), nullable=True),
            sa.Column("actual", sa.JSON(), nullable=True),
            sa.Column("contexts", sa.JSON(), nullable=True),
            sa.Column("scores", sa.JSON(), nullable=False),
            sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cost", sa.Float(), nullable=False, server_default="0"),
            sa.Column("tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "status", sa.String(16), nullable=False, server_default="success"
            ),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(
                ["run_id"],
                ["experiment_runs.id"],
                name="fk_experiment_run_items_run_id",
            ),
        )
        op.create_index(
            "ix_experiment_run_items_run_id", "experiment_run_items", ["run_id"]
        )
        op.create_index(
            "ix_experiment_run_items_experiment_id",
            "experiment_run_items",
            ["experiment_id"],
        )
        op.create_index(
            "ix_experiment_run_items_tenant_id", "experiment_run_items", ["tenant_id"]
        )
        op.create_index(
            "ix_experiment_run_items_sample_id", "experiment_run_items", ["sample_id"]
        )
        op.create_index(
            "ix_experiment_run_items_status", "experiment_run_items", ["status"]
        )
        # compare_runs 的核心 join: 按 run 取样本后以 sample_id 对齐
        op.create_index(
            "ix_experiment_run_item_run_sample",
            "experiment_run_items",
            ["run_id", "sample_id"],
        )
        op.create_index(
            "ix_experiment_run_item_tenant_run",
            "experiment_run_items",
            ["tenant_id", "run_id"],
        )


def downgrade() -> None:
    """Downgrade schema: 按依赖逆序删除 3 张实验对比表."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in reversed(_NEW_TABLES):
        if _has_table(inspector, table):
            op.drop_table(table)
