"""
services/audit_service.py 单元测试
使用独立临时 SQLite 异步数据库，覆盖 log / get_logs / list_logs。
"""

import tempfile
from pathlib import Path

import pytest
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


# ---------------- log ----------------


async def test_log_creates_entry_with_complete_fields(audit_service, db_session):
    """log 应写入完整字段并返回 AuditLog 对象"""
    entry = await audit_service.log(
        actor_id="M001",
        action="approve",
        evaluation_id="EVAL-001",
        employee_id="E1001",
        details={"comment": "同意", "score": 88},
        ip_address="10.0.0.1",
    )
    await db_session.flush()

    assert entry.log_id.startswith("LOG-")
    assert entry.actor_id == "M001"
    assert entry.action == "approve"
    assert entry.evaluation_id == "EVAL-001"
    assert entry.employee_id == "E1001"
    assert entry.details == {"comment": "同意", "score": 88}
    assert entry.ip_address == "10.0.0.1"
    assert entry.created_at is not None


async def test_log_with_minimal_fields(audit_service, db_session):
    """仅必填字段时，可选字段应为合理默认"""
    entry = await audit_service.log(actor_id="U001", action="view")
    await db_session.flush()

    assert entry.evaluation_id is None
    assert entry.employee_id is None
    assert entry.details == {}  # details or {} 默认空 dict
    assert entry.ip_address is None


async def test_log_details_none_defaults_to_empty_dict(audit_service, db_session):
    """details 显式传 None 时应存储为空 dict"""
    entry = await audit_service.log(actor_id="U002", action="export", details=None)
    await db_session.flush()
    assert entry.details == {}


async def test_log_log_id_is_unique(audit_service, db_session):
    """多次 log 应生成不同 log_id"""
    e1 = await audit_service.log(actor_id="A", action="x")
    e2 = await audit_service.log(actor_id="A", action="x")
    await db_session.flush()
    assert e1.log_id != e2.log_id


# ---------------- get_logs ----------------


async def test_get_logs_by_evaluation_id(audit_service, db_session):
    """get_logs 按 evaluation_id 过滤"""
    await audit_service.log(actor_id="M001", action="approve", evaluation_id="EVAL-A")
    await audit_service.log(actor_id="M002", action="reject", evaluation_id="EVAL-B")
    await audit_service.log(actor_id="M001", action="appeal", evaluation_id="EVAL-A")
    await db_session.flush()

    logs = await audit_service.get_logs(evaluation_id="EVAL-A")
    assert len(logs) == 2
    assert all(l.evaluation_id == "EVAL-A" for l in logs)


async def test_get_logs_by_employee_id(audit_service, db_session):
    """get_logs 按 employee_id 过滤"""
    await audit_service.log(actor_id="A", action="x", employee_id="E1001")
    await audit_service.log(actor_id="A", action="y", employee_id="E1002")
    await audit_service.log(actor_id="A", action="z", employee_id="E1001")
    await db_session.flush()

    logs = await audit_service.get_logs(employee_id="E1001")
    assert len(logs) == 2
    assert all(l.employee_id == "E1001" for l in logs)


async def test_get_logs_respects_limit(audit_service, db_session):
    """get_logs 应尊重 limit 参数"""
    for i in range(5):
        await audit_service.log(actor_id="A", action=f"a{i}")
    await db_session.flush()

    logs = await audit_service.get_logs(limit=2)
    assert len(logs) == 2


async def test_get_logs_returns_empty_when_no_match(audit_service, db_session):
    """无匹配记录时返回空列表"""
    await audit_service.log(actor_id="A", action="x", evaluation_id="EVAL-1")
    await db_session.flush()

    logs = await audit_service.get_logs(evaluation_id="EVAL-NOPE")
    assert logs == []


async def test_get_logs_ordered_by_created_at_desc(audit_service, db_session):
    """get_logs 应按 created_at 倒序返回"""
    import time

    e1 = await audit_service.log(actor_id="A", action="first")
    await db_session.flush()
    time.sleep(0.01)
    e2 = await audit_service.log(actor_id="A", action="second")
    await db_session.flush()

    logs = await audit_service.get_logs()
    assert logs[0].action == "second"
    assert logs[1].action == "first"


