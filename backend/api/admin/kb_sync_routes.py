"""知识库自动同步 Admin API

路由前缀: /api/v1/admin/kb-sync
权限: Role.ADMIN (router 级 dependencies)

完整端点 (7 个):
- POST   /sources          - 创建数据源
- GET    /sources          - 列表
- GET    /sources/{id}     - 详情
- PUT    /sources/{id}     - 更新
- DELETE /sources/{id}     - 删除
- POST   /sources/{id}/sync - 手动触发同步
- GET    /sources/{id}/logs - 同步日志
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.rbac import Role, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from services.kb_sync_service import KbSyncService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/kb-sync",
    tags=["admin-kb-sync"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class SourceCreate(BaseModel):
    """创建数据源请求"""

    name: str = Field(..., min_length=1, max_length=128, description="数据源名称")
    source_type: str = Field(
        ..., description="数据源类型: local_dir|s3|url|database|git"
    )
    config: Dict[str, Any] = Field(
        ..., description="数据源配置（按类型不同结构不同）"
    )
    collection_name: str = Field(..., min_length=1, description="关联向量库 collection")
    sync_interval_minutes: int = Field(
        default=60, ge=0, description="同步间隔（分钟），0 表示仅手动同步"
    )
    enabled: bool = Field(default=True, description="是否启用")


class SourceUpdate(BaseModel):
    """更新数据源请求（所有字段可选）"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    source_type: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    collection_name: Optional[str] = None
    sync_interval_minutes: Optional[int] = Field(default=None, ge=0)
    enabled: Optional[bool] = None


# ============================================================
# 路由
# ============================================================


@router.post(
    "/sources", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED
)
async def create_source(
    payload: SourceCreate,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """创建数据源"""
    service = KbSyncService(session)
    try:
        result = await service.create_source(
            tenant_id=tenant_id,
            name=payload.name,
            source_type=payload.source_type,
            config=payload.config,
            collection_name=payload.collection_name,
            sync_interval_minutes=payload.sync_interval_minutes,
            enabled=payload.enabled,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    await session.commit()
    return result


@router.get("/sources", response_model=Dict[str, Any])
async def list_sources(
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
    enabled_only: bool = False,
):
    """列出当前租户的数据源"""
    service = KbSyncService(session)
    items = await service.list_sources(tenant_id, enabled_only=enabled_only)
    return {"tenant_id": tenant_id, "items": items, "total": len(items)}


@router.get("/sources/{source_id}", response_model=Dict[str, Any])
async def get_source(
    source_id: int,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """获取数据源详情"""
    service = KbSyncService(session)
    result = await service.get_source(source_id, tenant_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"数据源 {source_id} 不存在",
        )
    return result


@router.put("/sources/{source_id}", response_model=Dict[str, Any])
async def update_source(
    source_id: int,
    payload: SourceUpdate,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """更新数据源"""
    update_data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="未提供任何更新字段",
        )
    service = KbSyncService(session)
    result = await service.update_source(source_id, tenant_id, **update_data)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"数据源 {source_id} 不存在",
        )
    await session.commit()
    return result


@router.delete("/sources/{source_id}", response_model=Dict[str, Any])
async def delete_source(
    source_id: int,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """删除数据源"""
    service = KbSyncService(session)
    deleted = await service.delete_source(source_id, tenant_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"数据源 {source_id} 不存在",
        )
    await session.commit()
    return {"deleted": True, "source_id": source_id}


@router.post("/sources/{source_id}/sync", response_model=Dict[str, Any])
async def sync_source(
    source_id: int,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """手动触发数据源同步

    执行同步流程: 扫描数据源 -> 检测变更 -> 更新向量库 -> 记录日志
    """
    service = KbSyncService()
    try:
        result = await service.sync_source(source_id, tenant_id, sync_type="manual")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    return result


@router.get("/sources/{source_id}/logs", response_model=Dict[str, Any])
async def get_sync_logs(
    source_id: int,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页条数"),
):
    """同步日志列表（分页）"""
    service = KbSyncService(session)
    return await service.get_sync_logs(source_id, tenant_id, page=page, size=size)
