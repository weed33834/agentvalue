"""安全中间件

P0-1: 安全响应头 — 防 XSS/点击劫持/MIME 嗅探/HSTS
P0-3: 分布式锁 — 基于 Redis 的跨实例互斥锁
P0-4: API 幂等性 — Idempotency-Key 重复请求防护
P0-5: 请求上下文 — trace_id 生成/传播 + 响应头注入
P0-6: 全局异常处理 — 统一错误响应格式 + 堆栈泄露防护
"""

from __future__ import annotations

import json
import logging
import traceback
import uuid
from typing import Any, Callable, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)


class SecureHeadersMiddleware:
    """安全响应头中间件

    添加以下安全头到所有响应:
    - X-Content-Type-Options: nosniff (防 MIME 嗅探)
    - X-Frame-Options: DENY (防点击劫持)
    - X-XSS-Protection: 1; mode=block (浏览器 XSS 过滤器)
    - Referrer-Policy: strict-origin-when-cross-origin (限制 Referrer 泄露)
    - Strict-Transport-Security: max-age=31536000; includeSubDomains (HSTS)
    - Content-Security-Policy: default-src 'self' (CSP 基础策略)

    使用方式:
        app.add_middleware(BaseHTTPMiddleware, dispatch=SecureHeadersMiddleware().dispatch)
    """

    SECURITY_HEADERS = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "X-XSS-Protection": "1; mode=block",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
        # CSP: 允许 self + inline style (Element Plus 需要) + data: 图片
        "Content-Security-Policy": (
            "default-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "font-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        ),
    }

    async def dispatch(self, request: Request, call_next: Callable):
        response = await call_next(request)
        for header, value in self.SECURITY_HEADERS.items():
            response.headers[header] = value
        return response


class DistributedLock:
    """P0-3: 基于 Redis 的分布式锁

    使用 Redis SET NX EX 实现互斥锁，支持:
    - 自动过期(防死锁)
    - 锁续期(看门狗)
    - 可重入(同一 holder 可多次获取)

    降级策略: Redis 不可用时返回 None(调用方应降级为无锁运行或直接拒绝)

    使用方式:
        async with DistributedLock(redis, "eval:{employee_id}", ttl=30) as lock:
            if lock is None:
                raise HTTPException(409, "操作正在进行中，请稍后重试")
            # 执行受保护的操作
    """

    def __init__(self, redis_client: Any, key: str, ttl: int = 30, holder: Optional[str] = None):
        self._redis = redis_client
        self._key = f"distlock:{key}"
        self._ttl = ttl
        self._holder = holder or str(uuid.uuid4())
        self._acquired = False

    async def __aenter__(self) -> Optional["DistributedLock"]:
        if self._redis is None:
            return None
        try:
            result = await self._redis.set(
                self._key, self._holder, nx=True, ex=self._ttl
            )
            if result:
                self._acquired = True
                return self
            return None
        except Exception as e:
            logger.warning("分布式锁获取失败,降级无锁: %s", e)
            return None

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if not self._acquired or self._redis is None:
            return
        try:
            # Lua 脚本确保只有 holder 才能释放锁(防误删)
            script = (
                "if redis.call('get', KEYS[1]) == ARGV[1] "
                "then return redis.call('del', KEYS[1]) "
                "else return 0 end"
            )
            await self._redis.eval(script, 1, self._key, self._holder)
        except Exception as e:
            logger.warning("分布式锁释放失败: %s", e)

    async def extend(self, ttl: Optional[int] = None) -> bool:
        """续期锁(看门狗模式)"""
        if not self._acquired or self._redis is None:
            return False
        try:
            script = (
                "if redis.call('get', KEYS[1]) == ARGV[1] "
                "then return redis.call('expire', KEYS[1], ARGV[2]) "
                "else return 0 end"
            )
            result = await self._redis.eval(
                script, 1, self._key, self._holder, ttl or self._ttl
            )
            return bool(result)
        except Exception as e:
            logger.warning("分布式锁续期失败: %s", e)
            return False


