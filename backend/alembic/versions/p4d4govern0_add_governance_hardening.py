"""add governance hardening (WS-4): 8x tenant_id + audit hash chain columns

Revision ID: p4d4govern0
Revises: p3c3webhook0
Create Date: 2026-08-08 00:00:00.000000

WS-4 企业级治理加固（多租户隔离 + 审计防篡改哈希链）的列级迁移：

1. 为 8 张缺租户归属的表补 ``tenant_id``（与 models/ 中模型列定义一致）:
   - models/artifact.py:          chat_artifacts    (Artifact)
   - models/models.py:            prompt_versions   (PromptVersion)
   - models/models.py:            prompt_eval_runs  (PromptEvalRun)
   - models/prompt_template.py:   agent_presets     (AgentPreset)
   - models/provider_models.py:   provider_templates (ProviderTemplate)
   - models/provider_models.py:   model_templates   (ModelTemplate)
   - models/skill.py:             skills            (Skill)
   - models/workflow.py:          workflow_runs     (WorkflowRun)

   ``nullable=True`` 与模型一致：存量行无租户，新行由服务层写入当前租户。
   索引名 ``ix_<table>_tenant_id`` 对齐 SQLAlchemy ``index=True`` 默认命名。

2. ``audit_logs`` 补防篡改哈希链两列:
   - prev_hash  String(64) nullable   (同租户上一条 entry_hash, 创世为 GENESIS_HASH)
   - entry_hash String(64) nullable   (sha256(canonical_json + prev_hash), 带索引)

实现说明:
- 全部用 ``batch_alter_table`` 以兼容 SQLite（无 ALTER COLUMN 支持）。
- 幂等：用 inspector 检查列是否已存在，避免 create_all 建表过的环境重复加列报错。
- 本迁移依赖 p3c3webhook0（WS-3 Webhook 订阅表，并发创建中），仅引用其 revision。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "p4d4govern0"
down_revision: Union[str, Sequence[str], None] = "p3c3webhook0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 需要补 tenant_id 的表（表名 -> SQLAlchemy 默认索引名 ix_<table>_tenant_id）
_TENANT_TABLES = [
    "chat_artifacts",
    "prompt_versions",
    "prompt_eval_runs",
    "agent_presets",
    "provider_templates",
    "model_templates",
    "skills",
    "workflow_runs",
]


def _has_column(inspector, table: str, column: str) -> bool:
    """检查列是否已存在（create_all 建表过的环境直接跳过）。"""
    try:
        cols = {c["name"] for c in inspector.get_columns(table)}
        return column in cols
    except Exception:
        return False


def _has_index(inspector, table: str, index_name: str) -> bool:
    try:
        indexes = {i["name"] for i in inspector.get_indexes(table)}
        return index_name in indexes
    except Exception:
        return False


def upgrade() -> None:
    """Upgrade schema: 补 8 张表的 tenant_id + audit_logs 哈希链两列。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ==================================================================
    # 1. 8 张表补 tenant_id（nullable + 索引, 对齐模型定义）
    # ==================================================================
    for table in _TENANT_TABLES:
        index_name = f"ix_{table}_tenant_id"
        with op.batch_alter_table(table) as batch_op:
            if not _has_column(inspector, table, "tenant_id"):
                batch_op.add_column(
                    sa.Column("tenant_id", sa.String(64), nullable=True)
                )
            if not _has_index(inspector, table, index_name):
                batch_op.create_index(index_name, ["tenant_id"])

    # ==================================================================
    # 2. audit_logs 补哈希链两列（prev_hash 无索引, entry_hash 带索引）
    # ==================================================================
    with op.batch_alter_table("audit_logs") as batch_op:
        if not _has_column(inspector, "audit_logs", "prev_hash"):
            batch_op.add_column(
                sa.Column("prev_hash", sa.String(64), nullable=True)
            )
        if not _has_column(inspector, "audit_logs", "entry_hash"):
            batch_op.add_column(
                sa.Column("entry_hash", sa.String(64), nullable=True)
            )
        if not _has_index(inspector, "audit_logs", "ix_audit_logs_entry_hash"):
            batch_op.create_index("ix_audit_logs_entry_hash", ["entry_hash"])


def downgrade() -> None:
    """Downgrade schema: 逆序移除所有新增列（含其索引）。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. 8 张表的 tenant_id（先删索引再删列）
    for table in reversed(_TENANT_TABLES):
        index_name = f"ix_{table}_tenant_id"
        with op.batch_alter_table(table) as batch_op:
            if _has_index(inspector, table, index_name):
                batch_op.drop_index(index_name)
            if _has_column(inspector, table, "tenant_id"):
                batch_op.drop_column("tenant_id")

    # 2. audit_logs 哈希链两列
    with op.batch_alter_table("audit_logs") as batch_op:
        if _has_index(inspector, "audit_logs", "ix_audit_logs_entry_hash"):
            batch_op.drop_index("ix_audit_logs_entry_hash")
        if _has_column(inspector, "audit_logs", "entry_hash"):
            batch_op.drop_column("entry_hash")
        if _has_column(inspector, "audit_logs", "prev_hash"):
            batch_op.drop_column("prev_hash")
