"""
校准会 API Router

端点:
- POST /api/v1/calibrations  创建校准会
- GET /api/v1/calibrations   列表 (可按 period / status 过滤)
- GET /api/v1/calibrations/{session_id}  详情 (含校准项列表)
- POST /api/v1/calibrations/{session_id}/items  添加校准项 (单条)
- POST /api/v1/calibrations/{session_id}/items/batch  批量添加校准项
- PATCH /api/v1/calibrations/{session_id}/items/{item_id}  调整分数 (单个)
- POST /api/v1/calibrations/{session_id}/items/batch-adjust  批量调整分数
- POST /api/v1/calibrations/{session_id}/complete  完成校准, 应用分数调整回 Evaluation

权限:
- 创建/操作校准会: manager / hr / admin
- 完成校准 (应用分数): 仅 hr / admin (manager 可参与讨论但不能直接应用)

事务边界由路由层控制。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_audit_service, get_evaluation_service
from auth.rbac import Role, get_client_ip, get_current_user_id, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from models.calibration import (
    CALIBRATION_STATUS_COMPLETED,
    CALIBRATION_STATUS_IN_PROGRESS,
    CALIBRATION_STATUS_SCHEDULED,
    CalibrationItem,
    CalibrationSession,
)
from models.models import Evaluation
from services.audit_service import AuditService
from services.evaluation_service import EvaluationService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/calibrations", tags=["calibrations"])

# 文本字段长度上限
_MAX_TEXT_LENGTH = 5000
# 单次批量添加校准项上限
_MAX_ITEMS_PER_BATCH = 100
# 单次批量调整上限
_MAX_ADJUST_PER_BATCH = 100


# ---------------- Schemas ----------------


class CreateCalibrationPayload(BaseModel):
    """创建校准会请求体"""

    model_config = ConfigDict(extra="forbid")

    period: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=256)
    participants: List[str] = Field(default_factory=list, max_length=50)
    notes: Optional[str] = Field(default=None, max_length=_MAX_TEXT_LENGTH)


class AddItemPayload(BaseModel):
    """添加校准项请求体"""

    model_config = ConfigDict(extra="forbid")

    evaluation_id: str = Field(min_length=1, max_length=128)


class BatchAddItemsPayload(BaseModel):
    """批量添加校准项请求体"""

    model_config = ConfigDict(extra="forbid")

    evaluation_ids: List[str] = Field(min_length=1, max_length=_MAX_ITEMS_PER_BATCH)


class AdjustItemPayload(BaseModel):
    """调整校准项分数请求体"""

    model_config = ConfigDict(extra="forbid")

    calibrated_score: float = Field(ge=0, le=100)
    adjustment_reason: Optional[str] = Field(
        default=None, max_length=_MAX_TEXT_LENGTH
    )


class BatchAdjustItem(BaseModel):
    """批量调整中的单项"""

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=128)
    calibrated_score: float = Field(ge=0, le=100)
    adjustment_reason: Optional[str] = Field(
        default=None, max_length=_MAX_TEXT_LENGTH
    )


class BatchAdjustPayload(BaseModel):
    """批量调整请求体"""

    model_config = ConfigDict(extra="forbid")

    items: List[BatchAdjustItem] = Field(
        min_length=1, max_length=_MAX_ADJUST_PER_BATCH
    )


# ---------------- Helpers ----------------


def _serialize_session(
    session: CalibrationSession, include_items: bool = False, items: Optional[List] = None
) -> Dict[str, Any]:
    """序列化 CalibrationSession"""
    data: Dict[str, Any] = {
        "session_id": session.session_id,
        "period": session.period,
        "title": session.title,
        "facilitator_id": session.facilitator_id,
        "status": session.status,
        "participants": session.participants or [],
        "notes": session.notes or "",
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "updated_at": session.updated_at.isoformat() if session.updated_at else None,
        "completed_at": session.completed_at.isoformat()
        if session.completed_at
        else None,
    }
    if include_items:
        data["items"] = [_serialize_item(it) for it in (items or [])]
        data["item_count"] = len(items or [])
        data["adjusted_count"] = sum(
            1 for it in (items or []) if it.calibrated_score is not None
        )
    return data


def _serialize_item(item: CalibrationItem) -> Dict[str, Any]:
    """序列化 CalibrationItem"""
    return {
        "item_id": item.item_id,
        "session_id": item.session_id,
        "evaluation_id": item.evaluation_id,
        "employee_id": item.employee_id,
        "original_score": item.original_score,
        "calibrated_score": item.calibrated_score,
        "adjustment_reason": item.adjustment_reason or "",
        "applied": bool(item.applied),
        "delta": (
            round(item.calibrated_score - item.original_score, 2)
            if item.calibrated_score is not None
            else None
        ),
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


async def _load_session_or_404(
    session_id: str, db: AsyncSession, tenant_id: str
) -> CalibrationSession:
    stmt = select(CalibrationSession).where(
        CalibrationSession.session_id == session_id,
        CalibrationSession.tenant_id == tenant_id,
    )
    obj = (await db.execute(stmt)).scalar_one_or_none()
    if obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="校准会不存在"
        )
    return obj


async def _load_item_or_404(
    item_id: str, session_id: str, db: AsyncSession, tenant_id: str
) -> CalibrationItem:
    stmt = select(CalibrationItem).where(
        CalibrationItem.item_id == item_id,
        CalibrationItem.session_id == session_id,
        CalibrationItem.tenant_id == tenant_id,
    )
    obj = (await db.execute(stmt)).scalar_one_or_none()
    if obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="校准项不存在"
        )
    return obj


# ---------------- Endpoints ----------------


@router.post("")
async def create_calibration(
    payload: CreateCalibrationPayload,
    request: Request,
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """创建校准会

    - 仅 manager / hr / admin 可创建
    - 校准会初始状态为 scheduled
    """
    actor_id = await get_current_user_id(request)
    tenant_id = get_current_tenant()

    cal_session = CalibrationSession(
        session_id=f"CAL-{uuid.uuid4().hex[:12]}",
        period=payload.period,
        title=payload.title,
        facilitator_id=actor_id,
        status=CALIBRATION_STATUS_SCHEDULED,
        participants=payload.participants,
        notes=payload.notes,
        tenant_id=tenant_id,
    )
    session.add(cal_session)
    await session.flush()

    await audit_service.log(
        actor_id=actor_id,
        action="create_calibration",
        details={
            "session_id": cal_session.session_id,
            "period": cal_session.period,
            "title": cal_session.title,
            "participant_count": len(payload.participants),
        },
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return _serialize_session(cal_session)


@router.get("")
async def list_calibrations(
    request: Request,
    period: Optional[str] = None,
    status_filter: Optional[str] = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """列表查询校准会, 可按 period / status 过滤"""
    tenant_id = get_current_tenant()
    if limit < 1 or limit > 500:
        limit = 100

    stmt = (
        select(CalibrationSession)
        .where(CalibrationSession.tenant_id == tenant_id)
        .order_by(CalibrationSession.created_at.desc())
        .limit(limit)
    )
    if period:
        stmt = stmt.where(CalibrationSession.period == period)
    if status_filter:
        stmt = stmt.where(CalibrationSession.status == status_filter)

    rows = (await session.execute(stmt)).scalars().all()
    return {
        "items": [_serialize_session(s) for s in rows],
        "total": len(rows),
    }


@router.get("/{session_id}")
async def get_calibration(
    session_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """校准会详情, 含所有校准项"""
    tenant_id = get_current_tenant()
    cal_session = await _load_session_or_404(session_id, session, tenant_id)

    item_stmt = (
        select(CalibrationItem)
        .where(
            CalibrationItem.session_id == session_id,
            CalibrationItem.tenant_id == tenant_id,
        )
        .order_by(CalibrationItem.created_at.asc())
    )
    items = (await session.execute(item_stmt)).scalars().all()
    return _serialize_session(cal_session, include_items=True, items=items)


@router.post("/{session_id}/items")
async def add_calibration_item(
    session_id: str,
    payload: AddItemPayload,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """添加单个校准项

    - 自动从 Evaluation 快照 original_score
    - 校准会状态为 completed 时禁止添加
    - 同一 evaluation 已在校准会中则返回 409
    """
    actor_id = await get_current_user_id(request)
    tenant_id = get_current_tenant()
    cal_session = await _load_session_or_404(session_id, session, tenant_id)

    if cal_session.status == CALIBRATION_STATUS_COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="校准会已完成, 不可添加校准项",
        )

    # 校验评估存在
    evaluation = await eval_service.get_evaluation(payload.evaluation_id)
    if not evaluation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="评估不存在"
        )

    item = CalibrationItem(
        item_id=f"CALITEM-{uuid.uuid4().hex[:12]}",
        session_id=session_id,
        evaluation_id=payload.evaluation_id,
        employee_id=evaluation.employee_id,
        original_score=float(evaluation.overall_score),
        calibrated_score=None,
        adjustment_reason=None,
        applied=0,
        tenant_id=tenant_id,
    )
    session.add(item)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该评估已在校准会中",
        )

    # 添加校准项时自动把校准会状态推进到 in_progress
    if cal_session.status == CALIBRATION_STATUS_SCHEDULED:
        cal_session.status = CALIBRATION_STATUS_IN_PROGRESS

    await audit_service.log(
        actor_id=actor_id,
        action="add_calibration_item",
        evaluation_id=payload.evaluation_id,
        employee_id=evaluation.employee_id,
        details={
            "session_id": session_id,
            "item_id": item.item_id,
            "original_score": item.original_score,
        },
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return _serialize_item(item)


@router.post("/{session_id}/items/batch")
async def batch_add_calibration_items(
    session_id: str,
    payload: BatchAddItemsPayload,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """批量添加校准项

    - 对每个 evaluation_id 单独处理, 部分失败不影响其他
    - 返回 created / skipped 列表
    """
    actor_id = await get_current_user_id(request)
    tenant_id = get_current_tenant()
    cal_session = await _load_session_or_404(session_id, session, tenant_id)

    if cal_session.status == CALIBRATION_STATUS_COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="校准会已完成, 不可添加校准项",
        )

    created: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    seen: set = set()

    for evaluation_id in payload.evaluation_ids:
        if evaluation_id in seen:
            skipped.append(
                {"evaluation_id": evaluation_id, "reason": "请求内重复, 跳过"}
            )
            continue
        seen.add(evaluation_id)

        evaluation = await eval_service.get_evaluation(evaluation_id)
        if not evaluation:
            skipped.append(
                {"evaluation_id": evaluation_id, "reason": "评估不存在"}
            )
            continue

        # 幂等: 已存在则跳过
        existing_stmt = select(CalibrationItem).where(
            CalibrationItem.session_id == session_id,
            CalibrationItem.evaluation_id == evaluation_id,
            CalibrationItem.tenant_id == tenant_id,
        )
        existing = (
            await session.execute(existing_stmt)
        ).scalar_one_or_none()
        if existing:
            skipped.append(
                {
                    "evaluation_id": evaluation_id,
                    "reason": "已在校准会中, 跳过",
                    "item_id": existing.item_id,
                }
            )
            continue

        item = CalibrationItem(
            item_id=f"CALITEM-{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            evaluation_id=evaluation_id,
            employee_id=evaluation.employee_id,
            original_score=float(evaluation.overall_score),
            calibrated_score=None,
            adjustment_reason=None,
            applied=0,
            tenant_id=tenant_id,
        )
        session.add(item)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            skipped.append(
                {
                    "evaluation_id": evaluation_id,
                    "reason": "并发冲突, 已在校准会中",
                }
            )
            continue
        created.append(_serialize_item(item))

    # 推进状态
    if cal_session.status == CALIBRATION_STATUS_SCHEDULED and created:
        cal_session.status = CALIBRATION_STATUS_IN_PROGRESS

    await audit_service.log(
        actor_id=actor_id,
        action="batch_add_calibration_items",
        details={
            "session_id": session_id,
            "created_count": len(created),
            "skipped_count": len(skipped),
        },
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return {
        "session_id": session_id,
        "created": created,
        "skipped": skipped,
        "created_count": len(created),
        "skipped_count": len(skipped),
    }


@router.patch("/{session_id}/items/{item_id}")
async def adjust_calibration_item(
    session_id: str,
    item_id: str,
    payload: AdjustItemPayload,
    request: Request,
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """调整单个校准项分数

    - 校准会状态为 completed 时禁止调整
    - applied=True 的项 (已完成校准) 禁止调整
    """
    actor_id = await get_current_user_id(request)
    tenant_id = get_current_tenant()
    cal_session = await _load_session_or_404(session_id, session, tenant_id)

    if cal_session.status == CALIBRATION_STATUS_COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="校准会已完成, 不可调整",
        )

    item = await _load_item_or_404(item_id, session_id, session, tenant_id)
    if item.applied:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该校准项已应用, 不可调整",
        )

    old_calibrated = item.calibrated_score
    item.calibrated_score = payload.calibrated_score
    if payload.adjustment_reason is not None:
        item.adjustment_reason = payload.adjustment_reason

    await audit_service.log(
        actor_id=actor_id,
        action="adjust_calibration_item",
        evaluation_id=item.evaluation_id,
        employee_id=item.employee_id,
        details={
            "session_id": session_id,
            "item_id": item_id,
            "original_score": item.original_score,
            "old_calibrated": old_calibrated,
            "new_calibrated": payload.calibrated_score,
            "reason": payload.adjustment_reason,
        },
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return _serialize_item(item)


@router.post("/{session_id}/items/batch-adjust")
async def batch_adjust_calibration_items(
    session_id: str,
    payload: BatchAdjustPayload,
    request: Request,
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """批量调整校准项分数

    - 单次最多 100 项
    - 校准会状态为 completed 时禁止调整
    - 部分失败 (item_id 不存在) 不影响其他项, 失败项返回在 skipped 中
    """
    actor_id = await get_current_user_id(request)
    tenant_id = get_current_tenant()
    cal_session = await _load_session_or_404(session_id, session, tenant_id)

    if cal_session.status == CALIBRATION_STATUS_COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="校准会已完成, 不可调整",
        )

    adjusted: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for spec in payload.items:
        item = await _load_item_or_404(spec.item_id, session_id, session, tenant_id)
        if item.applied:
            skipped.append(
                {
                    "item_id": spec.item_id,
                    "reason": "已应用, 跳过",
                }
            )
            continue
        old_calibrated = item.calibrated_score
        item.calibrated_score = spec.calibrated_score
        if spec.adjustment_reason is not None:
            item.adjustment_reason = spec.adjustment_reason
        adjusted.append(_serialize_item(item))

    await audit_service.log(
        actor_id=actor_id,
        action="batch_adjust_calibration_items",
        details={
            "session_id": session_id,
            "adjusted_count": len(adjusted),
            "skipped_count": len(skipped),
        },
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return {
        "session_id": session_id,
        "adjusted": adjusted,
        "skipped": skipped,
        "adjusted_count": len(adjusted),
        "skipped_count": len(skipped),
    }


@router.post("/{session_id}/complete")
async def complete_calibration(
    session_id: str,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.HR, Role.ADMIN)),
):
    """完成校准会, 批量应用分数调整回 Evaluation

    - 仅 hr / admin 可执行完成操作 (manager 可参与讨论但不能直接应用)
    - 把所有 calibrated_score 不为 None 且未 applied 的项写回 Evaluation.overall_score
    - 标记 session.status = completed, items.applied = 1
    - 已 completed 的校准会不可重复完成
    """
    actor_id = await get_current_user_id(request)
    tenant_id = get_current_tenant()
    cal_session = await _load_session_or_404(session_id, session, tenant_id)

    if cal_session.status == CALIBRATION_STATUS_COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="校准会已完成, 不可重复完成",
        )

    item_stmt = (
        select(CalibrationItem)
        .where(
            CalibrationItem.session_id == session_id,
            CalibrationItem.tenant_id == tenant_id,
        )
        .order_by(CalibrationItem.created_at.asc())
    )
    items = (await session.execute(item_stmt)).scalars().all()

    applied_count = 0
    applied_evaluations: List[Dict[str, Any]] = []
    for item in items:
        if item.applied:
            continue
        if item.calibrated_score is None:
            # 未调整的项标记 applied 但不修改 Evaluation
            item.applied = 1
            continue

        # 写回 Evaluation.overall_score
        evaluation = await eval_service.get_evaluation(item.evaluation_id)
        if not evaluation:
            logger.warning(
                "校准会完成: evaluation %s 不存在, 跳过",
                item.evaluation_id,
            )
            continue

        old_score = float(evaluation.overall_score)
        evaluation.overall_score = float(item.calibrated_score)
        item.applied = 1
        applied_count += 1
        applied_evaluations.append(
            {
                "evaluation_id": item.evaluation_id,
                "employee_id": item.employee_id,
                "original_score": item.original_score,
                "calibrated_score": item.calibrated_score,
                "previous_score": old_score,
                "delta": round(
                    float(item.calibrated_score) - item.original_score, 2
                ),
            }
        )

    cal_session.status = CALIBRATION_STATUS_COMPLETED
    cal_session.completed_at = datetime.now(timezone.utc)

    await audit_service.log(
        actor_id=actor_id,
        action="complete_calibration",
        details={
            "session_id": session_id,
            "applied_count": applied_count,
            "total_items": len(items),
            "applied_evaluations": applied_evaluations,
        },
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return {
        "session_id": session_id,
        "status": CALIBRATION_STATUS_COMPLETED,
        "total_items": len(items),
        "applied_count": applied_count,
        "applied_evaluations": applied_evaluations,
    }
