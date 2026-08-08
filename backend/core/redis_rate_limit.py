"""分布式令牌桶限流器（WS-4 企业级治理加固）

背景
----
`core/rate_limit.py` 的 slowapi Limiter 是**进程内**实现：多副本部署时每台机器
各计各的桶，N 台副本 = 额度放大 N 倍。本模块把令牌桶状态放进 Redis，用一条
**原子 Lua 脚本**完成「取桶→补令牌→扣令牌→写回」，整个判断在 Redis 单线程
事件循环里执行，天然正确（并发下不丢令牌、不超额）。

四维配额
--------
``tenant / api_key / user / endpoint`` 四个维度相互独立，一次请求必须通过
**所有适用维度**的桶才放行（例如一个请求同时受「租户总配额」和「该 API Key
配额」约束）。维度可按需在依赖里开关。

降级策略
--------
Redis 不可用（未配置 REDIS_URL / 连接失败 / 执行异常）时**失败放行**——
退回 `core/rate_limit.py` 的进程内 slowapi 限流（各路由既有的 ``@rate_limit``
装饰器仍然生效），保证业务不因限流组件故障而中断。降级会：
- 置位 ``degraded`` 标志，``is_redis_available()`` / ``get_status()`` 可上报；
- 每个降级间隔（``rate_limit_degrade_log_interval``，默认 60s）最多打一条
  WARNING，避免每请求刷日志。

注意：失败放行不等于无限制 —— 那是 slowapi 的职责；本模块的语义是「多副本
一致性的增强层，坏了就退回单机」。若生产要求 Redis 故障时**拒服**，可自行
改 ``_FAIL_OPEN``（当前刻意保持 fail-open，避免 Redis 抖动引发全站 429）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple, Union

from fastapi import HTTPException, Request, Response, status
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

# 令牌桶在 Redis 中的键前缀，按维度分区
_KEY_PREFIX = "agentvalue:rate_limit"

# 四维名称（顺序即文档展示顺序）
DIMENSIONS = ("tenant", "api_key", "user", "endpoint")

# 降级后 Redis 可达性重新探测间隔（秒）
_PROBE_INTERVAL = 5.0
# 桶键 TTL（秒）：最后一次访问后不清理，键自然过期回收，避免无限膨胀
_BUCKET_TTL = 3600

# Redis 客户端默认超时（秒）
_REDIS_SOCKET_TIMEOUT = 1.0


def _is_connection_error(exc: Exception) -> bool:
    """判断异常是否为 Redis 连接类故障（这类故障应触发整机降级）。

    Lua 执行抛出的业务类错误（类型错误/脚本错误）不算，只对当次 fail-open。
    """
    try:
        import redis.exceptions as rex

        if isinstance(
            exc, (rex.ConnectionError, rex.TimeoutError, rex.RedisConnectionError)
        ):
            return True
    except Exception:  # pragma: no cover - redis 库缺失时按 OSError 兜底判断
        pass
    return isinstance(exc, OSError)

# ---------------------------------------------------------------------------
# 原子 Lua 脚本：取桶 -> 补令牌 -> 扣令牌 -> 写回
# ---------------------------------------------------------------------------
# KEYS[1]   = 桶键（agentvalue:rate_limit:<dim>:<key>）
# ARGV[1]   = capacity（桶容量，即突发上限）
# ARGV[2]   = refill（每秒补充令牌数）
# ARGV[3]   = now（当前时间戳，秒，浮点，由调用方传入便于测试伪造时钟）
# ARGV[4]   = ttl（桶键存活秒数）
#
# 返回数组（Redis 会把 Lua 数值数组转成整数/浮点数组）:
#   [allowed, remaining, retry_after, reset_in]
#   allowed    = 1 放行 / 0 拒绝
#   remaining  = 扣减后剩余令牌（可能为浮点）
#   retry_after= 拒绝时需等待秒数（浮点），放行时为 0
#   reset_in   = 桶恢复到满所需秒数（浮点），用于 X-RateLimit-Reset
_LUA_TOKEN_BUCKET = """
local capacity = tonumber(ARGV[1])
local refill   = tonumber(ARGV[2])
local now      = tonumber(ARGV[3])
local ttl      = tonumber(ARGV[4])

