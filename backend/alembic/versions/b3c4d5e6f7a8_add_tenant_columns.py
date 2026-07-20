"""add tenant_id/manager_id/archived columns to business tables

Revision ID: b3c4d5e6f7a8
Revises: a1b2c3d4e5f6
Create Date: 2026-07-05 20:00:00.000000

P2-迁移drift修复: 初始迁移(2d49c8ec0ef7)与 models.py 不一致 —— models 中
evaluations/raw_inputs/approvals/audit_logs/feedback/memories/kb_docs/
evaluation_periods 表均定义了 tenant_id, evaluations 还定义了 manager_id /
archived / archived_at, 但初始迁移建表时漏建这些列。

注: 应用启动走 Base.metadata.create_all() 按 models 直接建表, 故运行时不崩;
但纯 alembic upgrade 部署会导致 schema 与 ORM 不一致。本迁移补齐列, 使迁移链
与 models.py 一致。

幂等: 用 inspector 检查列是否存在再 ADD, 兼容已通过 create_all 建表的环境。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 需要补 tenant_id 列的表(对齐 models.py 中各模型的 tenant_id 字段)
# 表名取自 models.py 的 __tablename__
_TENANT_TABLES = [
    "users",
    "evaluations",
    "raw_inputs",
    "approval_actions",
    "audit_logs",
    "feedback",
    "memories",
    "company_kb",
    "evaluation_periods",
    "dimension_scores",
    "evidence_refs",
]


def _has_column(inspector, table: str, column: str) -> bool:
    try:
        return column in {c["name"] for c in inspector.get_columns(table)}
    except Exception:
        return False


def _has_index(inspector, table: str, index: str) -> bool:
    try:
        return index in {i["name"] for i in inspector.get_indexes(table)}
    except Exception:
        return False


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    # 1) 各业务表补 tenant_id 列(default 租户回填,兼容历史数据)
    # 注: 保留 server_default="default" 不移除 —— SQLite 不支持 ALTER COLUMN 改
    # server_default, 移除会令纯 alembic upgrade 在 SQLite 上报 "near ALTER: syntax error"。
    # 应用层 ORM 已强制写入 tenant_id, server_default 仅作历史数据回填兜底,无副作用。
    for table in _TENANT_TABLES:
        if table not in existing_tables:
            continue
        if _has_column(inspector, table, "tenant_id"):
            continue
        op.add_column(
            table,
            sa.Column(
                "tenant_id",
                sa.String(length=64),
                nullable=False,
                server_default="default",
            ),
        )

    # 2) evaluations 补 manager_id / archived / archived_at
    if "evaluations" in existing_tables:
        if not _has_column(inspector, "evaluations", "manager_id"):
            op.add_column(
                "evaluations",
                sa.Column("manager_id", sa.String(length=64), nullable=True),
            )
        if not _has_column(inspector, "evaluations", "archived"):
            op.add_column(
                "evaluations",
                sa.Column(
                    "archived",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("0"),
                ),
            )
        if not _has_column(inspector, "evaluations", "archived_at"):
            op.add_column(
                "evaluations",
                sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
            )

    # 3) raw_inputs 补 archived / archived_at(对齐 models.py RawInput)
    if "raw_inputs" in existing_tables:
        if not _has_column(inspector, "raw_inputs", "archived"):
            op.add_column(
                "raw_inputs",
                sa.Column(
                    "archived",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("0"),
                ),
            )
        if not _has_column(inspector, "raw_inputs", "archived_at"):
            op.add_column(
                "raw_inputs",
                sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
            )

    # 4) users 补 manager_id(对齐 models.py User)
    if "users" in existing_tables and not _has_column(inspector, "users", "manager_id"):
        op.add_column(
            "users",
            sa.Column("manager_id", sa.String(length=64), nullable=True),
        )

    # 5) 关键索引(对齐 models.py __table_args__)
    _maybe_create_index(
        inspector, "evaluations", "ix_eval_tenant_status", ["tenant_id", "status"]
    )
    _maybe_create_index(
        inspector,
        "evaluations",
        "ix_eval_tenant_employee",
        ["tenant_id", "employee_id"],
    )
    _maybe_create_index(
        inspector,
        "raw_inputs",
        "ix_raw_tenant_employee_period",
        ["tenant_id", "employee_id", "period"],
    )
    _maybe_create_index(
        inspector, "users", "ix_user_tenant_role", ["tenant_id", "role"]
    )


def _maybe_create_index(inspector, table: str, name: str, columns: list) -> None:
    existing_tables = inspector.get_table_names()
    if table not in existing_tables:
        return
    if _has_index(inspector, table, name):
        return
    try:
        op.create_index(name, table, columns, unique=False)
    except Exception:
        # 索引创建失败不阻断(可能列未补齐等),记录后继续
        pass


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    # 索引
    for table, name in [
        ("evaluations", "ix_eval_tenant_status"),
        ("evaluations", "ix_eval_tenant_employee"),
        ("raw_inputs", "ix_raw_tenant_employee_period"),
        ("users", "ix_user_tenant_role"),
    ]:
        if table in existing_tables and _has_index(inspector, table, name):
            op.drop_index(name, table_name=table)

    # users.manager_id
    if "users" in existing_tables and _has_column(inspector, "users", "manager_id"):
        op.drop_column("users", "manager_id")

    # raw_inputs archived
    if "raw_inputs" in existing_tables:
        if _has_column(inspector, "raw_inputs", "archived_at"):
            op.drop_column("raw_inputs", "archived_at")
        if _has_column(inspector, "raw_inputs", "archived"):
            op.drop_column("raw_inputs", "archived")

    # evaluations manager/archived
    if "evaluations" in existing_tables:
        if _has_column(inspector, "evaluations", "archived_at"):
            op.drop_column("evaluations", "archived_at")
        if _has_column(inspector, "evaluations", "archived"):
            op.drop_column("evaluations", "archived")
        if _has_column(inspector, "evaluations", "manager_id"):
            op.drop_column("evaluations", "manager_id")

    # tenant_id
    for table in _TENANT_TABLES:
        if table in existing_tables and _has_column(inspector, table, "tenant_id"):
            op.drop_column(table, "tenant_id")
