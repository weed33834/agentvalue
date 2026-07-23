"""
FastAPI API 测试
"""

import tempfile
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agent.graph import create_evaluation_graph, create_evaluation_graph_with_interrupt
from agent.prompt_loader import PromptLoader
from agent.tools import AgentToolkit, DummyCompanyKB, DummyMemoryStore
from api.deps import AppState
from auth.rbac import Role
from core.config import Settings, get_settings
from core.database import close_db, init_db
from main import app

from .test_graph import FailingModelRouter, MockModelRouter, build_sample_llm_response


@pytest.fixture(autouse=True)
def temp_database(monkeypatch):
    """每个测试使用独立临时 SQLite 数据库"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp.name}"

    monkeypatch.setattr(get_settings(), "database_url", db_url)

    # 重新创建 engine（因为 core.database 在导入时已创建原 engine）
    from core import database as db_module

    db_module.engine = db_module.create_async_engine(
        db_url,
        echo=False,
        future=True,
    )
    db_module.AsyncSessionLocal = db_module.async_sessionmaker(
        bind=db_module.engine,
        class_=db_module.AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )

    yield

    # 清理
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
    """注入 Mock 后的 AppState，避免真实 LLM 调用"""
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
    # 预创建带 interrupt 的图（使用 mock router），供 interrupt 接口测试使用
    # P1-9: 图缓存改为按租户的 dict，测试用 default 租户预创建
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


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_model_status_admin(client, mock_app_state):
    resp = client.get("/api/v1/admin/model-status", headers={"x-user-role": "admin"})
    assert resp.status_code == 200
    data = resp.json()
    assert "recommended_tier" in data


def _wait_for_job(client, job_id: str, timeout: float = 10.0, headers=None) -> dict:
    """轮询异步评估任务，直到完成或超时"""
    import time

    start = time.time()
    while time.time() - start < timeout:
        resp = client.get(f"/api/v1/evaluations/jobs/{job_id}", headers=headers or {})
        assert resp.status_code == 200
        job = resp.json()
        if job["status"] in ("completed", "failed"):
            return job
        time.sleep(0.2)
    raise TimeoutError(f"任务 {job_id} 未在 {timeout}s 内完成")


def _fetch_evaluation(
    client, evaluation_id: str, role: str = "manager", user_id: Optional[str] = None
) -> dict:
    """通过 GET API 查询评估当前数据库状态"""
    headers: dict = {}
    if role:
        headers["x-user-role"] = role
    if user_id:
        headers["x-user-id"] = user_id
    resp = client.get(f"/api/v1/evaluations/{evaluation_id}", headers=headers)
    assert resp.status_code == 200
    return resp.json()


def _approve_evaluation(client, evaluation_id: str, actor_id: str = "M001") -> dict:
    """审批通过辅助函数"""
    resp = client.post(
        f"/api/v1/evaluations/{evaluation_id}/approve",
        json={"current_status": "ai_drafted", "actor_id": actor_id, "comment": "同意"},
        headers={"x-user-role": "manager", "x-user-id": actor_id},
    )
    assert resp.status_code == 200
    return resp.json()


def _reject_evaluation(client, evaluation_id: str, actor_id: str = "M001") -> dict:
    """驳回评估辅助函数"""
    resp = client.post(
        f"/api/v1/evaluations/{evaluation_id}/reject",
        json={
            "current_status": "ai_drafted",
            "actor_id": actor_id,
            "comment": "证据不足",
        },
        headers={"x-user-role": "manager", "x-user-id": actor_id},
    )
    assert resp.status_code == 200
    return resp.json()


def _appeal_evaluation(client, evaluation_id: str, actor_id: str = "E1001") -> dict:
    """申诉评估辅助函数"""
    resp = client.post(
        f"/api/v1/evaluations/{evaluation_id}/appeal",
        json={
            "current_status": "approved",
            "actor_id": actor_id,
            "comment": "对评分有异议",
        },
        headers={"x-user-id": actor_id},
    )
    assert resp.status_code == 200
    return resp.json()


def _build_mock_app_state_for_response(client, test_settings, response: dict):
    """构造使用指定 LLM response 的 AppState，用于高风险路由等场景"""
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


def test_create_evaluation(client, mock_app_state):
    payload = {
        "employee_id": "E1001",
        "period": "2026-W25",
        "raw_inputs": [
            {"input_id": "daily-001", "content": "完成了登录模块重构"},
        ],
    }
    resp = client.post(
        "/api/v1/evaluations", json=payload, headers={"x-user-id": "E1001"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending"
    assert "job_id" in data

    job = _wait_for_job(client, data["job_id"], headers={"x-user-id": "E1001"})
    assert job["status"] == "completed"
    assert job["evaluation"]["employee_id"] == "E1001"


@pytest.fixture
def created_evaluation_id(client, mock_app_state):
    """通过 API 创建一条评估并返回 evaluation_id"""
    payload = {
        "employee_id": "E1001",
        "period": "2026-W25",
        "raw_inputs": [
            {"input_id": "daily-001", "content": "完成了登录模块重构"},
        ],
    }
    resp = client.post(
        "/api/v1/evaluations", json=payload, headers={"x-user-id": "E1001"}
    )
    assert resp.status_code == 200
    job = _wait_for_job(client, resp.json()["job_id"], headers={"x-user-id": "E1001"})
    assert job["status"] == "completed"
    return job["evaluation"]["evaluation_id"]


def test_approve_evaluation(client, mock_app_state, created_evaluation_id):
    resp = client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/approve",
        json={
            "current_status": "manager_review",
            "actor_id": "M001",
            "comment": "同意",
        },
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "approved"


def test_reject_illegal_transition(client, mock_app_state, created_evaluation_id):
    # 先审批通过
    client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/approve",
        json={"current_status": "manager_review", "actor_id": "M001"},
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    # 再次 approve 已 approved 的状态应返回 400
    resp = client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/approve",
        json={"current_status": "approved", "actor_id": "M001"},
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    assert resp.status_code == 400


def test_get_evaluation_audit_logs(client, mock_app_state, created_evaluation_id):
    client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/approve",
        json={"current_status": "ai_drafted", "actor_id": "M001"},
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    resp = client.get(
        f"/api/v1/evaluations/{created_evaluation_id}/audit-logs",
        headers={"x-user-role": "manager"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["logs"]) >= 1


def test_get_evaluation_detail(client, mock_app_state, created_evaluation_id):
    resp = client.get(
        f"/api/v1/evaluations/{created_evaluation_id}",
        headers={"x-user-role": "manager"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["evaluation_id"] == created_evaluation_id
    assert "employee_view" in data
    assert "manager_view" in data


def test_get_evaluation_employee_view(client, mock_app_state, created_evaluation_id):
    resp = client.get(
        f"/api/v1/evaluations/{created_evaluation_id}/employee-view",
        headers={"x-user-id": "E1001"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["evaluation_id"] == created_evaluation_id
    assert "summary" in data["employee_view"]
    assert "growth_areas" in data["employee_view"]


def test_get_evaluation_manager_view(client, mock_app_state, created_evaluation_id):
    resp = client.get(
        f"/api/v1/evaluations/{created_evaluation_id}/manager-view",
        headers={"x-user-role": "manager"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["evaluation_id"] == created_evaluation_id
    assert "harsh_assessment" in data["manager_view"]


def test_create_evaluation_feedback(client, mock_app_state, created_evaluation_id):
    resp = client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/feedback",
        json={
            "content": "我认为评估中关于协作的部分可以更具体",
            "type": "feedback",
            "actor_id": "E1001",
        },
        headers={"x-user-id": "E1001"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["evaluation_id"] == created_evaluation_id
    assert data["content"]


def test_list_evaluation_feedback(client, mock_app_state, created_evaluation_id):
    """查询某评估下的反馈记录，返回内容与关联评估当前状态"""
    client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/feedback",
        json={"content": "协作维度证据偏少", "type": "feedback"},
        headers={"x-user-id": "E1001"},
    )
    resp = client.get(
        f"/api/v1/evaluations/{created_evaluation_id}/feedback",
        headers={"x-user-role": "manager"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1
    item = data["feedback"][0]
    assert item["content"] == "协作维度证据偏少"
    # 关联评估当前状态字段存在，供前端追踪处理进度
    assert "status" in item["evaluation"]
    assert item["evaluation"]["period"] == "2026-W25"


def test_list_employee_feedback_tracks_appeal_status(
    client, mock_app_state, created_evaluation_id
):
    """员工视角：提交申诉后，记录面板可查到申诉及评估回到 manager_review 的状态"""
    # 先审批通过
    client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/approve",
        json={"current_status": "manager_review", "actor_id": "M001"},
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    # 员工申诉
    client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/appeal",
        json={"comment": "对评分有异议"},
        headers={"x-user-id": "E1001"},
    )
    # 员工查询自己的反馈/申诉记录
    resp = client.get(
        "/api/v1/employees/E1001/feedback",
        headers={"x-user-id": "E1001"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["employee_id"] == "E1001"
    assert data["count"] >= 1
    appeal = next(f for f in data["feedback"] if f["type"] == "appeal")
    assert appeal["evaluation"]["status"] == "manager_review"


def test_get_pending_approvals(client, mock_app_state, created_evaluation_id):
    resp = client.get(
        "/api/v1/manager/pending-approvals",
        headers={"x-user-role": "hr"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert any(e["evaluation_id"] == created_evaluation_id for e in data["pending"])


def test_request_hr_review(client, mock_app_state, created_evaluation_id):
    resp = client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/request-hr-review",
        json={
            "current_status": "manager_review",
            "actor_id": "M001",
            "comment": "分数异常需复核",
        },
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "hr_audit"


def test_get_hr_audit_queue(client, mock_app_state, created_evaluation_id):
    # 先把评估送进 HR 复核
    client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/request-hr-review",
        json={"current_status": "manager_review", "actor_id": "M001"},
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    resp = client.get("/api/v1/hr/audit-queue", headers={"x-user-role": "hr"})
    assert resp.status_code == 200
    data = resp.json()
    assert any(e["evaluation_id"] == created_evaluation_id for e in data["pending"])


def test_appeal_evaluation(client, mock_app_state, created_evaluation_id):
    # 先审批通过再申诉
    client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/approve",
        json={"current_status": "manager_review", "actor_id": "M001"},
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    resp = client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/appeal",
        json={
            "current_status": "approved",
            "actor_id": "E1001",
            "comment": "对评分有异议",
        },
        headers={"x-user-id": "E1001"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "manager_review"


def test_re_evaluate(client, mock_app_state, created_evaluation_id):
    resp = client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/re-evaluate",
        json={"actor_id": "M001", "feedback": ["请重点关注代码质量"]},
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["evaluation_id"] == created_evaluation_id
    # 重新评估后状态重置为 ai_drafted（高风险时自动路由到 hr_audit）
    assert data["status"] in ("ai_drafted", "hr_audit")


# ---------------- 审批流与高风险路由测试 ----------------


def test_approve_ai_drafted_evaluation(client, mock_app_state, created_evaluation_id):
    """1. 从 ai_drafted 审批通过 -> approved，并验证数据库状态"""
    eval_before = _fetch_evaluation(client, created_evaluation_id)
    assert eval_before["status"] == "ai_drafted"

    resp = client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/approve",
        json={"current_status": "ai_drafted", "actor_id": "M001", "comment": "同意"},
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"

    eval_after = _fetch_evaluation(client, created_evaluation_id)
    assert eval_after["status"] == "approved"
    assert eval_after["approver_id"] == "M001"
    assert eval_after["approved_at"] is not None


def test_approve_manager_review_evaluation(
    client, mock_app_state, created_evaluation_id
):
    """2. 从 manager_review 审批通过 -> approved，并验证数据库状态"""
    _approve_evaluation(client, created_evaluation_id)
    appeal_resp = _appeal_evaluation(client, created_evaluation_id)
    assert appeal_resp["status"] == "manager_review"

    eval_before = _fetch_evaluation(client, created_evaluation_id)
    assert eval_before["status"] == "manager_review"
    assert eval_before["approved_at"] is None
    assert eval_before["approver_id"] is None

    resp = client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/approve",
        json={
            "current_status": "manager_review",
            "actor_id": "M001",
            "comment": "复核通过",
        },
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"

    eval_after = _fetch_evaluation(client, created_evaluation_id)
    assert eval_after["status"] == "approved"
    assert eval_after["approver_id"] == "M001"
    assert eval_after["approved_at"] is not None


def test_reject_evaluation(client, mock_app_state, created_evaluation_id):
    """3. 驳回评估 -> rejected，并验证数据库状态"""
    eval_before = _fetch_evaluation(client, created_evaluation_id)
    assert eval_before["status"] == "ai_drafted"

    resp = client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/reject",
        json={
            "current_status": "ai_drafted",
            "actor_id": "M001",
            "comment": "证据不足",
        },
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"

    eval_after = _fetch_evaluation(client, created_evaluation_id)
    assert eval_after["status"] == "rejected"
    assert eval_after["approved_at"] is None
    assert eval_after["approver_id"] is None


def test_request_hr_review_from_manager_review(
    client, mock_app_state, created_evaluation_id
):
    """4. 从 manager_review 申请 HR 复核 -> hr_audit，并验证数据库状态"""
    _approve_evaluation(client, created_evaluation_id)
    appeal_resp = _appeal_evaluation(client, created_evaluation_id)
    assert appeal_resp["status"] == "manager_review"

    eval_before = _fetch_evaluation(client, created_evaluation_id)
    assert eval_before["status"] == "manager_review"

    resp = client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/request-hr-review",
        json={
            "current_status": "manager_review",
            "actor_id": "M001",
            "comment": "需HR复核",
        },
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "hr_audit"

    eval_after = _fetch_evaluation(client, created_evaluation_id)
    assert eval_after["status"] == "hr_audit"


def test_appeal_rejected_evaluation(client, mock_app_state, created_evaluation_id):
    """5. 对 rejected 评估申诉 -> manager_review，并验证数据库状态"""
    _reject_evaluation(client, created_evaluation_id)

    eval_before = _fetch_evaluation(client, created_evaluation_id)
    assert eval_before["status"] == "rejected"

    resp = client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/appeal",
        json={
            "current_status": "rejected",
            "actor_id": "E1001",
            "comment": "对评分有异议",
        },
        headers={"x-user-id": "E1001"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "manager_review"

    eval_after = _fetch_evaluation(client, created_evaluation_id)
    assert eval_after["status"] == "manager_review"
    assert eval_after["approved_at"] is None
    assert eval_after["approver_id"] is None


def test_re_evaluate_rejected_evaluation(client, mock_app_state, created_evaluation_id):
    """6. 对 rejected 评估带反馈重新评估 -> ai_drafted 或 hr_audit，并验证数据库状态"""
    _reject_evaluation(client, created_evaluation_id)

    eval_before = _fetch_evaluation(client, created_evaluation_id)
    assert eval_before["status"] == "rejected"

    resp = client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/re-evaluate",
        json={"actor_id": "M001", "feedback": ["请重点关注代码质量"]},
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["evaluation_id"] == created_evaluation_id
    assert data["status"] in ("ai_drafted", "hr_audit")

    eval_after = _fetch_evaluation(client, created_evaluation_id)
    assert eval_after["status"] in ("ai_drafted", "hr_audit")
    assert eval_after["status"] != "rejected"


def test_high_risk_evaluation_auto_routing(client, test_settings):
    """7. 高风险评估（低分或关键风险标记）自动路由到 hr_audit，并验证数据库状态"""
    response = build_sample_llm_response()
    response["overall_score"] = 55.0
    response["manager_view"]["risk_flags"] = [
        {
            "level": "critical",
            "category": "产出",
            "description": "关键产出未达标",
            "suggested_action": "主管复核",
        }
    ]
    _build_mock_app_state_for_response(client, test_settings, response)

    payload = {
        "employee_id": "E1001",
        "period": "2026-W26",
        "raw_inputs": [{"input_id": "daily-hr-001", "content": "本周产出严重不足"}],
    }
    resp = client.post(
        "/api/v1/evaluations", json=payload, headers={"x-user-id": "E1001"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending"

    job = _wait_for_job(client, data["job_id"], headers={"x-user-id": "E1001"})
    assert job["status"] == "completed"
    evaluation_id = job["evaluation"]["evaluation_id"]

    # 验证异步任务结果与数据库状态均为 hr_audit
    assert job["evaluation"]["status"] == "hr_audit"
    eval_data = _fetch_evaluation(client, evaluation_id, role="hr")
    assert eval_data["status"] == "hr_audit"


def test_get_employee_dashboard(client, mock_app_state, created_evaluation_id):
    resp = client.get(
        "/api/v1/employees/E1001/dashboard",
        headers={"x-user-id": "E1001"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["employee_id"] == "E1001"


def test_create_input(client, mock_app_state):
    payload = {
        "employee_id": "E1002",
        "period": "2026-W25",
        "type": "daily_report",
        "content": "本周完成了用户管理模块开发",
    }
    resp = client.post("/api/v1/inputs", json=payload, headers={"x-user-id": "E1002"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["employee_id"] == "E1002"
    assert data["input_id"].startswith("input-")


# ---------------- JWT 认证测试 ----------------


def test_auth_register_and_login(client, mock_app_state):
    """注册 → 登录 → /me 全流程"""
    register_payload = {
        "user_id": "E2001",
        "name": "测试员工",
        "email": "test-register@agentvalue.ai",
        "password": "test123456",
        "role": "employee",
        "department": "测试部",
    }
    resp = client.post("/api/v1/auth/register", json=register_payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["access_token"]
    assert data["role"] == "employee"
    assert data["user_id"] == "E2001"

    # 登录
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "test-register@agentvalue.ai", "password": "test123456"},
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    assert token

    # /me
    resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    me = resp.json()
    assert me["user_id"] == "E2001"
    assert me["email"] == "test-register@agentvalue.ai"


def test_auth_login_wrong_password(client, mock_app_state):
    """错误密码应返回 401"""
    client.post(
        "/api/v1/auth/register",
        json={
            "user_id": "E2002",
            "name": "测试员工2",
            "email": "wrong-pwd@agentvalue.ai",
            "password": "correct123",
            "role": "employee",
        },
    )
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "wrong-pwd@agentvalue.ai", "password": "wrong123"},
    )
    assert resp.status_code == 401


def test_auth_register_duplicate_email(client, mock_app_state):
    """重复邮箱应返回 409"""
    payload = {
        "user_id": "E2003",
        "name": "测试员工3",
        "email": "dup@agentvalue.ai",
        "password": "test123456",
        "role": "employee",
    }
    resp = client.post("/api/v1/auth/register", json=payload)
    assert resp.status_code == 201
    payload["user_id"] = "E2004"
    resp = client.post("/api/v1/auth/register", json=payload)
    assert resp.status_code == 409


def test_auth_jwt_blocks_invalid_token(client, mock_app_state):
    """无效 token 应返回 401"""
    resp = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer invalid.token.here"},
    )
    assert resp.status_code == 401


def test_auth_jwt_role_enforced(client, mock_app_state):
    """JWT token 中的角色应被强制校验"""
    # 注册 employee
    client.post(
        "/api/v1/auth/register",
        json={
            "user_id": "E2005",
            "name": "普通员工",
            "email": "role-test@agentvalue.ai",
            "password": "test123456",
            "role": "employee",
        },
    )
    token = client.post(
        "/api/v1/auth/login",
        json={"email": "role-test@agentvalue.ai", "password": "test123456"},
    ).json()["access_token"]

    # employee 不应能访问 admin 接口
    resp = client.get(
        "/api/v1/admin/model-status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_auth_seed_demo_users(client, mock_app_state):
    """初始化演示账号"""
    resp = client.post("/api/v1/auth/seed-demo-users")
    assert resp.status_code == 200
    data = resp.json()
    assert "created" in data
    # 用演示账号登录
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "employee@agentvalue.ai", "password": "agentvalue123"},
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "employee"


def _register_and_login(
    client, user_id="E3001", email="logout-test@agentvalue.ai"
) -> str:
    """注册并登录,返回 access_token"""
    client.post(
        "/api/v1/auth/register",
        json={
            "user_id": user_id,
            "name": "登出测试",
            "email": email,
            "password": "test123456",
            "role": "employee",
        },
    )
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "test123456"},
    )
    assert resp.status_code == 200
    return resp.json()["access_token"]


def test_auth_jwt_contains_jti_claim(client, mock_app_state):
    """新签发的 JWT 应包含 jti claim,用于主动吊销"""
    import jwt as pyjwt

    from core.config import get_settings

    token = _register_and_login(client)
    payload = pyjwt.decode(
        token,
        get_settings().jwt_secret_key,
        algorithms=[get_settings().jwt_algorithm],
    )
    assert "jti" in payload
    assert payload["jti"]


def test_auth_logout_revokes_token(client, mock_app_state):
    """登出后旧 token 应被吊销,后续请求返回 401"""
    token = _register_and_login(client)

    # 登出前 /me 正常
    resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200

    # 登出
    resp = client.post(
        "/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["revoked"] is True

    # 登出后 /me 应 401(token 已被吊销)
    resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert "吊销" in resp.json()["detail"]


def test_auth_logout_idempotent(client, mock_app_state):
    """重复登出同一 token 应返回 200(幂等)"""
    token = _register_and_login(
        client, user_id="E3002", email="idempotent@agentvalue.ai"
    )

    resp1 = client.post(
        "/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp1.status_code == 200
    assert resp1.json()["revoked"] is True

    # 第二次登出:token 仍可 decode(签名有效、未过期),jti 重复写入黑名单
    resp2 = client.post(
        "/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp2.status_code == 200
    assert resp2.json()["revoked"] is True


def test_auth_logout_without_token_returns_401(client, mock_app_state):
    """未携带 token 调用登出应 401"""
    resp = client.post("/api/v1/auth/logout")
    assert resp.status_code == 401


def test_auth_logout_with_invalid_token_returns_401(client, mock_app_state):
    """无效 token 调用登出应 401"""
    resp = client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": "Bearer invalid.token.here"},
    )
    assert resp.status_code == 401


def test_auth_refresh_after_logout_blocked(client, mock_app_state):
    """登出吊销旧 token 后,旧 token 不能用于 refresh,必须重新登录"""
    token = _register_and_login(client, user_id="E3003", email="refresh@agentvalue.ai")

    # 登出旧 token
    client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"})
    # 旧 token 已吊销,/me 返回 401
    resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401

    # 用已吊销的 token 调 refresh 应 401(不能绕过黑名单续期)
    resp = client.post(
        "/api/v1/auth/refresh", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401
    assert "吊销" in resp.json()["detail"]

    # 重新登录获取新 token,新 token 可正常使用(新 jti 不在黑名单)
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "refresh@agentvalue.ai", "password": "test123456"},
    )
    assert resp.status_code == 200
    new_token = resp.json()["access_token"]
    resp = client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {new_token}"}
    )
    assert resp.status_code == 200


# ---------------- LangGraph 原生 interrupt 测试 ----------------


def test_interrupt_flow_approve(client, mock_app_state):
    """interrupt 工作流：启动 → 暂停 → 恢复审批通过"""
    payload = {
        "employee_id": "E3001",
        "period": "2026-W26",
        "raw_inputs": [
            {"input_id": "daily-int-001", "content": "完成了 interrupt 审批流开发"},
        ],
    }
    # 1. 启动，应触发 interrupt
    resp = client.post(
        "/api/v1/evaluations-interrupt",
        json=payload,
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "awaiting_review"
    thread_id = data["thread_id"]
    assert thread_id.startswith("thread-")
    assert data["interrupt"]["node"] == "manager_review"

    # 2. 查询状态
    resp = client.get(
        f"/api/v1/evaluations-interrupt/{thread_id}/state",
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    assert resp.status_code == 200
    state = resp.json()
    assert state["thread_id"] == thread_id

    # 3. 恢复：审批通过
    resp = client.post(
        f"/api/v1/evaluations-interrupt/{thread_id}/resume",
        json={"action": "approve", "comment": "同意"},
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    assert resp.status_code == 200
    result = resp.json()
    assert result["status"] == "approved"
    assert result["evaluation"]["status"] == "approved"
    assert result["evaluation"]["approver_id"] == "M001"


def test_interrupt_flow_reject(client, mock_app_state):
    """interrupt 工作流：驳回"""
    payload = {
        "employee_id": "E3002",
        "period": "2026-W26",
        "raw_inputs": [
            {"input_id": "daily-int-002", "content": "测试驳回流程"},
        ],
    }
    resp = client.post(
        "/api/v1/evaluations-interrupt",
        json=payload,
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    thread_id = resp.json()["thread_id"]

    resp = client.post(
        f"/api/v1/evaluations-interrupt/{thread_id}/resume",
        json={"action": "reject", "comment": "证据不足"},
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


def test_interrupt_resume_unknown_thread(client, mock_app_state):
    """恢复不存在的线程应 404"""
    resp = client.post(
        "/api/v1/evaluations-interrupt/nonexistent-thread/resume",
        json={"action": "approve"},
        headers={"x-user-role": "manager"},
    )
    assert resp.status_code == 404


def test_interrupt_resume_invalid_action(client, mock_app_state):
    """恢复时 action 非法应 400"""
    payload = {
        "employee_id": "E3003",
        "period": "2026-W26",
        "raw_inputs": [{"input_id": "d1", "content": "测试非法 action"}],
    }
    resp = client.post("/api/v1/evaluations-interrupt", json=payload)
    thread_id = resp.json()["thread_id"]

    resp = client.post(
        f"/api/v1/evaluations-interrupt/{thread_id}/resume",
        json={"action": "invalid_action"},
        headers={"x-user-role": "manager"},
    )
    assert resp.status_code == 400


# ---------------- 权限与边界用例 ----------------


def test_employee_cannot_access_other_evaluation(
    client, mock_app_state, created_evaluation_id
):
    """employee 角色访问他人评估时返回 403"""
    # created_evaluation_id 已为 E1001 创建评估，用 E9999 访问应被拒绝
    resp = client.get(
        f"/api/v1/evaluations/{created_evaluation_id}",
        headers={"x-user-id": "E9999"},
    )
    assert resp.status_code == 403


def test_employee_cannot_create_input_for_others(client, mock_app_state):
    """employee 角色为他人提交输入时，employee_id 被强制为自己的 ID"""
    payload = {
        "employee_id": "E1004",
        "period": "2026-W25",
        "type": "daily_report",
        "content": "尝试为他人提交输入",
    }
    resp = client.post("/api/v1/inputs", json=payload, headers={"x-user-id": "E1003"})
    assert resp.status_code == 200
    data = resp.json()
    # employee_id 被强制覆盖为当前用户 E1003
    assert data["employee_id"] == "E1003"


def test_get_input_not_found(client, mock_app_state):
    """查询不存在的 input_id 返回 404"""
    resp = client.get(
        "/api/v1/inputs/nonexistent-id",
        headers={"x-user-role": "manager"},
    )
    assert resp.status_code == 404


def test_list_inputs(client, mock_app_state):
    """测试 GET /api/v1/inputs 列表接口"""
    # 先创建一个 input
    payload = {
        "employee_id": "E1005",
        "period": "2026-W25",
        "type": "daily_report",
        "content": "列表接口测试输入",
    }
    resp = client.post("/api/v1/inputs", json=payload, headers={"x-user-id": "E1005"})
    assert resp.status_code == 200

    # 查询列表（manager 可见全部）
    resp = client.get(
        "/api/v1/inputs",
        headers={"x-user-role": "manager"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1


def test_get_evaluation_job_not_found(client, mock_app_state):
    """查询不存在的 job_id 返回 404"""
    resp = client.get("/api/v1/evaluations/jobs/nonexistent-job")
    assert resp.status_code == 404


def test_employee_cannot_access_job_of_others(client, mock_app_state):
    """employee 查看他人任务返回 403"""
    # 用 E1001 创建评估任务
    payload = {
        "employee_id": "E1001",
        "period": "2026-W25",
        "raw_inputs": [
            {"input_id": "daily-job-001", "content": "测试 job 权限隔离"},
        ],
    }
    resp = client.post(
        "/api/v1/evaluations", json=payload, headers={"x-user-id": "E1001"}
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    # 用 E9999 的 header 查询该 job_id，应返回 403
    resp = client.get(
        f"/api/v1/evaluations/jobs/{job_id}",
        headers={"x-user-id": "E9999"},
    )
    assert resp.status_code == 403


def test_re_evaluate_persists_to_db(client, mock_app_state, created_evaluation_id):
    """re-evaluate 后数据库状态确实更新"""
    # 先审批通过
    client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/approve",
        json={"current_status": "manager_review", "actor_id": "M001"},
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )

    # 调用 re-evaluate
    resp = client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/re-evaluate",
        json={"actor_id": "M001", "feedback": ["请重点关注代码质量"]},
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    assert resp.status_code == 200

    # 用 GET /evaluations/{id} 查询，断言状态已更新（不再是 approved）
    resp = client.get(
        f"/api/v1/evaluations/{created_evaluation_id}",
        headers={"x-user-role": "manager"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("ai_drafted", "manager_review", "hr_audit")
    assert data["status"] != "approved"


def test_re_evaluate_merges_historical_and_caller_feedback(
    client, mock_app_state, created_evaluation_id
):
    """re-evaluate 应从 DB 拉取历史反馈/申诉记录，并与调用方本次传入的反馈合并注入评估图"""
    # 在 DB 中预置 2 条历史反馈记录
    client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/feedback",
        json={"content": "协作维度证据偏少", "type": "feedback"},
        headers={"x-user-id": "E1001"},
    )
    client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/feedback",
        json={"content": "评分与实际产出不匹配", "type": "appeal"},
        headers={"x-user-id": "E1001"},
    )

    # 调用 re-evaluate，本次再传入 1 条 caller feedback（字符串形态）
    resp = client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/re-evaluate",
        json={"actor_id": "M001", "feedback": ["请重点关注代码质量"]},
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    assert resp.status_code == 200
    data = resp.json()
    # 历史反馈 2 条 + 调用方 1 条 = 合并后 3 条
    assert data["historical_feedback_count"] == 2
    assert data["caller_feedback_count"] == 1
    assert data["feedback_processed"] == 3
    assert data["evaluation_id"] == created_evaluation_id


def test_re_evaluate_accepts_dict_feedback(
    client, mock_app_state, created_evaluation_id
):
    """re-evaluate 调用方 feedback 兼容 dict 形态，type/content 字段被保留"""
    resp = client.post(
        f"/api/v1/evaluations/{created_evaluation_id}/re-evaluate",
        json={
            "actor_id": "M001",
            "feedback": [
                {"type": "appeal", "content": "对评分有异议：证据不足"},
            ],
        },
        headers={"x-user-role": "manager", "x-user-id": "M001"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["caller_feedback_count"] == 1
    assert data["historical_feedback_count"] == 0
    assert data["feedback_processed"] == 1


def test_seed_demo_users_disabled_in_production(client, mock_app_state, monkeypatch):
    """非演示模式下 seed-demo-users 返回 403"""
    monkeypatch.setattr(get_settings(), "auth_demo_mode", False)
    resp = client.post("/api/v1/auth/seed-demo-users")
    assert resp.status_code == 403


def _metric_value(client, name: str, label_match: str) -> float:
    """从 /metrics 提取指定指标样本值(按 label_match 子串过滤),找不到返回 0.0"""
    resp = client.get("/metrics")
    assert resp.status_code == 200
    for line in resp.text.splitlines():
        if line.startswith(name + "{") and label_match in line:
            return float(line.split()[-1])
    return 0.0


def test_evaluation_failure_metric_on_graph_error(client, test_settings):
    """评估图执行失败时,agentvalue_evaluation_failures_total{reason="graph_error"} 递增

    FailingModelRouter 的 Provider 抛 RuntimeError,call_llm 节点捕获后返回
    {"error": ...},routes 层走 graph_error 失败路径并埋点。
    """
    settings = Settings(model_tier="L0")
    settings.vector_store_dir = test_settings
    state = AppState(settings)
    prompt_dir = state.prompt_loader.prompts_dir
    state.memory_store = DummyMemoryStore()
    state.company_kb = DummyCompanyKB()
    mock_toolkit = AgentToolkit(DummyMemoryStore(), DummyCompanyKB())
    failing_router = FailingModelRouter()
    mock_prompt_loader = PromptLoader(prompt_dir)
    state.get_graph = lambda eval_service, tenant_id=None: create_evaluation_graph(
        toolkit=mock_toolkit,
        model_router=failing_router,
        prompt_loader=mock_prompt_loader,
    )
    client.app.state.app_state = state

    before = _metric_value(
        client, "agentvalue_evaluation_failures_total", 'reason="graph_error"'
    )

    payload = {
        "employee_id": "E1001",
        "period": "2026-W25",
        "raw_inputs": [{"input_id": "daily-001", "content": "完成了登录模块重构"}],
    }
    resp = client.post(
        "/api/v1/evaluations", json=payload, headers={"x-user-id": "E1001"}
    )
    assert resp.status_code == 200
    job = _wait_for_job(client, resp.json()["job_id"], headers={"x-user-id": "E1001"})
    assert job["status"] == "failed"

    after = _metric_value(
        client, "agentvalue_evaluation_failures_total", 'reason="graph_error"'
    )
    assert after > before
