"""Webhook 事件管理 Admin API

路由前缀: /api/v1/admin/webhook-events
权限: Role.ADMIN

完整功能:
- GET    /                  - 事件列表 (分页 + source/status 过滤)
- GET    /{event_id}        - 事件详情
- POST   /{event_id}/retry  - 重试失败的事件
- DELETE /{event_id}        - 删除事件记录
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.rbac import Role, require_role
from core.database import get_db
from models.models import WebhookEvent

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/webhook-events",
    tags=["admin-webhook-events"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


@router.get("")
async def list_webhook_events(
    source: Optional[str] = Query(None, description="按来源过滤 (feishu/gitlab/custom)"),
    event_status: Optional[str] = Query(None, alias="status", description="按状态过滤 (pending/processed/failed)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Webhook 事件列表 (分页 + 过滤)"""
    query = select(WebhookEvent).order_by(WebhookEvent.received_at.desc())

    if source:
        query = query.where(WebhookEvent.source == source)
    if event_status:
        query = query.where(WebhookEvent.status == event_status)

    # 总数
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # 分页
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    events = result.scalars().all()

    return {
        "items": [_event_to_dict(e) for e in events],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/{event_id}")
async def get_webhook_event(
    event_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Webhook 事件详情"""
    event = await db.get(WebhookEvent, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Webhook event not found")
    detail = _event_to_dict(event)
    # 解析 payload 供前端展示
    try:
        detail["parsed_payload"] = json.loads(event.payload)
    except (json.JSONDecodeError, TypeError):
        detail["parsed_payload"] = None
    return detail


@router.post("/{event_id}/retry")
async def retry_webhook_event(
    event_id: int,
    db: AsyncSession = Depends(get_db),
):
    """重试失败的 webhook 事件

    将事件状态重置为 pending,触发后台重新处理。
    """
    event = await db.get(WebhookEvent, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Webhook event not found")

    if event.status != "failed":
        raise HTTPException(
            status_code=400,
            detail=f"Only failed events can be retried (current: {event.status})",
        )

    event.status = "pending"
    event.error_message = None
    event.processed_at = None
    await db.commit()

    # 异步重新处理
    import asyncio

    try:
        payload = json.loads(event.payload)
    except (json.JSONDecodeError, TypeError):
        payload = {}

    from api.webhook_routes import _process_webhook_event

    asyncio.create_task(
        _process_webhook_event(
            source=event.source,
            event_type=event.event_type,
            payload=payload,
            tenant_id=event.tenant_id,
            raw_body=event.payload,
        )
    )

    logger.info("Webhook event %s 重试已触发", event_id)
    return {"status": "retrying", "event_id": event_id}


@router.delete("/{event_id}")
async def delete_webhook_event(
    event_id: int,
    db: AsyncSession = Depends(get_db),
):
    """删除 webhook 事件记录"""
    event = await db.get(WebhookEvent, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Webhook event not found")

    await db.delete(event)
    await db.commit()
    return {"status": "deleted", "event_id": event_id}


def _event_to_dict(event: WebhookEvent) -> Dict[str, Any]:
    """WebhookEvent → dict"""
    return {
        "id": event.id,
        "source": event.source,
        "event_type": event.event_type,
        "status": event.status,
        "error_message": event.error_message,
        "tenant_id": event.tenant_id,
        "received_at": event.received_at.isoformat() if event.received_at else None,
        "processed_at": event.processed_at.isoformat() if event.processed_at else None,
    }
