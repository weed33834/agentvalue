"""Feature Flag Admin API (P3-2: 应用级功能开关, 对标 Langfuse Feature Flag)

路由前缀: /api/v1/admin/feature-flags
权限: Role.ADMIN (router 级 dependencies)

完整功能 (8 端点):
- GET    /                       - 列表 (支持 category 过滤)
- POST   /                       - 创建
- GET    /{key}                  - 详情
- PUT    /{key}                  - 更新
- DELETE /{key}                  - 删除
- POST   /{key}/toggle           - 启用/禁用切换
- GET    /{key}/check            - 检查状态 (query: tenant_id? / user_id?)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import AppState, get_app_state
from auth.rbac import Role, require_role
from core.database import get_db
from core.feature_flag import FeatureFlagService
from models.feature_flag import FeatureFlag

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/feature-flags",
    tags=["admin-feature-flags"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class FeatureFlagCreate(BaseModel):
    """创建 Feature Flag"""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(
        ..., min_length=1, max_length=64, description="业务 key (主键, 不可改)"
    )
    description: str = Field(default="", max_length=256, description="用途描述")
    enabled: bool = Field(default=False, description="全局开关")
    rollout_percentage: int = Field(
        default=0, ge=0, le=100, description="灰度百分比 0-100"
    )
    target_tenant_ids: List[str] = Field(
        default_factory=list, description="精确受众租户列表"
    )
    target_user_ids: List[str] = Field(
        default_factory=list, description="精确受众用户列表"
    )
    category: str = Field(default="general", description="分类: general/model/agent/feature")


class FeatureFlagUpdate(BaseModel):
    """更新 Feature Flag (所有字段可选, key 不可改)"""

    model_config = ConfigDict(extra="forbid")

    description: Optional[str] = Field(default=None, max_length=256)
    enabled: Optional[bool] = None
    rollout_percentage: Optional[int] = Field(default=None, ge=0, le=100)
    target_tenant_ids: Optional[List[str]] = None
    target_user_ids: Optional[List[str]] = None
    category: Optional[str] = None


class FeatureFlagToggle(BaseModel):
    """启用/禁用切换"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(..., description="目标状态")


class FeatureFlagCheckResponse(BaseModel):
    """检查结果"""

    enabled: bool
    reason: str
    bucket: Optional[int] = None
    percentage: Optional[int] = None


# ============================================================
# 工具函数
# ============================================================


def _entity_to_dict(entity: FeatureFlag) -> Dict[str, Any]:
    """FeatureFlag entity → dict (供 API 返回)"""
    return {
        "key": entity.key,
        "description": entity.description,
        "enabled": entity.enabled,
        "rollout_percentage": entity.rollout_percentage,
        "target_tenant_ids": entity.target_tenant_ids or [],
        "target_user_ids": entity.target_user_ids or [],
        "category": entity.category,
        "created_at": entity.created_at.isoformat() if entity.created_at else None,
        "updated_at": entity.updated_at.isoformat() if entity.updated_at else None,
    }


def _get_service(app_state: AppState) -> FeatureFlagService:
    """从 AppState 获取 FeatureFlagService"""
    service = getattr(app_state, "feature_flag_service", None)
    if service is None:
        # 兜底: 测试场景可能未设置, 用 AsyncSessionLocal 构造一个临时实例
        from core.database import AsyncSessionLocal

        service = FeatureFlagService(AsyncSessionLocal)
    return service


# ============================================================
# 路由
# ============================================================


@router.get("", response_model=Dict[str, Any])
async def list_feature_flags(
    request: Request,
    category: Optional[str] = Query(None, description="按 category 过滤"),
    app_state: AppState = Depends(get_app_state),
):
    """列出所有 Feature Flag (支持 category 过滤)"""
    service = _get_service(app_state)
    flags = await service.list_flags(category=category)
    return {
        "items": [_entity_to_dict(f) for f in flags],
        "total": len(flags),
    }


@router.post(
    "",
    response_model=Dict[str, Any],
    status_code=status.HTTP_201_CREATED,
)
async def create_feature_flag(
    payload: FeatureFlagCreate,
    request: Request,
    app_state: AppState = Depends(get_app_state),
):
    """创建 Feature Flag"""
    service = _get_service(app_state)
    try:
        flag = await service.create_flag(
            key=payload.key,
            description=payload.description,
            enabled=payload.enabled,
            rollout_percentage=payload.rollout_percentage,
            target_tenant_ids=payload.target_tenant_ids,
            target_user_ids=payload.target_user_ids,
            category=payload.category,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )
    return _entity_to_dict(flag)


@router.get("/{key}", response_model=Dict[str, Any])
async def get_feature_flag(
    key: str,
    request: Request,
    app_state: AppState = Depends(get_app_state),
):
    """获取 Feature Flag 详情"""
    service = _get_service(app_state)
    flag = await service.get_flag(key)
    if flag is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"feature flag {key!r} 不存在",
        )
    return _entity_to_dict(flag)


@router.put("/{key}", response_model=Dict[str, Any])
async def update_feature_flag(
    key: str,
    payload: FeatureFlagUpdate,
    request: Request,
    app_state: AppState = Depends(get_app_state),
):
    """更新 Feature Flag (任意字段, key 不可改)"""
    service = _get_service(app_state)
    # 只传非 None 的字段
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="未提供任何更新字段",
        )
    try:
        flag = await service.update_flag(key, **fields)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )
    if flag is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"feature flag {key!r} 不存在",
        )
    return _entity_to_dict(flag)


@router.delete("/{key}", response_model=Dict[str, Any])
async def delete_feature_flag(
    key: str,
    request: Request,
    app_state: AppState = Depends(get_app_state),
):
    """删除 Feature Flag"""
    service = _get_service(app_state)
    deleted = await service.delete_flag(key)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"feature flag {key!r} 不存在",
        )
    return {"deleted": True, "key": key}


@router.post("/{key}/toggle", response_model=Dict[str, Any])
async def toggle_feature_flag(
    key: str,
    payload: FeatureFlagToggle,
    request: Request,
    app_state: AppState = Depends(get_app_state),
):
    """启用/禁用切换"""
    service = _get_service(app_state)
    flag = await service.toggle_flag(key, payload.enabled)
    if flag is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"feature flag {key!r} 不存在",
        )
    return {
        "key": flag.key,
        "enabled": flag.enabled,
    }


@router.get("/{key}/check", response_model=FeatureFlagCheckResponse)
async def check_feature_flag(
    key: str,
    request: Request,
    tenant_id: Optional[str] = Query(None, description="租户 ID"),
    user_id: Optional[str] = Query(None, description="用户 ID"),
    app_state: AppState = Depends(get_app_state),
):
    """检查 Feature Flag 是否启用

    返回 {enabled: bool, reason: str, bucket?: int, percentage?: int}
    reason 取值:
    - flag_not_found: flag 不存在
    - flag_disabled: enabled=False
    - target_user_hit: 命中 target_user_ids
    - target_tenant_hit: 命中 target_tenant_ids
    - rollout_percentage_hit: 百分比命中
    - rollout_percentage_miss: 百分比未命中 (附带 bucket/percentage)
    - default_off: 默认 False
    """
    service = _get_service(app_state)
    result = await service.explain(key, tenant_id=tenant_id, user_id=user_id)
    return FeatureFlagCheckResponse(**result)
