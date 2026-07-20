"""
auth/rbac.py 单元测试

覆盖：
- Role 枚举所有值（employee/manager/hr/admin）+ str 特性
- can_access 视图权限矩阵
- require_role 单角色：admin 通过 / employee/manager/hr 拒绝 403
- require_role 多角色：[MANAGER, HR] 场景
- 缺失 token 且关闭 demo mode 时 raise 401
- 无效 Bearer token raise 401（JWT 路径优先于 demo mode）
- demo mode 下 x-user-role header 鉴权 + 默认 employee + 无效角色 400
- token 黑名单：已吊销 jti 返回 401
- get_current_user_role / get_current_user_id 正常路径
- get_client_ip（x-forwarded-for / client.host / unknown）

测试策略：构造最小 FastAPI app + TestClient 端到端验证 require_role 依赖。
"""

import time

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from auth.jwt_handler import create_access_token, decode_access_token
from auth.rbac import (
    Role,
    VIEW_PERMISSIONS,
    can_access,
    get_client_ip,
    get_current_user_id,
    get_current_user_role,
    require_role,
)
from core.config import get_settings


# ---------------- Role 枚举 ----------------


class TestRoleEnum:
    def test_all_roles_present(self):
        assert Role.EMPLOYEE.value == "employee"
        assert Role.MANAGER.value == "manager"
        assert Role.HR.value == "hr"
        assert Role.ADMIN.value == "admin"

    def test_role_is_str_enum(self):
        """Role 继承 str，可直接当字符串用"""
        assert Role.ADMIN == "admin"
        assert isinstance(Role.ADMIN, str)

    def test_role_from_value(self):
        assert Role("employee") is Role.EMPLOYEE
        assert Role("admin") is Role.ADMIN

    def test_invalid_role_raises_value_error(self):
        with pytest.raises(ValueError):
            Role("superuser")


# ---------------- can_access 视图权限 ----------------


class TestCanAccess:
    def test_employee_only_employee_view(self):
        assert can_access(Role.EMPLOYEE, "employee_view") is True
        assert can_access(Role.EMPLOYEE, "manager_view") is False
        assert can_access(Role.EMPLOYEE, "audit") is False

    def test_manager_has_employee_manager_audit(self):
        for view in ["employee_view", "manager_view", "audit"]:
            assert can_access(Role.MANAGER, view) is True

    def test_hr_has_employee_manager_audit(self):
        for view in ["employee_view", "manager_view", "audit"]:
            assert can_access(Role.HR, view) is True

    def test_admin_has_all_views(self):
        for view in ["employee_view", "manager_view", "audit"]:
            assert can_access(Role.ADMIN, view) is True

    def test_unknown_view_returns_false(self):
        assert can_access(Role.ADMIN, "nonexistent_view") is False

    def test_view_permissions_covers_all_roles(self):
        """VIEW_PERMISSIONS 应覆盖全部 4 个角色"""
        assert set(VIEW_PERMISSIONS.keys()) == {
            Role.EMPLOYEE,
            Role.MANAGER,
            Role.HR,
            Role.ADMIN,
        }


# ---------------- 最小 FastAPI app（端到端 RBAC） ----------------


def _build_app() -> FastAPI:
    """构造挂载了 require_role 依赖的最小 FastAPI app"""
    app = FastAPI()

    @app.get("/admin-only")
    async def admin_only(role: Role = Depends(require_role(Role.ADMIN))):
        return {"role": role.value}

    @app.get("/manager-or-hr")
    async def manager_or_hr(
        role: Role = Depends(require_role(Role.MANAGER, Role.HR)),
    ):
        return {"role": role.value}

    @app.get("/me")
    async def me(
        role: Role = Depends(get_current_user_role),
        user_id: str = Depends(get_current_user_id),
    ):
        return {"role": role.value, "user_id": user_id}

    return app


@pytest.fixture
def client():
    app = _build_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def fresh_blacklist(monkeypatch):
    """每个测试注入全新的 InMemoryTokenBlacklist 到 auth.rbac。

    避免 module-level token_blacklist 的 asyncio.Lock 跨 TestClient 事件循环
    绑定（_LoopBound 在 3.10+ 会记住首个 loop，后续 TestClient 新 loop 会
    抛 RuntimeError: bound to a different event loop）。
    """
    from auth.token_blacklist import InMemoryTokenBlacklist

    fresh = InMemoryTokenBlacklist()
    monkeypatch.setattr("auth.rbac.token_blacklist", fresh)
    yield fresh


def _bearer(role: str, user_id: str = "u-1") -> dict:
    """生成 Bearer header（走 JWT 路径）"""
    token = create_access_token(user_id, role, name="tester")
    return {"Authorization": f"Bearer {token}"}


# ---------------- require_role 单角色 ----------------


class TestRequireRoleSingle:
    def test_admin_token_passes_admin_only(self, client):
        resp = client.get("/admin-only", headers=_bearer("admin"))
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

    def test_employee_token_forbidden(self, client):
        resp = client.get("/admin-only", headers=_bearer("employee"))
        assert resp.status_code == 403

    def test_manager_token_forbidden(self, client):
        resp = client.get("/admin-only", headers=_bearer("manager"))
        assert resp.status_code == 403

    def test_hr_token_forbidden(self, client):
        resp = client.get("/admin-only", headers=_bearer("hr"))
        assert resp.status_code == 403


