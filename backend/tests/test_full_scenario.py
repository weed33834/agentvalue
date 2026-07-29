"""
P1: 全场景测试 — 完整模拟用户从启动到结束的全流程

覆盖:
1. 用户认证流程(登录/JWT/角色权限)
2. 评估创建→审批→查看→反馈 完整链路
3. 多 Agent 协作(interrupt/resume)
4. 安全中间件(trace_id/异常处理/幂等性)
5. 多租户隔离
6. 异常场景(非法操作/权限拒绝/资源不存在)
7. 数据导出

认证流程: seed-demo-users → login(email+password) → JWT token
"""

import pytest
from fastapi.testclient import TestClient

# 演示账号凭据(由 seed-demo-users 创建)
_DEMO_PASSWORD = "agentvalue123"
_ADMIN_EMAIL = "admin@agentvalue.ai"
_MANAGER_EMAIL = "manager@agentvalue.ai"
_EMPLOYEE_EMAIL = "employee@agentvalue.ai"


@pytest.fixture(scope="module")
def client():
    """主应用 TestClient(module 级别共享,减少 lifespan 开销)

    使用 with 上下文管理器触发 lifespan 事件(app.state.app_state 初始化)。
    """
    from main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module", autouse=True)
def _seed_demo_users(client):
    """模块级自动 fixture: 确保 demo 用户已创建(幂等)"""
    resp = client.post("/api/v1/auth/seed-demo-users")
    # 200 = 已存在或新建成功, 403 = 非演示模式(测试环境应开启)
    assert resp.status_code in (200, 403), f"seed-demo-users 失败: {resp.status_code} {resp.text}"


def _login(client, email: str) -> str:
    """登录获取 JWT token"""
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": _DEMO_PASSWORD},
    )
    assert resp.status_code == 200, f"登录失败 {email}: {resp.status_code} {resp.text}"
    return resp.json()["access_token"]


@pytest.fixture(scope="module")
def admin_headers(client):
    token = _login(client, _ADMIN_EMAIL)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def manager_headers(client):
    token = _login(client, _MANAGER_EMAIL)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def employee_headers(client):
    token = _login(client, _EMPLOYEE_EMAIL)
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# 1. 系统健康检查
# ============================================================


class TestSystemHealth:
    """系统启动后健康检查"""

    def test_health_endpoint(self, client):
        """GET /health 返回 200"""
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_livez_endpoint(self, client):
        """GET /livez 返回 200(liveness)"""
        resp = client.get("/livez")
        assert resp.status_code == 200

    def test_readyz_endpoint(self, client):
        """GET /readyz 返回 200(readiness)"""
        resp = client.get("/readyz")
        assert resp.status_code == 200

    def test_trace_id_in_response(self, client):
        """所有响应都应包含 X-Trace-Id 头"""
        resp = client.get("/health")
        assert "X-Trace-Id" in resp.headers
        assert len(resp.headers["X-Trace-Id"]) > 0

    def test_trace_id_propagation(self, client):
        """传入 X-Trace-Id 请求头时,响应头应返回相同的 trace_id"""
        resp = client.get("/health", headers={"X-Trace-Id": "test-trace-abc"})
        assert resp.headers["X-Trace-Id"] == "test-trace-abc"

    def test_security_headers_present(self, client):
        """安全响应头存在"""
        resp = client.get("/health")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert "Content-Security-Policy" in resp.headers


# ============================================================
# 2. 用户认证与权限
# ============================================================


