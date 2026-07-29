"""合规认证管理 Admin API (P1-31: SOC2 / ISO27001)

路由前缀: /api/v1/admin/compliance
权限: Role.ADMIN

完整端点:
- GET  /controls               - 列出所有控制项 (可按框架过滤)
- GET  /controls/{control_id}   - 控制项详情
- POST /check                   - 执行自动化检查 (更新控制项状态)
- GET  /report                  - 生成合规报告 (JSON / CSV)
- PUT  /controls/{control_id}   - 手动更新控制项 (状态 / 负责人)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_audit_service
from auth.rbac import Role, get_current_user_id, require_role
from core.database import get_db
from models.compliance import SUPPORTED_FRAMEWORKS
from services.audit_service import AuditService
from services.compliance_service import ComplianceService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/compliance",
    tags=["admin-compliance"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class ControlUpdate(BaseModel):
    """手动更新控制项请求"""

    status: Optional[str] = Field(
        default=None,
        description="控制项状态: pass / fail / warning / not_applicable",
    )
    owner: Optional[str] = Field(default=None, description="负责人 (用户 ID)")
    framework: Optional[str] = Field(
        default=None, description="框架 (二次确认, SOC2 / ISO27001)"
    )
    evidence: Optional[dict] = Field(
        default=None, description="附加证据 (合并入证据快照)"
    )


# ============================================================
# 依赖
# ============================================================


def get_compliance_service(
    session: AsyncSession = Depends(get_db),
) -> ComplianceService:
    """FastAPI 依赖: 获取 ComplianceService 实例"""
    return ComplianceService(session)


# ============================================================
# 路由
# ============================================================


@router.get("/controls", response_model=Dict[str, Any])
async def list_controls(
    request: Request,
    service: ComplianceService = Depends(get_compliance_service),
    framework: Optional[str] = Query(
        default=None, description="按框架过滤 (SOC2 / ISO27001)"
    ),
):
    """列出所有控制项 (可按框架过滤)

    首次访问自动初始化预置控制项 (SOC2 + ISO27001)。
    """
    if framework and framework not in SUPPORTED_FRAMEWORKS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无效的框架: {framework}, 可选: {sorted(SUPPORTED_FRAMEWORKS)}",
        )
    # 首次访问自动初始化预置控制项
    await service.initialize_controls()
    controls = await service.list_controls(framework=framework)
    return {
        "total": len(controls),
        "framework": framework or "ALL",
        "controls": [ComplianceService._control_to_dict(c) for c in controls],
    }


@router.get("/controls/{control_id}", response_model=Dict[str, Any])
async def get_control(
    control_id: str,
    request: Request,
    service: ComplianceService = Depends(get_compliance_service),
    framework: Optional[str] = Query(
        default=None, description="框架 (二次确认, SOC2 / ISO27001)"
    ),
):
    """获取控制项详情"""
    # 首次访问自动初始化预置控制项
    await service.initialize_controls()
    control = await service.get_control(control_id, framework=framework)
    if control is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"控制项 {control_id} 不存在",
        )
    return ComplianceService._control_to_dict(control)


@router.post("/check", response_model=Dict[str, Any])
async def run_automated_check(
    request: Request,
    service: ComplianceService = Depends(get_compliance_service),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
    framework: Optional[str] = Query(
        default=None, description="仅检查指定框架 (默认全部)"
    ),
):
    """执行自动化检查: 收集证据 → 更新控制项状态 → 写入证据记录

    复用 check_prod_readiness / audit_service / db_backup / git / 安全扫描。
    """
    if framework and framework not in SUPPORTED_FRAMEWORKS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无效的框架: {framework}, 可选: {sorted(SUPPORTED_FRAMEWORKS)}",
        )
    result = await service.run_automated_check(framework=framework)
    await audit_service.log(
        actor_id=current_user_id,
        action="compliance_auto_check",
        details={
            "framework": framework or "ALL",
            "checked": result.get("checked"),
            "updated": result.get("updated"),
            "by_status": result.get("by_status"),
        },
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await service.session.commit()
    return result


@router.get("/report")
async def generate_report(
    request: Request,
    service: ComplianceService = Depends(get_compliance_service),
    framework: Optional[str] = Query(
        default=None, description="仅包含指定框架 (默认全部)"
    ),
    format: str = Query(
        default="json",
        alias="format",
        description="报告格式: json (结构化) / csv (控制矩阵下载)",
    ),
):
    """生成合规报告 (JSON 结构化报告 或 CSV 控制矩阵)

    - format=json: 返回 JSON 报告 (含 summary / controls / csv 字段)
    - format=csv : 返回 CSV 文件下载 (可导入 GRC 工具)
    """
    if framework and framework not in SUPPORTED_FRAMEWORKS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无效的框架: {framework}, 可选: {sorted(SUPPORTED_FRAMEWORKS)}",
        )
    # 报告需要控制项已存在 (首次访问自动初始化)
    await service.initialize_controls()
    fmt = format.lower()
    if fmt not in ("json", "csv"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无效的格式: {format}, 可选: json / csv",
        )
    report = await service.generate_report(framework=framework, fmt=fmt)

    if fmt == "csv":
        csv_str = report.get("csv", "")
        suffix = f"_{framework}" if framework else ""
        filename = f"compliance_matrix{suffix}.csv"
        return Response(
            content=csv_str,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    return report


@router.put("/controls/{control_id}", response_model=Dict[str, Any])
async def update_control(
    control_id: str,
    payload: ControlUpdate,
    request: Request,
    service: ComplianceService = Depends(get_compliance_service),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """手动更新控制项 (状态 / 负责人 / 证据)

    适用于无自动检查或需人工复核的控制项。
    """
    # 首次访问自动初始化预置控制项
    await service.initialize_controls()
    try:
        control = await service.update_control(
            control_id=control_id,
            status=payload.status,
            owner=payload.owner,
            framework=payload.framework,
            evidence=payload.evidence,
            collector=current_user_id,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        )
    await audit_service.log(
        actor_id=current_user_id,
        action="compliance_control_update",
        details={
            "control_id": control_id,
            "framework": payload.framework,
            "status": payload.status,
            "owner": payload.owner,
        },
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await service.session.commit()
    return ComplianceService._control_to_dict(control)