# ---------------- list_logs ----------------


async def test_list_logs_default_pagination(audit_service, db_session):
    """list_logs 默认分页返回结构与字段"""
    await audit_service.log(
        actor_id="A",
        action="approve",
        evaluation_id="E1",
        employee_id="E1001",
        ip_address="1.1.1.1",
    )
    await db_session.flush()

    result = await audit_service.list_logs()
    assert result["total"] == 1
    assert result["page"] == 1
    assert result["page_size"] == 20
    assert len(result["logs"]) == 1
    log = result["logs"][0]
    assert log["actor_id"] == "A"
    assert log["action"] == "approve"
    assert log["evaluation_id"] == "E1"
    assert log["employee_id"] == "E1001"
    assert log["ip_address"] == "1.1.1.1"
    assert "created_at" in log and isinstance(log["created_at"], str)


async def test_list_logs_pagination(audit_service, db_session):
    """list_logs 分页：page_size 控制每页条数，total 为总数"""
    for i in range(7):
        await audit_service.log(actor_id="A", action=f"a{i}")
    await db_session.flush()

    page1 = await audit_service.list_logs(page=1, page_size=3)
    page2 = await audit_service.list_logs(page=2, page_size=3)
    page3 = await audit_service.list_logs(page=3, page_size=3)

    assert page1["total"] == 7
    assert len(page1["logs"]) == 3
    assert len(page2["logs"]) == 3
    assert len(page3["logs"]) == 1


async def test_list_logs_filter_by_actor_id(audit_service, db_session):
    """list_logs 按 actor_id 筛选"""
    await audit_service.log(actor_id="M001", action="approve")
    await audit_service.log(actor_id="M002", action="approve")
    await audit_service.log(actor_id="M001", action="reject")
    await db_session.flush()

    result = await audit_service.list_logs(actor_id="M001")
    assert result["total"] == 2
    assert all(l["actor_id"] == "M001" for l in result["logs"])


async def test_list_logs_filter_by_action(audit_service, db_session):
    """list_logs 按 action 筛选"""
    await audit_service.log(actor_id="A", action="approve")
    await audit_service.log(actor_id="B", action="reject")
    await audit_service.log(actor_id="C", action="approve")
    await db_session.flush()

    result = await audit_service.list_logs(action="approve")
    assert result["total"] == 2
    assert all(l["action"] == "approve" for l in result["logs"])


async def test_list_logs_combined_filter(audit_service, db_session):
    """list_logs 同时按 actor_id 与 action 筛选"""
    await audit_service.log(actor_id="M001", action="approve")
    await audit_service.log(actor_id="M001", action="reject")
    await audit_service.log(actor_id="M002", action="approve")
    await db_session.flush()

    result = await audit_service.list_logs(actor_id="M001", action="approve")
    assert result["total"] == 1
    assert result["logs"][0]["actor_id"] == "M001"
    assert result["logs"][0]["action"] == "approve"


async def test_list_logs_empty_when_no_match(audit_service, db_session):
    """无匹配记录时 total=0 且 logs 为空"""
    await audit_service.log(actor_id="A", action="x")
    await db_session.flush()

    result = await audit_service.list_logs(actor_id="NOBODY")
    assert result["total"] == 0
    assert result["logs"] == []


async def test_list_logs_page_beyond_range_returns_empty(audit_service, db_session):
    """页码超出范围时返回空 logs 但 total 仍正确"""
    for i in range(3):
        await audit_service.log(actor_id="A", action=f"a{i}")
    await db_session.flush()

    result = await audit_service.list_logs(page=10, page_size=20)
    assert result["total"] == 3
    assert result["page"] == 10
    assert result["logs"] == []


# ====================================================================
# services/audit_decorator.py 单元测试
# 覆盖 contextvar 上下文 API 与 audit_action 装饰器自动审计行为
# ====================================================================

from types import SimpleNamespace  # noqa: E402

from services.audit_decorator import (  # noqa: E402
    audit_action,
    reset_audit_context,
    set_audit_context,
)


# ---------------- audit_action 装饰器 ----------------


