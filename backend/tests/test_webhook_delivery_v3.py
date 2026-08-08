"""WS-3 出站 Webhook 投递与公共 API 测试

覆盖:
- admin 路由 CRUD 全流程（创建自动生成 secret / 列表过滤 / 详情 / 更新 / 删除）
- GET /events 事件目录、GET /deliveries/stats 聚合统计
- POST /{id}/test ping 连通性自检（mock HTTP 传输，校验签名头）
- POST /deliveries/{id}/replay 死信重放
- 公共 API GET /me：坏 key 401、有效 key 200

数据库: 每个测试独立临时 SQLite（与 test_api.py 同款 fixture 模式），
TestClient 挂载完整 main.app（含 ApiKeyMiddleware 与特性路由注册）。
"""

import hashlib
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.config import get_settings
from core.database import close_db, init_db
from main import app
from models.models import DEFAULT_TENANT_ID, ApiKey

from api.middleware import invalidate_apikey_cache

# ============================================================
# 公共 fixture（沿用 test_api.py 的临时库模式）
# ============================================================


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
    # 清空 ApiKeyMiddleware 的进程内 key 缓存，避免跨用例污染
    invalidate_apikey_cache()

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
def session_factory(initialized_db):
    """指向临时库的 AsyncSessionLocal（供种子数据写入）"""
    from core.database import AsyncSessionLocal

    return AsyncSessionLocal


# ============================================================
# 测试替身
# ============================================================


class FakeResponse:
    """2xx 的假 HTTP 响应"""

    status_code = 200
    text = "ok"


class FakeAsyncClient:
    """记录请求的假 httpx.AsyncClient，固定返回 200"""

    requests: list = []

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, content=None, headers=None):
        FakeAsyncClient.requests.append(
            {"url": url, "content": content, "headers": dict(headers or {})}
        )
        return FakeResponse()


async def _not_blocked(url: str) -> bool:
    """SSRF 检查替身：放行一切 URL"""
    return False


def _admin_headers(user_id: str = "ADMIN001") -> dict:
    return {"x-user-role": "admin", "x-user-id": user_id}


def _create_subscription(client, **overrides):
    """便捷创建订阅"""
    payload = {
        "name": "测试订阅",
        "url": "https://hooks.example.com/agentvalue",
        "events": ["evaluation.*"],
        **overrides,
    }
    return client.post(
        "/api/v1/admin/webhook-subscriptions", json=payload, headers=_admin_headers()
    )


async def _seed_api_key(session_factory, plain_key: str, scopes: str = '["*"]') -> str:
    """播种一个有效 API Key，返回明文 key"""
    key_hash = hashlib.sha256(plain_key.encode("utf-8")).hexdigest()
    async with session_factory() as session:
        key = ApiKey(
            key_id=f"ak_{uuid.uuid4().hex[:10]}",
            key_hash=key_hash,
            key_prefix=plain_key[:12],
            name="test-key",
            scopes=scopes,
            tenant_id=DEFAULT_TENANT_ID,
            is_active=True,
        )
        session.add(key)
        await session.commit()
    return plain_key


# ============================================================
# 1. admin 路由 CRUD 全流程
# ============================================================


