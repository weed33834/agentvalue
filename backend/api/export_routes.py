"""
数据导出路由

路由前缀: /api/v1/export
权限: Role.ADMIN / Role.HR (router 级 dependencies)

端点:
- GET /api/v1/export/evaluations   - 导出评估结果 (CSV/Excel/JSON)
- GET /api/v1/export/audit-logs    - 导出审计日志 (CSV/JSON)
- GET /api/v1/export/analytics     - 导出分析报告 (CSV/Excel)
- GET /api/v1/export/notifications - 导出通知记录 (CSV)

支持 format 参数: csv / excel / json，默认 csv。
CSV 使用 StreamingResponse 流式输出，适配大数据量。
"""

import csv
import io
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from auth.rbac import Role, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from services.export_service import ExportService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/export",
    tags=["export"],
    dependencies=[Depends(require_role(Role.ADMIN, Role.HR))],
)


# ============================================================
# 通用格式输出辅助
# ============================================================


def _make_filename(prefix: str, fmt: str) -> str:
    """生成带时间戳的下载文件名"""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    ext = {"csv": "csv", "excel": "xlsx", "json": "json"}.get(fmt, "csv")
    return f"{prefix}_{ts}.{ext}"


def _build_csv_streaming_response(
    data: list[dict],
    headers: list[str],
    filename: str,
) -> StreamingResponse:
    """构建 CSV StreamingResponse（流式输出，适配大数据量）"""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    for row in data:
        writer.writerow([row.get(h, "") for h in headers])
    output.seek(0)

    def iter_csv():
        # 分块返回，避免一次性序列化大字符串
        chunk_size = 8192
        while True:
            chunk = output.read(chunk_size)
            if not chunk:
                break
            yield chunk

    return StreamingResponse(
        iter_csv(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


def _build_excel_response(
    service: ExportService,
    data: list[dict],
    headers: list[str],
    filename: str,
    sheet_name: str = "Sheet1",
) -> Response:
    """构建 Excel Response"""
    try:
        content = service.to_excel(data, headers, sheet_name=sheet_name)
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=str(e),
        )
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


# ============================================================
# 路由
# ============================================================


@router.get("/evaluations")
async def export_evaluations(
    format: str = Query("csv", description="导出格式: csv / excel / json"),
    employee_id: Optional[str] = Query(None, description="按员工 ID 过滤"),
    cycle: Optional[str] = Query(None, description="按评估周期过滤, 如 2026-W25"),
    start_date: Optional[str] = Query(None, description="起始日期 (ISO 格式)"),
    end_date: Optional[str] = Query(None, description="截止日期 (ISO 格式)"),
    session: AsyncSession = Depends(get_db),
):
    """导出评估结果

    支持按员工 ID、评估周期、创建时间范围过滤。
    format: csv (默认) / excel / json
    """
    if format not in ("csv", "excel", "json"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="format 仅支持 csv / excel / json",
        )

    service = ExportService(session)
    tenant_id = get_current_tenant()
    data = await service.export_evaluations(
        tenant_id=tenant_id,
        employee_id=employee_id,
        cycle=cycle,
        start_date=start_date,
        end_date=end_date,
    )

    headers = [
        "evaluation_id",
        "employee_id",
        "period",
        "overall_score",
        "status",
        "archived",
        "created_at",
        "updated_at",
        "approved_at",
        "approver_id",
    ]

    if format == "csv":
        filename = _make_filename("evaluations", "csv")
        return _build_csv_streaming_response(data, headers, filename)
    elif format == "excel":
        filename = _make_filename("evaluations", "excel")
        return _build_excel_response(service, data, headers, filename, "评估结果")
    else:
        return {"format": "json", "total": len(data), "items": data}


@router.get("/audit-logs")
async def export_audit_logs(
    format: str = Query("csv", description="导出格式: csv / json"),
    user_id: Optional[str] = Query(None, description="按操作人 ID 过滤"),
    action: Optional[str] = Query(None, description="按动作类型过滤"),
    start_date: Optional[str] = Query(None, description="起始日期 (ISO 格式)"),
    end_date: Optional[str] = Query(None, description="截止日期 (ISO 格式)"),
    session: AsyncSession = Depends(get_db),
):
    """导出审计日志

    支持按操作人、动作类型、时间范围过滤。
    format: csv (默认) / json
    """
    if format not in ("csv", "json"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="format 仅支持 csv / json",
        )

    service = ExportService(session)
    tenant_id = get_current_tenant()
    data = await service.export_audit_logs(
        tenant_id=tenant_id,
        user_id=user_id,
        action=action,
        start_date=start_date,
        end_date=end_date,
    )

    headers = [
        "log_id",
        "actor_id",
        "action",
        "evaluation_id",
        "employee_id",
        "details",
        "ip_address",
        "created_at",
    ]

    if format == "csv":
        filename = _make_filename("audit_logs", "csv")
        return _build_csv_streaming_response(data, headers, filename)
    else:
        return {"format": "json", "total": len(data), "items": data}


@router.get("/analytics")
async def export_analytics(
    report_type: str = Query(
        ..., description="报告类型: team_roi / growth / turnover / nine_box"
    ),
    format: str = Query("csv", description="导出格式: csv / excel"),
    session: AsyncSession = Depends(get_db),
):
    """导出分析报告

    report_type:
    - team_roi: 团队 ROI（各员工评估次数、均分、最高/最低分）
    - growth: 员工成长路径（历次评估得分趋势）
    - turnover: 离职风险（低分员工清单，启发式风险标记）
    - nine_box: 人才九宫格（绩效×潜力分布）

    format: csv (默认) / excel
    """
    if format not in ("csv", "excel"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="format 仅支持 csv / excel",
        )

    service = ExportService(session)
    tenant_id = get_current_tenant()

    try:
        data = await service.export_analytics(tenant_id, report_type)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    # 各报告类型的列定义
    headers_map = {
        "team_roi": [
            "employee_id",
            "evaluation_count",
            "avg_score",
            "max_score",
            "min_score",
            "latest_score",
        ],
        "growth": [
            "employee_id",
            "period",
            "overall_score",
            "status",
            "created_at",
        ],
        "turnover": [
            "employee_id",
            "latest_score",
            "latest_period",
            "risk_level",
            "status",
        ],
        "nine_box": [
            "employee_id",
            "overall_score",
            "performance",
            "potential",
            "box",
            "period",
        ],
    }
    sheet_name_map = {
        "team_roi": "团队ROI",
        "growth": "成长路径",
        "turnover": "离职风险",
        "nine_box": "人才九宫格",
    }
    headers = headers_map.get(report_type, [])

    if format == "csv":
        filename = _make_filename(f"analytics_{report_type}", "csv")
        return _build_csv_streaming_response(data, headers, filename)
    else:
        filename = _make_filename(f"analytics_{report_type}", "excel")
        sheet_name = sheet_name_map.get(report_type, "Sheet1")
        return _build_excel_response(service, data, headers, filename, sheet_name)


@router.get("/notifications")
async def export_notifications(
    format: str = Query("csv", description="导出格式: csv"),
    user_id: Optional[str] = Query(None, description="按用户 ID 过滤"),
    is_read: Optional[bool] = Query(None, description="按已读状态过滤"),
    start_date: Optional[str] = Query(None, description="起始日期 (ISO 格式)"),
    end_date: Optional[str] = Query(None, description="截止日期 (ISO 格式)"),
    session: AsyncSession = Depends(get_db),
):
    """导出通知记录

    支持按用户、已读状态、时间范围过滤。
    format: csv (默认)
    """
    if format not in ("csv", "json"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="format 仅支持 csv / json",
        )

    service = ExportService(session)
    tenant_id = get_current_tenant()
    data = await service.export_notifications(
        tenant_id=tenant_id,
        user_id=user_id,
        is_read=is_read,
        start_date=start_date,
        end_date=end_date,
    )

    headers = [
        "notification_id",
        "user_id",
        "title",
        "type",
        "category",
        "is_read",
        "read_at",
        "created_at",
    ]

    if format == "csv":
        filename = _make_filename("notifications", "csv")
        return _build_csv_streaming_response(data, headers, filename)
    else:
        return {"format": "json", "total": len(data), "items": data}
