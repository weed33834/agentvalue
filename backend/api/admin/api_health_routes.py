"""API 健康监控 Admin API

路由前缀: /api/v1/admin/api-health
权限: Role.ADMIN (router 级 dependencies)

对标 Langfuse 延迟监控 / 告警系统，完整端点 (7 个):
- GET    /endpoints                - 端点列表 + 健康状态概览
- GET    /endpoints/{path}/stats   - 端点详细统计（请求数/平均/P95/错误率）
- GET    /slo                      - SLO 列表
- POST   /slo                      - 创建 SLO
- PUT    /slo/{id}                 - 更新 SLO
- DELETE /slo/{id}                 - 删除 SLO
- GET    /slo/{id}/status          - SLO 达成状态
- GET    /slo/status              - 所有 SLO 状态概览
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.rbac import Role, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from services.api_health_service import ApiHealthService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/api-health",
    tags=["admin-api-health"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class SloCreate(BaseModel):
    """创建 SLO 请求

    不允许调用方指定 tenant_id，强制使用当前请求租户。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128, description="SLO 名称")
    endpoint: str = Field(..., description="目标端点路径")
    target_latency_ms: float = Field(..., gt=0, description="目标延迟上限（毫秒）")
    target_success_rate: float = Field(
        default=0.99, gt=0, le=1, description="目标成功率（0-1）"
    )
    window_minutes: int = Field(default=5, ge=1, description="统计窗口（分钟）")
    enabled: bool = Field(default=True, description="是否启用")


class SloUpdate(BaseModel):
    """更新 SLO 请求（所有字段可选）"""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    endpoint: Optional[str] = None
    target_latency_ms: Optional[float] = Field(default=None, gt=0)
    target_success_rate: Optional[float] = Field(default=None, gt=0, le=1)
    window_minutes: Optional[int] = Field(default=None, ge=1)
    enabled: Optional[bool] = None


# ============================================================
# 内部工具
# ============================================================


def _parse_datetime(value: str, field_name: str) -> datetime:
    """解析 ISO 8601 日期时间字符串，失败时抛 422。

    兼容多种常见格式:
    - 带时区偏移: 2026-07-01T00:00:00+00:00（URL 中 + 需编码为 %2B）
    - Z 后缀（JS toISOString 等）: 2026-07-01T00:00:00Z
    - 无时区: 2026-07-01T00:00:00（按 UTC 处理）
    - 仅日期: 2026-07-01（按当日 00:00:00 UTC 处理）
    """
    raw = value.strip()
    # Z 后缀 → +00:00（Python 3.10 的 fromisoformat 不支持 Z）
    if raw.endswith("Z") or raw.endswith("z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        # 尝试仅日期格式
        try:
            dt = datetime.fromisoformat(raw + "T00:00:00+00:00")
            return dt
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{field_name} 格式无效，需 ISO 8601（如 2026-07-01T00:00:00Z）",
            )


def _normalize_endpoint(path: str) -> str:
    """规范化端点路径：确保以 / 开头。"""
    if not path:
        return "/"
    return path if path.startswith("/") else "/" + path


# ============================================================
# 端点统计路由
# ============================================================


@router.get("/endpoints", response_model=Dict[str, Any])
async def list_endpoints(
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
    window_minutes: int = Query(
        default=5, ge=1, le=1440, description="统计窗口（分钟）"
    ),
):
    """端点列表 + 健康状态概览（近期有流量的端点）"""
    service = ApiHealthService(session)
    items = await service.list_endpoints(tenant_id, window_minutes=window_minutes)
    return {"tenant_id": tenant_id, "items": items, "total": len(items)}


@router.get("/endpoints/{endpoint_path:path}/stats", response_model=Dict[str, Any])
async def endpoint_stats(
    endpoint_path: str,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
    start_time: Optional[str] = Query(
        default=None, description="起始时间（ISO 8601），默认近 1 小时"
    ),
    end_time: Optional[str] = Query(
        default=None, description="结束时间（ISO 8601），默认当前"
    ),
):
    """端点详细统计（请求数/平均/P95 延迟/错误率）

    endpoint_path 为端点路径（含斜杠），如 api/v1/evaluations，
    会自动规范化为 /api/v1/evaluations。
    """
    endpoint = _normalize_endpoint(endpoint_path)
    now = datetime.now(timezone.utc)
    start = (
        _parse_datetime(start_time, "start_time")
        if start_time
        else now - timedelta(hours=1)
    )
    end = _parse_datetime(end_time, "end_time") if end_time else now
    service = ApiHealthService(session)
    return await service.get_endpoint_stats(endpoint, start, end, tenant_id)


# ============================================================
# SLO 路由
# ============================================================


@router.get("/slo", response_model=Dict[str, Any])
async def list_slos(
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
    enabled_only: bool = False,
):
    """列出当前租户的 SLO 定义"""
    service = ApiHealthService(session)
    items = await service.list_slos(tenant_id, enabled_only=enabled_only)
    return {"tenant_id": tenant_id, "items": items, "total": len(items)}


@router.post("/slo", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_slo(
    payload: SloCreate,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """创建 SLO 定义"""
    service = ApiHealthService(session)
    result = await service.create_slo(
        tenant_id=tenant_id,
        name=payload.name,
        endpoint=_normalize_endpoint(payload.endpoint),
        target_latency_ms=payload.target_latency_ms,
        target_success_rate=payload.target_success_rate,
        window_minutes=payload.window_minutes,
        enabled=payload.enabled,
    )
    await session.commit()
    return result


@router.put("/slo/{slo_id}", response_model=Dict[str, Any])
async def update_slo(
    slo_id: int,
    payload: SloUpdate,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """更新 SLO 定义"""
    update_data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="未提供任何更新字段",
        )
    # 规范化 endpoint
    if "endpoint" in update_data and update_data["endpoint"] is not None:
        update_data["endpoint"] = _normalize_endpoint(update_data["endpoint"])
    service = ApiHealthService(session)
    result = await service.update_slo(slo_id, tenant_id, **update_data)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"SLO {slo_id} 不存在",
        )
    await session.commit()
    return result


@router.delete("/slo/{slo_id}", response_model=Dict[str, Any])
async def delete_slo(
    slo_id: int,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """删除 SLO 定义"""
    service = ApiHealthService(session)
    deleted = await service.delete_slo(slo_id, tenant_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"SLO {slo_id} 不存在",
        )
    await session.commit()
    return {"deleted": True, "slo_id": slo_id}


@router.get("/slo/status", response_model=Dict[str, Any])
async def all_slo_status(
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """所有启用 SLO 状态概览（达成/违反计数 + 明细）"""
    service = ApiHealthService(session)
    return await service.get_all_slo_status(tenant_id)


@router.get("/slo/{slo_id}/status", response_model=Dict[str, Any])
async def slo_status(
    slo_id: int,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """单个 SLO 达成状态"""
    service = ApiHealthService(session)
    result = await service.get_slo_status(slo_id, tenant_id)
    if result.get("slo") is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"SLO {slo_id} 不存在",
        )
    return result
