"""Agent 版本管理 Admin API

路由前缀: /api/v1/admin/agents
权限: Role.ADMIN

完整端点:
- GET    /{agent_id}/versions                       - 列出版本
- POST   /{agent_id}/versions                       - 创建新版本
- GET    /{agent_id}/versions/{version_id}          - 版本详情
- POST   /{agent_id}/versions/{version_id}/publish  - 发布版本 (body: {"targets": ["feishu","web","api"]})
- POST   /{agent_id}/versions/{version_id}/archive  - 归档版本
- POST   /{agent_id}/rollback/{target_version}      - 回滚到指定版本
- GET    /{agent_id}/versions/{v1_id}/compare/{v2_id} - 对比版本
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_audit_service
from auth.rbac import Role, get_current_user_id, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from services.agent_version_service import AgentVersionService
from services.audit_service import AuditService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/agents",
    tags=["admin-agent-version"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class VersionCreate(BaseModel):
    """创建版本请求"""

    # model_config 在 Pydantic v2 中是保留属性, 用 alias 接收 JSON 中的 model_config 字段
    model_config = ConfigDict(populate_by_name=True)

    system_prompt: Optional[str] = Field(
        default=None, description="系统提示词 (None 时从 AgentPreset 继承)"
    )
    tools_config: Optional[list] = Field(
        default=None, description="工具配置 (None 时继承)"
    )
    model_cfg: Optional[dict] = Field(
        default=None, alias="model_config", description="模型配置 (None 时继承)"
    )
    temperature: int = Field(default=70, ge=0, le=100, description="温度 0-100")
    changelog: Optional[str] = Field(default=None, description="变更日志")


class PublishRequest(BaseModel):
    """发布版本请求"""

    targets: List[str] = Field(
        ..., description="目标渠道列表, 如 ['feishu','web','api']"
    )


# ============================================================
# 路由
# ============================================================


@router.get("/{agent_id}/versions", response_model=Dict[str, Any])
async def list_versions(
    agent_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """列出指定 Agent 的所有版本 (按版本号倒序)"""
    tenant_id = get_current_tenant()
    service = AgentVersionService(session)
    versions = await service.list_versions(agent_id, tenant_id=tenant_id)
    return {"agent_id": agent_id, "versions": versions, "total": len(versions)}


@router.post(
    "/{agent_id}/versions",
    response_model=Dict[str, Any],
    status_code=status.HTTP_201_CREATED,
)
async def create_version(
    agent_id: int,
    payload: VersionCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """创建新版本 (自动递增版本号)"""
    tenant_id = get_current_tenant()
    service = AgentVersionService(session)
    try:
        version = await service.create_version(
            agent_id,
            tenant_id=tenant_id,
            system_prompt=payload.system_prompt,
            tools_config=payload.tools_config,
            model_config=payload.model_cfg,
            temperature=payload.temperature,
            changelog=payload.changelog,
            created_by=current_user_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    await audit_service.log(
        actor_id=current_user_id,
        action="create_agent_version",
        details={
            "agent_id": agent_id,
            "version_number": version.version_number,
        },
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return AgentVersionService._version_to_dict(version)


@router.get("/{agent_id}/versions/{version_id}", response_model=Dict[str, Any])
async def get_version(
    agent_id: int,
    version_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """获取版本详情"""
    tenant_id = get_current_tenant()
    service = AgentVersionService(session)
    version = await service.get_version(version_id, tenant_id=tenant_id)
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"版本 {version_id} 不存在",
        )
    # 校验版本归属
    if version["agent_id"] != agent_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"版本 {version_id} 不属于 Agent {agent_id}",
        )
    return version


@router.post("/{agent_id}/versions/{version_id}/publish", response_model=Dict[str, Any])
async def publish_version(
    agent_id: int,
    version_id: int,
    payload: PublishRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """发布版本到指定渠道

    为每个目标渠道创建/更新发布记录, 并触发实际渠道发布逻辑。
    """
    tenant_id = get_current_tenant()
    service = AgentVersionService(session)
    # 先通过版本服务创建发布目标记录
    try:
        result = await service.publish_version(
            version_id, payload.targets, tenant_id=tenant_id
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    # 委托 PublishService 执行实际渠道发布
    publish_results: List[Dict[str, Any]] = []
    try:
        from services.publish_service import PublishService

        publish_service = PublishService(session)
        for channel in payload.targets:
            target = await service.get_publish_target(
                agent_id, channel, tenant_id=tenant_id
            )
            if target is not None:
                pub_result = await publish_service.publish(
                    agent_id=agent_id,
                    version_id=version_id,
                    channel=channel,
                    config={},
                    tenant_id=tenant_id,
                )
                publish_results.append(pub_result)
    except Exception as e:
        logger.warning("渠道发布执行失败: %s", e, exc_info=True)

    await audit_service.log(
        actor_id=current_user_id,
        action="publish_agent_version",
        details={
            "agent_id": agent_id,
            "version_id": version_id,
            "targets": payload.targets,
        },
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return {
        "version": result["version"],
        "targets": result["targets"],
        "publish_results": publish_results,
    }


@router.post("/{agent_id}/versions/{version_id}/archive", response_model=Dict[str, Any])
async def archive_version(
    agent_id: int,
    version_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """归档版本 (归档后不可再发布)"""
    tenant_id = get_current_tenant()
    service = AgentVersionService(session)
    try:
        version = await service.archive_version(version_id, tenant_id=tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await audit_service.log(
        actor_id=current_user_id,
        action="archive_agent_version",
        details={"agent_id": agent_id, "version_id": version_id},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return AgentVersionService._version_to_dict(version)


@router.post("/{agent_id}/rollback/{target_version}", response_model=Dict[str, Any])
async def rollback(
    agent_id: int,
    target_version: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """回滚到指定版本 (基于历史版本创建新版本)"""
    tenant_id = get_current_tenant()
    service = AgentVersionService(session)
    try:
        new_version = await service.rollback(
            agent_id, target_version, tenant_id=tenant_id, created_by=current_user_id
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    await audit_service.log(
        actor_id=current_user_id,
        action="rollback_agent_version",
        details={
            "agent_id": agent_id,
            "target_version": target_version,
            "new_version_number": new_version.version_number,
        },
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return AgentVersionService._version_to_dict(new_version)


@router.get(
    "/{agent_id}/versions/{v1_id}/compare/{v2_id}",
    response_model=Dict[str, Any],
)
async def compare_versions(
    agent_id: int,
    v1_id: int,
    v2_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """对比两个版本差异"""
    tenant_id = get_current_tenant()
    service = AgentVersionService(session)
    try:
        result = await service.compare_versions(v1_id, v2_id, tenant_id=tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    return result
