"""
Evidence API Router

暴露 EvidenceRef 查询端点，让前端能展示评估的引用/证据来源。

端点：
- GET /api/v1/evaluations/{evaluation_id}/evidence  返回某评估的所有证据引用（按 dimension 分组）

复用现有 EvidenceRef 表（models.py L375），service 层已写入，仅缺查询 API。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import assert_manager_team_access, get_evaluation_service
from auth.rbac import Role, get_current_user_id, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from models.models import DimensionScore, Evaluation, EvidenceRef, RawInput
from services.evaluation_service import EvaluationService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/evaluations", tags=["evidence"])


@router.get("/{evaluation_id}/evidence")
async def list_evidence(
    evaluation_id: str,
    request: Request,
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
    db: AsyncSession = Depends(get_db),
    eval_service: EvaluationService = Depends(get_evaluation_service),
):
    """返回某评估的所有证据引用，按 dimension 分组。

    返回结构:
    [
      {
        "dimension": "执行力",
        "score": 85,
        "items": [
          {"id": "...", "input_id": "...", "evidence_text": "...", "raw_input": {...}}
        ]
      }
    ]
    """
    tenant_id = get_current_tenant()

    # 1. 校验评估存在 + 租户隔离
    # 注意：路径参数是业务主键 Evaluation.evaluation_id（形如 EV-2026-Q3-E1001-xxxx），
    # 而 Evaluation.id 是自增整型代理键。EvidenceRef.evaluation_id 外键也指向业务主键，
    # 早期实现误用 Evaluation.id 比对字符串，导致该端点恒返回 404。
    eval_stmt = select(Evaluation).where(
        Evaluation.evaluation_id == evaluation_id,
        Evaluation.tenant_id == tenant_id,
    )
    eval_result = await db.execute(eval_stmt)
    evaluation = eval_result.scalar_one_or_none()
    if evaluation is None:
        raise HTTPException(status_code=404, detail="评估不存在")

    # 1.1 越权校验：与 GET /evaluations/{id} 保持一致的可见性边界
    # 员工仅可查看本人证据；主管仅可查看直属下属；HR/ADMIN 不受限。
    current_user_id = await get_current_user_id(request)
    if role == Role.EMPLOYEE and evaluation.employee_id != current_user_id:
        raise HTTPException(status_code=403, detail="无权访问该评估的证据链")
    await assert_manager_team_access(
        eval_service,
        role,
        evaluation.employee_id,
        current_user_id,
        detail="无权查看非直属下属的证据链",
    )

    # 2. 查 DimensionScore（含 dimension + score）
    dim_stmt = select(DimensionScore).where(
        DimensionScore.evaluation_id == evaluation_id
    )
    dim_result = await db.execute(dim_stmt)
    dimensions = list(dim_result.scalars().all())

    # 3. 查 EvidenceRef（按 evaluation_id）
    evidence_stmt = select(EvidenceRef).where(
        EvidenceRef.evaluation_id == evaluation_id
    )
    evidence_result = await db.execute(evidence_stmt)
    evidence_refs = list(evidence_result.scalars().all())

    # 4. 按 dimension 分组
    evidence_by_dim: Dict[str, List[Dict[str, Any]]] = {}
    for ref in evidence_refs:
        dim_name = getattr(ref, "dimension", None) or "未知维度"
        item = {
            "id": str(ref.id),
            "input_id": getattr(ref, "input_id", None),
            "evidence_text": getattr(ref, "evidence_text", None),
            "raw_input": None,
        }
        evidence_by_dim.setdefault(dim_name, []).append(item)

    # 5. 可选：关联 RawInput 拿原始内容
    input_ids = [
        ref.input_id
        for ref in evidence_refs
        if getattr(ref, "input_id", None)
    ]
    raw_inputs: Dict[str, Any] = {}
    if input_ids:
        raw_stmt = select(RawInput).where(RawInput.id.in_(input_ids))
        raw_result = await db.execute(raw_stmt)
        for ri in raw_result.scalars().all():
            raw_inputs[str(ri.id)] = {
                "id": str(ri.id),
                "employee_id": getattr(ri, "employee_id", None),
                "period": getattr(ri, "period", None),
                "source": getattr(ri, "source", None),
                "content": (getattr(ri, "content", None) or "")[:500],  # 截断
            }

    # 回填 raw_input
    for dim_name, items in evidence_by_dim.items():
        for item in items:
            if item["input_id"] and str(item["input_id"]) in raw_inputs:
                item["raw_input"] = raw_inputs[str(item["input_id"])]

    # 6. 组装结果（合并 dimension score）
    dim_score_map = {
        getattr(d, "dimension", None): d for d in dimensions
    }
    result: List[Dict[str, Any]] = []
    for dim_name, items in evidence_by_dim.items():
        dim_score = dim_score_map.get(dim_name)
        result.append(
            {
                "dimension": dim_name,
                "score": getattr(dim_score, "score", None) if dim_score else None,
                "items": items,
            }
        )

    return result