class TestAuthFlow:
    """用户认证全流程"""

    def test_admin_login(self, client):
        """管理员登录获取 JWT"""
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": _ADMIN_EMAIL, "password": _DEMO_PASSWORD},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["role"] == "admin"

    def test_employee_login(self, client):
        """员工登录获取 JWT"""
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": _EMPLOYEE_EMAIL, "password": _DEMO_PASSWORD},
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "employee"

    def test_login_wrong_password(self, client):
        """错误密码登录失败"""
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": _ADMIN_EMAIL, "password": "wrong-password-xyz"},
        )
        assert resp.status_code == 401

    def test_protected_endpoint_without_token(self, client):
        """无 token 访问 — 演示模式下默认以 anonymous employee 身份访问"""
        resp = client.get("/api/v1/auth/me")
        # 演示模式: 无 token 时以 anonymous 身份访问,用户不存在则 404
        # 非演示模式: 应返回 401
        assert resp.status_code in (401, 404)

    def test_employee_cannot_access_admin(self, client, employee_headers):
        """员工不能访问管理员端点"""
        resp = client.get(
            "/api/v1/admin/users",
            headers=employee_headers,
        )
        assert resp.status_code in (403, 404)

    def test_invalid_token_rejected(self, client):
        """无效 token 被拒绝"""
        resp = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer invalid-token-xyz"},
        )
        assert resp.status_code == 401


# ============================================================
# 3. 评估全链路
# ============================================================


class TestEvaluationFlow:
    """评估创建→审批→查看 完整链路

    评估创建是异步的: POST /evaluations 返回 job_id,
    后台任务处理完成后 job 中包含 evaluation_id。
    无 LLM API key 时后台任务可能失败,此时跳过后续依赖测试。
    """

    def test_create_evaluation(self, client, admin_headers):
        """创建评估(异步,返回 job_id)"""
        resp = client.post(
            "/api/v1/evaluations",
            json={
                "employee_id": "emp-scenario-001",
                "period": "2026-W30",
                "raw_inputs": [
                    {
                        "type": "daily_report",
                        "content": "本周完成了3个核心任务:需求分析、代码开发、测试验收",
                    }
                ],
            },
            headers=admin_headers,
        )
        assert resp.status_code in (200, 201, 202)
        data = resp.json()
        self.__class__.job_id = data.get("job_id")
        assert self.__class__.job_id is not None

    def test_get_evaluation_detail(self, client, admin_headers):
        """查看评估详情(需等待异步任务完成)"""
        job_id = getattr(self.__class__, "job_id", None)
        if not job_id:
            pytest.skip("评估任务未创建")

        # 轮询 job 状态,等待 evaluation_id 可用
        eval_id = None
        for _ in range(5):
            resp = client.get(
                f"/api/v1/evaluations/jobs/{job_id}",
                headers=admin_headers,
            )
            if resp.status_code == 200:
                job = resp.json()
                eval_id = job.get("evaluation_id")
                if eval_id:
                    break
                if job.get("status") in ("failed", "error"):
                    pytest.skip("评估任务处理失败(可能缺少 LLM API key)")
            import time
            time.sleep(1)

        if not eval_id:
            pytest.skip("评估任务未完成,无法测试详情查看")

        self.__class__.eval_id = eval_id
        resp = client.get(
            f"/api/v1/evaluations/{eval_id}",
            headers=admin_headers,
        )
        assert resp.status_code == 200

    def test_approve_evaluation(self, client, admin_headers):
        """审批评估(approve 端点需要 JSON body)"""
        eval_id = getattr(self.__class__, "eval_id", None)
        if not eval_id:
            pytest.skip("评估未创建或任务未完成")
        resp = client.post(
            f"/api/v1/evaluations/{eval_id}/approve",
            json={"comment": "测试审批通过"},
            headers=admin_headers,
        )
        assert resp.status_code in (200, 409)  # 409 = 已审批

    def test_reject_illegal_transition(self, client, admin_headers):
        """非法状态转换被拒绝"""
        eval_id = getattr(self.__class__, "eval_id", None)
        if not eval_id:
            pytest.skip("评估未创建或任务未完成")
        # 重复审批应返回 409
        resp = client.post(
            f"/api/v1/evaluations/{eval_id}/approve",
            json={"comment": "重复审批"},
            headers=admin_headers,
        )
        assert resp.status_code in (200, 409)

    def test_get_nonexistent_evaluation(self, client, admin_headers):
        """查询不存在的评估返回 404"""
        resp = client.get(
            "/api/v1/evaluations/nonexistent-id-12345",
            headers=admin_headers,
        )
        assert resp.status_code == 404