-- 读取桶状态（hash: tokens / last / capacity / refill）
local data = redis.call('HGETALL', KEYS[1])
local tokens = capacity
local last = now
local stored_capacity = capacity
local stored_refill = refill
if data[1] then
  for i = 1, #data, 2 do
    local f = data[i]
    local v = tonumber(data[i + 1])
    if f == 'tokens' then tokens = v
    elseif f == 'last' then last = v
    elseif f == 'capacity' then stored_capacity = v
    elseif f == 'refill' then stored_refill = v
    end
  end
end

-- 容量/补速以脚本参数为准（配置变更即时生效），仅展示字段沿用存量
capacity = capacity
refill = refill

-- 连续时间补令牌：令牌 = min(容量, 存量 + 流逝秒 * 补速)
local elapsed = now - last
if elapsed < 0 then elapsed = 0 end
tokens = tokens + elapsed * refill
if tokens > capacity then tokens = capacity end

local allowed = 0
local retry_after = 0.0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  retry_after = (1 - tokens) / refill
end

-- 写回（含展示字段，供 admin /buckets 查询状态）
redis.call('HMSET', KEYS[1],
  'tokens', tokens,
  'last', now,
  'capacity', stored_capacity,
  'refill', stored_refill)
redis.call('EXPIRE', KEYS[1], ttl)

