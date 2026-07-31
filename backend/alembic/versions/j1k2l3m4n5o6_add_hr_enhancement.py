"""add HR enhancement tables (360 reviews + calibration sessions)

Revision ID: j1k2l3m4n5o6
Revises: i0j1k2l3m4n5
Create Date: 2026-07-20 01:00:00.000000

HR 评估增强:
- review_cycles: 360° 环评邀请记录 (一个 evaluation 可对应多名评估人)
  - reviewer_role: peer / manager / subordinate / external
  - status: pending / submitted
  - scores: JSON 各维度评分
  - feedback_text: 文字反馈
- calibration_sessions: 校准会主记录
  - period / facilitator_id / status / participants JSON / notes
- calibration_items: 校准项 (一份评估的调整记录)
  - session_id / evaluation_id / original_score / calibrated_score / adjustment_reason

幂等: 用 inspector 检查表是否存在再 CREATE, 兼容已通过 create_all 建表的环境。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "j1k2l3m4n5o6"
down_revision: Union[str, Sequence[str], None] = "i0j1k2l3m4n5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_REVIEW_CYCLES = "review_cycles"
_CALIBRATION_SESSIONS = "calibration_sessions"
_CALIBRATION_ITEMS = "calibration_items"


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
    """Upgrade schema: 创建 review_cycles / calibration_sessions / calibration_items 表."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ---- review_cycles 表 ----
    if not _has_table(inspector, _REVIEW_CYCLES):
        op.create_table(
            _REVIEW_CYCLES,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("review_id", sa.String(length=128), nullable=False),
            sa.Column(
                "evaluation_id",
                sa.String(length=128),
                sa.ForeignKey("evaluations.evaluation_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "employee_id",
                sa.String(length=64),
                sa.ForeignKey("users.user_id"),
                nullable=False,
            ),
            sa.Column(
                "reviewer_id",
                sa.String(length=64),
                sa.ForeignKey("users.user_id"),
                nullable=False,
            ),
            sa.Column("reviewer_role", sa.String(length=32), nullable=False),
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("scores", sa.JSON(), nullable=True),
            sa.Column("overall_score", sa.Float(), nullable=True),
            sa.Column("feedback_text", sa.Text(), nullable=True),
            sa.Column("requested_by", sa.String(length=64), nullable=True),
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
            sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint(
                "reviewer_role IN ('peer','manager','subordinate','external')",
                name="ck_review_reviewer_role_valid",
            ),
            sa.CheckConstraint(
                "status IN ('pending','submitted')",
                name="ck_review_status_valid",
            ),
            sa.UniqueConstraint("review_id", name=op.f("uq_review_cycles_review_id")),
        )
        op.create_index(op.f("ix_review_cycles_id"), _REVIEW_CYCLES, ["id"])
        op.create_index(
            op.f("ix_review_cycles_review_id"),
            _REVIEW_CYCLES,
            ["review_id"],
            unique=True,
        )
        op.create_index(
            op.f("ix_review_cycles_evaluation_id"), _REVIEW_CYCLES, ["evaluation_id"]
        )
        op.create_index(
            op.f("ix_review_cycles_employee_id"), _REVIEW_CYCLES, ["employee_id"]
        )
        op.create_index(
            op.f("ix_review_cycles_reviewer_id"), _REVIEW_CYCLES, ["reviewer_id"]
        )
        op.create_index(
            op.f("ix_review_cycles_tenant_id"), _REVIEW_CYCLES, ["tenant_id"]
        )
        op.create_index(
            "ix_review_eval_reviewer",
            _REVIEW_CYCLES,
            ["evaluation_id", "reviewer_id"],
            unique=True,
        )
        op.create_index(
            "ix_review_tenant_eval", _REVIEW_CYCLES, ["tenant_id", "evaluation_id"]
        )
        op.create_index(
            "ix_review_tenant_reviewer",
            _REVIEW_CYCLES,
            ["tenant_id", "reviewer_id"],
        )

    # ---- calibration_sessions 表 ----
    if not _has_table(inspector, _CALIBRATION_SESSIONS):
        op.create_table(
            _CALIBRATION_SESSIONS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("session_id", sa.String(length=128), nullable=False),
            sa.Column("period", sa.String(length=32), nullable=False),
            sa.Column("title", sa.String(length=256), nullable=False),
            sa.Column(
                "facilitator_id",
                sa.String(length=64),
                sa.ForeignKey("users.user_id"),
                nullable=False,
            ),
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default="scheduled",
            ),
            sa.Column("participants", sa.JSON(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
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
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint(
                "status IN ('scheduled','in_progress','completed')",
                name="ck_calibration_session_status_valid",
            ),
            sa.UniqueConstraint(
                "session_id", name=op.f("uq_calibration_sessions_session_id")
            ),
        )
        op.create_index(
            op.f("ix_calibration_sessions_id"), _CALIBRATION_SESSIONS, ["id"]
        )
        op.create_index(
            op.f("ix_calibration_sessions_session_id"),
            _CALIBRATION_SESSIONS,
            ["session_id"],
            unique=True,
        )
        op.create_index(
            op.f("ix_calibration_sessions_period"), _CALIBRATION_SESSIONS, ["period"]
        )
        op.create_index(
            op.f("ix_calibration_sessions_facilitator_id"),
            _CALIBRATION_SESSIONS,
            ["facilitator_id"],
        )
        op.create_index(
            op.f("ix_calibration_sessions_tenant_id"),
            _CALIBRATION_SESSIONS,
            ["tenant_id"],
        )
        op.create_index(
            "ix_calibration_session_tenant_period",
            _CALIBRATION_SESSIONS,
            ["tenant_id", "period"],
        )
        op.create_index(
            "ix_calibration_session_tenant_status",
            _CALIBRATION_SESSIONS,
            ["tenant_id", "status"],
        )

    # ---- calibration_items 表 ----
    if not _has_table(inspector, _CALIBRATION_ITEMS):
        op.create_table(
            _CALIBRATION_ITEMS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("item_id", sa.String(length=128), nullable=False),
            sa.Column(
                "session_id",
                sa.String(length=128),
                sa.ForeignKey("calibration_sessions.session_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "evaluation_id",
                sa.String(length=128),
                sa.ForeignKey("evaluations.evaluation_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "employee_id",
                sa.String(length=64),
                sa.ForeignKey("users.user_id"),
                nullable=False,
            ),
            sa.Column("original_score", sa.Float(), nullable=False),
            sa.Column("calibrated_score", sa.Float(), nullable=True),
            sa.Column("adjustment_reason", sa.Text(), nullable=True),
            sa.Column("applied", sa.Integer(), nullable=False, server_default="0"),
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
            sa.CheckConstraint(
                "original_score >= 0 AND original_score <= 100",
                name="ck_calibration_item_original_score_range",
            ),
            sa.CheckConstraint(
                "(calibrated_score IS NULL) OR "
                "(calibrated_score >= 0 AND calibrated_score <= 100)",
                name="ck_calibration_item_calibrated_score_range",
            ),
            sa.UniqueConstraint("item_id", name=op.f("uq_calibration_items_item_id")),
        )
        op.create_index(op.f("ix_calibration_items_id"), _CALIBRATION_ITEMS, ["id"])
        op.create_index(
            op.f("ix_calibration_items_item_id"),
            _CALIBRATION_ITEMS,
            ["item_id"],
            unique=True,
        )
        op.create_index(
            op.f("ix_calibration_items_session_id"),
            _CALIBRATION_ITEMS,
            ["session_id"],
        )
        op.create_index(
            op.f("ix_calibration_items_evaluation_id"),
            _CALIBRATION_ITEMS,
            ["evaluation_id"],
        )
        op.create_index(
            op.f("ix_calibration_items_employee_id"),
            _CALIBRATION_ITEMS,
            ["employee_id"],
        )
        op.create_index(
            op.f("ix_calibration_items_tenant_id"),
            _CALIBRATION_ITEMS,
            ["tenant_id"],
        )
        op.create_index(
            "ix_calibration_item_session_eval",
            _CALIBRATION_ITEMS,
            ["session_id", "evaluation_id"],
            unique=True,
        )
        op.create_index(
            "ix_calibration_item_tenant_session",
            _CALIBRATION_ITEMS,
            ["tenant_id", "session_id"],
        )


def downgrade() -> None:
    """Downgrade schema: 删除 HR 增强相关表."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, _CALIBRATION_ITEMS):
        op.drop_table(_CALIBRATION_ITEMS)
    if _has_table(inspector, _CALIBRATION_SESSIONS):
        op.drop_table(_CALIBRATION_SESSIONS)
    if _has_table(inspector, _REVIEW_CYCLES):
        op.drop_table(_REVIEW_CYCLES)
