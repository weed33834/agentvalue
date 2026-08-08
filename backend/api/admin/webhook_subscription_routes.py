"""出站 Webhook 订阅 Admin API（WS-3 集成与开放能力，对标 Svix）

路由前缀: /api/v1/admin/webhook-subscriptions
权限: Role.ADMIN (router 级 dependencies)

端点:
- GET    /events                        - 事件目录（EVENT_CATALOG，静态）
- GET    /deliveries/stats              - 投递聚合统计（静态）
- POST   /deliveries/{delivery_id}/replay - 手动重放一条投递（含死信）
- GET    /                              - 订阅列表（分页，可按 enabled / event 过滤）
- POST   /                              - 创建订阅（secret 缺省时自动生成）
- GET    /{id}                          - 订阅详情
- PUT    /{id}                          - 更新订阅
- DELETE /{id}                          - 删除订阅（审计）
- POST   /{id}/test                     - 发送 ping 连通性自检
- GET    /{id}/deliveries               - 该订阅的投递日志（分页，可按 status 过滤）

路由顺序: 静态路径（/events、/deliveries/*）全部声明在动态 /{id} 之前，
否则 FastAPI 会把 "deliveries" 误匹配成订阅 id。

租户作用域从 get_current_tenant() 获取，管理员不能横向查看其他租户的订阅。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_audit_service
from auth.rbac import Role, get_current_user_id, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from models.webhook_subscription import (
    DELIVERY_STATUSES,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_TIMEOUT_SECONDS,
    WebhookDelivery,
    WebhookSubscription,
)
from services.audit_service import AuditService
from services.webhook_delivery_service import (
    EVENT_CATALOG,
    EVENT_NAMES,
    delivery_stats,
    generate_secret,
    replay,
    test_subscription,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/webhook-subscriptions",
    tags=["admin-webhook-subscriptions"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# 校验辅助
# ============================================================


def _validate_url(url: str) -> None:
    """仅允许 http/https，且以协议头开头，避免相对路径误配。"""
    if not url or not url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="url 必须是以 http:// 或 https:// 开头的绝对地址",
        )


def _validate_event_patterns(events: List[str]) -> List[str]:
    """校验订阅的事件模式：精确事件名 / ``*`` / ``<前缀>.*`` 通配。

    与 services.webhook_delivery_service.event_matches 的语义对齐：
    - 精确名必须登记在 EVENT_CATALOG 中（平台确实会发出的事件）；
    - 通配符前缀必须来自 EVENT_CATALOG 中已存在事件的前缀。
    """
    if not events:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="events 至少需要登记一个事件或通配模式",
        )
    prefixes = {name.split(".", 1)[0] for name in EVENT_NAMES}
    cleaned: List[str] = []
    for raw in events:
        pattern = str(raw).strip()
        if not pattern:
            continue
        if pattern in EVENT_NAMES or pattern == "*":
            cleaned.append(pattern)
        elif pattern.endswith(".*") and pattern[:-2] in prefixes:
            cleaned.append(pattern)
        else:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"非法事件模式: {pattern}（应为目录内事件名、* 或 <类别>.* 通配）",
            )
    if not cleaned:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="events 清洗后为空",
        )
    return cleaned


def _iso(value: Optional[datetime]) -> Optional[str]:
    """datetime → ISO8601 字符串（naive 视为 UTC）"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _sub_to_dict(sub: WebhookSubscription) -> Dict[str, Any]:
    """订阅 → 序列化 dict"""
    return {
        "id": sub.id,
        "tenant_id": sub.tenant_id,
        "name": sub.name,
        "url": sub.url,
        "events": sub.events or [],
        "secret": sub.secret,
        "headers": sub.headers or {},
        "enabled": sub.enabled,
        "description": sub.description,
        "max_attempts": sub.max_attempts,
        "timeout_seconds": sub.timeout_seconds,
        "created_by": sub.created_by,
        "last_delivery_at": _iso(sub.last_delivery_at),
        "last_status": sub.last_status,
        "consecutive_failures": sub.consecutive_failures or 0,
        "disabled_reason": sub.disabled_reason,
        "created_at": _iso(sub.created_at),
        "updated_at": _iso(sub.updated_at),
    }


def _delivery_to_dict(d: WebhookDelivery) -> Dict[str, Any]:
    """投递 → 序列化 dict"""
    return {
        "id": d.id,
        "subscription_id": d.subscription_id,
        "tenant_id": d.tenant_id,
        "event": d.event,
        "event_id": d.event_id,
        "payload": d.payload,
        "status": d.status,
        "attempt": d.attempt,
        "max_attempts": d.max_attempts,
        "next_retry_at": _iso(d.next_retry_at),
        "response_code": d.response_code,
        "response_body": d.response_body,
        "error": d.error,
        "duration_ms": d.duration_ms,
        "delivered_at": _iso(d.delivered_at),
        "created_at": _iso(d.created_at),
        "updated_at": _iso(d.updated_at),
    }