class TestAdminWebhookSubscriptions:
    """订阅 CRUD / 列表过滤 / 统计"""

    def test_events_catalog(self, client):
        """GET /events 返回平台事件目录（静态路径优先于 /{id}）"""
        resp = client.get(
            "/api/v1/admin/webhook-subscriptions/events", headers=_admin_headers()
        )
        assert resp.status_code == 200
        body = resp.json()
        names = {item["name"] for item in body["items"]}
        assert "evaluation.completed" in names
        assert "ping" in names
        assert body["total"] == len(body["items"])

    def test_create_subscription_auto_generates_secret(self, client):
        """未传 secret 时自动生成 whsec_ 前缀密钥"""
        resp = _create_subscription(client)
        assert resp.status_code == 201
        body = resp.json()
        assert body["id"] > 0
        assert body["secret"].startswith("whsec_")
        assert body["events"] == ["evaluation.*"]
        assert body["enabled"] is True
        assert body["max_attempts"] == 6

    def test_create_rejects_invalid_event_pattern(self, client):
        """未登记的事件模式应被拒绝（禁止订阅平台不会发出的假事件）"""
        resp = _create_subscription(client, events=["no.such.event"])
        assert resp.status_code == 422

    def test_crud_roundtrip(self, client):
        """创建 → 列表 → 详情 → 更新 → 删除"""
        created = _create_subscription(client, description="roundtrip").json()
        sub_id = created["id"]

        # 列表分页
        listing = client.get(
            "/api/v1/admin/webhook-subscriptions", headers=_admin_headers()
        ).json()
        assert listing["total"] >= 1
        assert listing["page"] == 1
        assert {item["id"] for item in listing["items"]} >= {sub_id}

        # 详情
        detail = client.get(
            f"/api/v1/admin/webhook-subscriptions/{sub_id}",
            headers=_admin_headers(),
        )
        assert detail.status_code == 200
        assert detail.json()["name"] == "测试订阅"

        # 更新（部分更新 + 旋转 secret）
        updated = client.put(
            f"/api/v1/admin/webhook-subscriptions/{sub_id}",
            json={"name": "新名字", "enabled": False, "secret": "whsec_rotated_0123456789abcdef"},
            headers=_admin_headers(),
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "新名字"
        assert updated.json()["enabled"] is False
        assert updated.json()["secret"] == "whsec_rotated_0123456789abcdef"

        # 删除（审计）
        deleted = client.delete(
            f"/api/v1/admin/webhook-subscriptions/{sub_id}", headers=_admin_headers()
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True

        # 删除后再查 → 404
        gone = client.get(
            f"/api/v1/admin/webhook-subscriptions/{sub_id}", headers=_admin_headers()
        )
        assert gone.status_code == 404

    def test_list_filters_enabled_and_event(self, client):
        """列表按 enabled / event 过滤"""
        _create_subscription(client, name="启用订阅")
        _create_subscription(client, name="停用订阅", enabled=False)
        _create_subscription(client, name="告警订阅", events=["alert.triggered"])

        enabled_only = client.get(
            "/api/v1/admin/webhook-subscriptions?enabled=true", headers=_admin_headers()
        ).json()
        assert all(item["enabled"] is True for item in enabled_only["items"])

        by_event = client.get(
            "/api/v1/admin/webhook-subscriptions?event=alert.triggered",
            headers=_admin_headers(),
        ).json()
        assert by_event["total"] == 1
        assert by_event["items"][0]["events"] == ["alert.triggered"]

    def test_deliveries_stats_aggregate(self, client, session_factory):
        """GET /deliveries/stats 聚合统计"""
        sub = _create_subscription(client).json()

        from models.webhook_subscription import WebhookDelivery

        async def _seed_delivery(status: str):
            async with session_factory() as session:
                session.add(
                    WebhookDelivery(
                        subscription_id=sub["id"],
                        tenant_id=DEFAULT_TENANT_ID,
                        event="evaluation.completed",
                        payload={"score": 1},
                        status=status,
                        attempt=1,
                        max_attempts=6,
                        duration_ms=120,
                    )
                )
                await session.commit()

        import asyncio

        asyncio.run(_seed_delivery("success"))
        asyncio.run(_seed_delivery("success"))
        asyncio.run(_seed_delivery("failed"))

        resp = client.get(
            "/api/v1/admin/webhook-subscriptions/deliveries/stats",
            headers=_admin_headers(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        assert body["by_status"]["success"] == 2
        assert body["by_status"]["failed"] == 1
        assert body["success_rate"] == round(2 / 3, 4)
        assert body["avg_duration_ms"] > 0


# ============================================================
# 2. 投递操作：test ping / 死信重放
# ============================================================


class TestDeliveryOperations:
    """test_subscription 与 replay（mock 传输层）"""

    def test_test_subscription_dispatches_ping(self, client, monkeypatch):
        """POST /{id}/test 发送 ping，签名头完整"""
        monkeypatch.setattr(
            "services.webhook_delivery_service._is_url_blocked", _not_blocked
        )
        FakeAsyncClient.requests = []
        monkeypatch.setattr(
            "services.webhook_delivery_service.httpx.AsyncClient", FakeAsyncClient
        )

        sub = _create_subscription(client).json()

        resp = client.post(
            f"/api/v1/admin/webhook-subscriptions/{sub['id']}/test",
            headers=_admin_headers(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["response_code"] == 200

        # 确实对外发出过一次请求，且带 Stripe 风格签名头
        assert len(FakeAsyncClient.requests) == 1
        headers = FakeAsyncClient.requests[0]["headers"]
        signature = headers.get("X-AgentValue-Signature", "")
        assert signature.startswith("t=") and ",v1=" in signature
        assert headers.get("X-AgentValue-Event") == "ping"
        assert headers.get("Content-Type") == "application/json"

    def test_replay_dead_delivery(self, client, session_factory, monkeypatch):
        """死信投递手动重放成功"""
        monkeypatch.setattr(
            "services.webhook_delivery_service._is_url_blocked", _not_blocked
        )
        FakeAsyncClient.requests = []
        monkeypatch.setattr(
            "services.webhook_delivery_service.httpx.AsyncClient", FakeAsyncClient
        )

        sub = _create_subscription(client).json()

        from models.webhook_subscription import WebhookDelivery

        async def _seed_dead() -> int:
            async with session_factory() as session:
                delivery = WebhookDelivery(
                    subscription_id=sub["id"],
                    tenant_id=DEFAULT_TENANT_ID,
                    event="evaluation.completed",
                    event_id="eval-001",
                    payload={"score": 1},
                    status="dead",
                    attempt=6,
                    max_attempts=6,
                    error="HTTP 500",
                    response_code=500,
                )
                session.add(delivery)
                await session.commit()
                return delivery.id

        import asyncio

        delivery_id = asyncio.run(_seed_dead())

        resp = client.post(
            f"/api/v1/admin/webhook-subscriptions/deliveries/{delivery_id}/replay",
            headers=_admin_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

        async def _check() -> str:
            async with session_factory() as session:
                from sqlalchemy import select

                row = (
                    await session.execute(
                        select(WebhookDelivery).where(WebhookDelivery.id == delivery_id)
                    )
                ).scalar_one()
                return row.status

        assert asyncio.run(_check()) == "success"


# ============================================================
# 3. 公共 API：GET /me
# ============================================================


class TestPublicApiWhoAmI:
    """API Key 门控的身份自省端点"""

    def test_me_rejects_bad_key(self, client):
        """坏 key → 401"""
        resp = client.get("/api/public/v1/me", headers={"X-API-Key": "ak_not_exist"})
        assert resp.status_code == 401

    def test_me_accepts_valid_key(self, client, session_factory):
        """有效 key → 200，返回租户 / scopes / 配额"""
        plain_key = asyncio_helper_seed(session_factory, "ak_seed_valid_00000001")

        resp = client.get("/api/public/v1/me", headers={"X-API-Key": plain_key})
        assert resp.status_code == 200
        body = resp.json()
        assert body["tenant_id"] == DEFAULT_TENANT_ID
        assert "*" in body["scopes"]
        assert body["key_id"].startswith("ak_")

    def test_me_missing_key(self, client):
        """不携带 key → 401（公共 API 只认 API Key，不认 JWT）"""
        resp = client.get("/api/public/v1/me")
        assert resp.status_code == 401


def asyncio_helper_seed(session_factory, plain_key: str) -> str:
    """同步包装：播种 API Key"""
    import asyncio

    return asyncio.run(_seed_api_key(session_factory, plain_key))
