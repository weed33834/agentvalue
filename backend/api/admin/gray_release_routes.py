"""灰度发布 / 蓝绿部署 Admin API

路由前缀: /api/v1/admin/gray-release
权限: Role.ADMIN

完整端点:
- POST   /releases                 - 创建灰度发布
- GET    /releases                 - 灰度发布列表 (支持 status 过滤)
- GET    /releases/{id}            - 灰度发布详情
- PUT    /releases/{id}            - 更新 (流量百分比 / 状态 / 配置等)
- POST   /releases/{id}/start      - 启动灰度
- POST   /releases/{id}/pause      - 暂停灰度
- POST   /releases/{id}/complete   - 完成 (100% 切换)
- POST   /releases/{id}/rollback   - 回滚
- GET    /agents/{agent_id}/active - 获取 Agent 当前灰度发布
- DELETE /releases/{id}            - 删除灰度发布
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_audit_service
from auth.rbac import Role, get_current_user_id, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from services.audit_service import AuditService
from services.gray_release_service import GrayReleaseService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/gray-release",
    tags=["admin-gray-release"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class ReleaseCreate(BaseModel):
    """创建灰度发布请求"""

    name: str = Field(..., min_length=1, max_length=128, description="灰度发布名称")
    agent_id: int = Field(..., description="Agent 预设 ID")
    version_id: int = Field(..., description="新版本 ID (灰度目标版本)")
    release_type: str = Field(
        default="canary", description="发布类型: canary / blue_green / rolling"
    )
    traffic_percentage: int = Field(
        default=0, ge=0, le=100, description="灰度流量百分比 (0-100)"
    )
    config: Optional[dict] = Field(
        default=None, description="灰度配置 (如 blue_green 的版本映射)"
    )
    description: Optional[str] = Field(default=None, description="备注 / 描述")


class ReleaseUpdate(BaseModel):
    """更新灰度发布请求"""

    traffic_percentage: Optional[int] = Field(
        default=None, ge=0, le=100, description="灰度流量百分比 (0-100)"
    )
    status: Optional[str] = Field(default=None, description="发布状态")
    config: Optional[dict] = Field(default=None, description="灰度配置")
    name: Optional[str] = Field(
        default=None, max_length=128, description="灰度发布名称"
    )
    description: Optional[str] = Field(default=None, description="备注 / 描述")


# ============================================================
# 路由
# ============================================================


@router.post(
    "/releases", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED
)
async def create_release(
    payload: ReleaseCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """创建灰度发布策略 (初始状态 draft)"""
    tenant_id = get_current_tenant()
    service = GrayReleaseService(session)
    try:
        release = await service.create_release(
            name=payload.name,
            agent_id=payload.agent_id,
            version_id=payload.version_id,
            release_type=payload.release_type,
            traffic_percentage=payload.traffic_percentage,
            config=payload.config,
            tenant_id=tenant_id,
            description=payload.description,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await audit_service.log(
        actor_id=current_user_id,
        action="create_gray_release",
        details={
            "release_id": release.id,
            "agent_id": payload.agent_id,
            "version_id": payload.version_id,
            "release_type": payload.release_type,
        },
        ip_address=request.headers.get("x-forwarded-for"),
    )
    result = GrayReleaseService._release_to_dict(release)
    await session.commit()
    return result


@router.get("/releases", response_model=Dict[str, Any])
async def list_releases(
    request: Request,
    status_filter: Optional[str] = Query(
        default=None, alias="status", description="按状态过滤"
    ),
    agent_id: Optional[int] = Query(default=None, description="按 Agent 过滤"),
    session: AsyncSession = Depends(get_db),
):
    """列出灰度发布 (按创建时间倒序)"""
    tenant_id = get_current_tenant()
    service = GrayReleaseService(session)
    releases = await service.list_releases(
        status=status_filter, tenant_id=tenant_id, agent_id=agent_id
    )
    return {"releases": releases, "total": len(releases)}


@router.get("/releases/{release_id}", response_model=Dict[str, Any])
async def get_release(
    release_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """获取灰度发布详情"""
    tenant_id = get_current_tenant()
    service = GrayReleaseService(session)
    release = await service.get_release(release_id, tenant_id=tenant_id)
    if release is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"灰度发布 {release_id} 不存在",
        )
    return GrayReleaseService._release_to_dict(release)


@router.put("/releases/{release_id}", response_model=Dict[str, Any])
async def update_release(
    release_id: int,
    payload: ReleaseUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """更新灰度发布 (流量百分比 / 状态 / 配置等)"""
    tenant_id = get_current_tenant()
    service = GrayReleaseService(session)
    try:
        release = await service.update_release(
            release_id,
            traffic_percentage=payload.traffic_percentage,
            status=payload.status,
            config=payload.config,
            name=payload.name,
            description=payload.description,
            tenant_id=tenant_id,
        )
    except ValueError as e:
        # 区分不存在 (404) 与状态非法 (400)
        msg = str(e)
        if "不存在" in msg:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)

    await audit_service.log(
        actor_id=current_user_id,
        action="update_gray_release",
        details={
            "release_id": release_id,
            "traffic_percentage": release.traffic_percentage,
            "status": release.status,
        },
        ip_address=request.headers.get("x-forwarded-for"),
    )
    result = GrayReleaseService._release_to_dict(release)
    await session.commit()
    return result


@router.post("/releases/{release_id}/start", response_model=Dict[str, Any])
async def start_release(
    release_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """启动灰度发布 (draft / paused → active)"""
    tenant_id = get_current_tenant()
    service = GrayReleaseService(session)
    try:
        release = await service.start_release(release_id, tenant_id=tenant_id)
    except ValueError as e:
        msg = str(e)
        if "不存在" in msg:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)

    result = GrayReleaseService._release_to_dict(release)
    await audit_service.log(
        actor_id=current_user_id,
        action="start_gray_release",
        details={"release_id": release_id},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return result


@router.post("/releases/{release_id}/pause", response_model=Dict[str, Any])
async def pause_release(
    release_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """暂停灰度发布 (active → paused)"""
    tenant_id = get_current_tenant()
    service = GrayReleaseService(session)
    try:
        release = await service.pause_release(release_id, tenant_id=tenant_id)
    except ValueError as e:
        msg = str(e)
        if "不存在" in msg:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)

    await audit_service.log(
        actor_id=current_user_id,
        action="pause_gray_release",
        details={"release_id": release_id},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    result = GrayReleaseService._release_to_dict(release)
    await session.commit()
    return result


@router.post("/releases/{release_id}/complete", response_model=Dict[str, Any])
async def complete_release(
    release_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """完成灰度发布 (100% 流量切换到新版本)"""
    tenant_id = get_current_tenant()
    service = GrayReleaseService(session)
    try:
        release = await service.complete_release(release_id, tenant_id=tenant_id)
    except ValueError as e:
        msg = str(e)
        if "不存在" in msg:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)

    await audit_service.log(
        actor_id=current_user_id,
        action="complete_gray_release",
        details={"release_id": release_id},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    result = GrayReleaseService._release_to_dict(release)
    await session.commit()
    return result


@router.post("/releases/{release_id}/rollback", response_model=Dict[str, Any])
async def rollback_release(
    release_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """回滚灰度发布 (流量切回基准版本)"""
    tenant_id = get_current_tenant()
    service = GrayReleaseService(session)
    try:
        release = await service.rollback_release(release_id, tenant_id=tenant_id)
    except ValueError as e:
        msg = str(e)
        if "不存在" in msg:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)

    await audit_service.log(
        actor_id=current_user_id,
        action="rollback_gray_release",
        details={"release_id": release_id},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    result = GrayReleaseService._release_to_dict(release)
    await session.commit()
    return result


@router.get("/agents/{agent_id}/active", response_model=Dict[str, Any])
async def get_active_release(
    agent_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """获取 Agent 当前进行中的灰度发布 (active / paused)"""
    tenant_id = get_current_tenant()
    service = GrayReleaseService(session)
    release = await service.get_active_release(agent_id, tenant_id=tenant_id)
    if release is None:
        return {"agent_id": agent_id, "active_release": None}
    return {
        "agent_id": agent_id,
        "active_release": GrayReleaseService._release_to_dict(release),
    }


@router.delete("/releases/{release_id}", response_model=Dict[str, Any])
async def delete_release(
    release_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """删除灰度发布 (仅 draft / rolled_back / completed 可删除)"""
    tenant_id = get_current_tenant()
    service = GrayReleaseService(session)
    try:
        await service.delete_release(release_id, tenant_id=tenant_id)
    except ValueError as e:
        msg = str(e)
        if "不存在" in msg:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)

    await audit_service.log(
        actor_id=current_user_id,
        action="delete_gray_release",
        details={"release_id": release_id},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return {"deleted": True, "release_id": release_id}
