"""
HTTP 中间件

TenantMiddleware：纯 ASGI 实现（不继承 BaseHTTPMiddleware，规避 contextvar 跨任务传播问题），
从 JWT claims 或 x-tenant-id header 提取 tenant_id 写入请求上下文，供 service 层做数据级过滤。
未携带时回退 default，单租户兼容。

ApiKeyMiddleware：纯 ASGI 实现，从 X-API-Key header 提取 API Key 并校验，
验证通过后注入 request.state.api_key_id 供下游使用，并异步更新 last_used_at。
"""

import hashlib
import logging
import time
from datetime import datetime, timezone

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from auth.jwt_handler import decode_access_token, extract_bearer_token
from core.config import get_settings
from core.tenant_context import reset_current_tenant, set_current_tenant
from models.models import DEFAULT_TENANT_ID, Tenant

logger = logging.getLogger(__name__)

# 进程级租户状态缓存：tenant_id -> (status, expiry_ts)
# 30 秒 TTL，避免每个请求查库；admin 改完状态最长 30s 后生效
_TENANT_CACHE_TTL_SECONDS = 30.0
_tenant_cache: dict[str, tuple[str, float]] = {}


def _extract_headers(scope: Scope) -> dict:
    """从 ASGI scope 提取 headers(全小写键)"""
    headers: dict = {}
    for raw_key, raw_value in scope.get("headers", []):
        try:
            key = raw_key.decode("latin-1").lower()
            headers[key] = raw_value.decode("latin-1")
        except Exception:
            continue
    return headers


def _extract_jwt_payload(headers: dict) -> dict:
    """从 Authorization header 提取 JWT payload,失败返回空 dict"""
    auth_header = headers.get("authorization")
    token = extract_bearer_token(auth_header)
    if not token:
        return {}
    payload = decode_access_token(token)
    return payload if isinstance(payload, dict) else {}


def _extract_client_ip(scope: Scope, headers: dict) -> str | None:
    """从 X-Forwarded-For / X-Real-IP / client 取真实 IP"""
    xff = headers.get("x-forwarded-for")
    if xff:
        # 取第一个 IP(原始 client)
        return xff.split(",")[0].strip()
    xri = headers.get("x-real-ip")
    if xri:
        return xri.strip()
    client = scope.get("client")
    if client:
        return client[0]
    return None


def _extract_tenant_id(scope: Scope) -> str:
    """从请求头解析租户：优先 JWT claims，其次 x-tenant-id header，兜底 default"""
    headers = _extract_headers(scope)

    # 1. JWT claims 中的 tenant_id（可信来源，签发时写入）
    payload = _extract_jwt_payload(headers)
    if payload:
        tenant_id = payload.get("tenant_id")
        if tenant_id:
            return tenant_id

    # 2. 显式 header（演示/调试用，生产应以 JWT 为准）
    header_tenant = headers.get("x-tenant-id")
    if header_tenant:
        return header_tenant.strip()

    # 3. 兜底默认租户，保持单租户历史行为
    return DEFAULT_TENANT_ID


async def _get_tenant_status(tenant_id: str) -> str:
    """查询租户状态，命中缓存直接返回，否则查库并写入缓存。

    demo 模式下（auth_demo_mode=True）测试环境无真实租户，统一返回 active 放行。
    查询异常或租户不存在时也返回 active，避免 DB 故障阻断全部请求。
    """
    settings = get_settings()
    if settings.auth_demo_mode:
        return "active"

    now = time.monotonic()
    cached = _tenant_cache.get(tenant_id)
    if cached and cached[1] > now:
        return cached[0]

    status = "active"
    try:
        from sqlalchemy import select

        from core.database import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Tenant.status).where(Tenant.tenant_id == tenant_id)
            )
            row = result.scalar_one_or_none()
            if row is not None:
                status = row
    except Exception:
        logger.exception("查询租户状态失败 tenant_id=%s,降级放行", tenant_id)
        status = "active"

    _tenant_cache[tenant_id] = (status, now + _TENANT_CACHE_TTL_SECONDS)
    return status


def invalidate_tenant_cache(tenant_id: str | None = None) -> None:
    """使租户状态缓存失效，供 admin 更新租户状态后立即生效调用。"""
    if tenant_id is None:
        _tenant_cache.clear()
    else:
        _tenant_cache.pop(tenant_id, None)


