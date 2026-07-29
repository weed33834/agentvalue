"""
services/compliance_service.py 单元测试 (P1-31: SOC2 / ISO27001 合规认证框架)

使用独立临时 SQLite 异步数据库, 覆盖:
- 控制项初始化 (SOC2 + ISO27001 数量 / 幂等)
- 自动化检查 (配置审计 / 访问日志 / 备份验证 / 安全扫描)
- 合规报告生成 (JSON + CSV)
- 手动更新控制项
- 框架过滤
"""

import csv
import io
import sqlite3
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.database import Base
from models.compliance import (
    CONTROL_STATUS_FAIL,
    CONTROL_STATUS_NOT_APPLICABLE,
    CONTROL_STATUS_PASS,
    CONTROL_STATUS_WARNING,
    ComplianceEvidence,
    FRAMEWORK_ISO27001,
    FRAMEWORK_SOC2,
)
from services.compliance_service import (
    COMPLIANCE_CONTROLS,
    ComplianceService,
    count_controls_by_framework,
)


# ============================================================
# 公共 fixture
# ============================================================


@pytest.fixture
async def db_session():
    """每个测试使用独立临时 SQLite 异步数据库"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_url = f"sqlite+aiosqlite:///{tmp.name}"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    async with SessionLocal() as session:
        yield session
    await engine.dispose()
    Path(tmp.name).unlink(missing_ok=True)


@pytest.fixture
def compliance_service(db_session):
    return ComplianceService(db_session)


@pytest.fixture
def stub_security_tools(monkeypatch):
    """桩掉安全扫描工具 (gitleaks / pip-audit), 避免测试依赖外部安装"""

    def _fake_run_security_tool(tool_name, cmd, cwd, *, clean_on_zero=False):
        # 模拟工具未安装, 返回 warning
        return {
            "status": CONTROL_STATUS_WARNING,
            "message": f"{tool_name} 未安装, 跳过扫描",
            "evidence": {"tool": tool_name, "installed": False, "exit_code": None},
        }

    monkeypatch.setattr(
        ComplianceService, "_run_security_tool", staticmethod(_fake_run_security_tool)
    )


# ============================================================
# 控制项初始化
# ============================================================


async def test_initialize_controls_creates_all(compliance_service, db_session):
    """初始化应创建全部预置控制项 (SOC2=24, ISO27001=17, 总计 41)"""
    created = await compliance_service.initialize_controls()
    assert created == len(COMPLIANCE_CONTROLS) == 41
    assert created == count_controls_by_framework(FRAMEWORK_SOC2) + count_controls_by_framework(
        FRAMEWORK_ISO27001
    )

    controls = await compliance_service.list_controls()
    soc2 = [c for c in controls if c.framework == FRAMEWORK_SOC2]
    iso = [c for c in controls if c.framework == FRAMEWORK_ISO27001]
    assert len(soc2) == 24
    assert len(iso) == 17

    # 初始状态均为 not_applicable
    assert all(c.status == CONTROL_STATUS_NOT_APPLICABLE for c in controls)
    # 证据快照包含 check_key/check_name 元信息
    assert all(isinstance(c.evidence, dict) for c in controls)
    assert all("check_key" in c.evidence for c in controls)


async def test_initialize_controls_idempotent(compliance_service, db_session):
    """重复初始化应幂等 (不重复创建)"""
    first = await compliance_service.initialize_controls()
    second = await compliance_service.initialize_controls()
    assert first == 41
    assert second == 0
    controls = await compliance_service.list_controls()
    assert len(controls) == 41


async def test_initialize_covers_soc2_five_dimensions(compliance_service, db_session):
    """SOC2 预置控制项应覆盖五个维度: 安全/可用性/处理完整性/机密性/隐私性"""
    await compliance_service.initialize_controls()
    controls = await compliance_service.list_controls(framework=FRAMEWORK_SOC2)
    categories = {c.category for c in controls}
    assert categories == {
        "security",
        "availability",
        "processing_integrity",
        "confidentiality",
        "privacy",
    }


async def test_initialize_covers_iso27001_four_themes(compliance_service, db_session):
    """ISO27001 预置控制项应覆盖四个主题: 组织/人员/物理/技术"""
    await compliance_service.initialize_controls()
    controls = await compliance_service.list_controls(framework=FRAMEWORK_ISO27001)
    categories = {c.category for c in controls}
    assert categories == {"organizational", "people", "physical", "technological"}


# ============================================================
# 框架过滤
# ============================================================


async def test_list_controls_framework_filter(compliance_service, db_session):
    """list_controls 按 framework 过滤应只返回对应框架的控制项"""
    await compliance_service.initialize_controls()

    soc2 = await compliance_service.list_controls(framework=FRAMEWORK_SOC2)
    iso = await compliance_service.list_controls(framework=FRAMEWORK_ISO27001)

    assert len(soc2) == 24
    assert len(iso) == 17
    assert all(c.framework == FRAMEWORK_SOC2 for c in soc2)
    assert all(c.framework == FRAMEWORK_ISO27001 for c in iso)
    # SOC2 与 ISO27001 控制编号集合无交集
    soc2_ids = {c.control_id for c in soc2}
    iso_ids = {c.control_id for c in iso}
    assert soc2_ids.isdisjoint(iso_ids)


# ============================================================
# 控制项详情
# ============================================================


async def test_get_control_returns_detail(compliance_service, db_session):
    """get_control 应返回控制项详情"""
    await compliance_service.initialize_controls()
    control = await compliance_service.get_control("CC6.1", framework=FRAMEWORK_SOC2)
    assert control is not None
    assert control.control_id == "CC6.1"
    assert control.framework == FRAMEWORK_SOC2
    assert control.title == "JWT 密钥强度"


async def test_get_control_returns_none_when_missing(compliance_service, db_session):
    """get_control 不存在时返回 None"""
    await compliance_service.initialize_controls()
    control = await compliance_service.get_control("NOPE.999")
    assert control is None


# ============================================================
# 自动化检查: 配置审计
# ============================================================


async def test_automated_check_config_audit(
    compliance_service, db_session, stub_security_tools
):
    """自动化检查: 配置审计 (JWT 密钥=pass, 演示模式=fail)

    conftest 设置 auth_demo_mode=True / jwt_secret_key 为非默认强随机值,
    因此 auth_demo_mode -> fail, jwt_secret_key -> pass (确定性)。
    """
    result = await compliance_service.run_automated_check()
    assert result["checked"] > 0

    # JWT 密钥强度 (conftest 配置非默认值) -> pass
    jwt_control = await compliance_service.get_control("CC6.1", framework=FRAMEWORK_SOC2)
    assert jwt_control.status == CONTROL_STATUS_PASS
    assert jwt_control.last_checked is not None
    assert jwt_control.evidence.get("check_key") == "config_audit"
    assert jwt_control.evidence.get("check_name") == "jwt_secret_key"

    # 演示模式 (conftest 开启 demo mode) -> fail
    demo_control = await compliance_service.get_control("CC1.1", framework=FRAMEWORK_SOC2)
    assert demo_control.status == CONTROL_STATUS_FAIL
    assert "AUTH_DEMO_MODE" in demo_control.evidence.get("message", "")

    # 证据时间序列记录已写入
    ev_rows = (
        await db_session.execute(
            select(ComplianceEvidence).where(
                ComplianceEvidence.control_id == "CC6.1"
            )
        )
    ).scalars().all()
    assert len(ev_rows) >= 1
    assert ev_rows[0].evidence_type == "config_audit"
    assert ev_rows[0].collector == "system"


async def test_automated_check_config_audit_extension_https_debug(
    compliance_service, db_session, stub_security_tools
):
    """配置审计扩展项: HTTPS 强制 / 调试模式关闭"""
    await compliance_service.run_automated_check()

    # 调试模式关闭 (conftest debug=False) -> pass
    debug_control = await compliance_service.get_control("CC7.1", framework=FRAMEWORK_SOC2)
    assert debug_control.status == CONTROL_STATUS_PASS
    assert debug_control.evidence.get("check_name") == "debug_mode_off"

    # HTTPS 强制: 控制项被检查并设置状态 (conftest cors 含 http:// 前缀 -> warning)
    https_control = await compliance_service.get_control("CC7.2", framework=FRAMEWORK_SOC2)
    assert https_control.evidence.get("check_name") == "https_enforced"
    assert https_control.status in (
        CONTROL_STATUS_PASS,
        CONTROL_STATUS_WARNING,
    )


# ============================================================
# 自动化检查: 访问日志
# ============================================================


async def test_automated_check_access_log(
    compliance_service, db_session, stub_security_tools
):
    """自动化检查: 访问日志 (审计日志启用=pass, PII 脱敏=pass)

    测试库已建表 (audit_logs 可查) 且 redact_pii 对样本掩码成功。
    """
    await compliance_service.run_automated_check()

    # 审计日志启用 -> pass (表可查)
    audit_control = await compliance_service.get_control("CC7.3", framework=FRAMEWORK_SOC2)
    assert audit_control.status == CONTROL_STATUS_PASS
    assert audit_control.evidence.get("check_key") == "access_log"
    assert audit_control.evidence.get("check_name") == "audit_log_enabled"

    # PII 脱敏 -> pass (redact_pii 成功掩码手机号/邮箱/身份证)
    pii_control = await compliance_service.get_control("CC7.4", framework=FRAMEWORK_SOC2)
    assert pii_control.status == CONTROL_STATUS_PASS
    details = pii_control.evidence.get("details", {})
    assert details.get("pii_working") is True
    # 原始样本含明文 PII, 脱敏后不应包含
    assert "13800138000" not in details.get("redacted_output", "")


# ============================================================
# 自动化检查: 备份验证
# ============================================================


async def test_automated_check_backup_verify(
    compliance_service, db_session, monkeypatch, stub_security_tools
):
    """自动化检查: 备份验证 (备份文件存在=pass, 恢复验证=pass)

    通过 monkeypatch 将 db_backup.BACKUP_DIR 指向含有效 SQLite 备份的临时目录,
    验证备份文件存在检查与 PRAGMA integrity_check 恢复验证。
    """
    # 准备临时备份目录 + 有效 SQLite 备份文件
    backup_dir = Path(tempfile.mkdtemp(prefix="compliance_backup_"))
    backup_file = backup_dir / "db_20260101_000000.sqlite"
    conn = sqlite3.connect(str(backup_file))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO t (id) VALUES (1)")
    conn.commit()
    conn.close()

    import scripts.db_backup as db_backup_mod

    monkeypatch.setattr(db_backup_mod, "BACKUP_DIR", backup_dir)

    await compliance_service.run_automated_check()

    # 备份文件存在 -> pass
    backup_control = await compliance_service.get_control(
        "A1.2", framework=FRAMEWORK_SOC2
    )
    assert backup_control.status == CONTROL_STATUS_PASS
    assert backup_control.evidence.get("check_name") == "backup_file_exists"
    assert backup_control.evidence.get("details", {}).get("backup_count") == 1

    # 恢复验证 (PRAGMA integrity_check = ok) -> pass
    restore_control = await compliance_service.get_control(
        "A1.3", framework=FRAMEWORK_SOC2
    )
    assert restore_control.status == CONTROL_STATUS_PASS
    assert restore_control.evidence.get("details", {}).get("restore_passed") is True

    # ISO27001 备份控制项同步更新 (同一底层检查)
    iso_backup = await compliance_service.get_control(
        "A.5.30", framework=FRAMEWORK_ISO27001
    )
    assert iso_backup.status == CONTROL_STATUS_PASS


async def test_automated_check_backup_missing(
    compliance_service, db_session, monkeypatch, stub_security_tools
):
    """备份验证: 无备份文件时 backup_file_exists -> warning, restore_test -> warning"""
    empty_dir = Path(tempfile.mkdtemp(prefix="compliance_empty_"))
    import scripts.db_backup as db_backup_mod

    monkeypatch.setattr(db_backup_mod, "BACKUP_DIR", empty_dir)

    await compliance_service.run_automated_check()

    backup_control = await compliance_service.get_control(
        "A1.2", framework=FRAMEWORK_SOC2
    )
    assert backup_control.status == CONTROL_STATUS_WARNING
    restore_control = await compliance_service.get_control(
        "A1.3", framework=FRAMEWORK_SOC2
    )
    assert restore_control.status == CONTROL_STATUS_WARNING


# ============================================================
# 自动化检查: 安全扫描
# ============================================================


async def test_automated_check_security_scan_pass(
    compliance_service, db_session, monkeypatch
):
    """自动化检查: 安全扫描 (gitleaks / pip-audit 通过 -> pass)"""

    def _clean_scan(tool_name, cmd, cwd, *, clean_on_zero=False):
        return {
            "status": CONTROL_STATUS_PASS,
            "message": f"{tool_name} 扫描通过, 未发现风险",
            "evidence": {
                "tool": tool_name,
                "installed": True,
                "exit_code": 0,
                "has_findings": False,
            },
        }

    monkeypatch.setattr(
        ComplianceService, "_run_security_tool", staticmethod(_clean_scan)
    )

    await compliance_service.run_automated_check()

    # gitleaks 扫描 -> pass
    gitleaks_control = await compliance_service.get_control(
        "CC8.3", framework=FRAMEWORK_SOC2
    )
    assert gitleaks_control.status == CONTROL_STATUS_PASS
    assert gitleaks_control.evidence.get("check_name") == "gitleaks_scan"

    # pip-audit 扫描 -> pass
    pip_control = await compliance_service.get_control(
        "CC8.2", framework=FRAMEWORK_SOC2
    )
    assert pip_control.status == CONTROL_STATUS_PASS
    assert pip_control.evidence.get("check_name") == "pip_audit_scan"

    # ISO27001 A.8.12 数据泄漏防护 (gitleaks) 同步 pass
    iso_dlp = await compliance_service.get_control(
        "A.8.12", framework=FRAMEWORK_ISO27001
    )
    assert iso_dlp.status == CONTROL_STATUS_PASS


async def test_automated_check_security_scan_findings(
    compliance_service, db_session, monkeypatch
):
    """自动化检查: 安全扫描发现风险 (非零退出码) -> fail"""

    def _dirty_scan(tool_name, cmd, cwd, *, clean_on_zero=False):
        return {
            "status": CONTROL_STATUS_FAIL,
            "message": f"{tool_name} 扫描发现风险 (exit_code=1)",
            "evidence": {
                "tool": tool_name,
                "installed": True,
                "exit_code": 1,
                "has_findings": True,
            },
        }

    monkeypatch.setattr(
        ComplianceService, "_run_security_tool", staticmethod(_dirty_scan)
    )

    await compliance_service.run_automated_check()

    gitleaks_control = await compliance_service.get_control(
        "CC8.3", framework=FRAMEWORK_SOC2
    )
    assert gitleaks_control.status == CONTROL_STATUS_FAIL
    pip_control = await compliance_service.get_control(
        "CC8.2", framework=FRAMEWORK_SOC2
    )
    assert pip_control.status == CONTROL_STATUS_FAIL


async def test_automated_check_manual_controls_unchanged(
    compliance_service, db_session, stub_security_tools
):
    """自动化检查: 无自动检查的控制项 (人工评估) 保持 not_applicable"""
    await compliance_service.run_automated_check()

    # A.5.1 信息安全策略 (check_key=None) 应保持 not_applicable
    manual_control = await compliance_service.get_control(
        "A.5.1", framework=FRAMEWORK_ISO27001
    )
    assert manual_control.status == CONTROL_STATUS_NOT_APPLICABLE
    assert manual_control.last_checked is None  # 未被自动检查触碰

    # P2.1 数据留存策略 (check_key=None) 同样保持
    p2 = await compliance_service.get_control("P2.1", framework=FRAMEWORK_SOC2)
    assert p2.status == CONTROL_STATUS_NOT_APPLICABLE


# ============================================================
# 合规报告生成
# ============================================================


async def test_generate_report_json(compliance_service, db_session, stub_security_tools):
    """生成 JSON 合规报告 (含 summary / controls / csv)"""
    await compliance_service.run_automated_check()

    report = await compliance_service.generate_report(fmt="json")
    assert "generated_at" in report
    assert report["framework"] == "ALL"
    assert "summary" in report
    assert "controls" in report
    assert "csv" in report

    summary = report["summary"]
    assert summary["total"] == 41
    assert summary["pass"] + summary["fail"] + summary["warning"] + summary[
        "not_applicable"
    ] == 41
    # 合规度在 [0, 100]
    assert 0.0 <= summary["compliance_score"] <= 100.0
    # by_framework 含两个框架
    assert FRAMEWORK_SOC2 in summary["by_framework"]
    assert FRAMEWORK_ISO27001 in summary["by_framework"]
    assert len(report["controls"]) == 41


async def test_generate_report_json_framework_filter(
    compliance_service, db_session, stub_security_tools
):
    """JSON 报告按框架过滤"""
    await compliance_service.run_automated_check()
    report = await compliance_service.generate_report(
        framework=FRAMEWORK_ISO27001, fmt="json"
    )
    assert report["framework"] == FRAMEWORK_ISO27001
    assert report["summary"]["total"] == 17
    assert all(
        c["framework"] == FRAMEWORK_ISO27001 for c in report["controls"]
    )


async def test_generate_report_csv(compliance_service, db_session, stub_security_tools):
    """生成 CSV 控制矩阵 (可导入 GRC 工具)"""
    await compliance_service.run_automated_check()

    result = await compliance_service.generate_report(fmt="csv")
    assert "csv" in result
    csv_str = result["csv"]
    reader = list(csv.reader(io.StringIO(csv_str)))
    # 表头 + 41 行数据
    assert len(reader) == 42
    header = reader[0]
    assert header == [
        "framework",
        "control_id",
        "title",
        "category",
        "status",
        "owner",
        "last_checked",
        "check_key",
        "check_name",
        "message",
    ]
    # 数据行第一列为框架, 全部为 SOC2 / ISO27001
    frameworks = {row[0] for row in reader[1:]}
    assert frameworks <= {FRAMEWORK_SOC2, FRAMEWORK_ISO27001}
    # 控制编号列含 CC6.1 (JWT 密钥强度)
    control_ids = {row[1] for row in reader[1:]}
    assert "CC6.1" in control_ids
    assert "A.5.17" in control_ids


async def test_generate_report_csv_framework_filter(
    compliance_service, db_session, stub_security_tools
):
    """CSV 报告按框架过滤"""
    await compliance_service.run_automated_check()
    result = await compliance_service.generate_report(
        framework=FRAMEWORK_SOC2, fmt="csv"
    )
    reader = list(csv.reader(io.StringIO(result["csv"])))
    # 表头 + 24 行
    assert len(reader) == 25
    assert all(row[0] == FRAMEWORK_SOC2 for row in reader[1:])


# ============================================================
# 手动更新控制项
# ============================================================


async def test_manual_update_control_status(compliance_service, db_session):
    """手动更新控制项状态 (含证据留痕)"""
    await compliance_service.initialize_controls()

    updated = await compliance_service.update_control(
        "A.5.1",
        status=CONTROL_STATUS_PASS,
        owner="dpo-001",
        framework=FRAMEWORK_ISO27001,
        collector="admin-001",
    )
    assert updated.status == CONTROL_STATUS_PASS
    assert updated.owner == "dpo-001"
    assert updated.last_checked is not None
    assert updated.evidence.get("status") == CONTROL_STATUS_PASS
    assert updated.evidence.get("last_updated_by") == "admin-001"

    # 证据时间序列记录已写入 (人工更新留痕)
    ev_rows = (
        await db_session.execute(
            select(ComplianceEvidence).where(
                ComplianceEvidence.control_id == "A.5.1"
            )
        )
    ).scalars().all()
    assert len(ev_rows) == 1
    assert ev_rows[0].evidence_type == "manual"
    assert ev_rows[0].collector == "admin-001"
    assert ev_rows[0].status == CONTROL_STATUS_PASS


async def test_manual_update_control_appends_evidence(compliance_service, db_session):
    """多次手动更新应追加多条证据记录 (时间序列)"""
    await compliance_service.initialize_controls()

    await compliance_service.update_control(
        "P2.1", status=CONTROL_STATUS_WARNING, collector="admin-001"
    )
    await compliance_service.update_control(
        "P2.1", status=CONTROL_STATUS_PASS, collector="admin-002"
    )

    ev_rows = (
        await db_session.execute(
            select(ComplianceEvidence)
            .where(ComplianceEvidence.control_id == "P2.1")
            .order_by(ComplianceEvidence.collected_at.asc())
        )
    ).scalars().all()
    assert len(ev_rows) == 2
    assert ev_rows[0].status == CONTROL_STATUS_WARNING
    assert ev_rows[1].status == CONTROL_STATUS_PASS


async def test_manual_update_invalid_status_raises(compliance_service, db_session):
    """手动更新传入非法状态应抛 ValueError"""
    await compliance_service.initialize_controls()
    with pytest.raises(ValueError, match="无效的控制项状态"):
        await compliance_service.update_control("CC6.1", status="invalid_status")


async def test_manual_update_nonexistent_control_raises(
    compliance_service, db_session
):
    """手动更新不存在的控制项应抛 ValueError"""
    await compliance_service.initialize_controls()
    with pytest.raises(ValueError, match="不存在"):
        await compliance_service.update_control("NOPE.999", status=CONTROL_STATUS_PASS)


async def test_manual_update_merges_evidence(compliance_service, db_session):
    """手动更新传入 evidence 应合并入证据快照"""
    await compliance_service.initialize_controls()
    updated = await compliance_service.update_control(
        "CC6.1",
        status=CONTROL_STATUS_PASS,
        evidence={"reviewer_note": "已复核, 密钥强度符合要求"},
        collector="admin-001",
    )
    assert updated.evidence.get("reviewer_note") == "已复核, 密钥强度符合要求"
    # 原有 check_key 元信息应保留 (合并而非覆盖)
    assert updated.evidence.get("check_key") == "config_audit"
