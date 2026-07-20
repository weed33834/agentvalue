"""
scripts/migrate.py 单元测试
覆盖 main() 入口、表创建、幂等性与表结构完整性，使用临时 SQLite 数据库隔离。

说明：alembic env.py 每次执行都会重新读取 get_settings().database_url，
因此只需 patch settings.database_url 指向临时文件即可控制迁移目标库。
"""

import sqlite3
import sys

import pytest

from core.config import get_settings


@pytest.fixture(autouse=True)
def _disable_alembic_fileconfig(monkeypatch):
    """禁用 alembic env.py 中的 fileConfig 调用。

    env.py 执行时会调用 logging.config.fileConfig(alembic.ini)，其默认
    disable_existing_loggers=True 会禁用所有未在 ini 中声明的 logger，
    污染全局日志配置并导致后续依赖 caplog 的测试失败。这里将其置为 no-op。
    """
    monkeypatch.setattr("logging.config.fileConfig", lambda *a, **k: None)
    yield


# 初始迁移应创建的全部业务表（与 models/models.py 中 ORM 模型一一对应）
# 注：'tenants' 由后续迁移 a1b2c3d4e5f6_add_tenants_table 创建,亦纳入期望集合,
# 确保新迁移不丢失覆盖。
EXPECTED_TABLES = {
    "users",
    "raw_inputs",
    "evaluations",
    "approval_actions",
    "audit_logs",
    "feedback",
    "memories",
    "company_kb",
    "evaluation_periods",
    "dimension_scores",
    "evidence_refs",
    "tenants",
}

# evaluations 表应包含的全部列
EXPECTED_EVALUATION_COLUMNS = {
    "id",
    "evaluation_id",
    "employee_id",
    "period",
    "overall_score",
    "employee_view",
    "manager_view",
    "audit",
    "status",
    "created_at",
    "updated_at",
    "approved_at",
    "approver_id",
}

# alembic 版本追踪表
ALEMBIC_TABLE = "alembic_version"


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """将应用配置 database_url 指向临时 SQLite 文件，隔离迁移测试。"""
    db_file = tmp_path / "migrate_test.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    settings = get_settings()
    monkeypatch.setattr(settings, "database_url", db_url)
    return str(db_file)


def _table_names(db_path):
    """读取 SQLite 文件中的全部表名"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def _table_columns(db_path, table):
    """读取指定表的全部列名"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    finally:
        conn.close()
    return {r[1] for r in rows}


