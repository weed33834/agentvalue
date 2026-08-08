"""分布式限流管理 Admin API（WS-4 企业级治理加固）

路由前缀: /api/v1/admin/rate-limits
权限: Role.ADMIN (router 级 dependencies)

端点:
- GET  /status          - 限流器状态：Redis 分布式限流生效中 / 降级为进程内 slowapi
- GET  /buckets         - 按维度 + key 查看当前桶状态（分页）
- POST /buckets/reset   - 重置单个桶（配额归零重计，写审计日志）

设计说明:
- 状态上报基于 core/redis_rate_limit.py 的 ``get_status()``，不额外探测 Redis
  （避免管理端自己触发降级路径）。
- 桶状态为运维观测能力：SCAN 收集键 + 逐键 HGETALL，桶量巨大时请用维度 +
  精确 key 定位，避免全量扫描。
- 重置操作属安全敏感动作，必须留审计（action=reset_rate_limit_bucket）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_audit_service
from auth.rbac import Role, get_current_user_id, require_role
from core.database import get_db
from core.redis_rate_limit import DIMENSIONS, get_rate_limiter
from services.audit_service import AuditService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/rate-limits",
    tags=["admin-rate-limits"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class BucketResetRequest(BaseModel):
    """重置桶请求"""

    model_config = ConfigDict(extra="forbid")

    dimension: str = Field(..., description="维度: tenant/api_key/user/endpoint")
    key: str = Field(..., min_length=1, max_length=256, description="桶键")


class BucketResetResponse(BaseModel):
    """重置桶响应"""

    model_config = ConfigDict(extra="forbid")

    reset: bool = Field(description="是否确有删除（桶不存在时 False）")
    dimension: str = Field(description="维度")
    key: str = Field(description="桶键")


# ============================================================
# 路由（全部为静态路径，无动态段冲突）
# ============================================================


@router.get("/status")
async def rate_limit_status(request: Request) -> Dict[str, Any]:
    """限流器运行状态。

    - ``active=True`` / ``mode=redis``：Redis 分布式限流生效中（多副本一致）。
    - ``active=False`` / ``mode=degraded``：Redis 不可用，已降级为进程内
      slowapi 限流（单实例语义，N 副本额度会放大 N 倍，需及时排查 Redis）。

    每次调用先做一次轻量 ping 探测，确保上报的是真实可用性而非乐观标志。
    """
    limiter = get_rate_limiter()
    await limiter.probe_availability()
    return limiter.get_status()


@router.get("/buckets")
async def list_buckets(
    request: Request,
    dimension: str = Query(..., description="维度: tenant/api_key/user/endpoint"),
    key: Optional[str] = Query(default=None, description="精确桶键（提供时只看该桶）"),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=200, description="每页条数"),
) -> Dict[str, Any]:
    """查看某维度下的桶状态（分页）。

    提供 ``key`` 时精确查询单个桶；否则 SCAN 列出该维度全部桶。降级模式下
    返回空列表（Redis 不可用无状态可查）。
    """
    if dimension not in DIMENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无效维度: {dimension}, 可选: {DIMENSIONS}",
        )
    limiter = get_rate_limiter()
    if key:
        state = await limiter.bucket_state(dimension, key)
        items = [state] if state is not None else []
        total = len(items)
    else:
        items, total = await limiter.list_buckets(dimension, page=page, size=size)
    return {
        "dimension": dimension,
        "items": items,
        "total": total,
        "page": page,
        "size": size,
    }


@router.post("/buckets/reset", response_model=BucketResetResponse)
async def reset_bucket(
    payload: BucketResetRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
) -> BucketResetResponse:
    """重置指定桶：删除 Redis 中的桶键，配额从满桶重新计。

    典型用途：管理员误把某租户/某 key 限死后解除，或压测后清状态。
    属于安全敏感操作，写入审计日志（action=reset_rate_limit_bucket）。
    """
    if payload.dimension not in DIMENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无效维度: {payload.dimension}, 可选: {DIMENSIONS}",
        )
    limiter = get_rate_limiter()
    reset = await limiter.reset_bucket(payload.dimension, payload.key)

    await audit_service.log(
        actor_id=current_user_id,
        action="reset_rate_limit_bucket",
        details={
            "dimension": payload.dimension,
            "key": payload.key,
            "reset": reset,
        },
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return BucketResetResponse(
        reset=reset, dimension=payload.dimension, key=payload.key
    )