# ============================================================
# Schemas
# ============================================================


class SubscriptionCreate(BaseModel):
    """创建订阅请求"""

    name: str = Field(..., min_length=1, max_length=128, description="订阅名称")
    url: str = Field(..., min_length=1, max_length=1024, description="目标 URL")
    events: List[str] = Field(
        ..., description="事件模式列表，如 [\"evaluation.*\", \"alert.triggered\"]"
    )
    secret: Optional[str] = Field(
        default=None, min_length=16, max_length=128, description="签名密钥，缺省自动生成"
    )
    headers: Optional[Dict[str, str]] = Field(
        default=None, description="附加请求头（不允许覆盖 X-AgentValue-* 签名头）"
    )
    enabled: bool = Field(default=True, description="是否启用")
    description: Optional[str] = Field(default=None, description="订阅描述")
    max_attempts: int = Field(
        default=DEFAULT_MAX_ATTEMPTS, ge=1, le=20, description="最大尝试次数"
    )
    timeout_seconds: int = Field(
        default=DEFAULT_TIMEOUT_SECONDS, ge=1, le=60, description="单次请求超时（秒）"
    )


class SubscriptionUpdate(BaseModel):
    """更新订阅请求（全部可选，仅更新提供的字段）"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    url: Optional[str] = Field(default=None, min_length=1, max_length=1024)
    events: Optional[List[str]] = Field(default=None)
    secret: Optional[str] = Field(default=None, min_length=16, max_length=128)
    headers: Optional[Dict[str, str]] = Field(default=None)
    enabled: Optional[bool] = Field(default=None)
    description: Optional[str] = Field(default=None)
    max_attempts: Optional[int] = Field(default=None, ge=1, le=20)
    timeout_seconds: Optional[int] = Field(default=None, ge=1, le=60)


# ============================================================
# 路由（静态路径在前，动态 /{id} 在后）
# ============================================================


@router.get("/events")
async def list_event_catalog() -> Dict[str, Any]:
    """返回平台真实会发出的事件目录（EVENT_CATALOG）。

    前端订阅页直接渲染为勾选项；只登记代码里确实 dispatch 的事件。
    """
    return {"items": EVENT_CATALOG, "total": len(EVENT_CATALOG)}


@router.get("/deliveries/stats")
async def get_delivery_stats(
    subscription_id: Optional[int] = Query(
        default=None, ge=1, description="限定单个订阅，缺省统计全租户"
    ),
) -> Dict[str, Any]:
    """投递聚合统计：总量 / 按状态分布 / 成功率 / 平均耗时。"""
    tenant_id = get_current_tenant()
    return await delivery_stats(tenant_id, subscription_id=subscription_id)


@router.post("/deliveries/{delivery_id}/replay")
async def replay_delivery(
    delivery_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """手动重放一条投递（含死信），保留原始 payload 与 event_id。"""
    tenant_id = get_current_tenant()
    # 先做租户归属校验，避免管理员重放其他租户的投递
    delivery = (
        await session.execute(
            select(WebhookDelivery).where(
                WebhookDelivery.id == delivery_id,
                WebhookDelivery.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if delivery is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="投递记录不存在"
        )
    result = await replay(delivery_id)
    await audit_service.log(
        actor_id=current_user_id,
        action="replay_webhook_delivery",
        details={
            "delivery_id": delivery_id,
            "subscription_id": delivery.subscription_id,
            "status": result.get("status"),
        },
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return {"delivery_id": delivery_id, **result}


@router.get("", response_model=Dict[str, Any])
async def list_subscriptions(
    enabled: Optional[bool] = Query(default=None, description="按启用状态过滤"),
    event: Optional[str] = Query(
        default=None, max_length=128, description="按订阅匹配的事件名过滤"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """分页列出当前租户的 Webhook 订阅。"""
    tenant_id = get_current_tenant()
    base = select(WebhookSubscription).where(
        WebhookSubscription.tenant_id == tenant_id
    )
    if enabled is not None:
        base = base.where(WebhookSubscription.enabled.is_(enabled))
    if event:
        base = base.where(WebhookSubscription.events.contains(event))

    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar() or 0
    rows = (
        (
            await session.execute(
                base.order_by(WebhookSubscription.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [_sub_to_dict(sub) for sub in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_subscription(
    payload: SubscriptionCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """创建订阅。secret 缺省时用 generate_secret() 自动生成。"""
    _validate_url(payload.url)
    events = _validate_event_patterns(payload.events)
    tenant_id = get_current_tenant()
    secret = payload.secret or generate_secret()

    sub = WebhookSubscription(
        tenant_id=tenant_id,
        name=payload.name.strip(),
        url=payload.url.strip(),
        events=events,
        secret=secret,
        headers=payload.headers or {},
        enabled=payload.enabled,
        description=payload.description,
        max_attempts=payload.max_attempts,
        timeout_seconds=payload.timeout_seconds,
        created_by=current_user_id,
    )
    session.add(sub)
    await session.flush()
    sub_id = sub.id

    await audit_service.log(
        actor_id=current_user_id,
        action="create_webhook_subscription",
        details={
            "subscription_id": sub_id,
            "name": payload.name,
            "url": payload.url,
            "events": events,
        },
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    await session.refresh(sub)
    return _sub_to_dict(sub)


@router.get("/{subscription_id}", response_model=Dict[str, Any])
async def get_subscription(
    subscription_id: int,
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """订阅详情。"""
    tenant_id = get_current_tenant()
    sub = (
        await session.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.id == subscription_id,
                WebhookSubscription.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="订阅不存在"
        )
    return _sub_to_dict(sub)


@router.put("/{subscription_id}", response_model=Dict[str, Any])
async def update_subscription(
    subscription_id: int,
    payload: SubscriptionUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """更新订阅（部分更新，仅覆盖提供的字段）。"""
    tenant_id = get_current_tenant()
    sub = (
        await session.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.id == subscription_id,
                WebhookSubscription.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="订阅不存在"
        )

    changed: Dict[str, Any] = {}
    if payload.name is not None:
        sub.name = payload.name.strip()
        changed["name"] = sub.name
    if payload.url is not None:
        _validate_url(payload.url)
        sub.url = payload.url.strip()
        changed["url"] = sub.url
    if payload.events is not None:
        sub.events = _validate_event_patterns(payload.events)
        changed["events"] = sub.events
    if payload.secret is not None:
        sub.secret = payload.secret
        changed["secret_rotated"] = True
    if payload.headers is not None:
        sub.headers = payload.headers
        changed["headers"] = True
    if payload.enabled is not None:
        sub.enabled = payload.enabled
        changed["enabled"] = sub.enabled
        # 人工重新启用时清空自动禁用原因
        if payload.enabled and sub.disabled_reason:
            sub.disabled_reason = None
    if payload.description is not None:
        sub.description = payload.description
        changed["description"] = True
    if payload.max_attempts is not None:
        sub.max_attempts = payload.max_attempts
        changed["max_attempts"] = sub.max_attempts
    if payload.timeout_seconds is not None:
        sub.timeout_seconds = payload.timeout_seconds
        changed["timeout_seconds"] = sub.timeout_seconds

    await audit_service.log(
        actor_id=current_user_id,
        action="update_webhook_subscription",
        details={"subscription_id": subscription_id, "changed": changed},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    await session.refresh(sub)
    return _sub_to_dict(sub)


@router.delete("/{subscription_id}", response_model=Dict[str, Any])
async def delete_subscription(
    subscription_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """删除订阅（审计留痕）。关联投递日志保留，供事后排查。"""
    tenant_id = get_current_tenant()
    sub = (
        await session.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.id == subscription_id,
                WebhookSubscription.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="订阅不存在"
        )
    sub_id = sub.id
    await session.delete(sub)

    await audit_service.log(
        actor_id=current_user_id,
        action="delete_webhook_subscription",
        details={"subscription_id": sub_id, "name": sub.name, "url": sub.url},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return {"deleted": True, "subscription_id": sub_id}


@router.post("/{subscription_id}/test", response_model=Dict[str, Any])
async def test_subscription_route(
    subscription_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """发送一条 ping 事件做连通性自检，走完整签名/SSRF/超时链路。"""
    tenant_id = get_current_tenant()
    sub = (
        await session.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.id == subscription_id,
                WebhookSubscription.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="订阅不存在"
        )
    result = await test_subscription(subscription_id)
    await audit_service.log(
        actor_id=current_user_id,
        action="test_webhook_subscription",
        details={
            "subscription_id": subscription_id,
            "status": result.get("status"),
            "response_code": result.get("response_code"),
        },
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return {"subscription_id": subscription_id, **result}


@router.get("/{subscription_id}/deliveries", response_model=Dict[str, Any])
async def list_subscription_deliveries(
    subscription_id: int,
    status_value: Optional[str] = Query(default=None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """分页查询某订阅的投递日志，可按状态过滤。"""
    tenant_id = get_current_tenant()
    sub = (
        await session.execute(
            select(WebhookSubscription.id).where(
                WebhookSubscription.id == subscription_id,
                WebhookSubscription.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="订阅不存在"
        )
    if status_value and status_value not in DELIVERY_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"非法投递状态: {status_value}（可选: {', '.join(DELIVERY_STATUSES)}）",
        )

    base = select(WebhookDelivery).where(
        WebhookDelivery.subscription_id == subscription_id,
        WebhookDelivery.tenant_id == tenant_id,
    )
    if status_value:
        base = base.where(WebhookDelivery.status == status_value)

    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar() or 0
    rows = (
        (
            await session.execute(
                base.order_by(WebhookDelivery.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [_delivery_to_dict(d) for d in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


__all__ = ["router"]
