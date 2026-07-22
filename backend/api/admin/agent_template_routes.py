"""Agent 模板市场 Admin API

路由前缀: /api/v1/admin/agent-templates
权限: Role.ADMIN (管理) / Role.HR (查看市场)

完整端点:
- POST   ""            - 创建模板
- GET    ""            - 列表 (分页 + 分类过滤 + 搜索)
- GET    /market       - 公开模板市场
- GET    /stats        - 统计
- GET    /{id}         - 详情
- PUT    /{id}         - 更新
- DELETE /{id}         - 删除
- POST   /{id}/install - 安装模板
- POST   /{id}/review  - 添加评价
- GET    /{id}/reviews - 评价列表
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.rbac import Role, get_current_user_id, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from services.agent_template_service import (
    TEMPLATE_CATEGORIES,
    AgentTemplateService,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/agent-templates",
    tags=["admin-agent-templates"],
    dependencies=[Depends(require_role(Role.ADMIN, Role.HR))],
)


# ============================================================
# Schemas
# ============================================================


class TemplateCreate(BaseModel):
    """创建模板请求"""

    name: str = Field(..., description="模板名称")
    description: Optional[str] = None
    category: str = Field(default="general", description="分类: hr/recruitment/evaluation/training/general")
    template_config: dict = Field(default_factory=dict, description="模板配置 JSON")
    author: Optional[str] = None
    version: str = "1.0.0"
    tags: Optional[List[str]] = None
    is_public: bool = False
    is_official: bool = False


class TemplateUpdate(BaseModel):
    """更新模板请求"""

    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    template_config: Optional[dict] = None
    author: Optional[str] = None
    version: Optional[str] = None
    tags: Optional[List[str]] = None
    is_public: Optional[bool] = None
    is_official: Optional[bool] = None


class ReviewCreate(BaseModel):
    """添加评价请求"""

    rating: int = Field(..., ge=1, le=5, description="评分 1-5")
    comment: Optional[str] = None


# ============================================================
# 路由
# ============================================================


@router.get("/stats", response_model=Dict[str, Any])
async def get_template_stats(
    session: AsyncSession = Depends(get_db),
):
    """模板统计 (分类统计 / 总数 / 平均评分)"""
    tenant_id = get_current_tenant()
    service = AgentTemplateService(session)
    return await service.get_template_stats(tenant_id=tenant_id)


@router.get("/market", response_model=Dict[str, Any])
async def list_market_templates(
    category: Optional[str] = Query(None, description="分类过滤"),
    keyword: Optional[str] = Query(None, description="搜索关键词"),
    page: int = Query(1, ge=1, description="页码"),
    size: int = Query(20, ge=1, le=100, description="每页条数"),
    session: AsyncSession = Depends(get_db),
):
    """公开模板市场 (所有租户可见的公开模板, 按下载量排序)"""
    service = AgentTemplateService(session)
    return await service.list_public_templates(
        category=category, keyword=keyword, page=page, size=size
    )


@router.post("", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_template(
    payload: TemplateCreate,
    session: AsyncSession = Depends(get_db),
):
    """创建 Agent 模板"""
    tenant_id = get_current_tenant()
    service = AgentTemplateService(session)
    try:
        template = await service.create_template(
            name=payload.name,
            description=payload.description,
            category=payload.category,
            template_config=payload.template_config,
            author=payload.author,
            version=payload.version,
            tags=payload.tags,
            is_public=payload.is_public,
            is_official=payload.is_official,
            tenant_id=tenant_id,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        )
    await session.commit()
    return AgentTemplateService._template_to_dict(template)


@router.get("", response_model=Dict[str, Any])
async def list_templates(
    category: Optional[str] = Query(None, description="分类过滤"),
    keyword: Optional[str] = Query(None, description="搜索关键词"),
    page: int = Query(1, ge=1, description="页码"),
    size: int = Query(20, ge=1, le=100, description="每页条数"),
    session: AsyncSession = Depends(get_db),
):
    """模板列表 (分页 + 分类过滤 + 搜索)

    搜索范围: 租户私有模板 + 公开市场模板。
    """
    tenant_id = get_current_tenant()
    service = AgentTemplateService(session)
    if keyword:
        return await service.search_templates(
            keyword=keyword, category=category, page=page, size=size, tenant_id=tenant_id
        )
    return await service.list_templates(
        category=category, page=page, size=size, tenant_id=tenant_id
    )


@router.get("/{template_id}", response_model=Dict[str, Any])
async def get_template(
    template_id: int,
    session: AsyncSession = Depends(get_db),
):
    """模板详情 (租户私有 + 公开市场均可见)"""
    tenant_id = get_current_tenant()
    service = AgentTemplateService(session)
    template = await service.get_template(template_id, tenant_id=tenant_id)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"模板 {template_id} 不存在",
        )
    return AgentTemplateService._template_to_dict(template)


@router.put("/{template_id}", response_model=Dict[str, Any])
async def update_template(
    template_id: int,
    payload: TemplateUpdate,
    session: AsyncSession = Depends(get_db),
):
    """更新模板 (仅租户自有模板可更新)"""
    tenant_id = get_current_tenant()
    service = AgentTemplateService(session)
    try:
        template = await service.update_template(
            template_id,
            name=payload.name,
            description=payload.description,
            category=payload.category,
            template_config=payload.template_config,
            author=payload.author,
            version=payload.version,
            tags=payload.tags,
            is_public=payload.is_public,
            is_official=payload.is_official,
            tenant_id=tenant_id,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        )
    await session.commit()
    return AgentTemplateService._template_to_dict(template)


@router.delete("/{template_id}", response_model=Dict[str, Any])
async def delete_template(
    template_id: int,
    session: AsyncSession = Depends(get_db),
):
    """删除模板 (仅租户自有模板可删除)"""
    tenant_id = get_current_tenant()
    service = AgentTemplateService(session)
    deleted = await service.delete_template(template_id, tenant_id=tenant_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"模板 {template_id} 不存在或无权删除",
        )
    await session.commit()
    return {"deleted": True, "id": template_id}


@router.post("/{template_id}/install", response_model=Dict[str, Any])
async def install_template(
    template_id: int,
    session: AsyncSession = Depends(get_db),
):
    """安装模板 (复制为当前租户私有模板, 增加下载计数)"""
    tenant_id = get_current_tenant()
    service = AgentTemplateService(session)
    try:
        result = await service.install_template(template_id, tenant_id=tenant_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        )
    await session.commit()
    return result


@router.post("/{template_id}/review", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def add_review(
    template_id: int,
    payload: ReviewCreate,
    session: AsyncSession = Depends(get_db),
    reviewer_id: str = Depends(get_current_user_id),
):
    """添加评价 (同一用户对同一模板只能评价一次)"""
    tenant_id = get_current_tenant()
    service = AgentTemplateService(session)
    try:
        review = await service.add_review(
            template_id,
            reviewer_id=reviewer_id,
            rating=payload.rating,
            comment=payload.comment,
            tenant_id=tenant_id,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        )
    await session.commit()
    return AgentTemplateService._review_to_dict(review)


@router.get("/{template_id}/reviews", response_model=Dict[str, Any])
async def list_reviews(
    template_id: int,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db),
):
    """模板评价列表"""
    tenant_id = get_current_tenant()
    service = AgentTemplateService(session)
    return await service.list_reviews(
        template_id, page=page, size=size, tenant_id=tenant_id
    )