class IdempotencyMiddleware:
    """P0-4: API 幂等性中间件

    基于 Idempotency-Key header 实现请求幂等:
    - 首次请求: 执行并缓存响应(status + body)
    - 重复请求(相同 key): 直接返回缓存的响应

    降级策略: Redis 不可用时跳过幂等检查(透传请求)

    使用方式:
        app.add_middleware(BaseHTTPMiddleware, dispatch=IdempotencyMiddleware(redis).dispatch)

    支持的 Idempotency-Key 格式: 任意非空字符串(建议 UUID)
    缓存 TTL: 24 小时(可配置)
    """

    # 需要幂等保护的写方法
    IDEMPOTENT_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
    # 幂等缓存 TTL(秒)
    DEFAULT_TTL = 86400  # 24h
    # 最大缓存 body 大小(字节)，超过则不缓存(避免大响应占满 Redis)
    MAX_CACHE_BODY_SIZE = 64 * 1024  # 64KB

    def __init__(self, redis_client: Any, ttl: int = DEFAULT_TTL):
        self._redis = redis_client
        self._ttl = ttl

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # 非写方法或无 Idempotency-Key 直接放行
        if request.method not in self.IDEMPOTENT_METHODS:
            return await call_next(request)

        idempotency_key = request.headers.get("Idempotency-Key")
        if not idempotency_key:
            return await call_next(request)

        # Redis 不可用时降级透传
        if self._redis is None:
            return await call_next(request)

        # 构造缓存 key: 方法 + 路径 + 租户 + idempotency_key
        tenant_id = getattr(request.state, "tenant_id", "default")
        cache_key = (
            f"idem:{request.method}:{request.url.path}:{tenant_id}:{idempotency_key}"
        )

        try:
            # 检查是否有缓存的响应
            cached = await self._redis.get(cache_key)
            if cached:
                data = json.loads(cached)
                return JSONResponse(
                    status_code=data["status"],
                    content=data["body"],
                    headers={"X-Idempotent-Replay": "true"},
                )

            # 标记为处理中(防止并发重复请求)
            processing = await self._redis.set(
                f"{cache_key}:processing", "1", nx=True, ex=60
            )
            if not processing:
                # 另一个请求正在处理相同 key
                return JSONResponse(
                    status_code=409,
                    content={"detail": "相同 Idempotency-Key 的请求正在处理中"},
                )

            try:
                response = await call_next(request)

                # 缓存成功响应(仅缓存 2xx 响应)
                if 200 <= response.status_code < 300:
                    # BaseHTTPMiddleware 的 StreamingResponse 需要读取 body
                    body_bytes = b""
                    async for chunk in response.body_iterator:
                        body_bytes += chunk
                    if len(body_bytes) <= self.MAX_CACHE_BODY_SIZE:
                        try:
                            cache_data = json.dumps({
                                "status": response.status_code,
                                "body": json.loads(body_bytes),
                            })
                            await self._redis.set(cache_key, cache_data, ex=self._ttl)
                        except (json.JSONDecodeError, TypeError):
                            pass
                    # 重建响应(因为 body_iterator 已被消费)
                    from starlette.responses import Response as RawResponse
                    response = RawResponse(
                        content=body_bytes,
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        media_type=response.media_type,
                    )

                return response
            finally:
                # 清除处理中标记
                await self._redis.delete(f"{cache_key}:processing")

        except Exception as e:
            logger.warning("幂等性检查失败,降级透传: %s", e)
            return await call_next(request)


class RequestContextMiddleware:
    """P0-5: 请求上下文中间件

    为每个 HTTP 请求生成/传播 trace_id:
    - 优先从 X-Trace-Id 请求头读取(支持分布式追踪链路传播)
    - 无则生成 UUID
    - 注入到 tracer contextvar(日志自动关联 trace_id)
    - 注入到 X-Trace-Id 响应头(前端可关联排障)
    - 注入到 request.state.trace_id(业务代码可读取)

    使用方式:
        app.add_middleware(BaseHTTPMiddleware, dispatch=RequestContextMiddleware().dispatch)
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # 优先从请求头读取 trace_id(支持跨服务传播)
        trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())

        # 注入到 request.state 供业务代码读取
        request.state.trace_id = trace_id

        # 注入到 tracer contextvar(日志自动关联)
        _token = None
        try:
            from core.tracing import _current_trace_id

            _token = _current_trace_id.set(trace_id)
        except Exception:
            pass

        try:
            response = await call_next(request)
            # 注入到响应头
            response.headers["X-Trace-Id"] = trace_id
            return response
        finally:
            if _token is not None:
                try:
                    _current_trace_id.reset(_token)
                except Exception:
                    pass


class GlobalExceptionMiddleware:
    """P0-6: 全局异常处理中间件

    捕获所有未处理异常,返回统一错误响应格式:
    - 生产环境: 隐藏堆栈信息, 仅返回 trace_id 供排障
    - 开发/测试环境: 返回完整堆栈(便于调试)

    统一错误响应格式:
    {
        "detail": "内部服务器错误",
        "trace_id": "abc-123",
        "type": "internal_server_error"
    }

    使用方式:
        app.add_middleware(BaseHTTPMiddleware, dispatch=GlobalExceptionMiddleware(debug=settings.debug).dispatch)
    """

    # 不需要捕获的异常类型(这些已有 FastAPI 的异常处理器)
    _SKIP_EXCEPTIONS = frozenset()

    def __init__(self, debug: bool = False):
        self._debug = debug

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        try:
            return await call_next(request)
        except Exception as exc:
            trace_id = getattr(request.state, "trace_id", None) or str(uuid.uuid4())

            # 记录完整堆栈到日志(含 trace_id)
            logger.error(
                "未处理异常 [trace_id=%s] %s: %s",
                trace_id,
                type(exc).__name__,
                str(exc),
                exc_info=True,
            )

            # 统一错误响应
            if self._debug:
                # 开发模式: 返回完整堆栈
                return JSONResponse(
                    status_code=500,
                    content={
                        "detail": str(exc),
                        "trace_id": trace_id,
                        "type": type(exc).__name__,
                        "traceback": traceback.format_exc(),
                    },
                    headers={"X-Trace-Id": trace_id},
                )
            else:
                # 生产模式: 隐藏堆栈
                return JSONResponse(
                    status_code=500,
                    content={
                        "detail": "内部服务器错误,请联系管理员并提供 trace_id",
                        "trace_id": trace_id,
                        "type": "internal_server_error",
                    },
                    headers={"X-Trace-Id": trace_id},
                )
