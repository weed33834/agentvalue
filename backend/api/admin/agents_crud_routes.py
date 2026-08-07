"""Agent 主体 CRUD Admin API (契约补齐)

路由前缀: /api/v1/admin/agents
权限: Role.ADMIN

背景:
    既有 /api/v1/admin/agents/* 路由全部是「版本」维度 (versions / rollback / compare)，
    缺少 Agent 主体本身的管理端点，导致前端 Agent 管理页整页 404。
    本模块补齐 admin 维度的 Agent 主体 CRUD，数据层复用 AgentPreset
    (与 /api/v1/presets/agents 同一张表)，但提供管理端特有能力:
    分页、关键字搜索、含非公开 Agent、最新版本信息聚合。

完整端点:
- GET  /            - Agent 列表 (分页 + 搜索 + 分类/状态过滤，含非公开)
- GET  /{agent_id}  - Agent 详情 (含完整配置 + 版本统计)
- POST /            - 创建 Agent

注意:
    路由注册顺序上，本 router 必须在 agent_version_routes 之后挂载，
    避免 /{agent_id} 抢占 /{agent_id}/versions 等更具体的路径。
    FastAPI 按注册顺序匹配，更具体的路径需先注册。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_audit_service
from auth.rbac import Role, get_current_user_id, require_role
from core.database import get_db
from models.agent_version import AgentVersion
from models.prompt_template import AgentPreset
from services.audit_service import AuditService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/agents",
    tags=["admin-agents"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class AgentCreate(BaseModel):
    """创建 Agent 请求"""

    name: str = Field(..., min_length=1, max_length=128, description="Agent 名称")
    system_prompt: str = Field(..., min_length=1, description="系统提示词")
    description: Optional[str] = Field(default=None, description="描述")
    avatar: Optional[str] = Field(default=None, max_length=512, description="头像")
    category: str = Field(default="general", max_length=64, description="分类")
    tags: List[str] = Field(default_factory=list, description="标签列表")
    model_tier: str = Field(default="L0", max_length=10, description="模型层级")
    enabled_tools: List[str] = Field(default_factory=list, description="启用的工具")
    temperature: int = Field(default=70, ge=0, le=100, description="温度 0-100")
    is_public: bool = Field(default=True, description="是否公开")


# ============================================================
# 工具函数
# ============================================================


def _serialize(preset: AgentPreset, include_config: bool = True) -> Dict[str, Any]:
    """序列化 AgentPreset (字段与 /api/v1/presets/agents 保持一致)"""
    data: Dict[str, Any] = {
        "id": preset.id,
        "name": preset.name,
        "description": preset.description,
        "avatar": preset.avatar,
        "category": preset.category,
        "tags": preset.tags or [],
        "model_tier": preset.model_tier,
        "is_builtin": preset.is_builtin,
        "is_public": preset.is_public,
        "use_count": preset.use_count,
        "created_by": preset.created_by,
        "created_at": preset.created_at.isoformat() if preset.created_at else None,
        "updated_at": preset.updated_at.isoformat() if preset.updated_at else None,
    }
    if include_config:
        data["system_prompt"] = preset.system_prompt
        data["enabled_tools"] = preset.enabled_tools or []
        data["temperature"] = preset.temperature
    return data


# ============================================================
# 路由
# ============================================================


@router.get("", response_model=Dict[str, Any])
async def list_agents(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    keyword: Optional[str] = Query(default=None, description="按名称/描述模糊搜索"),
    category: Optional[str] = Query(default=None, description="按分类过滤"),
    is_public: Optional[bool] = Query(default=None, description="是否公开"),
    session: AsyncSession = Depends(get_db),
):
    """Agent 列表 (管理端: 含非公开 Agent，支持分页与搜索)"""
    stmt = select(AgentPreset)
    count_stmt = select(func.count()).select_from(AgentPreset)

    conditions = []
    if keyword:
        # 参数化绑定，避免 SQL 注入
        like = f"%{keyword}%"
        conditions.append(
            or_(
                AgentPreset.name.like(like),
                AgentPreset.description.like(like),
            )
        )
    if category:
        conditions.append(AgentPreset.category == category)
    if is_public is not None:
        conditions.append(AgentPreset.is_public.is_(is_public))

    for cond in conditions:
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    total = (await session.execute(count_stmt)).scalar_one()
    stmt = (
        stmt.order_by(AgentPreset.updated_at.desc(), AgentPreset.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await session.execute(stmt)).scalars().all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_serialize(p, include_config=False) for p in rows],
    }


@router.get("/{agent_id}", response_model=Dict[str, Any])
async def get_agent(
    agent_id: int,
    session: AsyncSession = Depends(get_db),
):
    """Agent 详情 (含完整配置与版本统计)"""
    preset = (
        await session.execute(select(AgentPreset).where(AgentPreset.id == agent_id))
    ).scalar_one_or_none()
    if preset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Agent 不存在"
        )

    data = _serialize(preset, include_config=True)

    # 聚合版本统计 (版本表缺失时不应影响主体详情返回)
    try:
        version_count = (
            await session.execute(
                select(func.count())
                .select_from(AgentVersion)
                .where(AgentVersion.agent_id == agent_id)
            )
        ).scalar_one()
        latest = (
            await session.execute(
                select(AgentVersion)
                .where(AgentVersion.agent_id == agent_id)
                .order_by(AgentVersion.version_number.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        data["version_count"] = version_count
        data["latest_version"] = (
            {
                "id": latest.id,
                "version_number": latest.version_number,
                "status": latest.status,
            }
            if latest is not None
            else None
        )
    except Exception as exc:  # pragma: no cover - 版本表异常不阻断主体查询
        logger.warning("聚合 Agent 版本信息失败 agent_id=%s: %s", agent_id, exc)
        data["version_count"] = 0
        data["latest_version"] = None

    return data


@router.post("", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """创建 Agent"""
    preset = AgentPreset(
        name=payload.name,
        description=payload.description,
        avatar=payload.avatar,
        system_prompt=payload.system_prompt,
        category=payload.category,
        tags=payload.tags,
        model_tier=payload.model_tier,
        enabled_tools=payload.enabled_tools,
        temperature=payload.temperature,
        is_builtin=False,
        is_public=payload.is_public,
        use_count=0,
        created_by=int(current_user_id) if str(current_user_id).isdigit() else None,
    )
    session.add(preset)
    await session.flush()

    await audit_service.log(
        actor_id=current_user_id,
        action="create_agent",
        details={"agent_id": preset.id, "name": preset.name},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    await session.refresh(preset)
    return _serialize(preset, include_config=True)
