"""多环境管理 Admin API

路由前缀: /api/v1/admin/environments
权限: Role.ADMIN

完整端点:
- POST   ""               - 创建环境
- GET    ""               - 环境列表
- GET    /{id}            - 环境详情
- PUT    /{id}            - 更新环境
- DELETE /{id}            - 删除环境 (不允许删除默认环境)
- GET    /{id}/config     - 获取合并后配置
- POST   /{id}/deploy     - 部署 Agent 到环境
- POST   /{id}/undeploy   - 取消部署
- GET    /{id}/deployments - 部署列表
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_audit_service
from auth.rbac import Role, get_current_user_id, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from services.audit_service import AuditService
from services.environment_service import EnvironmentService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/environments",
    tags=["admin-environments"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class EnvironmentCreate(BaseModel):
    """创建环境请求"""

    name: str = Field(
        ...,
        min_length=1,
        max_length=32,
        description="环境名称 (dev/staging/prod/custom)",
    )
    display_name: str = Field(default="", max_length=64, description="展示名称")
    description: Optional[str] = Field(default=None, description="环境描述")
    config: Optional[dict] = Field(default=None, description="环境级配置覆盖")
    variables: Optional[dict] = Field(default=None, description="环境变量覆盖")
    is_default: bool = Field(default=False, description="是否默认环境")


class EnvironmentUpdate(BaseModel):
    """更新环境请求"""

    config: Optional[dict] = Field(default=None, description="环境级配置覆盖")
    variables: Optional[dict] = Field(default=None, description="环境变量覆盖")
    description: Optional[str] = Field(default=None, description="环境描述")
    display_name: Optional[str] = Field(
        default=None, max_length=64, description="展示名称"
    )


class DeployRequest(BaseModel):
    """部署 Agent 到环境请求"""

    agent_id: int = Field(..., description="Agent 预设 ID")
    version_id: int = Field(..., description="版本 ID")
    config_snapshot: Optional[dict] = Field(default=None, description="部署时配置快照")


class UndeployRequest(BaseModel):
    """取消部署请求"""

    agent_id: int = Field(..., description="Agent 预设 ID")


# ============================================================
# 路由
# ============================================================


@router.post("", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_environment(
    payload: EnvironmentCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """创建环境"""
    tenant_id = get_current_tenant()
    service = EnvironmentService(session)
    try:
        environment = await service.create_environment(
            name=payload.name,
            display_name=payload.display_name,
            description=payload.description,
            config=payload.config,
            variables=payload.variables,
            tenant_id=tenant_id,
            is_default=payload.is_default,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await audit_service.log(
        actor_id=current_user_id,
        action="create_environment",
        details={"env_id": environment.id, "name": payload.name},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    result = EnvironmentService._environment_to_dict(environment)
    await session.commit()
    return result


@router.get("", response_model=Dict[str, Any])
async def list_environments(
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """列出所有环境"""
    tenant_id = get_current_tenant()
    service = EnvironmentService(session)
    environments = await service.list_environments(tenant_id=tenant_id)
    return {"environments": environments, "total": len(environments)}


@router.get("/{env_id}", response_model=Dict[str, Any])
async def get_environment(
    env_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """获取环境详情"""
    tenant_id = get_current_tenant()
    service = EnvironmentService(session)
    environment = await service.get_environment(env_id, tenant_id=tenant_id)
    if environment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"环境 {env_id} 不存在",
        )
    return EnvironmentService._environment_to_dict(environment)


@router.put("/{env_id}", response_model=Dict[str, Any])
async def update_environment(
    env_id: int,
    payload: EnvironmentUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """更新环境 (配置 / 变量 / 描述 / 展示名称)"""
    tenant_id = get_current_tenant()
    service = EnvironmentService(session)
    try:
        environment = await service.update_environment(
            env_id,
            config=payload.config,
            variables=payload.variables,
            description=payload.description,
            display_name=payload.display_name,
            tenant_id=tenant_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    await audit_service.log(
        actor_id=current_user_id,
        action="update_environment",
        details={"env_id": env_id},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    result = EnvironmentService._environment_to_dict(environment)
    await session.commit()
    return result


@router.delete("/{env_id}", response_model=Dict[str, Any])
async def delete_environment(
    env_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """删除环境 (不允许删除默认环境)"""
    tenant_id = get_current_tenant()
    service = EnvironmentService(session)
    try:
        await service.delete_environment(env_id, tenant_id=tenant_id)
    except ValueError as e:
        msg = str(e)
        if "不存在" in msg:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)

    await audit_service.log(
        actor_id=current_user_id,
        action="delete_environment",
        details={"env_id": env_id},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return {"deleted": True, "env_id": env_id}


@router.get("/{env_id}/config", response_model=Dict[str, Any])
async def get_environment_config(
    env_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """获取环境合并后配置 (深度合并默认配置 + 环境覆盖)"""
    tenant_id = get_current_tenant()
    service = EnvironmentService(session)
    try:
        config = await service.get_environment_config(env_id, tenant_id=tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    return {"env_id": env_id, "config": config}


@router.post("/{env_id}/deploy", response_model=Dict[str, Any])
async def deploy_agent(
    env_id: int,
    payload: DeployRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """部署 Agent 版本到环境"""
    tenant_id = get_current_tenant()
    service = EnvironmentService(session)
    try:
        deployment = await service.deploy_agent(
            env_id,
            payload.agent_id,
            payload.version_id,
            config_snapshot=payload.config_snapshot,
            deployed_by=current_user_id,
            tenant_id=tenant_id,
        )
    except ValueError as e:
        msg = str(e)
        if "不存在" in msg:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)

    await audit_service.log(
        actor_id=current_user_id,
        action="deploy_agent_to_environment",
        details={
            "env_id": env_id,
            "agent_id": payload.agent_id,
            "version_id": payload.version_id,
        },
        ip_address=request.headers.get("x-forwarded-for"),
    )
    result = EnvironmentService._deployment_to_dict(deployment)
    await session.commit()
    return result


@router.post("/{env_id}/undeploy", response_model=Dict[str, Any])
async def undeploy_agent(
    env_id: int,
    payload: UndeployRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """取消 Agent 在环境的部署"""
    tenant_id = get_current_tenant()
    service = EnvironmentService(session)
    try:
        deployment = await service.undeploy_agent(
            env_id, payload.agent_id, tenant_id=tenant_id
        )
    except ValueError as e:
        msg = str(e)
        if "不存在" in msg or "无部署记录" in msg:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)

    await audit_service.log(
        actor_id=current_user_id,
        action="undeploy_agent_from_environment",
        details={"env_id": env_id, "agent_id": payload.agent_id},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    result = EnvironmentService._deployment_to_dict(deployment)
    await session.commit()
    return result


@router.get("/{env_id}/deployments", response_model=Dict[str, Any])
async def get_deployments(
    env_id: int,
    request: Request,
    agent_id: Optional[int] = None,
    session: AsyncSession = Depends(get_db),
):
    """获取环境的部署列表 (可按 agent_id 过滤)"""
    tenant_id = get_current_tenant()
    service = EnvironmentService(session)
    deployments = await service.get_deployments(
        env_id=env_id, agent_id=agent_id, tenant_id=tenant_id
    )
    return {"env_id": env_id, "deployments": deployments, "total": len(deployments)}
