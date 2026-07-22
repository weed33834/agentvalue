"""发布管理 Admin API

路由前缀: /api/v1/admin/publish
权限: Role.ADMIN

完整端点:
- POST   /{agent_id}/feishu    - 发布到飞书
- POST   /{agent_id}/wechat    - 发布到微信
- POST   /{agent_id}/dingtalk  - 发布到钉钉
- POST   /{agent_id}/web       - 发布Web嵌入
- POST   /{agent_id}/api       - 发布API接入
- DELETE /{agent_id}/{channel} - 取消发布
- GET    /{agent_id}/status    - 获取发布状态
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_audit_service
from auth.rbac import Role, get_current_user_id, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from services.audit_service import AuditService
from services.publish_service import PUBLISH_CHANNELS, PublishService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/publish",
    tags=["admin-publish"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class PublishConfig(BaseModel):
    """渠道发布配置 (所有字段可选, 按渠道按需提供)"""

    # 飞书
    app_id: Optional[str] = Field(default=None, description="飞书应用 App ID")
    verification_token: Optional[str] = Field(
        default=None, description="飞书验证 Token"
    )
    # 微信
    # app_id 复用
    # 钉钉
    robot_name: Optional[str] = Field(default=None, description="钉钉机器人名称")
    # Web
    domain: Optional[str] = Field(default=None, description="Web 嵌入域名")
    theme: Optional[str] = Field(default=None, description="Web 主题: light / dark")
    width: Optional[str] = Field(default=None, description="iframe 宽度")
    height: Optional[str] = Field(default=None, description="iframe 高度")
    # API
    rate_limit: Optional[int] = Field(default=None, description="API 速率限制 (次/分钟)")


# ============================================================
# 路由
# ============================================================


def _config_to_dict(payload: Optional[PublishConfig]) -> dict:
    """将 PublishConfig 转为 dict (过滤 None 值)"""
    if payload is None:
        return {}
    return {k: v for k, v in payload.model_dump().items() if v is not None}


async def _resolve_version_id(session: AsyncSession, agent_id: int, *, tenant_id: str = "default") -> int:
    """获取 Agent 的最新版本 ID (用于发布)

    优先取 published 状态的最新版本, 否则取最新版本。
    """
    from sqlalchemy import select

    from models.agent_version import AgentVersion

    # 优先取 published 状态的最新版本
    result = await session.execute(
        select(AgentVersion)
        .where(
            AgentVersion.agent_id == agent_id,
            AgentVersion.status == "published",
            AgentVersion.tenant_id == tenant_id,
        )
        .order_by(AgentVersion.version_number.desc())
        .limit(1)
    )
    version = result.scalar_one_or_none()
    if version is not None:
        return version.id

    # 否则取最新版本
    result = await session.execute(
        select(AgentVersion)
        .where(
            AgentVersion.agent_id == agent_id,
            AgentVersion.tenant_id == tenant_id,
        )
        .order_by(AgentVersion.version_number.desc())
        .limit(1)
    )
    version = result.scalar_one_or_none()
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id} 没有任何版本, 请先创建版本",
        )
    return version.id


@router.post("/{agent_id}/feishu", response_model=Dict[str, Any])
async def publish_to_feishu(
    agent_id: int,
    payload: Optional[PublishConfig] = None,
    request: Request = None,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """发布到飞书"""
    tenant_id = get_current_tenant()
    version_id = await _resolve_version_id(session, agent_id, tenant_id=tenant_id)
    service = PublishService(session)
    try:
        result = await service.publish_to_feishu(
            agent_id, version_id, _config_to_dict(payload), tenant_id=tenant_id
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await audit_service.log(
        actor_id=current_user_id,
        action="publish_to_feishu",
        details={"agent_id": agent_id, "version_id": version_id},
        ip_address=request.headers.get("x-forwarded-for") if request else None,
    )
    await session.commit()
    return result


@router.post("/{agent_id}/wechat", response_model=Dict[str, Any])
async def publish_to_wechat(
    agent_id: int,
    payload: Optional[PublishConfig] = None,
    request: Request = None,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """发布到微信"""
    tenant_id = get_current_tenant()
    version_id = await _resolve_version_id(session, agent_id, tenant_id=tenant_id)
    service = PublishService(session)
    try:
        result = await service.publish_to_wechat(
            agent_id, version_id, _config_to_dict(payload), tenant_id=tenant_id
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await audit_service.log(
        actor_id=current_user_id,
        action="publish_to_wechat",
        details={"agent_id": agent_id, "version_id": version_id},
        ip_address=request.headers.get("x-forwarded-for") if request else None,
    )
    await session.commit()
    return result


@router.post("/{agent_id}/dingtalk", response_model=Dict[str, Any])
async def publish_to_dingtalk(
    agent_id: int,
    payload: Optional[PublishConfig] = None,
    request: Request = None,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """发布到钉钉"""
    tenant_id = get_current_tenant()
    version_id = await _resolve_version_id(session, agent_id, tenant_id=tenant_id)
    service = PublishService(session)
    try:
        result = await service.publish_to_dingtalk(
            agent_id, version_id, _config_to_dict(payload), tenant_id=tenant_id
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await audit_service.log(
        actor_id=current_user_id,
        action="publish_to_dingtalk",
        details={"agent_id": agent_id, "version_id": version_id},
        ip_address=request.headers.get("x-forwarded-for") if request else None,
    )
    await session.commit()
    return result


@router.post("/{agent_id}/web", response_model=Dict[str, Any])
async def publish_to_web(
    agent_id: int,
    payload: Optional[PublishConfig] = None,
    request: Request = None,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """发布 Web 嵌入"""
    tenant_id = get_current_tenant()
    version_id = await _resolve_version_id(session, agent_id, tenant_id=tenant_id)
    service = PublishService(session)
    try:
        result = await service.publish_to_web(
            agent_id, version_id, _config_to_dict(payload), tenant_id=tenant_id
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await audit_service.log(
        actor_id=current_user_id,
        action="publish_to_web",
        details={"agent_id": agent_id, "version_id": version_id},
        ip_address=request.headers.get("x-forwarded-for") if request else None,
    )
    await session.commit()
    return result


@router.post("/{agent_id}/api", response_model=Dict[str, Any])
async def publish_to_api(
    agent_id: int,
    payload: Optional[PublishConfig] = None,
    request: Request = None,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """发布 API 接入"""
    tenant_id = get_current_tenant()
    version_id = await _resolve_version_id(session, agent_id, tenant_id=tenant_id)
    service = PublishService(session)
    try:
        result = await service.publish_to_api(
            agent_id, version_id, _config_to_dict(payload), tenant_id=tenant_id
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await audit_service.log(
        actor_id=current_user_id,
        action="publish_to_api",
        details={"agent_id": agent_id, "version_id": version_id},
        ip_address=request.headers.get("x-forwarded-for") if request else None,
    )
    await session.commit()
    return result


@router.delete("/{agent_id}/{channel}", response_model=Dict[str, Any])
async def unpublish(
    agent_id: int,
    channel: str,
    request: Request = None,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """取消发布"""
    if channel not in PUBLISH_CHANNELS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的渠道: {channel}, 可选: {PUBLISH_CHANNELS}",
        )

    tenant_id = get_current_tenant()
    service = PublishService(session)
    try:
        result = await service.unpublish(agent_id, channel, tenant_id=tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    await audit_service.log(
        actor_id=current_user_id,
        action="unpublish",
        details={"agent_id": agent_id, "channel": channel},
        ip_address=request.headers.get("x-forwarded-for") if request else None,
    )
    await session.commit()
    return result


@router.get("/{agent_id}/status", response_model=Dict[str, Any])
async def get_publish_status(
    agent_id: int,
    request: Request = None,
    session: AsyncSession = Depends(get_db),
):
    """获取所有渠道发布状态"""
    tenant_id = get_current_tenant()
    service = PublishService(session)
    return await service.get_publish_status(agent_id, tenant_id=tenant_id)
