"""
HTTP 中间件

TenantMiddleware：纯 ASGI 实现（不继承 BaseHTTPMiddleware，规避 contextvar 跨任务传播问题），
从 JWT claims 或 x-tenant-id header 提取 tenant_id 写入请求上下文，供 service 层做数据级过滤。
未携带时回退 default，单租户兼容。
"""

import logging
import time

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
