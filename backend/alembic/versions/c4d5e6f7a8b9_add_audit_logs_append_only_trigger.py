"""add audit_logs append-only trigger (PostgreSQL)

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-07-12 16:10:00.000000

P1-H1: 在 DB 层强制 audit_logs 表 append-only。

背景:
  services/audit_service.py 的 log() 仅做 INSERT,应用层已 append-only,
  但 DB 层无强制约束 —— 拥有 DB 写权限的角色仍可 UPDATE/DELETE 篡改审计记录。
  本迁移在 PostgreSQL 上创建 BEFORE UPDATE/DELETE/TRUNCATE trigger,任何修改
  尝试直接 RAISE EXCEPTION 阻断。

兼容性:
  SQLite 不支持 PostgreSQL 的 plpgsql trigger 语法,本迁移在非 PostgreSQL
  dialect 下跳过 trigger 创建(SQLite 主要用于测试与本地开发,append-only 由
  应用层保证)。生产部署使用 PostgreSQL 时 trigger 生效。

  退役/归档场景需通过专用迁移脚本临时禁用 trigger(session_replication_role
  或 DROP/RECREATE),迁移后立即恢复,而非保留 DELETE 权限。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, Sequence[str], None] = "b3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 审计日志 append-only 守护函数与 trigger(P1-9 决策)
# 函数名与 architecture-decisions.md P1-9 示例一致,trigger 命名带 _ai 后缀避免
# 与未来其他业务 trigger 冲突
_FUNCTION_NAME = "block_audit_log_modification"
_TRIGGER_NO_UPDATE = "audit_log_no_update"
_TRIGGER_NO_DELETE = "audit_log_no_delete"
_TRIGGER_NO_TRUNCATE = "audit_log_no_truncate"


def _is_postgresql(bind) -> bool:
    return bind.dialect.name in ("postgresql", "postgres")


def _trigger_exists(bind, trigger_name: str) -> bool:
    """检测 PG trigger 是否已存在(幂等保护)"""
    sql = sa.text("SELECT 1 FROM pg_trigger WHERE tgname = :name LIMIT 1")
    result = bind.execute(sql, {"name": trigger_name}).scalar()
    return result is not None


def _function_exists(bind, function_name: str) -> bool:
    """检测 PG function 是否已存在(幂等保护)"""
    sql = sa.text("SELECT 1 FROM pg_proc WHERE proname = :name LIMIT 1")
    result = bind.execute(sql, {"name": function_name}).scalar()
    return result is not None


def upgrade() -> None:
    """Upgrade schema: PostgreSQL 创建 append-only trigger,其他 dialect 跳过。"""
    bind = op.get_bind()
    if not _is_postgresql(bind):
        # SQLite(测试/本地)不支持 plpgsql trigger,append-only 由应用层保证
        return

    # 守护函数:任意 UPDATE/DELETE/TRUNCATE 尝试直接抛异常阻断
    if not _function_exists(bind, _FUNCTION_NAME):
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {_FUNCTION_NAME}() RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION 'audit_logs is append-only: % not allowed', TG_OP;
            END;
            $$ LANGUAGE plpgsql
            """
        )

    # BEFORE UPDATE: 阻止篡改已有审计记录
    if not _trigger_exists(bind, _TRIGGER_NO_UPDATE):
        op.execute(
            f"""
            CREATE TRIGGER {_TRIGGER_NO_UPDATE}
              BEFORE UPDATE ON audit_logs
              FOR EACH ROW EXECUTE FUNCTION {_FUNCTION_NAME}()
            """
        )

    # BEFORE DELETE: 阻止删除审计记录(归档走专用迁移临时禁用 trigger)
    if not _trigger_exists(bind, _TRIGGER_NO_DELETE):
        op.execute(
            f"""
            CREATE TRIGGER {_TRIGGER_NO_DELETE}
              BEFORE DELETE ON audit_logs
              FOR EACH ROW EXECUTE FUNCTION {_FUNCTION_NAME}()
            """
        )

    # BEFORE TRUNCATE: 阻止批量清空(防止绕过单行 DELETE 限制)
    if not _trigger_exists(bind, _TRIGGER_NO_TRUNCATE):
        op.execute(
            f"""
            CREATE TRIGGER {_TRIGGER_NO_TRUNCATE}
              BEFORE TRUNCATE ON audit_logs
              FOR EACH STATEMENT EXECUTE FUNCTION {_FUNCTION_NAME}()
            """
        )


def downgrade() -> None:
    """Downgrade schema: 移除 trigger 与守护函数。"""
    bind = op.get_bind()
    if not _is_postgresql(bind):
        return

    for trigger_name in (
        _TRIGGER_NO_TRUNCATE,
        _TRIGGER_NO_DELETE,
        _TRIGGER_NO_UPDATE,
    ):
        if _trigger_exists(bind, trigger_name):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON audit_logs")

    if _function_exists(bind, _FUNCTION_NAME):
        op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION_NAME}()")
