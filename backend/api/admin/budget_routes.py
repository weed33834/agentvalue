"""预算管理 Admin API

路由前缀: /api/v1/admin/budgets
权限: Role.ADMIN (router 级 dependencies)

完整功能 (6 端点):
- GET    /            - 列出当前租户的预算
- POST   /            - 创建预算
- PUT    /{budget_id} - 更新预算
- DELETE /{budget_id} - 删除预算
- GET    /status      - 获取预算状态 (当前租户)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_audit_service
from auth.rbac import Role, get_current_user_id, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from services.audit_service import AuditService
from services.budget_service import BudgetService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/budgets",
    tags=["admin-budgets"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class BudgetCreate(BaseModel):
    """创建预算请求

    H4: 不再允许调用方指定 tenant_id, 强制使用当前请求租户, 防止跨租户创建预算。
    """

    model_config = ConfigDict(extra="forbid")

    budget_type: str = Field(
        description="预算类型: monthly / daily"
    )
    budget_limit: float = Field(
        gt=0, description="预算上限（美元）"
    )
    alert_threshold: float = Field(
        default=0.8, gt=0, le=1, description="告警阈值 (0-1, 默认 0.8)"
    )
    period_start: Optional[datetime] = Field(
        default=None, description="周期开始时间"
    )
    period_end: Optional[datetime] = Field(
        default=None, description="周期结束时间"
    )


class BudgetUpdate(BaseModel):
    """更新预算请求 (所有字段可选)"""

    model_config = ConfigDict(extra="forbid")

    budget_type: Optional[str] = None
    budget_limit: Optional[float] = Field(default=None, gt=0)
    alert_threshold: Optional[float] = Field(default=None, gt=0, le=1)
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None


# ============================================================
# 路由
# ============================================================


@router.get("", response_model=Dict[str, Any])
async def list_budgets(
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """列出当前租户的所有预算"""
    tenant_id = get_current_tenant()
    service = BudgetService(session)
    budgets = await service.get_budgets(tenant_id)
    return {
        "tenant_id": tenant_id,
        "items": budgets,
        "total": len(budgets),
    }


@router.post("", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_budget(
    payload: BudgetCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """创建预算"""
    # H4: 强制使用当前请求租户, 不允许调用方指定 tenant_id
    tenant_id = get_current_tenant()

    service = BudgetService(session)
    try:
        result = await service.create_budget(
            tenant_id=tenant_id,
            budget_type=payload.budget_type,
            limit=payload.budget_limit,
            threshold=payload.alert_threshold,
            period_start=payload.period_start,
            period_end=payload.period_end,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )

    await audit_service.log(
        actor_id=current_user_id,
        action="create_budget",
        details={
            "tenant_id": tenant_id,
            "budget_id": result["id"],
            "budget_type": payload.budget_type,
            "budget_limit": payload.budget_limit,
        },
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return result


@router.put("/{budget_id}", response_model=Dict[str, Any])
async def update_budget(
    budget_id: int,
    payload: BudgetUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """更新预算配置"""
    # H1: 从当前租户上下文获取 tenant_id, 防止 IDOR 越权操作他租户预算
    tenant_id = get_current_tenant()
    update_data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="未提供任何更新字段",
        )

    service = BudgetService(session)
    result = await service.update_budget(budget_id, update_data, tenant_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"预算 {budget_id} 不存在",
        )

    await audit_service.log(
        actor_id=current_user_id,
        action="update_budget",
        details={"budget_id": budget_id, "changed": update_data},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return result


@router.delete("/{budget_id}", response_model=Dict[str, Any])
async def delete_budget(
    budget_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """删除预算"""
    # H1: 从当前租户上下文获取 tenant_id, 防止 IDOR 越权操作他租户预算
    tenant_id = get_current_tenant()
    service = BudgetService(session)
    deleted = await service.delete_budget(budget_id, tenant_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"预算 {budget_id} 不存在",
        )

    await audit_service.log(
        actor_id=current_user_id,
        action="delete_budget",
        details={"budget_id": budget_id},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return {"deleted": True, "budget_id": budget_id}


@router.get("/status", response_model=Dict[str, Any])
async def get_budget_status(
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """获取当前租户的预算状态（百分比、是否告警、剩余额度）

    H4: 不再通过路径参数接受 tenant_id, 强制使用当前请求租户, 防止跨租户查看。
    """
    tenant_id = get_current_tenant()
    service = BudgetService(session)
    statuses = await service.get_budget_status(tenant_id)
    return {
        "tenant_id": tenant_id,
        "budgets": statuses,
        "total": len(statuses),
    }
