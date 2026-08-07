"""发布记录管理 Admin API (契约补齐)

路由前缀: /api/v1/admin/publish
权限: Role.ADMIN

背景:
    既有 publish_routes.py 提供的是「按渠道操作」端点
    (POST /{agent_id}/feishu、DELETE /{agent_id}/{channel} 等)，
    缺少跨 Agent 的发布记录列表 / 创建 / 更新，导致前端"发布运维"页整页 404。
    本模块补齐记录维度的管理端点，数据层复用 AgentPublishTarget，
    发布动作复用 PublishService，不重复实现渠道逻辑。

完整端点:
- GET  /                        - 发布记录列表 (跨 Agent 聚合，支持 agent_id/channel/status 过滤 + 分页)
- POST /                        - 创建发布记录 (body 指定 agent_id + channel，委托 PublishService 实际发布)
- PUT  /{publish_id}            - 更新发布记录 (渠道配置 / 状态)
- POST /{agent_id}/{channel}    - 通用渠道发布 (与既有 5 个静态渠道端点等价的动态入口)

注意:
    本 router 必须在 publish_routes 之后挂载。
    - PUT /{publish_id} 与既有 DELETE /{agent_id}/{channel} 不冲突 (方法 + 层级均不同)。
    - POST /{agent_id}/{channel} 注册在 publish_routes 的 5 个静态渠道路由之后，
      FastAPI 按注册顺序匹配，静态路径优先命中，本路由只兜底其余合法渠道。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin.publish_routes import _resolve_version_id
from api.deps import get_audit_service
from auth.rbac import Role, get_current_user_id, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from models.agent_version import AgentPublishTarget
from services.audit_service import AuditService
from services.publish_service import PUBLISH_CHANNELS, PublishService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/publish",
    tags=["admin-publish"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)

# 允许更新的发布状态
_ALLOWED_STATUS = {"pending", "published", "failed"}


# ============================================================
# Schemas
# ============================================================


class PublishRecordCreate(BaseModel):
    """创建发布记录请求"""

    agent_id: int = Field(..., description="Agent 预设 ID")
    channel: str = Field(
        ..., description="发布渠道: web / api / feishu / dingtalk / wechat"
    )
    version_id: Optional[int] = Field(
        default=None, description="版本 ID (不传则自动解析该 Agent 的最新可用版本)"
    )
    config: Optional[dict] = Field(default=None, description="渠道配置")


class ChannelPublishRequest(BaseModel):
    """通用渠道发布请求"""

    version_id: Optional[int] = Field(
        default=None, description="版本 ID (不传则自动解析该 Agent 的最新可用版本)"
    )
    config: Optional[dict] = Field(default=None, description="渠道配置")


class PublishRecordUpdate(BaseModel):
    """更新发布记录请求"""

    config: Optional[dict] = Field(default=None, description="渠道配置")
    status: Optional[str] = Field(
        default=None, description="发布状态: pending / published / failed"
    )


# ============================================================
# 工具函数
# ============================================================


def _serialize(target: AgentPublishTarget) -> Dict[str, Any]:
    """序列化发布记录"""
    return {
        "id": target.id,
        "tenant_id": target.tenant_id,
        "agent_id": target.agent_id,
        "version_id": target.version_id,
        "channel": target.channel,
        "config": target.config or {},
        "status": target.status,
        "published_at": (
            target.published_at.isoformat() if target.published_at else None
        ),
        "error_message": target.error_message,
    }


# ============================================================
# 路由
# ============================================================


@router.get("", response_model=Dict[str, Any])
async def list_publish_records(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    agent_id: Optional[int] = Query(default=None, description="按 Agent 过滤"),
    channel: Optional[str] = Query(default=None, description="按渠道过滤"),
    status_filter: Optional[str] = Query(
        default=None, alias="status", description="按状态过滤"
    ),
    session: AsyncSession = Depends(get_db),
):
    """发布记录列表 (跨 Agent 聚合)"""
    tenant_id = get_current_tenant()

    stmt = select(AgentPublishTarget).where(
        AgentPublishTarget.tenant_id == tenant_id
    )
    count_stmt = (
        select(func.count())
        .select_from(AgentPublishTarget)
        .where(AgentPublishTarget.tenant_id == tenant_id)
    )

    conditions = []
    if agent_id is not None:
        conditions.append(AgentPublishTarget.agent_id == agent_id)
    if channel:
        conditions.append(AgentPublishTarget.channel == channel)
    if status_filter:
        conditions.append(AgentPublishTarget.status == status_filter)

    for cond in conditions:
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    total = (await session.execute(count_stmt)).scalar_one()
    stmt = (
        stmt.order_by(AgentPublishTarget.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await session.execute(stmt)).scalars().all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_serialize(t) for t in rows],
    }


@router.post("", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_publish_record(
    payload: PublishRecordCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """创建发布记录 (委托 PublishService 执行实际渠道发布)"""
    if payload.channel not in PUBLISH_CHANNELS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"不支持的渠道: {payload.channel}，可选: {sorted(PUBLISH_CHANNELS)}",
        )

    tenant_id = get_current_tenant()
    # 未指定版本时，复用 publish_routes 的解析逻辑取最新可用版本
    version_id = payload.version_id
    if version_id is None:
        version_id = await _resolve_version_id(
            session, payload.agent_id, tenant_id=tenant_id
        )

    service = PublishService(session)
    try:
        result = await service.publish(
            payload.agent_id,
            version_id,
            payload.channel,
            payload.config or {},
            tenant_id=tenant_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    await audit_service.log(
        actor_id=current_user_id,
        action="create_publish_record",
        details={"agent_id": payload.agent_id, "channel": payload.channel},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return result


@router.put("/{publish_id}", response_model=Dict[str, Any])
async def update_publish_record(
    publish_id: int,
    payload: PublishRecordUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """更新发布记录 (渠道配置 / 状态)"""
    tenant_id = get_current_tenant()
    target = (
        await session.execute(
            select(AgentPublishTarget).where(
                AgentPublishTarget.id == publish_id,
                AgentPublishTarget.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="发布记录不存在"
        )

    changed: Dict[str, Any] = {}
    if payload.config is not None:
        target.config = payload.config
        changed["config"] = True
    if payload.status is not None:
        if payload.status not in _ALLOWED_STATUS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"非法状态: {payload.status}，可选: {sorted(_ALLOWED_STATUS)}",
            )
        target.status = payload.status
        changed["status"] = payload.status

    if not changed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="未提供任何更新字段",
        )

    await session.flush()
    await audit_service.log(
        actor_id=current_user_id,
        action="update_publish_record",
        details={"publish_id": publish_id, "changed": changed},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    await session.refresh(target)
    return _serialize(target)


@router.post("/{agent_id}/{channel}", response_model=Dict[str, Any])
async def publish_to_channel(
    agent_id: int,
    channel: str,
    request: Request,
    payload: Optional[ChannelPublishRequest] = None,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """通用渠道发布

    与 publish_routes 的 5 个静态渠道端点 (/{agent_id}/feishu 等) 行为一致，
    区别是渠道通过路径参数动态传入，前端无需为每个渠道维护独立方法。
    静态路由先注册，因此 feishu/wechat/dingtalk/web/api 仍由原实现处理。
    """
    if channel not in PUBLISH_CHANNELS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"不支持的渠道: {channel}，可选: {sorted(PUBLISH_CHANNELS)}",
        )

    tenant_id = get_current_tenant()
    body = payload or ChannelPublishRequest()
    version_id = body.version_id
    if version_id is None:
        version_id = await _resolve_version_id(session, agent_id, tenant_id=tenant_id)

    service = PublishService(session)
    try:
        result = await service.publish(
            agent_id,
            version_id,
            channel,
            body.config or {},
            tenant_id=tenant_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    await audit_service.log(
        actor_id=current_user_id,
        action="publish_to_channel",
        details={"agent_id": agent_id, "channel": channel, "version_id": version_id},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return result
