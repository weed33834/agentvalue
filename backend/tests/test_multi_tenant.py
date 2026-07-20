"""
Phase 9.1 多租户与权限矩阵测试

覆盖：
- 9.1.1/9.1.2 数据级隔离：两租户数据互不可见（service 层 + API 层）
- 9.1.2 RBAC：employee 只能看自己、hr 看全 tenant、跨租户不可见
- 9.1.3 向量库分 collection：memory/kb 按 tenant 前缀隔离
- 默认 tenant 兼容：未设 tenant 时数据落 default
- 9.1.4 租户管理 API CRUD + 权限
"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.config import Settings, get_settings
from core.database import Base, close_db, init_db
from core.tenant_context import (
    get_current_tenant,
    reset_current_tenant,
    set_current_tenant,
    tenant_scope,
)
from main import app
from memory.vector_store import ChromaCompanyKB, ChromaMemoryStore
from models import Evaluation  # 触发模型注册
from models.constants import EvaluationStatus
from services.audit_service import AuditService
from services.evaluation_service import EvaluationService


# ---------------- 通用辅助 ----------------


def _eval_data(
    evaluation_id="EVAL-1",
    employee_id="E1001",
    period="2026-W01",
    score=80.0,
    status=EvaluationStatus.AI_DRAFTED,
):
    """构造一份合法的评估数据字典"""
    return {
        "evaluation_id": evaluation_id,
        "employee_id": employee_id,
        "period": period,
        "overall_score": score,
        "employee_view": {
            "summary": "表现稳定",
            "strengths": ["执行力强"],
            "growth_areas": [
                {
                    "dimension": "执行力",
                    "score": 85,
                    "evidence": ["按时完成日报"],
                    "improvement_actions": ["继续保持"],
                }
            ],
            "next_week_focus": ["保持节奏"],
        },
        "manager_view": {
            "harsh_assessment": "稳定但缺乏突破",
            "risk_flags": [],
            "roi_analysis": "ROI 正常",
            "reallocation_suggestion": "维持现状",
            "hidden_issues": [],
        },
        "audit": {
            "model_name": "qwen2.5-7b",
            "model_tier": "L2",
            "confidence_score": 0.8,
            "raw_data_refs": ["input-1"],
            "triggered_rules": [],
            "processing_time_ms": 1200,
            "prompt_version": "v0.1",
        },
        "status": status,
    }


@pytest.fixture(autouse=True)
def _reset_tenant_ctx():
    """每个测试前后重置租户上下文为 default，避免 contextvar 跨测试泄漏"""
    token = set_current_tenant("default")
    yield
    reset_current_tenant(token)
    set_current_tenant("default")


# ---------------- service 层隔离测试 ----------------


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


async def test_raw_inputs_tenant_isolated(db_session):
    """两租户的原始输入互不可见"""
    svc = EvaluationService(db_session)
    with tenant_scope("acme"):
        await svc.create_raw_input(
            {
                "input_id": "IN-acme",
                "employee_id": "E1",
                "period": "2026-W01",
                "type": "daily_report",
                "content": "acme 输入",
            }
        )
        await db_session.flush()
    with tenant_scope("globex"):
        await svc.create_raw_input(
            {
                "input_id": "IN-globex",
                "employee_id": "E1",
                "period": "2026-W01",
                "type": "daily_report",
                "content": "globex 输入",
            }
        )
        await db_session.flush()

    # acme 只看到自己的输入
    with tenant_scope("acme"):
        rows = await svc.list_raw_inputs(employee_id="E1")
        assert len(rows) == 1
        assert rows[0].input_id == "IN-acme"
    # globex 只看到自己的输入
    with tenant_scope("globex"):
        rows = await svc.list_raw_inputs(employee_id="E1")
        assert len(rows) == 1
        assert rows[0].input_id == "IN-globex"


async def test_evaluations_tenant_isolated(db_session):
    """两租户的评估互不可见"""
    svc = EvaluationService(db_session)
    with tenant_scope("acme"):
        await svc.create_user({"user_id": "E1", "name": "E1", "role": "employee"})
        await svc.create_evaluation(_eval_data("EVAL-acme", "E1", score=70.0))
        await db_session.flush()
    with tenant_scope("globex"):
        await svc.create_user({"user_id": "E1", "name": "E1", "role": "employee"})
        await svc.create_evaluation(_eval_data("EVAL-globex", "E1", score=90.0))
        await db_session.flush()

    with tenant_scope("acme"):
        result = await svc.list_evaluations(employee_id="E1")
        assert result["total"] == 1
        assert result["items"][0].evaluation_id == "EVAL-acme"
    with tenant_scope("globex"):
        result = await svc.list_evaluations(employee_id="E1")
        assert result["total"] == 1
        assert result["items"][0].evaluation_id == "EVAL-globex"


async def test_users_same_id_different_tenants(db_session):
    """同一 user_id 可存在于不同租户，get_user 按当前租户返回"""
    svc = EvaluationService(db_session)
    with tenant_scope("acme"):
        await svc.create_user(
            {"user_id": "E1", "name": "acme-员工", "role": "employee"}
        )
        await db_session.flush()
    with tenant_scope("globex"):
        await svc.create_user(
            {"user_id": "E1", "name": "globex-员工", "role": "employee"}
        )
        await db_session.flush()

    with tenant_scope("acme"):
        user = await svc.get_user("E1")
        assert user is not None
        assert user.name == "acme-员工"
        assert user.tenant_id == "acme"
    with tenant_scope("globex"):
        user = await svc.get_user("E1")
        assert user is not None
        assert user.name == "globex-员工"
        assert user.tenant_id == "globex"


async def test_audit_logs_tenant_isolated(db_session):
    """审计日志按租户隔离"""
    audit_svc = AuditService(db_session)
    with tenant_scope("acme"):
        await audit_svc.log(actor_id="A1", action="create_input", details={"k": "acme"})
        await db_session.flush()
    with tenant_scope("globex"):
        await audit_svc.log(
            actor_id="A1", action="create_input", details={"k": "globex"}
        )
        await db_session.flush()

    with tenant_scope("acme"):
        logs = await audit_svc.get_logs()
        assert len(logs) == 1
        assert logs[0].details["k"] == "acme"
        assert logs[0].tenant_id == "acme"
    with tenant_scope("globex"):
        logs = await audit_svc.get_logs()
        assert len(logs) == 1
        assert logs[0].details["k"] == "globex"
        assert logs[0].tenant_id == "globex"


async def test_default_tenant_compatibility(db_session):
    """未显式设租户时数据落 default，且可被 default 上下文检索到（单租户兼容）"""
    assert get_current_tenant() == "default"
    svc = EvaluationService(db_session)
    await svc.create_user({"user_id": "E1", "name": "E1", "role": "employee"})
    await svc.create_raw_input(
        {
            "input_id": "IN-def",
            "employee_id": "E1",
            "period": "2026-W01",
            "type": "daily_report",
            "content": "default 输入",
        }
    )
    await db_session.flush()

    user = await svc.get_user("E1")
    assert user is not None
    assert user.tenant_id == "default"
    rows = await svc.list_raw_inputs(employee_id="E1")
    assert len(rows) == 1
    assert rows[0].tenant_id == "default"


# ---------------- 向量库分 collection 测试 ----------------


async def test_memory_store_collection_isolation():
    """memory 向量库按 tenant 前缀隔离：acme 写入 globex 检索不到"""
    settings = Settings(vector_store_dir=get_settings().vector_store_dir)
    store_acme = ChromaMemoryStore(settings=settings, tenant_id="acme")
    store_globex = ChromaMemoryStore(settings=settings, tenant_id="globex")
    try:
        await store_acme.add_memory(
            "E1", {"period": "2026-W01", "summary": "acme 记忆"}
        )
        # globex 检索不应命中 acme 的记忆
        history = await store_globex.get_employee_history(
            "E1", period="2026-W99", limit=5
        )
        assert history == []
        # acme 自己能检索到
        history = await store_acme.get_employee_history(
            "E1", period="2026-W99", limit=5
        )
        assert len(history) == 1
        assert history[0]["summary"] == "acme 记忆"
    finally:
        await store_acme.close()
        await store_globex.close()


async def test_kb_store_collection_isolation():
    """公司知识库按 tenant 前缀隔离"""
    settings = Settings(vector_store_dir=get_settings().vector_store_dir)
    kb_acme = ChromaCompanyKB(settings=settings, tenant_id="acme")
    kb_globex = ChromaCompanyKB(settings=settings, tenant_id="globex")
    try:
        await kb_acme.add_document(
            kb_id="KB-acme", title="acme 规范", content="acme 代码规范"
        )
        # globex 查询不应命中 acme 的文档
        results = await kb_globex.query("acme", top_k=5)
        assert results == []
        # acme 自己能查到
        results = await kb_acme.query("acme", top_k=5)
        assert len(results) >= 1
        assert results[0]["kb_id"] == "KB-acme"
    finally:
        await kb_acme.close()
        await kb_globex.close()


def test_vector_store_default_collection_name():
    """默认租户 collection 名为 agentvalue_memory_default / agentvalue_kb_default（向后兼容）"""
    settings = Settings(vector_store_dir=get_settings().vector_store_dir)
    store = ChromaMemoryStore(settings=settings, tenant_id="default")
    assert store.collection.name == "agentvalue_memory_default"
    store.client.close()

    kb = ChromaCompanyKB(settings=settings, tenant_id="default")
    assert kb.collection.name == "agentvalue_kb_default"
    kb.client.close()


def test_vector_store_tenant_collection_name_prefix():
    """非默认租户 collection 名带 tenant 前缀"""
    settings = Settings(vector_store_dir=get_settings().vector_store_dir)
    store = ChromaMemoryStore(settings=settings, tenant_id="acme")
    assert store.collection.name == "agentvalue_memory_acme"
    store.client.close()

    kb = ChromaCompanyKB(settings=settings, tenant_id="globex")
    assert kb.collection.name == "agentvalue_kb_globex"
    kb.client.close()


# ---------------- API 层隔离 + RBAC 测试 ----------------


@pytest.fixture
def temp_database(monkeypatch):
    """每个 API 测试使用独立临时 SQLite 数据库"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp.name}"
    monkeypatch.setattr(get_settings(), "database_url", db_url)

    from core import database as db_module

    db_module.engine = db_module.create_async_engine(db_url, echo=False, future=True)
    db_module.AsyncSessionLocal = db_module.async_sessionmaker(
        bind=db_module.engine,
        class_=db_module.AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    yield
    try:
        Path(tmp.name).unlink(missing_ok=True)
    except Exception:
        pass


@pytest.fixture
async def initialized_db(temp_database):
    await init_db()
    yield
    await close_db()


@pytest.fixture
def client(initialized_db):
    with TestClient(app) as c:
        yield c


def _headers(role="employee", user_id="E1", tenant="default"):
    """演示模式鉴权 + 租户 header"""
    return {"x-user-role": role, "x-user-id": user_id, "x-tenant-id": tenant}


def _seed_eval(
    tenant_id, employee_id, evaluation_id, score=80.0, status=EvaluationStatus.APPROVED
):
    """通过全局 session 在指定租户下写入用户 + 评估，供 API 查询。

    在同步测试中用 asyncio.run 驱动异步落库，TestClient 请求间不存在活跃事件循环。
    """
    import asyncio

    from core.database import AsyncSessionLocal

    async def _do():
        with tenant_scope(tenant_id):
            async with AsyncSessionLocal() as session:
                svc = EvaluationService(session)
                await svc.create_user(
                    {"user_id": employee_id, "name": employee_id, "role": "employee"}
                )
                await svc.create_evaluation(
                    _eval_data(evaluation_id, employee_id, score=score, status=status)
                )
                await session.commit()

    asyncio.run(_do())


def test_api_inputs_isolated_by_tenant(client):
    """API 层：tenant A 提交的输入对 tenant B 不可见"""
    # acme 提交输入
    resp = client.post(
        "/api/v1/inputs",
        json={"employee_id": "E1", "period": "2026-W01", "content": "acme 输入"},
        headers=_headers("employee", "E1", "acme"),
    )
    assert resp.status_code == 200

    # globex 查询应看不到 acme 的输入
    resp = client.get("/api/v1/inputs", headers=_headers("employee", "E1", "globex"))
    assert resp.status_code == 200
    assert resp.json()["count"] == 0

    # acme 自己能查到
    resp = client.get("/api/v1/inputs", headers=_headers("employee", "E1", "acme"))
    assert resp.status_code == 200
    assert resp.json()["count"] == 1


def test_api_cross_tenant_data_invisible(client):
    """跨租户：同一 employee_id 在 tenant B 看不到 tenant A 的评估历史"""
    _seed_eval("acme", "E1", "EVAL-acme", score=70.0)

    # globex 查询 E1 历史应为空
    resp = client.get(
        "/api/v1/employees/E1/history", headers=_headers("hr", "HR1", "globex")
    )
    assert resp.status_code == 200
    assert resp.json()["evaluations"] == []

    # acme 查询 E1 历史应有一条
    resp = client.get(
        "/api/v1/employees/E1/history", headers=_headers("hr", "HR1", "acme")
    )
    assert resp.status_code == 200
    assert len(resp.json()["evaluations"]) == 1
    assert resp.json()["evaluations"][0]["evaluation_id"] == "EVAL-acme"


def test_api_employee_only_sees_own_dashboard(client):
    """RBAC：employee 只能看自己的评估历史，看不到同租户他人的"""
    _seed_eval("acme", "E1", "EVAL-E1", score=70.0)
    _seed_eval("acme", "E2", "EVAL-E2", score=90.0)

    # E1 查询 E2 的历史：employee 角色会被强制改写为查自己，看到自己的而非 E2 的
    resp = client.get(
        "/api/v1/employees/E2/history", headers=_headers("employee", "E1", "acme")
    )
    assert resp.status_code == 200
    # employee 被改写为查自己(E1)，应看到 E1 的评估
    evaluations = resp.json()["evaluations"]
    assert len(evaluations) == 1
    assert evaluations[0]["evaluation_id"] == "EVAL-E1"


def test_api_hr_sees_all_employees_in_tenant(client):
    """RBAC：HR 可查看同租户任意员工的评估历史"""
    _seed_eval("acme", "E1", "EVAL-E1", score=70.0)
    _seed_eval("acme", "E2", "EVAL-E2", score=90.0)

    resp = client.get(
        "/api/v1/employees/E2/history", headers=_headers("hr", "HR1", "acme")
    )
    assert resp.status_code == 200
    assert len(resp.json()["evaluations"]) == 1
    assert resp.json()["evaluations"][0]["evaluation_id"] == "EVAL-E2"


# ---------------- 租户管理 API CRUD 测试 ----------------


def test_create_tenant_admin_success(client):
    """ADMIN 可创建租户"""
    resp = client.post(
        "/api/v1/tenants",
        json={"tenant_id": "acme", "name": "ACME 公司", "plan": "pro"},
        headers=_headers("admin", "ADMIN1", "default"),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["tenant_id"] == "acme"
    assert data["name"] == "ACME 公司"
    assert data["plan"] == "pro"
    assert data["status"] == "active"


def test_create_tenant_duplicate_409(client):
    """重复 tenant_id 返回 409"""
    payload = {"tenant_id": "dup", "name": "重复租户", "plan": "free"}
    client.post(
        "/api/v1/tenants", json=payload, headers=_headers("admin", "ADMIN1", "default")
    )
    resp = client.post(
        "/api/v1/tenants", json=payload, headers=_headers("admin", "ADMIN1", "default")
    )
    assert resp.status_code == 409


def test_create_tenant_non_admin_forbidden(client):
    """HR/employee 无权创建租户"""
    resp = client.post(
        "/api/v1/tenants",
        json={"tenant_id": "x", "name": "x", "plan": "free"},
        headers=_headers("hr", "HR1", "default"),
    )
    assert resp.status_code == 403


def test_list_tenants_admin(client):
    """ADMIN 可列出全部租户"""
    for tid in ("acme", "globex"):
        client.post(
            "/api/v1/tenants",
            json={"tenant_id": tid, "name": tid, "plan": "free"},
            headers=_headers("admin", "ADMIN1", "default"),
        )
    resp = client.get("/api/v1/tenants", headers=_headers("admin", "ADMIN1", "default"))
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 2
    ids = {t["tenant_id"] for t in data["items"]}
    assert {"acme", "globex"}.issubset(ids)


def test_get_tenant_admin_any(client):
    """ADMIN 可查任意租户详情"""
    client.post(
        "/api/v1/tenants",
        json={"tenant_id": "acme", "name": "ACME", "plan": "free"},
        headers=_headers("admin", "ADMIN1", "default"),
    )
    resp = client.get(
        "/api/v1/tenants/acme", headers=_headers("admin", "ADMIN1", "default")
    )
    assert resp.status_code == 200
    assert resp.json()["tenant_id"] == "acme"


def test_get_tenant_hr_own_tenant_ok(client):
    """HR 可查自己所属租户详情"""
    client.post(
        "/api/v1/tenants",
        json={"tenant_id": "acme", "name": "ACME", "plan": "free"},
        headers=_headers("admin", "ADMIN1", "default"),
    )
    resp = client.get("/api/v1/tenants/acme", headers=_headers("hr", "HR1", "acme"))
    assert resp.status_code == 200
    assert resp.json()["tenant_id"] == "acme"


def test_get_tenant_hr_other_tenant_forbidden(client):
    """HR 不能查其他租户详情"""
    client.post(
        "/api/v1/tenants",
        json={"tenant_id": "acme", "name": "ACME", "plan": "free"},
        headers=_headers("admin", "ADMIN1", "default"),
    )
    resp = client.get("/api/v1/tenants/acme", headers=_headers("hr", "HR1", "globex"))
    assert resp.status_code == 403


def test_get_tenant_not_found(client):
    """查询不存在的租户返回 404"""
    resp = client.get(
        "/api/v1/tenants/nope", headers=_headers("admin", "ADMIN1", "default")
    )
    assert resp.status_code == 404


def test_update_tenant_status_admin(client):
    """ADMIN 可更新租户状态"""
    client.post(
        "/api/v1/tenants",
        json={"tenant_id": "acme", "name": "ACME", "plan": "free"},
        headers=_headers("admin", "ADMIN1", "default"),
    )
    resp = client.put(
        "/api/v1/tenants/acme/status",
        json={"status": "suspended"},
        headers=_headers("admin", "ADMIN1", "default"),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "suspended"


def test_update_tenant_status_non_admin_forbidden(client):
    """HR 无权更新租户状态"""
    client.post(
        "/api/v1/tenants",
        json={"tenant_id": "acme", "name": "ACME", "plan": "free"},
        headers=_headers("admin", "ADMIN1", "default"),
    )
    resp = client.put(
        "/api/v1/tenants/acme/status",
        json={"status": "suspended"},
        headers=_headers("hr", "HR1", "acme"),
    )
    assert resp.status_code == 403


def test_update_tenant_status_not_found(client):
    """更新不存在的租户状态返回 404"""
    resp = client.put(
        "/api/v1/tenants/nope/status",
        json={"status": "suspended"},
        headers=_headers("admin", "ADMIN1", "default"),
    )
    assert resp.status_code == 404
