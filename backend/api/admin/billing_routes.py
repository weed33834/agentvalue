"""账单管理 Admin API

路由前缀: /api/v1/admin/billing
权限: Role.ADMIN (router 级 dependencies)

完整功能 (4 端点):
- GET /summary    - 账单汇总（带日期范围参数）
- GET /by-user    - 按用户汇总
- GET /by-endpoint - 按端点汇总
- GET /export     - 导出账单（CSV/JSON）
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from auth.rbac import Role, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from services.billing_service import BillingService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/billing",
    tags=["admin-billing"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


def _parse_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    """解析 ISO 8601 日期时间字符串"""
    if not dt_str:
        return None
    try:
        # 兼容带时区和不带时区的格式
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except ValueError:
        return None


# ============================================================
# 路由
# ============================================================


@router.get("/summary", response_model=Dict[str, Any])
async def get_billing_summary(
    request: Request,
    period_start: Optional[str] = Query(
        None, description="起始时间 (ISO 8601)，默认30天前"
    ),
    period_end: Optional[str] = Query(
        None, description="截止时间 (ISO 8601)，默认当前时间"
    ),
    session: AsyncSession = Depends(get_db),
):
    """获取账单汇总（总请求数、总 token、总成本）

    H3: tenant_id 从当前请求上下文获取, 不再接受查询参数, 防止跨租户查看账单。
    """
    tid = get_current_tenant()
    ps = _parse_datetime(period_start)
    pe = _parse_datetime(period_end)

    service = BillingService(session)
    return await service.get_billing_summary(tid, ps, pe)


@router.get("/by-user", response_model=List[Dict[str, Any]])
async def get_billing_by_user(
    request: Request,
    user_id: Optional[str] = Query(None, description="按用户 ID 过滤"),
    period_start: Optional[str] = Query(None, description="起始时间 (ISO 8601)"),
    period_end: Optional[str] = Query(None, description="截止时间 (ISO 8601)"),
    session: AsyncSession = Depends(get_db),
):
    """按用户汇总计费

    H3: tenant_id 从当前请求上下文获取, 不再接受查询参数, 防止跨租户查看账单。
    """
    tid = get_current_tenant()
    ps = _parse_datetime(period_start)
    pe = _parse_datetime(period_end)

    service = BillingService(session)
    return await service.get_billing_by_user(tid, user_id, ps, pe)


@router.get("/by-endpoint", response_model=List[Dict[str, Any]])
async def get_billing_by_endpoint(
    request: Request,
    period_start: Optional[str] = Query(None, description="起始时间 (ISO 8601)"),
    period_end: Optional[str] = Query(None, description="截止时间 (ISO 8601)"),
    session: AsyncSession = Depends(get_db),
):
    """按端点汇总计费

    H3: tenant_id 从当前请求上下文获取, 不再接受查询参数, 防止跨租户查看账单。
    """
    tid = get_current_tenant()
    ps = _parse_datetime(period_start)
    pe = _parse_datetime(period_end)

    service = BillingService(session)
    return await service.get_billing_by_endpoint(tid, ps, pe)


@router.get("/export")
async def export_billing(
    request: Request,
    format: str = Query("json", description="导出格式: csv / json"),
    period_start: Optional[str] = Query(None, description="起始时间 (ISO 8601)"),
    period_end: Optional[str] = Query(None, description="截止时间 (ISO 8601)"),
    session: AsyncSession = Depends(get_db),
):
    """导出账单数据（CSV / JSON）

    返回文件下载响应，Content-Disposition 附件方式。

    H3: tenant_id 从当前请求上下文获取, 不再接受查询参数, 防止跨租户导出账单。
    """
    tid = get_current_tenant()
    ps = _parse_datetime(period_start)
    pe = _parse_datetime(period_end)
    fmt = format.lower()
    if fmt not in ("csv", "json"):
        fmt = "json"

    service = BillingService(session)
    filename, content = await service.export_billing(tid, ps, pe, format=fmt)

    media_type = "text/csv" if fmt == "csv" else "application/json"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