def _table_indexes(db_path, table):
    """读取指定表的全部索引名"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA index_list('{table}')").fetchall()
    finally:
        conn.close()
    return {r[1] for r in rows}


class TestMigrateMain:
    """migrate.main() 的 upgrade 子命令"""

    def test_main_upgrade_creates_all_tables(self, temp_db, monkeypatch):
        """main() upgrade 子命令应创建全部业务表与 alembic 版本表"""
        from scripts import migrate

        monkeypatch.setattr(sys, "argv", ["migrate", "upgrade"])
        migrate.main()

        tables = _table_names(temp_db)
        missing = EXPECTED_TABLES - tables
        assert not missing, f"缺少表: {missing}"
        assert ALEMBIC_TABLE in tables, "缺少 alembic 版本表"

    def test_main_upgrade_is_idempotent(self, temp_db, monkeypatch):
        """重复执行 upgrade 不应报错，表结构保持完整"""
        from scripts import migrate

        monkeypatch.setattr(sys, "argv", ["migrate", "upgrade"])
        migrate.main()
        # 第二次执行：alembic 检测到已是最新版本，应无异常
        migrate.main()

        tables = _table_names(temp_db)
        assert EXPECTED_TABLES.issubset(tables)
        # 幂等执行后版本表仍只有一条记录
        conn = sqlite3.connect(temp_db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM alembic_version").fetchone()[0]
        finally:
            conn.close()
        assert count == 1

    def test_main_upgrade_records_version(self, temp_db, monkeypatch):
        """迁移后 alembic_version 表应记录非空版本号"""
        from scripts import migrate

        monkeypatch.setattr(sys, "argv", ["migrate", "upgrade"])
        migrate.main()

        conn = sqlite3.connect(temp_db)
        try:
            rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
        finally:
            conn.close()
        assert rows, "alembic_version 表为空"
        assert rows[0][0], "版本号为空"

    def test_upgrade_function_direct_call(self, temp_db):
        """upgrade() 函数直接调用应等价于 main() upgrade 子命令"""
        from scripts import migrate

        # 直接调用 upgrade()，默认升级到 head
        migrate.upgrade()

        tables = _table_names(temp_db)
        assert EXPECTED_TABLES.issubset(tables)

    def test_downgrade_then_upgrade_roundtrip(self, temp_db, monkeypatch):
        """downgrade() 回退后表被删除，再次 upgrade 应重建表"""
        from scripts import migrate

        monkeypatch.setattr(sys, "argv", ["migrate", "upgrade"])
        migrate.main()
        assert EXPECTED_TABLES.issubset(_table_names(temp_db))

        # 回退到 base：迁移链可能含多个 revision,downgrade -1 仅回退最近一个,
        # 这里显式回退到 base 才能确保所有业务表被删除(测试注释语义)。
        migrate.downgrade("base")
        tables_after_down = _table_names(temp_db)
        assert not EXPECTED_TABLES.issubset(
            tables_after_down
        ), "downgrade 后业务表仍存在"

        # 再次升级，表应重建（验证迁移可逆且可重复应用）
        migrate.upgrade()
        assert EXPECTED_TABLES.issubset(_table_names(temp_db))

    def test_main_current_subcommand(self, temp_db, monkeypatch):
        """main() current 子命令应在已迁移库上正常执行且不报错"""
        from scripts import migrate

        monkeypatch.setattr(sys, "argv", ["migrate", "upgrade"])
        migrate.main()
        # current 子命令查询当前版本（alembic 经 logging 输出，此处仅验证不抛异常）
        monkeypatch.setattr(sys, "argv", ["migrate", "current"])
        migrate.main()  # 不应抛出异常

        # 版本表应保持完整，未被只读命令影响
        conn = sqlite3.connect(temp_db)
        try:
            rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
        finally:
            conn.close()
        assert rows and rows[0][0]

    def test_main_history_subcommand(self, temp_db, monkeypatch):
        """main() history 子命令应查询迁移历史且不报错"""
        from scripts import migrate

        monkeypatch.setattr(sys, "argv", ["migrate", "upgrade"])
        migrate.main()
        # history 子命令列出迁移历史（alembic 经 logging 输出，此处仅验证不抛异常）
        monkeypatch.setattr(sys, "argv", ["migrate", "history"])
        migrate.main()  # 不应抛出异常


class TestMigrateSchema:
    """迁移后表结构完整性校验"""

    def test_evaluations_table_has_all_columns(self, temp_db, monkeypatch):
        """evaluations 表应包含全部预期列"""
        from scripts import migrate

        monkeypatch.setattr(sys, "argv", ["migrate", "upgrade"])
        migrate.main()

        cols = _table_columns(temp_db, "evaluations")
        missing = EXPECTED_EVALUATION_COLUMNS - cols
        assert not missing, f"evaluations 表缺少列: {missing}"

    def test_evaluations_table_has_indexes(self, temp_db, monkeypatch):
        """evaluations 表应创建关键索引"""
        from scripts import migrate

        monkeypatch.setattr(sys, "argv", ["migrate", "upgrade"])
        migrate.main()

        indexes = _table_indexes(temp_db, "evaluations")
        assert "ix_eval_employee_status" in indexes
        assert "ix_evaluations_evaluation_id" in indexes
        assert "ix_evaluations_period" in indexes

    def test_raw_inputs_table_columns(self, temp_db, monkeypatch):
        """raw_inputs 表应存在且含唯一约束相关列"""
        from scripts import migrate

        monkeypatch.setattr(sys, "argv", ["migrate", "upgrade"])
        migrate.main()

        cols = _table_columns(temp_db, "raw_inputs")
        for col in ("employee_id", "period", "input_id"):
            assert col in cols, f"raw_inputs 表缺少列: {col}"

    def test_raw_inputs_enforces_unique_constraint(self, temp_db, monkeypatch):
        """raw_inputs 表的 (employee_id, period, input_id) 唯一约束应生效"""
        from scripts import migrate

        monkeypatch.setattr(sys, "argv", ["migrate", "upgrade"])
        migrate.main()

        conn = sqlite3.connect(temp_db)
        try:
            # 先插入用户以满足外键约束（SQLite 默认未开启外键，此处仅为稳妥）
            conn.execute(
                "INSERT INTO users (id, user_id, name, role, created_at, updated_at) "
                "VALUES (1, 'E1', '张三', 'employee', '2026-01-01', '2026-01-01')"
            )
            conn.execute(
                "INSERT INTO raw_inputs (id, input_id, employee_id, period, type, "
                "content, attachments, created_at) "
                "VALUES (1, 'in-1', 'E1', 'W1', 'daily_report', 'x', '[]', '2026-01-01')"
            )
            # 重复 (employee_id, period, input_id) 应违反唯一约束
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO raw_inputs (id, input_id, employee_id, period, type, "
                    "content, attachments, created_at) "
                    "VALUES (2, 'in-1', 'E1', 'W1', 'daily_report', 'y', '[]', '2026-01-01')"
                )
        finally:
            conn.close()

    def test_all_tables_present_after_migration(self, temp_db, monkeypatch):
        """迁移后全部业务表与版本表齐全"""
        from scripts import migrate

        monkeypatch.setattr(sys, "argv", ["migrate", "upgrade"])
        migrate.main()

        tables = _table_names(temp_db)
        expected = EXPECTED_TABLES | {ALEMBIC_TABLE}
        assert expected.issubset(tables), f"缺少: {expected - tables}"
