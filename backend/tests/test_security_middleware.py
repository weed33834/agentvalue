"""
P0 安全中间件测试

覆盖:
- SecureHeadersMiddleware: 安全响应头注入
- DistributedLock: Redis 分布式锁(获取/释放/续期/降级)
- IdempotencyMiddleware: API 幂等性(首次/重复/并发/降级)
"""


import pytest

from core.security_middleware import (
    DistributedLock,
    IdempotencyMiddleware,
    SecureHeadersMiddleware,
)


# ====== SecureHeadersMiddleware ======


class TestSecureHeaders:
    @pytest.mark.asyncio
    async def test_security_headers_added(self):
        """所有响应都应包含安全响应头"""
        from starlette.testclient import TestClient
        from fastapi import FastAPI
        from starlette.middleware.base import BaseHTTPMiddleware

        app = FastAPI()

        @app.get("/test")
        def test_endpoint():
            return {"ok": True}

        app.add_middleware(
            BaseHTTPMiddleware,
            dispatch=SecureHeadersMiddleware().dispatch,
        )

        client = TestClient(app)
        resp = client.get("/test")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert "X-XSS-Protection" in resp.headers
        assert "Referrer-Policy" in resp.headers
        assert "Strict-Transport-Security" in resp.headers
        assert "Content-Security-Policy" in resp.headers


# ====== DistributedLock ======


class TestDistributedLock:
    @pytest.mark.asyncio
    async def test_acquire_and_release(self):
        """正常获取/释放锁"""
        import fakeredis.aioredis

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        async with DistributedLock(redis, "test:lock:1", ttl=10) as lock:
            assert lock is not None
            assert lock._acquired is True
            # 锁已存在
            val = await redis.get("distlock:test:lock:1")
            assert val is not None

        # 释放后锁应被删除
        val = await redis.get("distlock:test:lock:1")
        assert val is None
        await redis.aclose()

    @pytest.mark.asyncio
    async def test_lock_contention(self):
        """并发时第二个锁获取失败"""
        import fakeredis.aioredis

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        async with DistributedLock(redis, "test:lock:2", ttl=10) as lock1:
            assert lock1 is not None
            # 第二个获取同 key 的锁应返回 None
            async with DistributedLock(redis, "test:lock:2", ttl=10) as lock2:
                assert lock2 is None
        await redis.aclose()

    @pytest.mark.asyncio
    async def test_lock_degrade_without_redis(self):
        """Redis 为 None 时降级返回 None"""
        async with DistributedLock(None, "test:lock:3") as lock:
            assert lock is None

    @pytest.mark.asyncio
    async def test_lock_extend(self):
        """锁续期"""
        import fakeredis.aioredis

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        async with DistributedLock(redis, "test:lock:4", ttl=5) as lock:
            assert lock is not None
            result = await lock.extend(ttl=30)
            assert result is True
            # TTL 应被更新
            ttl = await redis.ttl("distlock:test:lock:4")
            assert ttl > 5
        await redis.aclose()


# ====== IdempotencyMiddleware ======


