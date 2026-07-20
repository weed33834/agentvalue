"""
P3-3: TenantMiddleware 审计上下文 contextvar 重置测试

用 httpx.AsyncClient + ASGITransport 直接调 middleware,验证:
- 请求结束后 _current_actor_id / _current_actor_ip contextvar 已 reset
- 异常路径(下游抛异常)仍 reset(不泄漏到下一次请求)
- 携带 JWT 时 actor_id 从 sub 解析并写入 contextvar
"""

import json

import httpx
import pytest

from api.middleware import TenantMiddleware
from auth.jwt_handler import create_access_token
from services.audit_decorator import _current_actor_id, _current_actor_ip


def _build_asgi_app(captured: dict, mode: str = "ok"):
    """构造一个被 TenantMiddleware 包裹的最小 ASGI app。

    mode:
      ok   - 返回 200,并在响应前把当前 contextvar 快照写入 captured
      boom - 抛 RuntimeError,模拟下游异常
    """

    async def inner(scope, receive, send):
        if scope.get("type") != "http":
            return
        captured["actor_id_during"] = _current_actor_id.get()
        captured["actor_ip_during"] = _current_actor_ip.get()
        if mode == "boom":
            raise RuntimeError("downstream boom")
        body = json.dumps({"actor_id": _current_actor_id.get()}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"application/json"]],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return TenantMiddleware(inner)


@pytest.mark.asyncio
async def test_contextvar_reset_after_successful_request():
    """请求结束后 _current_actor_id 已 reset 回 None"""
    pre = _current_actor_id.get()
    assert pre is None, "测试前置:_current_actor_id 应为 None"

    captured: dict = {}
    app = _build_asgi_app(captured, mode="ok")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/ok")
        assert resp.status_code == 200

    assert "actor_id_during" in captured
    assert _current_actor_id.get() is None, "contextvar 未 reset,泄漏到测试外"
    assert _current_actor_ip.get() is None


@pytest.mark.asyncio
async def test_contextvar_reset_after_downstream_exception():
    """下游抛异常时,middleware 的 finally 仍应 reset contextvar"""
    pre = _current_actor_id.get()
    assert pre is None

    captured: dict = {}
    app = _build_asgi_app(captured, mode="boom")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(RuntimeError, match="downstream boom"):
            await client.get("/boom")

    assert "actor_id_during" in captured
    assert _current_actor_id.get() is None, "异常路径 contextvar 未 reset"
    assert _current_actor_ip.get() is None


@pytest.mark.asyncio
async def test_contextvar_not_leaked_between_requests():
    """第一次请求带 JWT(actor=U123),第二次不带 JWT(actor 应为 system 而非残留 U123)"""
    token = create_access_token(user_id="U123", role="employee")

    seen: list = []

    async def inner(scope, receive, send):
        seen.append(_current_actor_id.get())
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"application/json"]],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})

    app = TenantMiddleware(inner)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 第一次:带 JWT,actor_id 应为 U123
        await client.get("/a", headers={"Authorization": f"Bearer {token}"})
        assert _current_actor_id.get() is None, "第一次请求后未 reset"
        # 第二次:不带 JWT,actor_id 应为 system(set_audit_context(None) -> "system"),
        # 不应残留第一次的 U123
        await client.get("/b")
        assert _current_actor_id.get() is None, "第二次请求后未 reset"

    # 第一次请求期间看到 U123,第二次看到 system(非 U123 残留)
    assert seen[0] == "U123"
    assert seen[1] == "system"
    assert seen[1] != "U123"


@pytest.mark.asyncio
async def test_actor_id_propagated_from_jwt_sub():
    """携带 JWT 时,actor_id 从 payload.sub 解析并写入 contextvar"""
    token = create_access_token(user_id="U123", role="employee", name="张三")

    captured: dict = {}

    async def inner(scope, receive, send):
        captured["actor_id_during"] = _current_actor_id.get()
        body = json.dumps({"actor_id": _current_actor_id.get()}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"application/json"]],
            }
        )
        await send({"type": "http.response.body", "body": body})

    app = TenantMiddleware(inner)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/anything",
            headers={"Authorization": f"Bearer {token}", "X-Real-IP": "10.0.0.1"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["actor_id"] == "U123"

    assert captured["actor_id_during"] == "U123"
    assert _current_actor_id.get() is None


@pytest.mark.asyncio
async def test_actor_ip_extracted_from_headers():
    """X-Forwarded-For 应被提取为 actor_ip contextvar"""
    captured: dict = {}

    async def inner(scope, receive, send):
        captured["actor_ip_during"] = _current_actor_ip.get()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"application/json"]],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})

    app = TenantMiddleware(inner)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/x", headers={"X-Forwarded-For": "203.0.113.5, 10.0.0.1"}
        )
        assert resp.status_code == 200

    assert captured["actor_ip_during"] == "203.0.113.5"
    assert _current_actor_ip.get() is None