local reset_in = (capacity - tokens) / refill
return {allowed, tokens, retry_after, reset_in}
"""


# ---------------------------------------------------------------------------
# 结果类型
# ---------------------------------------------------------------------------


@dataclass
class RateLimitResult:
    """一次多维检查的结果，供依赖/中间件写响应头或决定 429。"""

    allowed: bool = True
    # 命中桶里最小的剩余令牌数（向下取整后 ≥0）
    remaining: int = 0
    # 触发拒绝/作为展示的桶容量
    limit: int = 0
    # 桶恢复到满的秒数（向上取整），用于 X-RateLimit-Reset（epoch 秒）
    reset_in: int = 0
    # 429 时的 Retry-After（秒，向上取整）
    retry_after: int = 0
    # 是否降级（Redis 不可用，本次检查未真正限流）
    degraded: bool = False
    # 拒绝时命中的维度名
    dimension: Optional[str] = None
    # 命中的桶键
    key: Optional[str] = None

    def to_headers(self) -> Dict[str, str]:
        """标准限流响应头（降级时返回空，避免上报误导性配额）。"""
        if self.degraded:
            return {}
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(self.remaining, 0)),
            "X-RateLimit-Reset": str(int(time.time()) + self.reset_in),
        }


# ---------------------------------------------------------------------------
# 限流器
# ---------------------------------------------------------------------------


class RedisRateLimiter:
    """Redis 分布式令牌桶限流器。

    用法::

        limiter = get_rate_limiter()
        result = await limiter.check(
            tenant="t1", api_key="k1", user="u1", endpoint="/api/v1/chat"
        )
        if not result.allowed:
            raise HTTPException(429, headers=result.to_headers())

    降级：构造时未配置 REDIS_URL 或运行中 Redis 失联都会置 ``_available=False``，
    此时 ``check()`` 直接放行并标记 degraded（交由 slowapi 进程内限流兜底）。
    """

    def __init__(
        self,
        settings=None,
        client: Optional[object] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        from core.config import get_settings

        self._settings = settings or get_settings()
        self._clock = clock
        self._client = client
        self._sha: Optional[str] = None
        self._available = False
        self._last_probe = 0.0
        self._degraded_log_ts = 0.0
        # 未配置 REDIS_URL：直接置降级并打一次日志（main.py 依赖此行为）
        if not self._settings.redis_url:
            self._log_degraded("REDIS_URL 未配置,分布式限流降级为进程内 slowapi")
            return
        if client is None:
            try:
                import redis.asyncio as aioredis

                self._client = aioredis.from_url(
                    self._settings.redis_url,
                    decode_responses=True,
                    socket_timeout=_REDIS_SOCKET_TIMEOUT,
                    socket_connect_timeout=_REDIS_SOCKET_TIMEOUT,
                )
                # 乐观置位：首次请求时用 ping 验证，失败再降级
                self._available = True
            except Exception as exc:  # pragma: no cover - redis 库本身缺失等
                logger.warning("Redis 客户端初始化失败,分布式限流降级: %s", exc)
                self._available = False
        else:
            # 测试注入的客户端视为可用，首次请求时仍会 ping 验证
            self._available = True

    # ── 降级与可用性 ──────────────────────────────────────────────────────

    def _log_degraded(self, message: str) -> None:
        now = time.monotonic()
        interval = getattr(
            self._settings, "rate_limit_degrade_log_interval", 60
        ) or 60
        if now - self._degraded_log_ts >= interval:
            self._degraded_log_ts = now
            logger.warning("[rate_limit] 降级为进程内限流: %s", message)

    def is_redis_available(self) -> bool:
        """Redis 当前是否可用（供健康检查/状态端点上报）。

        注意：构造时是乐观置位，真实可用性由首次 ``probe_availability()`` /
        ``check()`` 验证；连接类错误会即时翻转本标志。
        """
        return self._available

    async def _ping(self) -> bool:
        """真实 ping 一次并更新 ``_available``；成功置 True，失败置 False。"""
        try:
            assert self._client is not None
            await self._client.ping()
            if not self._available:
                logger.info("[rate_limit] Redis 恢复,分布式限流重新激活")
            self._available = True
            return True
        except Exception:
            self._available = False
            return False

    async def probe_availability(self) -> bool:
        """主动 ping 探测真实可用性（admin /status 端点用，无条件执行）。"""
        ok = await self._ping()
        if not ok:
            self._last_probe = time.monotonic()
            self._log_degraded("Redis ping 探测失败")
        return ok

    async def _ensure_available(self) -> bool:
        """可用则直接返回；降级态按间隔重探 Redis 可达性。"""
        if self._available:
            return True
        now = time.monotonic()
        if now - self._last_probe < _PROBE_INTERVAL:
            return False
        self._last_probe = now
        ok = await self._ping()
        if not ok:
            self._log_degraded("Redis 仍不可达")
        return ok

    async def _load_script(self) -> Optional[str]:
        """SCRIPT LOAD 一次并缓存 sha；返回 None 表示失败。"""
        if self._sha:
            return self._sha
        try:
            assert self._client is not None
            self._sha = await self._client.script_load(_LUA_TOKEN_BUCKET)
            return self._sha
        except Exception as exc:
            logger.debug("[rate_limit] SCRIPT LOAD 失败: %s", exc)
            return None

    # ── 桶计算 ────────────────────────────────────────────────────────────

    def _bucket_spec(self, dimension: str) -> Tuple[float, float]:
        """按维度返回 (capacity, refill_per_second)。

        配额来源统一走 ``core.config.RateLimitBuckets``（与 Settings 中 WS-4
        配置项同步，未单独配置的维度回退默认档）。
        """
        from core.config import RateLimitBuckets

        return RateLimitBuckets.from_settings(self._settings).spec_for(dimension)

    @staticmethod
    def _bucket_key(dimension: str, key: str) -> str:
        return f"{_KEY_PREFIX}:{dimension}:{key}"

    async def _eval_bucket(
        self, dimension: str, key: str
    ) -> Optional[List[Union[int, float]]]:
        """对单个桶执行原子 Lua；失败返回 None（调用方按 fail-open 处理）。

        连接类错误会即时把 ``_available`` 翻为 False（降级），避免后续每个
        请求都吃一遍连接超时延迟；Lua/类型类错误只对当次 fail-open。
        """
        sha = await self._load_script()
        capacity, refill = self._bucket_spec(dimension)
        redis_key = self._bucket_key(dimension, key)
        args = [
            capacity,
            refill,
            self._clock(),
            _BUCKET_TTL,
        ]
        try:
            assert self._client is not None
            try:
                raw = await self._client.evalsha(sha, 1, redis_key, *args)
            except Exception:
                # NOSCRIPT（Redis 重启丢脚本缓存）等脚本缺失错误 -> EVAL 兜底
                self._sha = None
                raw = await self._client.eval(_LUA_TOKEN_BUCKET, 1, redis_key, *args)
            return list(raw) if raw is not None else None
        except Exception as exc:
            if _is_connection_error(exc):
                self._available = False
                self._last_probe = time.monotonic()
                self._log_degraded(f"Redis 连接异常: {type(exc).__name__}")
            logger.debug(
                "[rate_limit] 桶计算失败 dim=%s key=%s: %s", dimension, key, exc
            )
            return None

    # ── 对外接口 ──────────────────────────────────────────────────────────

    async def check(
        self,
        *,
        tenant: Optional[str] = None,
        api_key: Optional[str] = None,
        user: Optional[str] = None,
        endpoint: Optional[str] = None,
    ) -> RateLimitResult:
        """检查一次请求是否通过全部适用维度的桶。

        任一维度未提供 key（None/空串）则跳过该维度；key 为空说明该请求
        没有该身份（例如匿名请求无 api_key），不参与该维度的配额。
        """
        if not getattr(self._settings, "redis_rate_limit_enabled", True):
            return RateLimitResult(degraded=True)
        if not await self._ensure_available():
            return RateLimitResult(degraded=True)

        result = RateLimitResult(degraded=False)

        dims: Dict[str, str] = {}
        if tenant:
            dims["tenant"] = tenant
        if api_key:
            dims["api_key"] = api_key
        if user:
            dims["user"] = user
        if endpoint:
            dims["endpoint"] = endpoint

        # 无任何可用维度：无身份匿名请求仍放行（IP 级限流由 slowapi 负责）
        if not dims:
            return result

        # remaining 取各桶最小值（最紧约束）；limit 取最紧桶的容量
        result.remaining = 2**63 - 1
        for dimension, key in dims.items():
            raw = await self._eval_bucket(dimension, key)
            if raw is None:
                # 单桶执行失败：fail-open，但本次调用标记降级
                result.degraded = True
                continue
            allowed = int(raw[0])
            remaining = float(raw[1])
            retry_after = float(raw[2])
            reset_in = float(raw[3])
            if allowed == 1:
                rem = int(remaining)
                if rem < result.remaining:
                    result.remaining = rem
                    result.limit = int(self._bucket_spec(dimension)[0])
                result.reset_in = max(result.reset_in, int(reset_in) + 1)
            else:
                return RateLimitResult(
                    allowed=False,
                    remaining=int(remaining),
                    limit=int(self._bucket_spec(dimension)[0]),
                    reset_in=int(reset_in) + 1,
                    retry_after=int(retry_after) + 1,
                    degraded=False,
                    dimension=dimension,
                    key=key,
                )
        if result.remaining == 2**63 - 1:
            result.remaining = 0

        result.allowed = True
        return result

    async def bucket_state(self, dimension: str, key: str) -> Optional[Dict[str, object]]:
        """读取单个桶当前状态（admin /buckets 用）；桶不存在返回 None。"""
        if dimension not in DIMENSIONS:
            return None
        if not await self._ensure_available():
            return None
        try:
            assert self._client is not None
            data = await self._client.hgetall(self._bucket_key(dimension, key))
            if not data:
                return None
            tokens = float(data.get("tokens", 0))
            capacity = float(data.get("capacity", 0))
            refill = float(data.get("refill", 0))
            return {
                "dimension": dimension,
                "key": key,
                "tokens": round(tokens, 3),
                "capacity": capacity,
                "refill_per_second": refill,
                "reset_in_seconds": int((capacity - tokens) / refill) if refill else 0,
            }
        except Exception as exc:
            logger.debug("[rate_limit] 读取桶状态失败 %s:%s: %s", dimension, key, exc)
            return None

    async def list_buckets(
        self, dimension: str, page: int = 1, size: int = 20
    ) -> Tuple[List[Dict[str, object]], int]:
        """分页列出某维度下全部桶（SCAN 收集键 + 逐键 HGETALL）。"""
        if dimension not in DIMENSIONS:
            return [], 0
        if not await self._ensure_available():
            return [], 0
        try:
            assert self._client is not None
            pattern = f"{_KEY_PREFIX}:{dimension}:*"
            keys: List[str] = []
            async for scan_key in self._client.scan_iter(match=pattern, count=200):
                keys.append(scan_key)
            keys.sort()
            total = len(keys)
            start = (page - 1) * size
            page_keys = keys[start : start + size]
            items: List[Dict[str, object]] = []
            for rk in page_keys:
                key = rk.rsplit(":", 1)[-1]
                state = await self.bucket_state(dimension, key)
                if state is not None:
                    items.append(state)
            return items, total
        except Exception as exc:
            logger.debug("[rate_limit] 列出桶失败 %s: %s", dimension, exc)
            return [], 0

    async def reset_bucket(self, dimension: str, key: str) -> bool:
        """删除一个桶（配额归零重计）。返回是否确有删除。"""
        if dimension not in DIMENSIONS:
            return False
        if not await self._ensure_available():
            return False
        try:
            assert self._client is not None
            deleted = await self._client.delete(self._bucket_key(dimension, key))
            return bool(deleted)
        except Exception as exc:
            logger.debug("[rate_limit] 重置桶失败 %s:%s: %s", dimension, key, exc)
            return False

    def get_status(self) -> Dict[str, object]:
        """限流器状态（admin /status 用）。"""
        return {
            "enabled": bool(getattr(self._settings, "redis_rate_limit_enabled", True)),
            "active": self._available,
            "degraded": not self._available,
            "redis_available": self._available,
            "mode": "redis" if self._available else "degraded",
            "dimensions": list(DIMENSIONS),
            "fallback": "slowapi 进程内限流",
        }


# ---------------------------------------------------------------------------
# 模块级单例
# ---------------------------------------------------------------------------

_rate_limiter: Optional[RedisRateLimiter] = None


def get_rate_limiter() -> RedisRateLimiter:
    """获取进程级限流器单例（惰性创建，避免 import 期建 Redis 连接）。"""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RedisRateLimiter()
    return _rate_limiter


def is_redis_available() -> bool:
    """Redis 分布式限流当前是否可用（健康端点 / 状态上报）。"""
    return get_rate_limiter().is_redis_available()


# ---------------------------------------------------------------------------
# 身份键解析（依赖 / 中间件共用）
# ---------------------------------------------------------------------------


async def resolve_request_keys(
    request: Request,
    key_builder: Optional[Callable[[Request], Dict[str, Optional[str]]]] = None,
) -> Dict[str, Optional[str]]:
    """从请求解析四维身份键。

    - tenant:  当前租户上下文（TenantMiddleware 已写入）
    - api_key: ApiKeyMiddleware 注入的 ``request.state.api_key_id``
    - user:    JWT ``sub`` / 演示模式 ``x-user-id``（解析失败按匿名跳过）
    - endpoint: 请求路径（不含 query）

    ``key_builder`` 可返回部分覆盖（自定义依赖用），返回 None 或未提及的
    维度保持默认解析结果。
    """
    from core.tenant_context import get_current_tenant

    keys: Dict[str, Optional[str]] = {
        "tenant": get_current_tenant(),
        "api_key": getattr(request.state, "api_key_id", None),
        "user": None,
        "endpoint": request.url.path,
    }
    if key_builder is not None:
        try:
            overrides = key_builder(request) or {}
            for dim, val in overrides.items():
                if dim in DIMENSIONS:
                    keys[dim] = val
        except Exception:  # pragma: no cover - 自定义 builder 失败不阻断
            logger.debug("rate_limit key_builder 解析失败", exc_info=True)
    keys["user"] = await _resolve_user_key(request)
    return keys


async def _resolve_user_key(request: Request) -> Optional[str]:
    """非致命地解析用户 ID；无 JWT / 无演示身份时返回 None（跳过用户维度）。"""
    try:
        from auth.jwt_handler import decode_access_token_async, extract_bearer_token

        token = extract_bearer_token(request.headers.get("authorization"))
        if token:
            payload = await decode_access_token_async(token)
            if payload and payload.get("sub"):
                return str(payload["sub"])
    except Exception:  # noqa: BLE001 - 限流不因身份解析异常而失败
        logger.debug("rate_limit 解析 JWT 用户失败", exc_info=True)
    try:
        from core.config import get_settings

        if get_settings().auth_demo_mode:
            return request.headers.get("x-user-id") or None
    except Exception:  # pragma: no cover
        pass
    return None


# ---------------------------------------------------------------------------
# FastAPI 依赖
# ---------------------------------------------------------------------------


def rate_limit(
    key_builder: Optional[Callable[[Request], Dict[str, Optional[str]]]] = None,
    *,
    tenant: bool = True,
    api_key: bool = True,
    user: bool = True,
    endpoint: bool = True,
) -> Callable:
    """FastAPI 限流依赖，可整路由或单端点使用。

    Args:
        key_builder: 可选回调 ``(request) -> {dimension: key, ...}``，覆盖默认
            身份键解析（例如自定义按 IP 限流：``lambda r: {"user": r.client.host}``）。
        tenant/api_key/user/endpoint: 参与检查的维度开关。

    用法::

        @router.get("/chat", dependencies=[Depends(rate_limit(endpoint=True))])
        async def chat(...): ...

    降级时依赖直接放行，由 slowapi 进程内限流兜底。
    """

    async def _dependency(request: Request, response: Response) -> None:
        limiter = get_rate_limiter()
        keys = await resolve_request_keys(request, key_builder)
        kwargs = {
            "tenant": keys["tenant"] if tenant else None,
            "api_key": keys["api_key"] if api_key else None,
            "user": keys["user"] if user else None,
            "endpoint": keys["endpoint"] if endpoint else None,
        }
        result = await limiter.check(**kwargs)
        headers = result.to_headers()
        for name, value in headers.items():
            response.headers[name] = value
        if not result.allowed:
            retry_headers = dict(headers)
            if result.retry_after:
                retry_headers["Retry-After"] = str(result.retry_after)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "请求过于频繁,已触发速率限制",
                    "dimension": result.dimension,
                    "key": result.key,
                    "limit": result.limit,
                    "retry_after": result.retry_after,
                },
                headers=retry_headers,
            )

    return _dependency


# ---------------------------------------------------------------------------
# 全站中间件（main.py 挂载，慢在 TenantMiddleware / ApiKeyMiddleware 之后执行）
# ---------------------------------------------------------------------------


class RedisRateLimitMiddleware:
    """纯 ASGI 中间件：对每个 HTTP 请求应用四维分布式限流。

    注册位置必须在 ``TenantMiddleware`` / ``ApiKeyMiddleware`` 之后（即代码里
    **先于**它们 add_middleware），才能读到已写入的租户上下文与 ``api_key_id``。

    - Redis 正常：四维桶检查，超限返回 429 + 标准头，并在响应注入限流头。
    - Redis 降级：直接透传，由 slowapi 进程内限流兜底（不阻断业务）。
    """

    def __init__(
        self,
        app: ASGIApp,
        limiter: Optional[RedisRateLimiter] = None,
    ) -> None:
        self.app = app
        self._limiter = limiter or get_rate_limiter()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if not getattr(self._limiter._settings, "redis_rate_limit_enabled", True):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        keys = await resolve_request_keys(request)
        result = await self._limiter.check(**keys)

        if not result.allowed:
            headers = result.to_headers()
            if result.retry_after:
                headers["Retry-After"] = str(result.retry_after)
            response = JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "detail": {
                        "error": "请求过于频繁,已触发速率限制",
                        "dimension": result.dimension,
                        "key": result.key,
                        "limit": result.limit,
                        "retry_after": result.retry_after,
                    }
                },
                headers=headers,
            )
            await response(scope, receive, send)
            return

        response_headers: Dict[str, str] = result.to_headers()

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start" and response_headers:
                merged = dict(message.get("headers", []))
                for name, value in response_headers.items():
                    merged[name.encode("latin-1")] = value.encode("latin-1")
                message["headers"] = list(merged.items())
            await send(message)

        await self.app(scope, receive, send_wrapper)


__all__ = [
    "DIMENSIONS",
    "RateLimitResult",
    "RedisRateLimiter",
    "RedisRateLimitMiddleware",
    "get_rate_limiter",
    "is_redis_available",
    "rate_limit",
    "resolve_request_keys",
]