class TestIdempotencyMiddleware:
    @pytest.mark.asyncio
    async def test_first_request_executes(self):
        """首次请求正常执行"""
        import fakeredis.aioredis
        from starlette.testclient import TestClient
        from fastapi import FastAPI
        from starlette.middleware.base import BaseHTTPMiddleware

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        app = FastAPI()
        call_count = 0

        @app.post("/create")
        def create():
            nonlocal call_count
            call_count += 1
            return {"id": call_count}

        mw = IdempotencyMiddleware(redis_client=redis)
        app.add_middleware(BaseHTTPMiddleware, dispatch=mw.dispatch)

        client = TestClient(app)
        resp = client.post(
            "/create", json={"data": "test"}, headers={"Idempotency-Key": "key-001"}
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == 1
        assert call_count == 1
        await redis.aclose()

    @pytest.mark.asyncio
    async def test_duplicate_request_returns_cached(self):
        """相同 Idempotency-Key 的重复请求返回缓存响应"""
        import fakeredis.aioredis
        from httpx import AsyncClient, ASGITransport
        from fastapi import FastAPI
        from starlette.middleware.base import BaseHTTPMiddleware

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        app = FastAPI()
        call_count = 0

        @app.post("/create")
        def create():
            nonlocal call_count
            call_count += 1
            return {"id": call_count}

        mw = IdempotencyMiddleware(redis_client=redis)
        app.add_middleware(BaseHTTPMiddleware, dispatch=mw.dispatch)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 第一次请求
            resp1 = await client.post(
                "/create", json={"data": "test"}, headers={"Idempotency-Key": "key-002"}
            )
            assert resp1.status_code == 200
            assert resp1.json()["id"] == 1
            # 第二次相同 key 的请求
            resp2 = await client.post(
                "/create", json={"data": "test"}, headers={"Idempotency-Key": "key-002"}
            )
            assert resp2.status_code == 200
            assert resp2.json()["id"] == 1  # 返回缓存的 id=1
            assert resp2.headers.get("X-Idempotent-Replay") == "true"
            assert call_count == 1  # 只执行了一次
        await redis.aclose()

    @pytest.mark.asyncio
    async def test_no_key_passes_through(self):
        """无 Idempotency-Key 的请求直接透传"""
        import fakeredis.aioredis
        from starlette.testclient import TestClient
        from fastapi import FastAPI
        from starlette.middleware.base import BaseHTTPMiddleware

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        app = FastAPI()
        call_count = 0

        @app.post("/create")
        def create():
            nonlocal call_count
            call_count += 1
            return {"id": call_count}

        mw = IdempotencyMiddleware(redis_client=redis)
        app.add_middleware(BaseHTTPMiddleware, dispatch=mw.dispatch)

        client = TestClient(app)
        resp1 = client.post("/create", json={"data": "test1"})
        resp2 = client.post("/create", json={"data": "test2"})
        assert resp1.json()["id"] == 1
        assert resp2.json()["id"] == 2
        assert call_count == 2
        await redis.aclose()

    @pytest.mark.asyncio
    async def test_degrade_without_redis(self):
        """Redis 为 None 时降级透传"""
        from starlette.testclient import TestClient
        from fastapi import FastAPI
        from starlette.middleware.base import BaseHTTPMiddleware

        app = FastAPI()
        call_count = 0

        @app.post("/create")
        def create():
            nonlocal call_count
            call_count += 1
            return {"id": call_count}

        mw = IdempotencyMiddleware(redis_client=None)
        app.add_middleware(BaseHTTPMiddleware, dispatch=mw.dispatch)

        client = TestClient(app)
        resp1 = client.post(
            "/create", json={"data": "test"}, headers={"Idempotency-Key": "key-003"}
        )
        resp2 = client.post(
            "/create", json={"data": "test"}, headers={"Idempotency-Key": "key-003"}
        )
        # 无 Redis 时两次都执行
        assert resp1.json()["id"] == 1
        assert resp2.json()["id"] == 2
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_get_requests_not_cached(self):
        """GET 请求不受幂等中间件影响"""
        import fakeredis.aioredis
        from starlette.testclient import TestClient
        from fastapi import FastAPI
        from starlette.middleware.base import BaseHTTPMiddleware

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        app = FastAPI()
        call_count = 0

        @app.get("/data")
        def get_data():
            nonlocal call_count
            call_count += 1
            return {"count": call_count}

        mw = IdempotencyMiddleware(redis_client=redis)
        app.add_middleware(BaseHTTPMiddleware, dispatch=mw.dispatch)

        client = TestClient(app)
        client.get("/data", headers={"Idempotency-Key": "key-004"})
        client.get("/data", headers={"Idempotency-Key": "key-004"})
        assert call_count == 2  # GET 不缓存
        await redis.aclose()


# ====== RequestContextMiddleware ======


class TestRequestContextMiddleware:
    @pytest.mark.asyncio
    async def test_trace_id_generated(self):
        """无 X-Trace-Id 请求头时自动生成 trace_id"""
        from starlette.testclient import TestClient
        from fastapi import FastAPI
        from starlette.middleware.base import BaseHTTPMiddleware

        from core.security_middleware import RequestContextMiddleware

        app = FastAPI()

        @app.get("/test")
        def test_endpoint():
            return {"ok": True}

        app.add_middleware(
            BaseHTTPMiddleware, dispatch=RequestContextMiddleware().dispatch
        )

        client = TestClient(app)
        resp = client.get("/test")
        assert resp.status_code == 200
        assert "X-Trace-Id" in resp.headers
        assert len(resp.headers["X-Trace-Id"]) > 0

    @pytest.mark.asyncio
    async def test_trace_id_propagated(self):
        """有 X-Trace-Id 请求头时使用传入的 trace_id"""
        from starlette.testclient import TestClient
        from fastapi import FastAPI
        from starlette.middleware.base import BaseHTTPMiddleware

        from core.security_middleware import RequestContextMiddleware

        app = FastAPI()

        @app.get("/test")
        def test_endpoint():
            return {"ok": True}

        app.add_middleware(
            BaseHTTPMiddleware, dispatch=RequestContextMiddleware().dispatch
        )

        client = TestClient(app)
        resp = client.get("/test", headers={"X-Trace-Id": "my-trace-123"})
        assert resp.status_code == 200
        assert resp.headers["X-Trace-Id"] == "my-trace-123"


# ====== GlobalExceptionMiddleware ======


class TestGlobalExceptionMiddleware:
    @pytest.mark.asyncio
    async def test_production_mode_hides_stack(self):
        """生产模式: 未处理异常返回统一格式, 不泄露堆栈"""
        from starlette.testclient import TestClient
        from fastapi import FastAPI
        from starlette.middleware.base import BaseHTTPMiddleware

        from core.security_middleware import GlobalExceptionMiddleware

        app = FastAPI()

        @app.get("/error")
        def error_endpoint():
            raise RuntimeError("数据库连接失败: password=secret123")

        app.add_middleware(
            BaseHTTPMiddleware,
            dispatch=GlobalExceptionMiddleware(debug=False).dispatch,
        )

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/error")
        assert resp.status_code == 500
        data = resp.json()
        assert "trace_id" in data
        assert data["type"] == "internal_server_error"
        assert "password=secret123" not in data["detail"]
        assert "traceback" not in data

    @pytest.mark.asyncio
    async def test_debug_mode_shows_stack(self):
        """调试模式: 返回完整堆栈"""
        from starlette.testclient import TestClient
        from fastapi import FastAPI
        from starlette.middleware.base import BaseHTTPMiddleware

        from core.security_middleware import GlobalExceptionMiddleware

        app = FastAPI()

        @app.get("/error")
        def error_endpoint():
            raise ValueError("test error for debugging")

        app.add_middleware(
            BaseHTTPMiddleware,
            dispatch=GlobalExceptionMiddleware(debug=True).dispatch,
        )

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/error")
        assert resp.status_code == 500
        data = resp.json()
        assert data["detail"] == "test error for debugging"
        assert data["type"] == "ValueError"
        assert "traceback" in data

    @pytest.mark.asyncio
    async def test_normal_request_unaffected(self):
        """正常请求不受全局异常中间件影响"""
        from starlette.testclient import TestClient
        from fastapi import FastAPI
        from starlette.middleware.base import BaseHTTPMiddleware

        from core.security_middleware import GlobalExceptionMiddleware

        app = FastAPI()

        @app.get("/ok")
        def ok_endpoint():
            return {"status": "ok"}

        app.add_middleware(
            BaseHTTPMiddleware,
            dispatch=GlobalExceptionMiddleware(debug=False).dispatch,
        )

        client = TestClient(app)
        resp = client.get("/ok")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