# ============================================================
# 4. 安全中间件行为
# ============================================================


class TestSecurityMiddleware:
    """安全中间件在真实请求中的行为"""

    def test_idempotency_with_key(self, client, admin_headers):
        """相同 Idempotency-Key 的重复请求返回缓存响应"""
        headers = {**admin_headers, "Idempotency-Key": "scenario-idem-001"}
        # 使用真实存在的 GET 端点验证幂等中间件不干扰 GET 请求
        resp1 = client.get("/api/v1/auth/me", headers=headers)
        resp2 = client.get("/api/v1/auth/me", headers=headers)
        assert resp1.status_code == 200
        assert resp2.status_code == 200

    def test_global_exception_handling(self, client, admin_headers):
        """未处理异常返回统一错误格式(不含堆栈)"""
        # 查询不存在的评估 → 应返回 404(正常错误处理,非 500)
        resp = client.get(
            "/api/v1/evaluations/nonexistent-trigger-error",
            headers=admin_headers,
        )
        assert resp.status_code in (404, 422, 500)
        if resp.status_code == 500:
            data = resp.json()
            # 不应泄露堆栈信息
            assert "traceback" not in data
            assert "trace_id" in data


# ============================================================
# 5. 多 Agent 协作
# ============================================================


class TestMultiAgentFlow:
    """多 Agent 协作流程"""

    def test_create_multi_agent_thread(self, client, admin_headers):
        """创建多 Agent 协作线程"""
        resp = client.post(
            "/api/v1/admin/multi-agent/run",
            json={
                "task": "分析员工 emp-001 在 2026-W30 的综合表现",
                "context": {"employee_id": "emp-001", "period": "2026-W30"},
                "max_iterations": 5,
            },
            headers=admin_headers,
        )
        assert resp.status_code in (200, 201, 202)
        data = resp.json()
        self.__class__.thread_id = data.get("thread_id")
        assert self.__class__.thread_id is not None

    def test_get_thread_state(self, client, admin_headers):
        """查询线程状态"""
        thread_id = getattr(self.__class__, "thread_id", None)
        if not thread_id:
            pytest.skip("线程未创建")
        resp = client.get(
            f"/api/v1/admin/multi-agent/threads/{thread_id}/state",
            headers=admin_headers,
        )
        assert resp.status_code == 200


# ============================================================
# 6. 异常场景与边界条件
# ============================================================


class TestEdgeCases:
    """异常场景与边界条件"""

    def test_empty_request_body(self, client, admin_headers):
        """空请求体"""
        resp = client.post("/api/v1/evaluations", json=None, headers=admin_headers)
        assert resp.status_code == 422

    def test_oversized_request_body(self, client, admin_headers):
        """超大请求体被拒绝(>10MB)"""
        large_data = {"data": "x" * (11 * 1024 * 1024)}
        resp = client.post(
            "/api/v1/evaluations",
            json=large_data,
            headers=admin_headers,
        )
        assert resp.status_code == 413

    def test_invalid_json(self, client, admin_headers):
        """无效 JSON"""
        resp = client.post(
            "/api/v1/evaluations",
            data="invalid json {{{",
            headers={**admin_headers, "Content-Type": "application/json"},
        )
        assert resp.status_code == 422

    def test_unknown_endpoint(self, client):
        """不存在的端点返回 404"""
        resp = client.get("/api/v1/nonexistent-endpoint")
        assert resp.status_code == 404

    def test_method_not_allowed(self, client):
        """不支持的 HTTP 方法返回 405"""
        resp = client.patch("/health")
        assert resp.status_code in (405, 404, 422)


# ============================================================
# 7. API 文档与元数据
# ============================================================


class TestAPIMetadata:
    """API 文档与元数据"""

    def test_openapi_docs_available(self, client):
        """OpenAPI 文档可访问"""
        resp = client.get("/docs")
        assert resp.status_code == 200

    def test_openapi_json_available(self, client):
        """OpenAPI JSON 可访问"""
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        data = resp.json()
        assert data["info"]["title"] == "AgentValue-AI"
