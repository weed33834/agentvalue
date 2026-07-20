"""
高级分析路由（Phase 9.2）
团队 ROI、员工成长路径、离职风险预测三类端点。
权限：team-roi / attrition-risk 仅 manager/hr/admin；growth-path 员工看自己、主管看下属。
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

from api.deps import assert_manager_team_access, get_evaluation_service
from auth.rbac import Role, get_current_user_id, require_role
from services.analytics_service import AnalyticsService
from services.evaluation_service import EvaluationService

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


def get_analytics_service(
    eval_service: EvaluationService = Depends(get_evaluation_service),
) -> AnalyticsService:
    """分析服务依赖：复用 EvaluationService 的只读查询"""
    return AnalyticsService(eval_service)


@router.get("/team-roi")
async def get_team_roi(
    request: Request,
    member_ids: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    analytics: AnalyticsService = Depends(get_analytics_service),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """团队 ROI 仪表盘数据
    示例：/api/v1/analytics/team-roi?member_ids=E1,E2&start=2026-W20&end=2026-W25
    """
    member_list = [m.strip() for m in member_ids.split(",") if m.strip()]
    if not member_list:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="member_ids 必填，逗号分隔",
        )
    if len(member_list) > 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="member_ids 数量超限(最多 200)",
        )
    # H7：manager 仅能查询直属下属集合的 ROI；HR/ADMIN 不受限
    if role == Role.MANAGER:
        current_user_id = await get_current_user_id(request)
        reports = await eval_service.list_direct_reports(current_user_id)
        report_ids = {r.user_id for r in reports}
        unknown = [m for m in member_list if m not in report_ids]
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"无权查询非直属下属: {unknown}",
            )
    # 周期参数成对校验
    period_range = None
    if start or end:
        if not (start and end):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="start 与 end 需同时提供，形如 2026-W20",
            )
        period_range = (start, end)
    return await analytics.get_team_roi(member_list, period_range)


@router.get("/growth-path/{employee_id}")
async def get_growth_path(
    employee_id: str,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    analytics: AnalyticsService = Depends(get_analytics_service),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """员工成长路径推荐：员工仅看自己，manager 仅看下属，HR/ADMIN 不受限"""
    if role == Role.EMPLOYEE:
        employee_id = await get_current_user_id(request)
    else:
        await assert_manager_team_access(
            eval_service,
            role,
            employee_id,
            await get_current_user_id(request),
            detail="无权查看非直属下属的成长路径",
        )
    return await analytics.get_growth_path(employee_id)


@router.get("/attrition-risk")
async def get_attrition_risk(
    request: Request,
    member_ids: str,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    analytics: AnalyticsService = Depends(get_analytics_service),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """离职风险预警：仅 manager/hr/admin
    示例：/api/v1/analytics/attrition-risk?member_ids=E1,E2
    """
    member_list = [m.strip() for m in member_ids.split(",") if m.strip()]
    if not member_list:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="member_ids 必填，逗号分隔",
        )
    if len(member_list) > 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="member_ids 数量超限(最多 200)",
        )
    # H7：manager 仅能查询直属下属集合的离职风险；HR/ADMIN 不受限
    if role == Role.MANAGER:
        current_user_id = await get_current_user_id(request)
        reports = await eval_service.list_direct_reports(current_user_id)
        report_ids = {r.user_id for r in reports}
        unknown = [m for m in member_list if m not in report_ids]
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"无权查询非直属下属: {unknown}",
            )
    return await analytics.get_attrition_risk(member_list)


@router.get("/talent-matrix")
async def get_talent_matrix(
    request: Request,
    period: Optional[str] = None,
    member_ids: Optional[str] = None,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    analytics: AnalyticsService = Depends(get_analytics_service),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """人才九宫格: 绩效 × 潜力 3x3 矩阵

    - 查询所有已审批 (approved) 评估, 提取绩效分数和潜力分数
    - 横轴=绩效(低/中/高), 纵轴=潜力(低/中/高)
    - 返回每个格子里的员工列表

    权限:
    - HR/ADMIN: 不受限, 可看全公司
    - MANAGER: 仅能看直属下属集合 (自动按 manager_id 过滤)

    示例:
    - GET /api/v1/analytics/talent-matrix
    - GET /api/v1/analytics/talent-matrix?period=2026-W25
    - GET /api/v1/analytics/talent-matrix?member_ids=E1,E2
    """
    member_id_list: Optional[List[str]] = None
    if member_ids:
        member_id_list = [m.strip() for m in member_ids.split(",") if m.strip()]
        if len(member_id_list) > 500:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="member_ids 数量超限(最多 500)",
            )

    # H7: manager 仅能查看直属下属集合; HR/ADMIN 不受限
    if role == Role.MANAGER:
        current_user_id = await get_current_user_id(request)
        reports = await eval_service.list_direct_reports(current_user_id)
        report_ids = {r.user_id for r in reports}
        if member_id_list is not None:
            # 取交集, 且校验是否全部在 report_ids 内
            unknown = [m for m in member_id_list if m not in report_ids]
            if unknown:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"无权查看非直属下属: {unknown}",
                )
        else:
            member_id_list = sorted(report_ids)

    return await analytics.get_talent_matrix(
        period=period, member_ids=member_id_list
    )
