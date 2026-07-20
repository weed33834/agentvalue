"""
后端修复回归测试

T1: JWT 黑名单业务端点测试（登出后吊销 token 不能再访问业务端点）
T2: 租户挂起访问测试（suspended 租户被 middleware 拦截 403）
T3: manager H7 越权测试（M001/E1001 与 M002/E1002 跨团队互不可见）
T4: re_evaluate DimensionScore 一致性测试（update_evaluation 刷新维度得分/证据）
T5: thread_store 清理测试（终态清理 + _put_thread 超限淘汰）
T6: watermark/verify 测试（心跳与异常事件两条路径）
"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agent.graph import create_evaluation_graph, create_evaluation_graph_with_interrupt
from agent.prompt_loader import PromptLoader
from agent.tools import AgentToolkit, DummyCompanyKB, DummyMemoryStore
from api.deps import AppState
from core.config import Settings, get_settings
from core.database import Base, close_db, init_db
from main import app
from models import DimensionScore, EvidenceRef
from models.constants import EvaluationStatus
from services.evaluation_service import EvaluationService

from .test_graph import MockModelRouter, build_sample_llm_response

# ======================= API 测试 fixtures =======================


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


@pytest.fixture
def mock_app_state(client, test_settings):
    """注入 Mock AppState，避免真实 LLM 调用，含 interrupt 图"""
    settings = Settings(model_tier="L0")
    settings.vector_store_dir = test_settings
    state = AppState(settings)

    prompt_dir = state.prompt_loader.prompts_dir
    response = build_sample_llm_response()
    state.memory_store = DummyMemoryStore()
    state.company_kb = DummyCompanyKB()
    mock_toolkit = AgentToolkit(DummyMemoryStore(), DummyCompanyKB())
    mock_router = MockModelRouter(response)
    mock_prompt_loader = PromptLoader(prompt_dir)

    state.get_graph = lambda eval_service, tenant_id=None: create_evaluation_graph(
        toolkit=mock_toolkit,
        model_router=mock_router,
        prompt_loader=mock_prompt_loader,
    )
    # P1-9: 图缓存改为按租户的 dict
    from models.models import DEFAULT_TENANT_ID

    state._interrupt_graphs = {
        DEFAULT_TENANT_ID: create_evaluation_graph_with_interrupt(
            toolkit=mock_toolkit,
            model_router=mock_router,
            prompt_loader=mock_prompt_loader,
        )
    }
    client.app.state.app_state = state
    return state


def _build_mock_app_state_for_response(client, test_settings, response: dict):
    """构造使用指定 LLM response 的 AppState，用于重评切换维度等场景"""
    settings = Settings(model_tier="L0")
    settings.vector_store_dir = test_settings
    state = AppState(settings)

    prompt_dir = state.prompt_loader.prompts_dir
    state.memory_store = DummyMemoryStore()
    state.company_kb = DummyCompanyKB()
    mock_toolkit = AgentToolkit(DummyMemoryStore(), DummyCompanyKB())
    mock_router = MockModelRouter(response)
    mock_prompt_loader = PromptLoader(prompt_dir)

    state.get_graph = lambda eval_service, tenant_id=None: create_evaluation_graph(
        toolkit=mock_toolkit,
        model_router=mock_router,
        prompt_loader=mock_prompt_loader,
    )
    client.app.state.app_state = state
    return state


# ======================= 服务层测试 fixtures =======================


@pytest.fixture
async def db_session():
    """每个服务层测试使用独立临时 SQLite 异步数据库"""
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


# ======================= 通用辅助 =======================


def _eval_data(
    evaluation_id, employee_id, period="2026-W25", score=80.0, status="ai_drafted"
):
    return {
        "evaluation_id": evaluation_id,
        "employee_id": employee_id,
        "period": period,
        "overall_score": score,
        "employee_view": {
            "summary": "本周表现稳定，完成了既定任务",
            "strengths": ["执行力强"],
            "growth_areas": [
                {
                    "dimension": "执行力",
                    "score": 85,
                    "evidence": ["按时完成日报提交"],
                    "improvement_actions": ["继续保持节奏"],
                }
            ],
            "next_week_focus": ["保持节奏"],
        },
        "manager_view": {
            "harsh_assessment": "稳定但缺乏突破，需关注成长瓶颈",
            "risk_flags": [],
            "roi_analysis": "ROI 正常",
            "reallocation_suggestion": "维持现状",
            "hidden_issues": [],
        },
        "audit": {
            "model_name": "mock-model",
            "model_tier": "L2",
            "confidence_score": 0.8,
            "raw_data_refs": ["input-1"],
            "triggered_rules": [],
            "processing_time_ms": 100,
            "prompt_version": "v0.1",
        },
        "status": status,
    }


async def _create_user(user_id, name, role, manager_id=None):
    """直接通过 DB 创建用户（含 manager_id 关系）"""
    from core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        svc = EvaluationService(session)
        await svc.create_user(
            {"user_id": user_id, "name": name, "role": role, "manager_id": manager_id}
        )
        await session.commit()


async def _create_evaluation_direct(
    evaluation_id, employee_id, period="2026-W25", status="ai_drafted"
):
    """直接通过 DB 写入评估记录，绕过 LLM 图"""
    from core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        svc = EvaluationService(session)
        await svc.create_evaluation(
            _eval_data(evaluation_id, employee_id, period=period, status=status)
        )
        await session.commit()


# ======================= T1: JWT 黑名单业务端点测试 =======================


def test_t1_jwt_blacklist_blocks_business_endpoint(client, mock_app_state):
    """登出吊销 token 后，业务端点应返回 401（token 已被吊销）"""
    # 1. 注册 employee 获取 JWT
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "user_id": "T1-U1",
            "name": "T1 测试用户",
            "email": "t1-user@agentvalue.ai",
            "password": "t1password123",
            "role": "employee",
        },
    )
    assert resp.status_code == 201, resp.text
    token = resp.json()["access_token"]
    auth_header = {"Authorization": f"Bearer {token}"}

    # 2. 有效 token 可访问业务端点
    resp = client.get("/api/v1/inputs", headers=auth_header)
    assert resp.status_code == 200

    # 3. 登出吊销 token
    resp = client.post("/api/v1/auth/logout", headers=auth_header)
    assert resp.status_code == 200
    assert resp.json()["revoked"] is True

    # 4. 吊销后的 token 访问业务端点应 401
    resp = client.get("/api/v1/inputs", headers=auth_header)
    assert resp.status_code == 401
    assert "吊销" in resp.json()["detail"]


# ======================= T2: 租户挂起访问测试 =======================


def test_t2_suspended_tenant_blocked_by_middleware(client, mock_app_state, monkeypatch):
    """suspended 租户的请求被 TenantMiddleware 拦截返回 403"""
    admin_headers = {
        "x-user-role": "admin",
        "x-user-id": "ADMIN1",
        "x-tenant-id": "default",
    }

    # 1. 创建租户 acme
    resp = client.post(
        "/api/v1/tenants",
        json={"tenant_id": "acme-t2", "name": "ACME T2", "plan": "free"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    # 2. 将 acme 置为 suspended
    resp = client.put(
        "/api/v1/tenants/acme-t2/status",
        json={"status": "suspended"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "suspended"

    # 3. 关闭 demo 模式，使 middleware 真正查库校验租户状态
    monkeypatch.setattr(get_settings(), "auth_demo_mode", False)
    from api.middleware import invalidate_tenant_cache

    invalidate_tenant_cache("acme-t2")

    # 4. suspended 租户的请求应被 403 拦截
    resp = client.get("/health", headers={"x-tenant-id": "acme-t2"})
    assert resp.status_code == 403
    assert "访问被拒绝" in resp.json()["detail"]

    # 5. active 租户仍可访问（default 租户由 init_db 创建，状态 active）
    invalidate_tenant_cache("default")
    resp = client.get("/health", headers={"x-tenant-id": "default"})
    assert resp.status_code == 200


# ======================= T3: manager H7 越权测试 =======================


@pytest.mark.asyncio
async def test_t3_manager_h7_cross_team_isolation(client, mock_app_state):
    """M001 只能访问直属下属 E1001，不能访问 M002 的下属 E1002，反之亦然"""
    await _create_user("M001", "主管一", "manager")
    await _create_user("M002", "主管二", "manager")
    await _create_user("E1001", "员工一", "employee", manager_id="M001")
    await _create_user("E1002", "员工二", "employee", manager_id="M002")
    await _create_evaluation_direct("EV-T3-E1001", "E1001")
    await _create_evaluation_direct("EV-T3-E1002", "E1002")

    # M001 可访问直属下属 E1001 的评估
    resp = client.get(
        "/api/v1/evaluations/EV-T3-E1001",
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    assert resp.status_code == 200

    # M001 不能访问 E1002（M002 的下属）的评估
    resp = client.get(
        "/api/v1/evaluations/EV-T3-E1002",
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    assert resp.status_code == 403
    assert "非直属下属" in resp.json()["detail"]

    # M002 不能访问 E1001（M001 的下属）的看板
    resp = client.get(
        "/api/v1/employees/E1001/dashboard",
        headers={"x-user-role": "manager", "x-user-id": "M002"},
    )
    assert resp.status_code == 403

    # M002 可访问直属下属 E1002 的看板
    resp = client.get(
        "/api/v1/employees/E1002/dashboard",
        headers={"x-user-role": "manager", "x-user-id": "M002"},
    )
    assert resp.status_code == 200

    # HR 不受团队限制，可访问任意员工评估
    resp = client.get(
        "/api/v1/evaluations/EV-T3-E1002",
        headers={"x-user-role": "hr", "x-user-id": "HR1"},
    )
    assert resp.status_code == 200


# ======================= T4: re_evaluate DimensionScore 一致性测试 =======================


async def test_t4_update_evaluation_refreshes_dimension_scores(db_session):
    """update_evaluation 应删除旧 DimensionScore/EvidenceRef 并按新 growth_areas 重新写入"""
    svc = EvaluationService(db_session)
    await svc.create_user({"user_id": "E1001", "name": "E1001", "role": "employee"})

    # 1. 创建评估：单一维度 "执行力"，证据 "按时完成日报提交"
    await svc.create_evaluation(_eval_data("EVAL-T4", "E1001"))
    await db_session.flush()

    old_dims = (
        (
            await db_session.execute(
                select(DimensionScore).where(DimensionScore.evaluation_id == "EVAL-T4")
            )
        )
        .scalars()
        .all()
    )
    old_refs = (
        (
            await db_session.execute(
                select(EvidenceRef).where(EvidenceRef.evaluation_id == "EVAL-T4")
            )
        )
        .scalars()
        .all()
    )
    assert len(old_dims) == 1
    assert old_dims[0].dimension == "执行力"
    assert len(old_refs) == 1
    assert old_refs[0].evidence_text == "按时完成日报提交"

    # 2. 更新评估：维度改为 "代码质量" + "协作沟通"，证据与分数均不同
    new_data = {
        "employee_view": {
            "summary": "重评后表现",
            "growth_areas": [
                {
                    "dimension": "代码质量",
                    "score": 90,
                    "evidence": ["CR 通过率 100%"],
                    "improvement_actions": ["继续保持"],
                },
                {
                    "dimension": "协作沟通",
                    "score": 78,
                    "evidence": ["主动组织技术分享", "辅导新人完成 CR"],
                    "improvement_actions": ["扩大分享覆盖面"],
                },
            ],
        },
        "overall_score": 88.0,
        "status": EvaluationStatus.AI_DRAFTED,
    }
    await svc.update_evaluation("EVAL-T4", new_data)
    await db_session.flush()

    # 3. 旧维度/证据应被删除，新维度/证据应被写入
    new_dims = (
        (
            await db_session.execute(
                select(DimensionScore).where(DimensionScore.evaluation_id == "EVAL-T4")
            )
        )
        .scalars()
        .all()
    )
    new_refs = (
        (
            await db_session.execute(
                select(EvidenceRef).where(EvidenceRef.evaluation_id == "EVAL-T4")
            )
        )
        .scalars()
        .all()
    )

    dim_names = {d.dimension for d in new_dims}
    assert dim_names == {"代码质量", "协作沟通"}
    assert "执行力" not in dim_names  # 旧维度已删除
    assert len(new_dims) == 2
    # 证据条数 = 1 + 2 = 3
    assert len(new_refs) == 3
    evidence_texts = {r.evidence_text for r in new_refs}
    assert "按时完成日报提交" not in evidence_texts  # 旧证据已删除
    assert "CR 通过率 100%" in evidence_texts


def test_t4_re_evaluate_api_refreshes_dimension_scores(
    client, mock_app_state, test_settings
):
    """API 层：re-evaluate 后 DimensionScore 与新 growth_areas 一致，旧维度不残留"""
    import asyncio

    from core.database import AsyncSessionLocal

    # 1. 创建一条评估（mock 默认返回维度 "业务影响"）
    payload = {
        "employee_id": "E1001",
        "period": "2026-W25",
        "raw_inputs": [
            {"input_id": "daily-t4", "content": "完成了登录模块重构与单测"},
        ],
    }
    resp = client.post(
        "/api/v1/evaluations", json=payload, headers={"x-user-id": "E1001"}
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    # 轮询至完成
    import time

    deadline = time.time() + 10
    eval_id = None
    while time.time() < deadline:
        r = client.get(
            f"/api/v1/evaluations/jobs/{job_id}", headers={"x-user-id": "E1001"}
        )
        job = r.json()
        if job["status"] in ("completed", "failed"):
            assert job["status"] == "completed", job
            eval_id = job["evaluation"]["evaluation_id"]
            break
        time.sleep(0.2)
    assert eval_id is not None

    async def _query_dims():
        async with AsyncSessionLocal() as s:
            rows = (
                (
                    await s.execute(
                        select(DimensionScore).where(
                            DimensionScore.evaluation_id == eval_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            return {d.dimension for d in rows}

    initial_dims = asyncio.run(_query_dims())
    assert "业务影响" in initial_dims

    # 2. 切换 AppState，使重评返回不同维度 "创新能力"
    new_response = build_sample_llm_response()
    new_response["employee_view"]["growth_areas"] = [
        {
            "dimension": "创新能力",
            "score": 92,
            "evidence": ["提出架构优化方案并被采纳"],
            "improvement_actions": ["持续探索新技术"],
        }
    ]
    _build_mock_app_state_for_response(client, test_settings, new_response)

    # 3. 调用 re-evaluate
    resp = client.post(
        f"/api/v1/evaluations/{eval_id}/re-evaluate",
        json={"actor_id": "M001", "feedback": ["请关注创新维度"]},
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    assert resp.status_code == 200, resp.text

    # 4. DimensionScore 应反映新维度，旧维度 "业务影响" 不残留
    refreshed_dims = asyncio.run(_query_dims())
    assert "创新能力" in refreshed_dims
    assert "业务影响" not in refreshed_dims


# ======================= T5: thread_store 清理测试 =======================


def test_t5_put_thread_evicts_oldest(monkeypatch):
    """_put_thread 超过 _MAX_THREADS 时按插入顺序淘汰最早条目"""
    from api import routes as routes_module

    monkeypatch.setattr(routes_module, "_MAX_THREADS", 5)
    routes_module.thread_store.clear()

    for i in range(8):
        routes_module._put_thread(f"t-{i}", {"index": i})

    assert len(routes_module.thread_store) == 5
    # 最早 3 条被淘汰（t-0 ~ t-2），保留 t-3 ~ t-7
    for evicted in ("t-0", "t-1", "t-2"):
        assert evicted not in routes_module.thread_store
    for kept in ("t-3", "t-6", "t-7"):
        assert kept in routes_module.thread_store
    routes_module.thread_store.clear()


def test_t5_thread_store_cleared_after_terminal_resume(client, mock_app_state):
    """interrupt 工作流审批通过（终态）后，thread_store 应清理该 thread"""
    from api.routes import thread_store

    payload = {
        "employee_id": "E-T5",
        "period": "2026-W25",
        "raw_inputs": [{"input_id": "daily-t5", "content": "测试 thread_store 清理"}],
    }
    resp = client.post(
        "/api/v1/evaluations-interrupt",
        json=payload,
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    assert resp.status_code == 200
    thread_id = resp.json()["thread_id"]
    assert thread_id in thread_store  # 暂停时已写入

    resp = client.post(
        f"/api/v1/evaluations-interrupt/{thread_id}/resume",
        json={"action": "approve", "comment": "同意"},
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    # 终态后 thread_store 应已清理该条目
    assert thread_id not in thread_store


# ======================= T6: watermark/verify 测试 =======================


def test_t6_watermark_heartbeat(client, mock_app_state):
    """正常心跳（visible=True 且无 visibility_event）返回 heartbeat"""
    resp = client.post(
        "/api/v1/watermark/verify",
        json={"visible": True, "density": "medium"},
        headers={"x-user-role": "employee", "x-user-id": "E1001"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["event"] == "heartbeat"


def test_t6_watermark_visibility_change(client, mock_app_state):
    """切后台等异常事件（visible=False）返回 visibility_change 并记审计"""
    resp = client.post(
        "/api/v1/watermark/verify",
        json={
            "visible": False,
            "visibility_event": "hidden",
            "density": "medium",
        },
        headers={"x-user-role": "employee", "x-user-id": "E1001"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "recorded"
    assert data["event"] == "visibility_change"

    # 审计日志应记录该异常事件（直接查 AuditLog 表，绕过租户上下文）
    import asyncio

    from core.database import AsyncSessionLocal
    from models import AuditLog

    async def _query_logs():
        async with AsyncSessionLocal() as s:
            result = await s.execute(
                select(AuditLog).where(
                    AuditLog.actor_id == "E1001",
                    AuditLog.action == "watermark_visibility_change",
                )
            )
            return result.scalars().all()

    logs = asyncio.run(_query_logs())
    assert len(logs) >= 1