class TenantMiddleware:
    """纯 ASGI 租户中间件，仅拦截 http 类型请求"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = _extract_headers(scope)
        tenant_id = _extract_tenant_id(scope)

        # 校验租户状态：非 active 租户拒绝访问（403）
        # demo 模式下直接放行，避免测试环境无真实租户时全 403
        tenant_status = await _get_tenant_status(tenant_id)
        if tenant_status != "active":
            response = JSONResponse(
                status_code=403,
                content={
                    "detail": f"租户 {tenant_id} 当前状态为 {tenant_status},访问被拒绝"
                },
            )
            await response(scope, receive, send)
            return

        # P1-3 修复: 注入 audit context(actor_id + ip), 供 service 层 audit_action 装饰器读取
        # 未携带 JWT 时 actor_id=None, 装饰器兜底为 "system"
        from services.audit_decorator import reset_audit_context, set_audit_context

        payload = _extract_jwt_payload(headers)
        if payload:
            actor_id = (
                payload.get("sub") or payload.get("user_id") or payload.get("uid")
            )
        else:
            actor_id = None
        client_ip = _extract_client_ip(scope, headers)

        tenant_token = set_current_tenant(tenant_id)
        audit_token = set_audit_context(actor_id, client_ip)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_audit_context(audit_token)
            reset_current_tenant(tenant_token)


# ====== API Key 认证中间件 ======

# 进程级 API Key 哈希缓存：key_hash -> (key_id, tenant_id, expiry_ts)
# 30 秒 TTL，避免每个请求查库；admin 吊销 key 后最长 30s 后生效
_APIKEY_CACHE_TTL_SECONDS = 30.0
_apikey_cache: dict[str, tuple[str, str, float]] = {}


def invalidate_apikey_cache(key_hash: str | None = None) -> None:
    """使 API Key 缓存失效，供 admin 吊销/轮换 key 后立即生效调用。"""
    if key_hash is None:
        _apikey_cache.clear()
    else:
        _apikey_cache.pop(key_hash, None)


async def _verify_api_key(plain_key: str) -> tuple[str, str] | None:
    """校验明文 API Key，返回 (key_id, tenant_id) 或 None。

    流程：
    1. sha256(plain_key) 得到 key_hash
    2. 命中进程缓存直接返回（30s TTL）
    3. 未命中则查库比对 key_hash，校验 is_active / expires_at
    4. 命中后异步更新 last_used_at（不阻塞请求）
    """
    key_hash = hashlib.sha256(plain_key.encode("utf-8")).hexdigest()

    # 1. 进程缓存
    now = time.monotonic()
    cached = _apikey_cache.get(key_hash)
    if cached and cached[2] > now:
        return cached[0], cached[1]

    # 2. 查库
    try:
        from sqlalchemy import select

        from core.database import AsyncSessionLocal
        from models.models import ApiKey

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ApiKey).where(
                    ApiKey.key_hash == key_hash,
                    ApiKey.is_active == True,  # noqa: E712
                )
            )
            api_key = result.scalar_one_or_none()
            if api_key is None:
                return None
            # 校验过期时间
            if api_key.expires_at is not None:
                if api_key.expires_at <= datetime.now(timezone.utc):
                    return None
            key_id = api_key.key_id
            tenant_id = api_key.tenant_id
    except Exception:
        logger.exception("校验 API Key 失败,降级放行(视为无 key)")
        return None

    # 3. 写缓存
    _apikey_cache[key_hash] = (key_id, tenant_id, now + _APIKEY_CACHE_TTL_SECONDS)

    # 4. 异步更新 last_used_at（best-effort，不阻塞请求，不因失败中断）
    try:
        from sqlalchemy import update

        from core.database import AsyncSessionLocal as _SessionLocal
        from models.models import ApiKey as _ApiKey

        async with _SessionLocal() as session:
            await session.execute(
                update(_ApiKey)
                .where(_ApiKey.key_hash == key_hash)
                .values(last_used_at=datetime.now(timezone.utc))
            )
            await session.commit()
    except Exception:
        logger.debug("更新 API Key last_used_at 失败", exc_info=True)

    return key_id, tenant_id


class ApiKeyMiddleware:
    """纯 ASGI API Key 认证中间件

    从 X-API-Key header 提取 API Key 并校验。
    - 验证通过：注入 scope["state"]["api_key_id"] 供下游读取
    - 验证失败：不阻断请求（仅记录日志），由具体端点的鉴权层决定是否拒绝
    - 未携带 X-API-Key：直接放行（走 JWT 鉴权路径）

    说明：本中间件不强制要求所有请求都携带 API Key，
    仅在有 X-API-Key header 时做校验并注入身份。
    是否需要 API Key 鉴权由具体路由的 dependencies 决定。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = _extract_headers(scope)
        api_key_header = headers.get("x-api-key")

        if api_key_header:
            result = await _verify_api_key(api_key_header)
            if result is not None:
                key_id, tenant_id = result
                # 注入到 scope["state"]，下游可通过 request.state.api_key_id 读取
                state = scope.setdefault("state", {})
                state["api_key_id"] = key_id
                # 用 API Key 的 tenant_id 设置租户上下文
                # 若 JWT 中间件后续也设置租户，JWT 的优先级更高（因为 TenantMiddleware 在内层）
                token = set_current_tenant(tenant_id)
                try:
                    await self.app(scope, receive, send)
                finally:
                    reset_current_tenant(token)
                return
            else:
                logger.warning("API Key 校验失败,可能已吊销或无效")
                # 返回 401，明确拒绝无效 API Key
                response = JSONResponse(
                    status_code=401,
                    content={"detail": "API Key 无效或已吊销"},
                )
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)
