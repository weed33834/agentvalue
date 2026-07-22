"""配额管理 Admin API

路由前缀: /api/v1/admin/quota
权限: Role.ADMIN (router 级 dependencies)

完整功能 (5 端点):
- GET  /             - 获取当前租户配额
- PUT  /             - 更新配额 (max_requests / max_tokens / max_api_keys / enabled)
- GET  /usage        - 获取使用统计 (最近 N 天)
- GET  /usage/daily  - 获取每日使用量 (最近30天)
- POST /reset        - 重置当前用量 (仅 admin)

H2: 所有路由不再接受路径参数 tenant_id, 改为从 get_current_tenant() 获取,
防止跨租户配额操控。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_audit_service
from auth.rbac import Role, get_current_user_id, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from models.quota_models import TenantQuota
from services.audit_service import AuditService
from services.quota_service import QuotaService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/quota",
    tags=["admin-quota"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class QuotaUpdate(BaseModel):
    """更新配额请求 (所有字段可选)"""

    model_config = ConfigDict(extra="forbid")

    max_requests_per_day: Optional[int] = Field(
        default=None, ge=0, description="日最大请求数"
    )
    max_tokens_per_day: Optional[int] = Field(
        default=None, ge=0, description="日最大 token 数"
    )
    max_api_keys: Optional[int] = Field(
        default=None, ge=0, description="最大 API Key 数量"
    )
    enabled: Optional[bool] = Field(default=None, description="是否启用配额限制")


# ============================================================
# 路由
# ============================================================


@router.get("", response_model=Dict[str, Any])
async def get_quota(
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """获取当前租户配额配置（不存在则自动创建默认配额）

    H2: tenant_id 从当前请求上下文获取, 不再接受路径参数。
    """
    tenant_id = get_current_tenant()
    service = QuotaService(session)
    return await service.get_quota(tenant_id)


@router.put("", response_model=Dict[str, Any])
async def update_quota(
    payload: QuotaUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """更新当前租户配额配置

    可更新: max_requests_per_day / max_tokens_per_day / max_api_keys / enabled

    H2: tenant_id 从当前请求上下文获取, 不再接受路径参数。
    """
    # H2: 从当前租户上下文获取 tenant_id, 防止跨租户配额操控
    tenant_id = get_current_tenant()
    # 过滤掉 None 值
    update_data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="未提供任何更新字段",
        )

    service = QuotaService(session)
    result = await service.update_quota(tenant_id, **update_data)

    await audit_service.log(
        actor_id=current_user_id,
        action="update_quota",
        details={"tenant_id": tenant_id, "changed": update_data},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return result


@router.get("/usage", response_model=Dict[str, Any])
async def get_usage_stats(
    request: Request,
    days: int = Query(30, ge=1, le=365, description="统计最近 N 天"),
    session: AsyncSession = Depends(get_db),
):
    """获取当前租户使用统计（最近 N 天聚合）

    H2: tenant_id 从当前请求上下文获取, 不再接受路径参数。
    """
    tenant_id = get_current_tenant()
    service = QuotaService(session)
    usage = await service.get_usage_stats(tenant_id, days=days)

    # 计算汇总
    total_requests = sum(item["request_count"] for item in usage)
    total_tokens = sum(item["token_count"] for item in usage)
    total_cost = sum(item["cost_usd"] for item in usage)

    return {
        "tenant_id": tenant_id,
        "days": days,
        "summary": {
            "total_requests": total_requests,
            "total_tokens": total_tokens,
            "total_cost": round(total_cost, 6),
        },
        "daily": usage,
    }


@router.get("/usage/daily", response_model=List[Dict[str, Any]])
async def get_daily_usage(
    request: Request,
    days: int = Query(30, ge=1, le=365, description="返回最近 N 天每日用量"),
    session: AsyncSession = Depends(get_db),
):
    """获取每日使用量（最近30天，按天明细）

    H2: tenant_id 从当前请求上下文获取, 不再接受路径参数。
    """
    tenant_id = get_current_tenant()
    service = QuotaService(session)
    return await service.get_usage_stats(tenant_id, days=days)


@router.post("/reset", response_model=Dict[str, Any])
async def reset_quota_usage(
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """重置当前租户当前日用量（手动触发，将 current_requests/tokens 清零）

    H2: tenant_id 从当前请求上下文获取, 不再接受路径参数。
    """
    # H2: 从当前租户上下文获取 tenant_id, 防止跨租户重置他租户用量
    tenant_id = get_current_tenant()
    now = datetime.now(timezone.utc)
    result = await session.execute(
        sa_update(TenantQuota)
        .where(TenantQuota.tenant_id == tenant_id)
        .values(
            current_requests_today=0,
            current_tokens_today=0,
            quota_reset_at=now,
        )
    )
    affected = result.rowcount or 0
    if affected == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"租户 {tenant_id} 的配额记录不存在",
        )

    await audit_service.log(
        actor_id=current_user_id,
        action="reset_quota_usage",
        details={"tenant_id": tenant_id, "reset_at": now.isoformat()},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()

    return {
        "tenant_id": tenant_id,
        "reset": True,
        "reset_at": now.isoformat(),
        "message": "当前日用量已重置",
    }
