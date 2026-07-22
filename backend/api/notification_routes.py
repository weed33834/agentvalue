"""
站内通知 API Router

端点:
- GET    /api/v1/notifications                       通知列表(分页, 可筛选 unread)
- GET    /api/v1/notifications/unread-count           未读通知数量
- PUT    /api/v1/notifications/{notification_id}/read  标记单条已读
- PUT    /api/v1/notifications/read-all                全部标记已读
- DELETE /api/v1/notifications/{notification_id}       删除单条通知

权限:所有端点需要登录(EMPLOYEE/MANAGER/HR/ADMIN),仅能操作自己的通知。
事务边界由路由层控制(service 层不 commit)。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from auth.rbac import Role, get_current_user_id, require_role
from core.database import get_db
from services.notification_service import NotificationService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


def get_notification_service(
    session: AsyncSession = Depends(get_db),
) -> NotificationService:
    """通知服务依赖"""
    return NotificationService(session)


@router.get("")
async def list_notifications(
    request: Request,
    unread_only: bool = Query(False, description="仅返回未读通知"),
    page: int = Query(1, ge=1, description="页码(从 1 开始)"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    service: NotificationService = Depends(get_notification_service),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """获取当前用户的通知列表(分页, 可筛选 unread)"""
    user_id = await get_current_user_id(request)
    result = await service.list_notifications(
        user_id=user_id,
        unread_only=unread_only,
        page=page,
        page_size=page_size,
    )
    return result


@router.get("/unread-count")
async def get_unread_count(
    request: Request,
    service: NotificationService = Depends(get_notification_service),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """获取当前用户的未读通知数量"""
    user_id = await get_current_user_id(request)
    count = await service.get_unread_count(user_id)
    return {"unread_count": count}


@router.put("/{notification_id}/read")
async def mark_as_read(
    notification_id: str,
    request: Request,
    service: NotificationService = Depends(get_notification_service),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """标记单条通知为已读"""
    user_id = await get_current_user_id(request)
    ok = await service.mark_as_read(notification_id, user_id=user_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="通知不存在",
        )
    await service.session.commit()
    return {"status": "ok", "notification_id": notification_id}


@router.put("/read-all")
async def mark_all_as_read(
    request: Request,
    service: NotificationService = Depends(get_notification_service),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """标记当前用户所有未读通知为已读"""
    user_id = await get_current_user_id(request)
    count = await service.mark_all_as_read(user_id)
    await service.session.commit()
    return {"status": "ok", "marked_count": count}


@router.delete("/{notification_id}")
async def delete_notification(
    notification_id: str,
    request: Request,
    service: NotificationService = Depends(get_notification_service),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """删除单条通知"""
    user_id = await get_current_user_id(request)
    ok = await service.delete_notification(notification_id, user_id=user_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="通知不存在",
        )
    await service.session.commit()
    return {"status": "ok", "notification_id": notification_id}
