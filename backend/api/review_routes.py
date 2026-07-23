"""
360° 环评 API Router

端点:
- POST /api/v1/evaluations/{evaluation_id}/reviews/request
    发起 360° 环评, 指定评估人列表 (含角色)
- GET /api/v1/evaluations/{evaluation_id}/reviews
    获取某评估的所有环评记录
- POST /api/v1/reviews/{review_id}/submit
    评估人提交评分和反馈 (scores JSON + feedback_text)
- GET /api/v1/reviews/{review_id}/state
    查看评估人提交状态

权限:
- 发起环评: manager / hr / admin (manager 仅能对直属下属的评估发起)
- 提交评分: 评估人本人 (employee/manager/hr/admin 均可, 但需匹配 reviewer_id)
- 查看状态: 评估人本人 / 被评估员工直属 manager / hr / admin

事务边界由路由层控制 (service 层不 commit)。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from api.deps import (
    assert_manager_team_access,
    get_audit_service,
    get_evaluation_service,
)
from auth.rbac import Role, get_client_ip, get_current_user_id, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from models.review_cycle import (
    REVIEWER_ROLES,
    REVIEW_STATUS_PENDING,
    REVIEW_STATUS_SUBMITTED,
    ReviewCycle,
)
from services.audit_service import AuditService
from services.evaluation_service import EvaluationService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["reviews"])

# 文本字段长度上限 (与 routes.py 一致)
_MAX_TEXT_LENGTH = 5000
# 单次发起环评邀请的评估人数量上限
_MAX_REVIEWERS_PER_REQUEST = 30
# 单份评分维度数量上限
_MAX_SCORE_DIMENSIONS = 20


def _validate_feedback_text(value: Optional[str]) -> str:
    """校验 feedback_text 类型与长度, None 转空串"""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="feedback_text 必须为字符串",
        )
    if len(value) > _MAX_TEXT_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"feedback_text 长度超限(最多 {_MAX_TEXT_LENGTH} 字符)",
        )
    return value


def _validate_scores(scores: Optional[Dict[str, Any]]) -> Dict[str, float]:
    """校验 scores JSON: 必须是 {维度名: 分数} 字典, 分数 0-100"""
    if scores is None:
        return {}
    if not isinstance(scores, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="scores 必须是 JSON 对象",
        )
    if len(scores) > _MAX_SCORE_DIMENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"scores 维度数量超限(最多 {_MAX_SCORE_DIMENSIONS} 个)",
        )
    result: Dict[str, float] = {}
    for dim, raw in scores.items():
        if not isinstance(dim, str) or not dim.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="scores 的 key 必须是非空字符串(维度名)",
            )
        try:
            score = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"维度 {dim} 的分数必须是数字",
            )
        if score < 0 or score > 100:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"维度 {dim} 的分数必须在 0-100 之间",
            )
        result[dim.strip()] = score
    return result


# ---------------- Schemas ----------------


class ReviewerSpec(BaseModel):
    """单个评估人规格"""

    model_config = ConfigDict(extra="forbid")

    reviewer_id: str = Field(min_length=1, max_length=64)
    reviewer_role: str = Field(min_length=1, max_length=32)


class RequestReviewsPayload(BaseModel):
    """发起 360° 环评请求体"""

    model_config = ConfigDict(extra="forbid")

    reviewers: List[ReviewerSpec] = Field(
        min_length=1, max_length=_MAX_REVIEWERS_PER_REQUEST
    )


class SubmitReviewPayload(BaseModel):
    """评估人提交评分请求体"""

    model_config = ConfigDict(extra="forbid")

    scores: Dict[str, Any] = Field(default_factory=dict)
    overall_score: Optional[float] = Field(default=None, ge=0, le=100)
    feedback_text: Optional[str] = Field(default=None, max_length=_MAX_TEXT_LENGTH)


# ---------------- Helpers ----------------


def _serialize_review(
    review: ReviewCycle, include_scores: bool = True
) -> Dict[str, Any]:
    """序列化 ReviewCycle 为 dict

    include_scores: False 时隐藏 scores / feedback_text (用于未提交时查看状态,
    或评估人查看其他评估人的提交情况时隐藏明细)。
    """
    data: Dict[str, Any] = {
        "review_id": review.review_id,
        "evaluation_id": review.evaluation_id,
        "employee_id": review.employee_id,
        "reviewer_id": review.reviewer_id,
        "reviewer_role": review.reviewer_role,
        "status": review.status,
        "overall_score": review.overall_score,
        "requested_by": review.requested_by,
        "created_at": review.created_at.isoformat() if review.created_at else None,
        "updated_at": review.updated_at.isoformat() if review.updated_at else None,
        "submitted_at": (
            review.submitted_at.isoformat() if review.submitted_at else None
        ),
    }
    if include_scores:
        data["scores"] = review.scores or {}
        data["feedback_text"] = review.feedback_text or ""
    return data


# ---------------- Endpoints ----------------


@router.post("/evaluations/{evaluation_id}/reviews/request")
async def request_reviews(
    evaluation_id: str,
    payload: RequestReviewsPayload,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """发起 360° 环评, 为指定评估批量创建评估人邀请

    - manager 仅能对直属下属的评估发起环评 (H7 越权校验)
    - 同一 evaluation + reviewer 已存在则跳过 (幂等)
    - 自动忽略 reviewer_id == 被评估员工本人 (不能自评)
    """
    evaluation = await eval_service.get_evaluation(evaluation_id)
    if not evaluation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="评估不存在")

    actor_id = await get_current_user_id(request)
    # manager 仅能对直属下属发起环评
    await assert_manager_team_access(
        eval_service, role, evaluation.employee_id, actor_id
    )

    tenant_id = get_current_tenant()
    created: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for spec in payload.reviewers:
        if spec.reviewer_role not in REVIEWER_ROLES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"无效的 reviewer_role: {spec.reviewer_role}, "
                    f"可选: peer/manager/subordinate/external"
                ),
            )
        # 跳过自评
        if spec.reviewer_id == evaluation.employee_id:
            skipped.append(
                {
                    "reviewer_id": spec.reviewer_id,
                    "reason": "不能自评, 跳过",
                }
            )
            continue

        # 幂等: 已存在则跳过
        existing_stmt = select(ReviewCycle).where(
            ReviewCycle.evaluation_id == evaluation_id,
            ReviewCycle.reviewer_id == spec.reviewer_id,
            ReviewCycle.tenant_id == tenant_id,
        )
        existing = (await session.execute(existing_stmt)).scalar_one_or_none()
        if existing:
            skipped.append(
                {
                    "reviewer_id": spec.reviewer_id,
                    "reason": "已存在邀请, 跳过",
                    "review_id": existing.review_id,
                }
            )
            continue

        review = ReviewCycle(
            review_id=f"REV-{uuid.uuid4().hex[:12]}",
            evaluation_id=evaluation_id,
            employee_id=evaluation.employee_id,
            reviewer_id=spec.reviewer_id,
            reviewer_role=spec.reviewer_role,
            status=REVIEW_STATUS_PENDING,
            requested_by=actor_id,
            tenant_id=tenant_id,
        )
        session.add(review)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            skipped.append(
                {
                    "reviewer_id": spec.reviewer_id,
                    "reason": "并发冲突, 已存在邀请",
                }
            )
            continue
        created.append(_serialize_review(review, include_scores=False))

    await audit_service.log(
        actor_id=actor_id,
        action="request_reviews",
        evaluation_id=evaluation_id,
        employee_id=evaluation.employee_id,
        details={
            "created_count": len(created),
            "skipped_count": len(skipped),
            "reviewer_ids": [c["reviewer_id"] for c in created],
        },
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return {
        "evaluation_id": evaluation_id,
        "created": created,
        "skipped": skipped,
        "created_count": len(created),
        "skipped_count": len(skipped),
    }


@router.get("/evaluations/{evaluation_id}/reviews")
async def list_reviews(
    evaluation_id: str,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """获取某评估的所有环评记录

    - HR / ADMIN / 直属 manager: 可见所有评估人的完整评分
    - 被评估员工本人: 仅可见汇总后的均值, 隐藏单个评估人明细
    - 其他员工: 403

    返回:
    - items: 各评估人记录列表
    - summary: 汇总 (各维度均分 + 总体均分 + 提交进度)
    """
    evaluation = await eval_service.get_evaluation(evaluation_id)
    if not evaluation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="评估不存在")

    actor_id = await get_current_user_id(request)
    tenant_id = get_current_tenant()

    # 权限: employee 仅能查看自己的评估的环评; manager 仅能看直属下属; HR/ADMIN 不限
    is_owner = role == Role.EMPLOYEE and evaluation.employee_id == actor_id
    if role == Role.EMPLOYEE and not is_owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="无权查看该评估的环评"
        )
    if role == Role.MANAGER:
        await assert_manager_team_access(
            eval_service, role, evaluation.employee_id, actor_id
        )

    stmt = (
        select(ReviewCycle)
        .where(
            ReviewCycle.evaluation_id == evaluation_id,
            ReviewCycle.tenant_id == tenant_id,
        )
        .order_by(ReviewCycle.created_at.asc())
    )
    rows = (await session.execute(stmt)).scalars().all()

    # 被评估员工本人: 仅返回已提交记录的汇总, 不暴露单个评估人 ID/角色明细
    if is_owner:
        submitted = [r for r in rows if r.status == REVIEW_STATUS_SUBMITTED]
        return {
            "evaluation_id": evaluation_id,
            "total": len(rows),
            "submitted_count": len(submitted),
            "pending_count": len(rows) - len(submitted),
            "summary": _aggregate_reviews(submitted),
            # 员工视角: 仅返回汇总, 不暴露单个评估人明细
            "items": [],
        }

    return {
        "evaluation_id": evaluation_id,
        "total": len(rows),
        "submitted_count": sum(1 for r in rows if r.status == REVIEW_STATUS_SUBMITTED),
        "pending_count": sum(1 for r in rows if r.status == REVIEW_STATUS_PENDING),
        "summary": _aggregate_reviews(
            [r for r in rows if r.status == REVIEW_STATUS_SUBMITTED]
        ),
        "items": [_serialize_review(r, include_scores=True) for r in rows],
    }


def _aggregate_reviews(reviews: List[ReviewCycle]) -> Dict[str, Any]:
    """聚合已提交环评: 各维度均分 + 总体均分"""
    if not reviews:
        return {
            "dimension_avg": {},
            "overall_avg": None,
            "submitted_count": 0,
        }
    dim_sum: Dict[str, float] = {}
    dim_count: Dict[str, int] = {}
    overall_scores: List[float] = []
    for r in reviews:
        if r.scores:
            for dim, score in r.scores.items():
                dim_sum[dim] = dim_sum.get(dim, 0.0) + float(score)
                dim_count[dim] = dim_count.get(dim, 0) + 1
        if r.overall_score is not None:
            overall_scores.append(float(r.overall_score))

    dimension_avg = {
        dim: round(dim_sum[dim] / dim_count[dim], 2)
        for dim in dim_sum
        if dim_count[dim] > 0
    }
    overall_avg = (
        round(sum(overall_scores) / len(overall_scores), 2) if overall_scores else None
    )
    return {
        "dimension_avg": dimension_avg,
        "overall_avg": overall_avg,
        "submitted_count": len(reviews),
    }


@router.post("/reviews/{review_id}/submit")
async def submit_review(
    review_id: str,
    payload: SubmitReviewPayload,
    request: Request,
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """评估人提交评分和反馈

    - 仅评估人本人可提交 (reviewer_id == 当前用户 ID)
    - 已 submitted 的记录不可重复提交 (返回 409)
    - 若未传 overall_score, 自动取各维度均值
    """
    actor_id = await get_current_user_id(request)
    tenant_id = get_current_tenant()

    stmt = select(ReviewCycle).where(
        ReviewCycle.review_id == review_id,
        ReviewCycle.tenant_id == tenant_id,
    )
    review = (await session.execute(stmt)).scalar_one_or_none()
    if review is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="环评邀请不存在"
        )

    if review.reviewer_id != actor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅评估人本人可提交评分",
        )

    if review.status == REVIEW_STATUS_SUBMITTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该环评已提交, 不可重复提交",
        )

    scores = _validate_scores(payload.scores)
    feedback_text = _validate_feedback_text(payload.feedback_text)

    # overall_score: 优先用前端传入, 否则取各维度均值
    overall_score = payload.overall_score
    if overall_score is None and scores:
        overall_score = round(sum(scores.values()) / len(scores), 2)

    review.scores = scores
    review.feedback_text = feedback_text
    review.overall_score = overall_score
    review.status = REVIEW_STATUS_SUBMITTED
    review.submitted_at = datetime.now(timezone.utc)

    await session.flush()
    await audit_service.log(
        actor_id=actor_id,
        action="submit_review",
        evaluation_id=review.evaluation_id,
        employee_id=review.employee_id,
        details={
            "review_id": review_id,
            "reviewer_role": review.reviewer_role,
            "overall_score": overall_score,
            "dimension_count": len(scores),
        },
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return _serialize_review(review, include_scores=True)


@router.get("/reviews/{review_id}/state")
async def get_review_state(
    review_id: str,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """查看评估人提交状态

    - 评估人本人: 可见自己状态 (含 scores 摘要)
    - 被评估员工的直属 manager / HR / ADMIN: 可见状态 (不含 scores 明细)
    - 其他: 403
    """
    actor_id = await get_current_user_id(request)
    tenant_id = get_current_tenant()

    stmt = select(ReviewCycle).where(
        ReviewCycle.review_id == review_id,
        ReviewCycle.tenant_id == tenant_id,
    )
    review = (await session.execute(stmt)).scalar_one_or_none()
    if review is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="环评邀请不存在"
        )

    # 评估人本人: 完整可见
    if review.reviewer_id == actor_id:
        return _serialize_review(review, include_scores=True)

    # 否则: 必须是被评估员工的 manager / hr / admin
    if role == Role.EMPLOYEE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权查看该环评状态",
        )

    if role == Role.MANAGER:
        await assert_manager_team_access(
            eval_service, role, review.employee_id, actor_id
        )

    # manager / hr / admin: 仅返回状态, 不暴露 scores / feedback_text 明细
    return _serialize_review(review, include_scores=False)
