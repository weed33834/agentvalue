"""模型负载均衡 Admin API

路由前缀: /api/v1/admin/model-lb
权限: Role.ADMIN (router 级 dependencies)

完整端点 (10 个):
- POST   /instances                  - 创建实例
- GET    /instances                  - 列表
- GET    /instances/{id}             - 详情
- PUT    /instances/{id}             - 更新
- DELETE /instances/{id}             - 删除
- POST   /instances/{id}/health-check - 健康检查
- POST   /health-check-all           - 全部健康检查
- GET    /configs                    - 负载均衡配置列表
- POST   /configs                    - 创建配置
- PUT    /configs/{id}               - 更新配置
- DELETE /configs/{id}               - 删除配置
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.rbac import Role, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from services.model_load_balancer_service import ModelLoadBalancerService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/model-lb",
    tags=["admin-model-lb"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class InstanceCreate(BaseModel):
    """创建模型实例请求"""

    name: str = Field(..., min_length=1, max_length=128, description="实例名称")
    provider: str = Field(
        ..., description="Provider 类型: openai|local|azure|anthropic"
    )
    model_name: str = Field(..., min_length=1, description="模型名称")
    base_url: Optional[str] = Field(default=None, description="API base URL")
    api_key_ref: Optional[str] = Field(
        default=None,
        description="API Key 引用（如 env:OPENAI_API_KEY），不存明文",
    )
    weight: int = Field(default=1, ge=1, description="权重")
    max_concurrent: int = Field(default=10, ge=1, description="最大并发数")


class InstanceUpdate(BaseModel):
    """更新模型实例请求（所有字段可选）"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    provider: Optional[str] = None
    model_name: Optional[str] = None
    base_url: Optional[str] = None
    api_key_ref: Optional[str] = None
    weight: Optional[int] = Field(default=None, ge=1)
    max_concurrent: Optional[int] = Field(default=None, ge=1)
    enabled: Optional[bool] = None


class ConfigInstanceItem(BaseModel):
    """负载均衡配置中的实例项"""

    instance_id: int = Field(..., description="实例 ID")
    weight: int = Field(default=1, ge=1, description="权重")


class ConfigCreate(BaseModel):
    """创建负载均衡配置请求"""

    name: str = Field(..., min_length=1, max_length=128, description="配置名称")
    strategy: str = Field(
        ...,
        description="策略: round_robin|weighted|least_connections|latency_aware",
    )
    instances: List[Dict[str, Any]] = Field(
        ..., min_length=1, description="实例列表 [{instance_id, weight}]"
    )
    enabled: bool = Field(default=True, description="是否启用")


class ConfigUpdate(BaseModel):
    """更新负载均衡配置请求（所有字段可选）"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    strategy: Optional[str] = None
    instances: Optional[List[Dict[str, Any]]] = Field(default=None, min_length=1)
    enabled: Optional[bool] = None


# ============================================================
# 实例路由
# ============================================================


@router.post(
    "/instances",
    response_model=Dict[str, Any],
    status_code=status.HTTP_201_CREATED,
)
async def create_instance(
    payload: InstanceCreate,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """创建模型实例"""
    service = ModelLoadBalancerService(session)
    try:
        result = await service.create_instance(
            tenant_id=tenant_id,
            name=payload.name,
            provider=payload.provider,
            model_name=payload.model_name,
            base_url=payload.base_url,
            api_key_ref=payload.api_key_ref,
            weight=payload.weight,
            max_concurrent=payload.max_concurrent,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    await session.commit()
    return result


@router.get("/instances", response_model=Dict[str, Any])
async def list_instances(
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
    enabled_only: bool = False,
):
    """列出当前租户的模型实例"""
    service = ModelLoadBalancerService(session)
    items = await service.list_instances(tenant_id, enabled_only=enabled_only)
    return {"tenant_id": tenant_id, "items": items, "total": len(items)}


@router.get("/instances/{instance_id}", response_model=Dict[str, Any])
async def get_instance(
    instance_id: int,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """获取模型实例详情"""
    service = ModelLoadBalancerService(session)
    result = await service.get_instance(instance_id, tenant_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"模型实例 {instance_id} 不存在",
        )
    return result


@router.put("/instances/{instance_id}", response_model=Dict[str, Any])
async def update_instance(
    instance_id: int,
    payload: InstanceUpdate,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """更新模型实例"""
    update_data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="未提供任何更新字段",
        )
    service = ModelLoadBalancerService(session)
    result = await service.update_instance(instance_id, tenant_id, **update_data)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"模型实例 {instance_id} 不存在",
        )
    await session.commit()
    return result


@router.delete("/instances/{instance_id}", response_model=Dict[str, Any])
async def delete_instance(
    instance_id: int,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """删除模型实例"""
    service = ModelLoadBalancerService(session)
    deleted = await service.delete_instance(instance_id, tenant_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"模型实例 {instance_id} 不存在",
        )
    await session.commit()
    return {"deleted": True, "instance_id": instance_id}


@router.post("/instances/{instance_id}/health-check", response_model=Dict[str, Any])
async def health_check_instance(
    instance_id: int,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """检查单个实例健康状态

    发送请求到 /models 端点，记录延迟与状态，更新 health_status 和 avg_latency_ms。
    """
    service = ModelLoadBalancerService()
    try:
        result = await service.health_check(instance_id, tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    return result


@router.post("/health-check-all", response_model=Dict[str, Any])
async def health_check_all(
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """检查当前租户所有启用实例的健康状态"""
    service = ModelLoadBalancerService()
    return await service.health_check_all(tenant_id)


# ============================================================
# 负载均衡配置路由
# ============================================================


@router.get("/configs", response_model=Dict[str, Any])
async def list_lb_configs(
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
    enabled_only: bool = False,
):
    """列出当前租户的负载均衡配置"""
    service = ModelLoadBalancerService(session)
    items = await service.list_lb_configs(tenant_id, enabled_only=enabled_only)
    return {"tenant_id": tenant_id, "items": items, "total": len(items)}


@router.post(
    "/configs",
    response_model=Dict[str, Any],
    status_code=status.HTTP_201_CREATED,
)
async def create_lb_config(
    payload: ConfigCreate,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """创建负载均衡配置"""
    service = ModelLoadBalancerService(session)
    try:
        result = await service.create_lb_config(
            tenant_id=tenant_id,
            name=payload.name,
            strategy=payload.strategy,
            instances=payload.instances,
            enabled=payload.enabled,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    await session.commit()
    return result


@router.put("/configs/{config_id}", response_model=Dict[str, Any])
async def update_lb_config(
    config_id: int,
    payload: ConfigUpdate,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """更新负载均衡配置"""
    update_data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="未提供任何更新字段",
        )
    service = ModelLoadBalancerService(session)
    result = await service.update_lb_config(config_id, tenant_id, **update_data)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"负载均衡配置 {config_id} 不存在",
        )
    await session.commit()
    return result


@router.delete("/configs/{config_id}", response_model=Dict[str, Any])
async def delete_lb_config(
    config_id: int,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """删除负载均衡配置"""
    service = ModelLoadBalancerService(session)
    deleted = await service.delete_lb_config(config_id, tenant_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"负载均衡配置 {config_id} 不存在",
        )
    await session.commit()
    return {"deleted": True, "config_id": config_id}