class TestAuditDecorator:
    """audit_action 装饰器：service 方法成功后自动写审计日志"""

    @pytest.fixture
    def fake_service(self, db_session):
        """构造一个带 @audit_action 装饰方法的 fake service，self.session 指向 db_session"""

        class FakeService:
            def __init__(self, session):
                self.session = session

            @audit_action("create_evaluation")
            async def create_with_return_obj(self, evaluation_data):
                """返回带 evaluation_id 属性的对象（模拟 Evaluation）"""
                return SimpleNamespace(evaluation_id=evaluation_data["evaluation_id"])

            @audit_action("view")
            async def view_no_return_attr(self, evaluation_id):
                """返回无 ID 属性的值，resource_id 应从 kwargs 提取"""
                return {"ok": True}

            @audit_action("approve", resource_type="evaluation")
            async def approve_with_kwargs_approver(self, evaluation_id, approver_id):
                """approver_id 在 kwargs，应作为 actor 兜底"""
                return SimpleNamespace(evaluation_id=evaluation_id)

            @audit_action("create_evaluation")
            async def raise_business_error(self, evaluation_data):
                """业务方法抛异常，按 P1-N3 应记 {action}_failed 审计"""
                raise ValueError("业务失败")

        return FakeService(db_session)

    async def _count_logs(self, db_session):
        from sqlalchemy import select, func

        return (await db_session.execute(select(func.count(AuditLog.id)))).scalar() or 0

    async def test_decorator_writes_audit_log_on_success(
        self, fake_service, db_session
    ):
        """被装饰方法成功返回后，应自动写一条审计日志"""
        before = await self._count_logs(db_session)
        result = await fake_service.create_with_return_obj(
            {"evaluation_id": "EVAL-DEC-1"}
        )
        await db_session.flush()
        after = await self._count_logs(db_session)
        # 业务结果仍正常返回
        assert result.evaluation_id == "EVAL-DEC-1"
        # 审计日志多了一条
        assert after - before == 1

    async def test_decorator_extracts_actor_from_contextvar(
        self, fake_service, db_session
    ):
        """actor_id 应优先从 contextvar 提取"""
        token = set_audit_context("U-CTX", "10.0.0.99")
        try:
            await fake_service.create_with_return_obj({"evaluation_id": "EVAL-CTX"})
            await db_session.flush()
        finally:
            reset_audit_context(token)

        from sqlalchemy import select

        log = (
            await db_session.execute(
                select(AuditLog).where(AuditLog.evaluation_id == "EVAL-CTX")
            )
        ).scalar_one()
        assert log.actor_id == "U-CTX"
        assert log.ip_address == "10.0.0.99"
        assert log.action == "create_evaluation"

    async def test_decorator_extracts_actor_from_kwargs_fallback(
        self, fake_service, db_session
    ):
        """contextvar 未设置时，actor_id 应从 kwargs（approver_id）兜底"""
        # 不调 set_audit_context，验证 kwargs 兜底
        await fake_service.approve_with_kwargs_approver(
            evaluation_id="EVAL-KW", approver_id="M-KW"
        )
        await db_session.flush()

        from sqlalchemy import select

        log = (
            await db_session.execute(
                select(AuditLog).where(AuditLog.evaluation_id == "EVAL-KW")
            )
        ).scalar_one()
        # approver_id 作为 actor 兜底
        assert log.actor_id == "M-KW"
        assert log.action == "approve"

    async def test_decorator_extracts_resource_id_from_return_value(
        self, fake_service, db_session
    ):
        """resource_id 应从返回值的 evaluation_id 属性提取"""
        await fake_service.create_with_return_obj({"evaluation_id": "EVAL-RID"})
        await db_session.flush()

        from sqlalchemy import select

        log = (
            await db_session.execute(
                select(AuditLog).where(AuditLog.evaluation_id == "EVAL-RID")
            )
        ).scalar_one()
        assert log.evaluation_id == "EVAL-RID"

    async def test_decorator_extracts_resource_id_from_kwargs(
        self, fake_service, db_session
    ):
        """返回值无 ID 属性时，resource_id 应从 kwargs（evaluation_id）提取"""
        await fake_service.view_no_return_attr(evaluation_id="EVAL-FROM-KW")
        await db_session.flush()

        from sqlalchemy import select

        # view 资源类型，evaluation_id 应被设置
        log = (
            await db_session.execute(
                select(AuditLog).where(AuditLog.evaluation_id == "EVAL-FROM-KW")
            )
        ).scalar_one()
        assert log.action == "view"

    async def test_decorator_default_actor_system_when_no_context(
        self, fake_service, db_session
    ):
        """无 contextvar 且 kwargs 无 actor_id 时，actor 默认 'system'"""
        await fake_service.create_with_return_obj({"evaluation_id": "EVAL-SYS"})
        await db_session.flush()

        from sqlalchemy import select

        log = (
            await db_session.execute(
                select(AuditLog).where(AuditLog.evaluation_id == "EVAL-SYS")
            )
        ).scalar_one()
        assert log.actor_id == "system"

    async def test_decorator_does_not_write_log_on_business_exception(
        self, fake_service, db_session, monkeypatch
    ):
        """P1-N3 + P2-5: 业务方法抛异常时,应通过独立 session 写一条 {action}_failed 审计日志,
        记录异常类型与消息,供安全审计追查越权/非法操作。独立 session 保证业务 rollback
        不影响 _failed 审计留存。"""
        # P2-5: _failed 分支改用 AsyncSessionLocal 独立 session,这里 monkeypatch 指向测试
        # db_session 以便断言落库行为(生产环境会落到独立连接并立即 commit)
        from contextlib import asynccontextmanager

        from services import audit_decorator as ad_module

        @asynccontextmanager
        async def _fake_session_local():
            yield db_session

        monkeypatch.setattr(ad_module, "AsyncSessionLocal", _fake_session_local)

        before = await self._count_logs(db_session)
        with pytest.raises(ValueError, match="业务失败"):
            await fake_service.raise_business_error({"evaluation_id": "EVAL-ERR"})
        await db_session.flush()
        after = await self._count_logs(db_session)
        assert after == before + 1  # 新增一条 *_failed 审计

        # 验证审计内容: action 应为 create_evaluation_failed, 含异常信息
        from sqlalchemy import select

        log = (
            await db_session.execute(
                select(AuditLog)
                .where(AuditLog.action == "create_evaluation_failed")
                .order_by(AuditLog.created_at.desc())
            )
        ).scalar_one()
        assert log.actor_id == "system"
        details = log.details or {}
        assert details.get("exception_type") == "ValueError"
        assert "业务失败" in details.get("exception_msg", "")

    async def test_decorator_audit_failure_does_not_block_business(
        self, fake_service, db_session, monkeypatch
    ):
        """审计写入抛异常时，业务结果仍应正常返回（不阻断业务）"""

        # 让 AuditService.log 抛异常
        async def _failing_log(self, *args, **kwargs):
            raise RuntimeError("审计写入失败")

        from services import audit_decorator as ad_module
        from services import audit_service as as_module

        monkeypatch.setattr(as_module.AuditService, "log", _failing_log)

        # 业务调用应成功，不抛
        result = await fake_service.create_with_return_obj(
            {"evaluation_id": "EVAL-AUDIT-FAIL"}
        )
        assert result.evaluation_id == "EVAL-AUDIT-FAIL"

    async def test_decorator_records_metric(
        self, fake_service, db_session, monkeypatch
    ):
        """成功审计后应调 record_audit_log(action) 埋点"""
        recorded = []
        from core import metrics as metrics_module

        monkeypatch.setattr(
            metrics_module,
            "record_audit_log",
            lambda action: recorded.append(action),
        )
        await fake_service.create_with_return_obj({"evaluation_id": "EVAL-METRIC"})
        await db_session.flush()
        assert "create_evaluation" in recorded

    async def test_decorator_records_failure_metric_on_audit_error(
        self, fake_service, db_session, monkeypatch
    ):
        """审计异常时应调 record_audit_log_failure 埋点"""

        async def _failing_log(self, *args, **kwargs):
            raise RuntimeError("审计失败")

        from services import audit_service as as_module

        monkeypatch.setattr(as_module.AuditService, "log", _failing_log)

        failures = []
        from core import metrics as metrics_module

        monkeypatch.setattr(
            metrics_module,
            "record_audit_log_failure",
            lambda: failures.append(1),
        )
        await fake_service.create_with_return_obj({"evaluation_id": "EVAL-FAIL-METRIC"})
        assert len(failures) == 1
