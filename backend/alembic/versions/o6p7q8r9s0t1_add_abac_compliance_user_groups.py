"""add abac / compliance / user_groups tables (补齐 5 张缺失表)

Revision ID: o6p7q8r9s0t1
Revises: n5o6p7q8r9s0
Create Date: 2026-07-29 00:00:00.000000

补齐 5 个 model 中定义了 __tablename__ 但在 alembic 迁移中缺失 create_table 的表。

覆盖的 model 文件与表:
- models/policy.py:        abac_policies            (P1-7 ABAC 属性级访问控制策略)
- models/compliance.py:    compliance_controls      (P1-31 合规控制项)
- models/compliance.py:    compliance_evidences     (P1-31 合规证据时间序列)
- models/user_group.py:    user_groups              (用户组主表)
- models/user_group.py:    user_group_members        (组成员关联表)

幂等: 用 inspector 检查表是否存在再 CREATE，兼容已通过 create_all 建表的环境。
表创建顺序按逻辑外键依赖排列（父表先于子表），downgrade 用 reversed 顺序 drop。
注: 这 5 张表在 model 中均未建立 DB 外键（策略上不依赖外键级联，兼容 SQLite），
故仅按逻辑依赖排序，不产生实际 FK 约束。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "o6p7q8r9s0t1"
down_revision: Union[str, Sequence[str], None] = "n5o6p7q8r9s0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 按逻辑外键依赖顺序排列（父表在前，子表在后），downgrade 用 reversed 顺序 drop
_NEW_TABLES = [
    "abac_policies",         # 独立 (策略表)
    "compliance_controls",   # 独立 (控制项主表)
    "compliance_evidences",  # 逻辑引用 compliance_controls.control_id (无 DB FK)
    "user_groups",           # 独立 (用户组主表)
    "user_group_members",    # 逻辑引用 user_groups.group_id (无 DB FK)
]


def _has_table(inspector, name: str) -> bool:
    try:
        return name in inspector.get_table_names()
    except Exception:
        return False


def upgrade() -> None:
    """Upgrade schema: 补齐 5 张缺失的表."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ==================================================================
    # 1. abac_policies (models/policy.py)
    # ==================================================================
    if not _has_table(inspector, "abac_policies"):
        op.create_table(
            "abac_policies",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("policy_id", sa.String(64), nullable=False, unique=True),
            sa.Column("subject_type", sa.String(16), nullable=False),
            sa.Column("subject_id", sa.String(128), nullable=False),
            sa.Column("resource_type", sa.String(64), nullable=False),
            sa.Column("resource_id", sa.String(128), nullable=False, server_default="*"),
            sa.Column("action", sa.String(128), nullable=False),
            sa.Column("effect", sa.String(16), nullable=False, server_default="allow"),
            sa.Column("condition", sa.JSON(), nullable=True),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("effect IN ('allow', 'deny')", name="ck_abac_policy_effect_valid"),
            sa.CheckConstraint("subject_type IN ('user', 'role', 'group')", name="ck_abac_policy_subject_type_valid"),
        )
        op.create_index("ix_abac_policies_policy_id", "abac_policies", ["policy_id"])
        op.create_index("ix_abac_policies_tenant_id", "abac_policies", ["tenant_id"])
        op.create_index("ix_abac_policy_tenant_resource_action", "abac_policies", ["tenant_id", "resource_type", "action"])
        op.create_index("ix_abac_policy_tenant_subject", "abac_policies", ["tenant_id", "subject_type", "subject_id"])

    # ==================================================================
    # 2. compliance_controls (models/compliance.py)
    # ==================================================================
    if not _has_table(inspector, "compliance_controls"):
        op.create_table(
            "compliance_controls",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("framework", sa.String(16), nullable=False),
            sa.Column("control_id", sa.String(64), nullable=False),
            sa.Column("title", sa.String(256), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("category", sa.String(32), nullable=False, server_default=""),
            sa.Column("status", sa.String(16), nullable=False, server_default="not_applicable"),
            sa.Column("evidence", sa.JSON(), nullable=True, server_default=sa.text("'{}'")),
            sa.Column("last_checked", sa.DateTime(timezone=True), nullable=True),
            sa.Column("owner", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_id", "framework", "control_id", name="uix_compliance_control"),
        )
        op.create_index("ix_compliance_controls_tenant_id", "compliance_controls", ["tenant_id"])
        op.create_index("ix_compliance_controls_framework", "compliance_controls", ["framework"])
        op.create_index("ix_compliance_controls_control_id", "compliance_controls", ["control_id"])
        op.create_index("ix_compliance_controls_status", "compliance_controls", ["status"])
        op.create_index("ix_compliance_framework_status", "compliance_controls", ["framework", "status"])
        op.create_index("ix_compliance_tenant_framework", "compliance_controls", ["tenant_id", "framework"])

    # ==================================================================
    # 3. compliance_evidences (models/compliance.py)
    # ==================================================================
    if not _has_table(inspector, "compliance_evidences"):
        op.create_table(
            "compliance_evidences",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("framework", sa.String(16), nullable=False),
            sa.Column("control_id", sa.String(64), nullable=False),
            sa.Column("evidence_type", sa.String(32), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("evidence_data", sa.JSON(), nullable=True, server_default=sa.text("'{}'")),
            sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("collector", sa.String(64), nullable=False, server_default="system"),
        )
        op.create_index("ix_compliance_evidences_tenant_id", "compliance_evidences", ["tenant_id"])
        op.create_index("ix_compliance_evidences_framework", "compliance_evidences", ["framework"])
        op.create_index("ix_compliance_evidences_control_id", "compliance_evidences", ["control_id"])
        op.create_index("ix_compliance_evidences_evidence_type", "compliance_evidences", ["evidence_type"])
        op.create_index("ix_compliance_ev_control_time", "compliance_evidences", ["control_id", "collected_at"])
        op.create_index("ix_compliance_ev_tenant_time", "compliance_evidences", ["tenant_id", "collected_at"])

    # ==================================================================
    # 4. user_groups (models/user_group.py)
    # ==================================================================
    if not _has_table(inspector, "user_groups"):
        op.create_table(
            "user_groups",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("group_id", sa.String(64), nullable=False),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_id", "group_id", name="uix_tenant_user_group_id"),
        )
        op.create_index("ix_user_groups_group_id", "user_groups", ["group_id"])
        op.create_index("ix_user_groups_tenant_id", "user_groups", ["tenant_id"])
        op.create_index("ix_user_group_tenant_name", "user_groups", ["tenant_id", "name"])

    # ==================================================================
    # 5. user_group_members (models/user_group.py)
    # ==================================================================
    if not _has_table(inspector, "user_group_members"):
        op.create_table(
            "user_group_members",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.String(64), nullable=False),
            sa.Column("group_id", sa.String(64), nullable=False),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_id", "user_id", "group_id", name="uix_tenant_user_group_member"),
        )
        op.create_index("ix_user_group_members_user_id", "user_group_members", ["user_id"])
        op.create_index("ix_user_group_members_group_id", "user_group_members", ["group_id"])
        op.create_index("ix_user_group_members_tenant_id", "user_group_members", ["tenant_id"])
        op.create_index("ix_user_group_member_tenant_group", "user_group_members", ["tenant_id", "group_id"])


def downgrade() -> None:
    """Downgrade schema: 按依赖逆序删除所有表."""
    for table in reversed(_NEW_TABLES):
        op.drop_table(table)
