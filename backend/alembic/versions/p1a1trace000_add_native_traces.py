"""add native trace/span storage (WS-1 原生可观测性)

Revision ID: p1a1trace000
Revises: o6p7q8r9s0t1
Create Date: 2026-08-08 00:00:00.000000

补齐自托管部署下的原生链路追踪存储，替代此前"仅 models.py:541 一个 trace_ids
JSON 外链 Langfuse、无 span 表"的状态（v3.0 审计结论 2.1）。

新增表:
- models/trace_models.py: trace_records  (链路主记录 + token/成本汇总)
- models/trace_models.py: span_records   (链路内的执行片段, 软引用 trace_records)

设计说明:
- span_records 通过 trace_id 字符串软引用 trace_records，**不建 DB 外键**：
  span 采用批量异步写入，可能先于 trace 收尾记录落库，外键会造成写入失败；
  且与本仓既有 5 张表（abac_policies 等）保持一致的"逻辑引用不加 FK"策略。
- trace_records.trace_id 唯一，span_records.span_id 不唯一（仅索引），
  避免极端并发下的 uuid 冲突把观测写入变成业务故障。

幂等: 用 inspector 检查表是否存在再 CREATE，兼容已通过 create_all 建表的环境。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "p1a1trace000"
down_revision: Union[str, Sequence[str], None] = "o6p7q8r9s0t1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 按逻辑引用顺序排列（父表在前，子表在后），downgrade 用 reversed 顺序 drop
_NEW_TABLES = [
    "trace_records",  # 独立 (链路主表)
    "span_records",   # 逻辑引用 trace_records.trace_id (无 DB FK)
]

# 各表的索引清单，供 downgrade 显式 drop_index。
# 注: SQLite/PostgreSQL 在 DROP TABLE 时会连带删除其索引，此处显式先删是为了
# 让 downgrade 在"表被外部改造过"的环境下也有确定性行为，并与 upgrade 严格对称。
_INDEXES = {
    "trace_records": [
        "ix_trace_records_trace_id",
        "ix_trace_records_tenant_id",
        "ix_trace_records_kind",
        "ix_trace_records_status",
        "ix_trace_records_started_at",
        "ix_trace_records_user_id",
        "ix_trace_records_session_id",
        "ix_trace_records_tenant_started",
        "ix_trace_records_tenant_kind",
        "ix_trace_records_tenant_status",
    ],
    "span_records": [
        "ix_span_records_span_id",
        "ix_span_records_trace_id",
        "ix_span_records_parent_span_id",
        "ix_span_records_tenant_id",
        "ix_span_records_kind",
        "ix_span_records_status",
        "ix_span_records_started_at",
        "ix_span_records_model",
        "ix_span_records_tenant_started",
        "ix_span_records_trace_started",
        "ix_span_records_tenant_model",
    ],
}


def _has_table(inspector, name: str) -> bool:
    try:
        return name in inspector.get_table_names()
    except Exception:
        return False


def upgrade() -> None:
    """Upgrade schema: 新增 trace_records / span_records 两张表."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ==================================================================
    # 1. trace_records (models/trace_models.py: TraceRecord)
    # ==================================================================
    if not _has_table(inspector, "trace_records"):
        op.create_table(
            "trace_records",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("trace_id", sa.String(64), nullable=False, unique=True),
            sa.Column("tenant_id", sa.String(64), nullable=True, server_default="default"),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("kind", sa.String(32), nullable=False, server_default="chat"),
            sa.Column("status", sa.String(16), nullable=False, server_default="running"),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration_ms", sa.Float(), nullable=True),
            sa.Column("user_id", sa.String(64), nullable=True),
            sa.Column("session_id", sa.String(128), nullable=True),
            sa.Column("total_spans", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_completion_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_cost", sa.Float(), nullable=False, server_default="0"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("tags", sa.JSON(), nullable=True),
            sa.Column("trace_metadata", sa.JSON(), nullable=True),
        )
        op.create_index("ix_trace_records_trace_id", "trace_records", ["trace_id"], unique=True)
        op.create_index("ix_trace_records_tenant_id", "trace_records", ["tenant_id"])
        op.create_index("ix_trace_records_kind", "trace_records", ["kind"])
        op.create_index("ix_trace_records_status", "trace_records", ["status"])
        op.create_index("ix_trace_records_started_at", "trace_records", ["started_at"])
        op.create_index("ix_trace_records_user_id", "trace_records", ["user_id"])
        op.create_index("ix_trace_records_session_id", "trace_records", ["session_id"])
        op.create_index("ix_trace_records_tenant_started", "trace_records", ["tenant_id", "started_at"])
        op.create_index("ix_trace_records_tenant_kind", "trace_records", ["tenant_id", "kind"])
        op.create_index("ix_trace_records_tenant_status", "trace_records", ["tenant_id", "status"])

    # ==================================================================
    # 2. span_records (models/trace_models.py: SpanRecord)
    # ==================================================================
    if not _has_table(inspector, "span_records"):
        op.create_table(
            "span_records",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("span_id", sa.String(64), nullable=False),
            sa.Column("trace_id", sa.String(64), nullable=False),
            sa.Column("parent_span_id", sa.String(64), nullable=True),
            sa.Column("tenant_id", sa.String(64), nullable=True, server_default="default"),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("kind", sa.String(32), nullable=False, server_default="chain"),
            sa.Column("status", sa.String(16), nullable=False, server_default="running"),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration_ms", sa.Float(), nullable=True),
            sa.Column("input", sa.JSON(), nullable=True),
            sa.Column("output", sa.JSON(), nullable=True),
            sa.Column("model", sa.String(128), nullable=True),
            sa.Column("provider", sa.String(64), nullable=True),
            sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cost", sa.Float(), nullable=False, server_default="0"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("attributes", sa.JSON(), nullable=True),
        )
        op.create_index("ix_span_records_span_id", "span_records", ["span_id"])
        op.create_index("ix_span_records_trace_id", "span_records", ["trace_id"])
        op.create_index("ix_span_records_parent_span_id", "span_records", ["parent_span_id"])
        op.create_index("ix_span_records_tenant_id", "span_records", ["tenant_id"])
        op.create_index("ix_span_records_kind", "span_records", ["kind"])
        op.create_index("ix_span_records_status", "span_records", ["status"])
        op.create_index("ix_span_records_started_at", "span_records", ["started_at"])
        op.create_index("ix_span_records_model", "span_records", ["model"])
        op.create_index("ix_span_records_tenant_started", "span_records", ["tenant_id", "started_at"])
        op.create_index("ix_span_records_trace_started", "span_records", ["trace_id", "started_at"])
        op.create_index("ix_span_records_tenant_model", "span_records", ["tenant_id", "model"])


def downgrade() -> None:
    """Downgrade schema: 按依赖逆序删除索引与表."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table in reversed(_NEW_TABLES):
        if not _has_table(inspector, table):
            continue
        existing = set()
        try:
            existing = {ix["name"] for ix in inspector.get_indexes(table)}
        except Exception:
            existing = set()
        for index_name in _INDEXES.get(table, []):
            if index_name in existing:
                op.drop_index(index_name, table_name=table)
        op.drop_table(table)