# ---------------- require_role 多角色 ----------------


class TestRequireRoleMultiple:
    def test_manager_passes(self, client):
        resp = client.get("/manager-or-hr", headers=_bearer("manager"))
        assert resp.status_code == 200
        assert resp.json()["role"] == "manager"

    def test_hr_passes(self, client):
        resp = client.get("/manager-or-hr", headers=_bearer("hr"))
        assert resp.status_code == 200
        assert resp.json()["role"] == "hr"

    def test_admin_not_in_list_forbidden(self, client):
        """admin 不在 [MANAGER, HR] 中，应 403"""
        resp = client.get("/manager-or-hr", headers=_bearer("admin"))
        assert resp.status_code == 403

    def test_employee_not_in_list_forbidden(self, client):
        resp = client.get("/manager-or-hr", headers=_bearer("employee"))
        assert resp.status_code == 403


# ---------------- 缺失 / 无效 token → 401 ----------------


class TestUnauthorizedAccess:
    def test_no_token_demo_mode_off_returns_401(self, client, monkeypatch):
        """关闭 demo mode 后无 token 应返回 401"""
        monkeypatch.setattr(get_settings(), "auth_demo_mode", False)
        resp = client.get("/me")
        assert resp.status_code == 401

    def test_invalid_bearer_token_returns_401(self, client):
        """无效 Bearer token 返回 401（JWT 路径优先于 demo mode）"""
        resp = client.get("/me", headers={"Authorization": "Bearer invalid.token.here"})
        assert resp.status_code == 401

    def test_malformed_authorization_header_returns_401(self, client, monkeypatch):
        """非 Bearer 格式 Authorization header，demo mode 关闭时 401"""
        monkeypatch.setattr(get_settings(), "auth_demo_mode", False)
        resp = client.get("/me", headers={"Authorization": "Basic abc123"})
        assert resp.status_code == 401


# ---------------- demo mode header 鉴权 ----------------


class TestDemoModeHeaders:
    def test_x_user_role_admin_passes(self, client):
        """demo mode 下 x-user-role header 可伪造 admin 身份"""
        resp = client.get(
            "/admin-only",
            headers={"x-user-role": "admin", "x-user-id": "demo-admin"},
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

    def test_x_user_role_employee_forbidden_on_admin(self, client):
        resp = client.get("/admin-only", headers={"x-user-role": "employee"})
        assert resp.status_code == 403

    def test_default_role_is_employee_when_no_header(self, client):
        """demo mode 下未提供 x-user-role 时默认 employee"""
        resp = client.get("/me", headers={"x-user-id": "anon"})
        assert resp.status_code == 200
        assert resp.json()["role"] == "employee"

    def test_invalid_role_header_returns_400(self, client):
        """demo mode 下无效角色 header 返回 400"""
        resp = client.get("/me", headers={"x-user-role": "superuser"})
        assert resp.status_code == 400


# ---------------- token 黑名单吊销 ----------------


class TestTokenBlacklistRevocation:
    def test_revoked_jti_returns_401(self, client, fresh_blacklist):
        """已吊销 jti 的 token 应返回 401"""
        token = create_access_token("u-revoked", "admin")
        payload = decode_access_token(token)
        assert payload is not None
        jti = payload["jti"]
        # 直接写 _store 模拟吊销（绕过 asyncio.Lock 跨 loop 问题）
        fresh_blacklist._store[jti] = time.time() + 300
        resp = client.get("/admin-only", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert "吊销" in resp.json()["detail"]

    def test_non_revoked_token_passes(self, client, fresh_blacklist):
        """未吊销的 token 正常通过"""
        token = create_access_token("u-ok", "admin")
        resp = client.get("/admin-only", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200


# ---------------- get_current_user_role / get_current_user_id ----------------


class TestCurrentUserResolvers:
    def test_resolves_role_and_id_from_jwt(self, client):
        """JWT token 中 sub/role 正确解析到 /me 响应"""
        resp = client.get(
            "/me",
            headers=_bearer("hr", user_id="HR-007"),
        )
        assert resp.status_code == 200
        assert resp.json() == {"role": "hr", "user_id": "HR-007"}

    def test_resolves_role_and_id_from_demo_header(self, client):
        resp = client.get(
            "/me",
            headers={"x-user-role": "manager", "x-user-id": "M-001"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"role": "manager", "user_id": "M-001"}


# ---------------- get_client_ip ----------------


def _make_request(headers: list, client_host=None) -> Request:
    """构造最小 Starlette Request 用于 get_client_ip 测试"""
    scope = {
        "type": "http",
        "headers": headers,
        "client": (client_host, 8000) if client_host else None,
    }
    return Request(scope)


class TestGetClientIp:
    def test_extract_from_x_forwarded_for(self):
        """x-forwarded-for 存在时取第一个 IP"""
        req = _make_request([(b"x-forwarded-for", b"203.0.113.5, 10.0.0.1")])
        assert get_client_ip(req) == "203.0.113.5"

    def test_fallback_to_client_host(self):
        """无 x-forwarded-for 时回退到 client.host"""
        req = _make_request([], client_host="192.168.1.100")
        assert get_client_ip(req) == "192.168.1.100"

    def test_unknown_when_no_client(self):
        """无 client 信息时返回 'unknown'"""
        req = _make_request([])
        assert get_client_ip(req) == "unknown"
