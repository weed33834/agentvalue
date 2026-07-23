"""告警管理 Admin API

路由前缀: /api/v1/admin/alerts
权限: Role.ADMIN

完整端点:
- GET  /                       - 列表 (支持 severity / status 过滤)
- POST /                       - 手动创建告警
- POST /{alert_id}/acknowledge - 确认告警
- POST /{alert_id}/resolve     - 解决告警
- GET  /stats                  - 告警统计 (按级别 / 状态 / 来源分组)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_audit_service
from auth.rbac import Role, get_current_user_id, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from services.alert_service import (
    ALERT_SEVERITIES,
    ALERT_STATUSES,
    AlertService,
)
from services.audit_service import AuditService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/alerts",
    tags=["admin-alerts"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class AlertCreate(BaseModel):
    """创建告警请求"""

    severity: str = Field(..., description="告警级别: critical / warning / info")
    title: str = Field(..., description="告警标题")
    message: str = Field(..., description="告警消息内容")
    source: str = Field(default="manual", description="告警来源")
    metadata: Optional[dict] = Field(default=None, description="附加元数据")
    # 是否立即发送通知
    notify: bool = Field(default=True, description="是否立即发送通知")


# ============================================================
# 路由
# ============================================================


@router.get("/", response_model=Dict[str, Any])
async def list_alerts(
    request: Request,
    session: AsyncSession = Depends(get_db),
    severity: Optional[str] = Query(default=None, description="按级别过滤"),
    source: Optional[str] = Query(default=None, description="按来源过滤"),
    status_filter: Optional[str] = Query(
        default=None, alias="status", description="按状态过滤"
    ),
    page: int = Query(default=1, ge=1, description="页码 (从 1 开始)"),
    size: int = Query(default=20, ge=1, le=100, description="每页条数"),
):
    """分页查询告警列表 (支持 severity / source / status 过滤)"""
    if severity and severity not in ALERT_SEVERITIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无效的级别: {severity}, 可选: {ALERT_SEVERITIES}",
        )
    if status_filter and status_filter not in ALERT_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无效的状态: {status_filter}, 可选: {ALERT_STATUSES}",
        )
    tenant_id = get_current_tenant()
    service = AlertService(session)
    return await service.list_alerts(
        severity=severity,
        source=source,
        status=status_filter,
        page=page,
        size=size,
        tenant_id=tenant_id,
    )


@router.get("/stats", response_model=Dict[str, Any])
async def get_alert_stats(
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """告警统计 (按级别 / 状态 / 来源分组)"""
    tenant_id = get_current_tenant()
    service = AlertService(session)
    return await service.get_alert_stats(tenant_id=tenant_id)


@router.post(
    "/",
    response_model=Dict[str, Any],
    status_code=status.HTTP_201_CREATED,
)
async def create_alert(
    payload: AlertCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """手动创建告警

    创建后若 notify=True, 自动通过配置的通道发送通知。
    """
    tenant_id = get_current_tenant()
    service = AlertService(session)
    try:
        alert = await service.create_alert(
            severity=payload.severity,
            title=payload.title,
            message=payload.message,
            source=payload.source,
            metadata=payload.metadata,
            tenant_id=tenant_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    # 发送通知
    notify_results = None
    if payload.notify:
        notify_results = await service.send_alert(alert)

    await audit_service.log(
        actor_id=current_user_id,
        action="create_alert",
        details={
            "alert_id": alert.id,
            "severity": payload.severity,
            "source": payload.source,
        },
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()

    result = AlertService._alert_to_dict(alert)
    result["notify_results"] = notify_results
    return result


@router.post("/{alert_id}/acknowledge", response_model=Dict[str, Any])
async def acknowledge_alert(
    alert_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """确认告警 (active → acknowledged)"""
    tenant_id = get_current_tenant()
    service = AlertService(session)
    try:
        alert = await service.acknowledge_alert(
            alert_id, current_user_id, tenant_id=tenant_id
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await audit_service.log(
        actor_id=current_user_id,
        action="acknowledge_alert",
        details={"alert_id": alert_id},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return AlertService._alert_to_dict(alert)


@router.post("/{alert_id}/resolve", response_model=Dict[str, Any])
async def resolve_alert(
    alert_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """解决告警 (→ resolved)"""
    tenant_id = get_current_tenant()
    service = AlertService(session)
    try:
        alert = await service.resolve_alert(
            alert_id, current_user_id, tenant_id=tenant_id
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await audit_service.log(
        actor_id=current_user_id,
        action="resolve_alert",
        details={"alert_id": alert_id},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return AlertService._alert_to_dict(alert)
