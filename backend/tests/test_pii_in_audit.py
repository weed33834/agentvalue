"""
P0-3: 验证 AuditService 落库前对 details 做 PII 脱敏。

覆盖:
- log() 写入含手机号/邮箱/身份证号/银行卡号的 details, 落库后应为脱敏值
- record_guard_check() 的 triggered_rules 同样被脱敏(可能含被拦截原文)
- record_guard_result() 便捷方法透传 GuardResult 时也脱敏
- 嵌套 dict/list 结构的字符串值递归脱敏
- 非 details 字段(actor_id / ip_address / evaluation_id)不被破坏
"""

import tempfile
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.database import Base
from models import AuditLog  # 触发模型注册
from services.audit_service import AuditService


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
def audit_service(db_session):
    return AuditService(db_session)


async def _fetch_log(db_session, action: str) -> AuditLog:
    return (
        await db_session.execute(select(AuditLog).where(AuditLog.action == action))
    ).scalar_one()


# ---------------- log() PII 脱敏 ----------------


async def test_log_redacts_phone_and_email(audit_service, db_session):
    """log 写入的 details 含手机号/邮箱,落库后应替换为脱敏值,明文不入库"""
    await audit_service.log(
        actor_id="M001",
        action="approve",
        details={"comment": "联系 13800138000 或 test@example.com 确认"},
    )
    await db_session.flush()

    log = await _fetch_log(db_session, "approve")
    stored = log.details["comment"]
    assert "13800138000" not in stored
    assert "test@example.com" not in stored
    assert "138****8000" in stored
    assert "te***@example.com" in stored


async def test_log_redacts_idcard_and_bankcard(audit_service, db_session):
    """身份证号/银行卡号也应被脱敏"""
    await audit_service.log(
        actor_id="M001",
        action="export",
        details={
            "idcard": "110101199001011234",
            "bankcard": "6228480000000001234",
        },
    )
    await db_session.flush()

    log = await _fetch_log(db_session, "export")
    assert "110101199001011234" not in log.details["idcard"]
    assert "6228480000000001234" not in log.details["bankcard"]
    assert log.details["idcard"].endswith("1234")
    assert log.details["bankcard"].endswith("1234")


async def test_log_redacts_nested_dict_and_list(audit_service, db_session):
    """嵌套 dict / list 中的字符串值应递归脱敏"""
    await audit_service.log(
        actor_id="M001",
        action="update",
        details={
            "meta": {"contact": "邮箱 user@demo.com"},
            "tags": ["手机 13912345678", "正常文本"],
            "count": 2,
        },
    )
    await db_session.flush()

    log = await _fetch_log(db_session, "update")
    assert "user@demo.com" not in log.details["meta"]["contact"]
    assert "us***@demo.com" in log.details["meta"]["contact"]
    assert "13912345678" not in log.details["tags"][0]
    assert "139****5678" in log.details["tags"][0]
    assert log.details["tags"][1] == "正常文本"
    # 非字符串类型原样保留
    assert log.details["count"] == 2


async def test_log_preserves_non_pii_details(audit_service, db_session):
    """无 PII 的 details 应原样落库(脱敏幂等,不破坏正常内容)"""
    await audit_service.log(
        actor_id="M001",
        action="approve",
        details={"comment": "同意", "score": 88, "tags": ["稳定", "执行力强"]},
    )
    await db_session.flush()

    log = await _fetch_log(db_session, "approve")
    assert log.details == {"comment": "同意", "score": 88, "tags": ["稳定", "执行力强"]}


async def test_log_none_details_becomes_empty_dict(audit_service, db_session):
    """details=None 脱敏后仍为空 dict"""
    await audit_service.log(actor_id="U001", action="view", details=None)
    await db_session.flush()
    log = await _fetch_log(db_session, "view")
    assert log.details == {}


async def test_log_non_details_fields_not_corrupted(audit_service, db_session):
    """脱敏只作用于 details,actor_id/ip_address/evaluation_id 不受影响"""
    await audit_service.log(
        actor_id="M001",
        action="approve",
        evaluation_id="EVAL-1",
        employee_id="E1001",
        details={"note": "联系 13800138000"},
        ip_address="10.0.0.1",
    )
    await db_session.flush()

    log = await _fetch_log(db_session, "approve")
    assert log.actor_id == "M001"
    assert log.evaluation_id == "EVAL-1"
    assert log.employee_id == "E1001"
    assert log.ip_address == "10.0.0.1"


# ---------------- record_guard_check() triggered_rules 脱敏 ----------------


async def test_record_guard_check_redacts_triggered_rules(audit_service, db_session):
    """triggered_rules 可能含被拦截原文,落库前应脱敏"""
    await audit_service.record_guard_check(
        guard_type="input",
        result="blocked",
        triggered_rules=[
            "input[0]:injection_pattern:联系 13800138000",
            "input[0]:malicious_pattern:drop table",
        ],
        would_be_false_positive=False,
        evaluation_id="EVAL-1",
    )
    await db_session.flush()

    log = await _fetch_log(db_session, "guard_check")
    rules = log.details["triggered_rules"]
    assert "13800138000" not in rules[0]
    assert "138****8000" in rules[0]
    # 无 PII 的规则原样保留
    assert rules[1] == "input[0]:malicious_pattern:drop table"
    assert log.details["guard_type"] == "input"
    assert log.details["result"] == "blocked"
    assert "would_be_false_positive" not in log.details


async def test_record_guard_check_marks_false_positive(audit_service, db_session):
    """would_be_false_positive=True 时 details 应含该标记"""
    await audit_service.record_guard_check(
        guard_type="output",
        result="blocked",
        triggered_rules=["biased_words:性别"],
        would_be_false_positive=True,
    )
    await db_session.flush()

    log = await _fetch_log(db_session, "guard_check")
    assert log.details["would_be_false_positive"] is True
