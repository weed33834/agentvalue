"""
审批流服务
管理评估状态机的合法转换与审批记录，持久化到数据库。
注意：transition 不在内部 commit，由调用方控制事务边界以保证原子性。
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import List, Literal, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.metrics import record_approval_transition
from core.tenant_context import get_current_tenant
from models import ApprovalAction, Evaluation
from models.constants import EvaluationStatus
from services.audit_decorator import audit_action

logger = logging.getLogger(__name__)


class ApprovalService:
    """审批服务（数据库实现）"""

    # M2：单次评估最多申诉次数，超过上限拒绝再次申诉
    MAX_APPEALS = 2

    VALID_TRANSITIONS = {
        EvaluationStatus.AI_DRAFTED: {
            "approve": EvaluationStatus.APPROVED,
            "reject": EvaluationStatus.REJECTED,
            "request_hr_review": EvaluationStatus.HR_AUDIT,
        },
        EvaluationStatus.MANAGER_REVIEW: {
            "approve": EvaluationStatus.APPROVED,
            "reject": EvaluationStatus.REJECTED,
            "request_hr_review": EvaluationStatus.HR_AUDIT,
        },
        EvaluationStatus.HR_AUDIT: {
            "approve": EvaluationStatus.APPROVED,
            "reject": EvaluationStatus.REJECTED,
            "request_manager_review": EvaluationStatus.MANAGER_REVIEW,
            # M2：HR 可退回重评，评估回到 ai_drafted 等待重新生成
            "require_reeval": EvaluationStatus.AI_DRAFTED,
        },
        EvaluationStatus.APPROVED: {"appeal": EvaluationStatus.MANAGER_REVIEW},
        EvaluationStatus.REJECTED: {"appeal": EvaluationStatus.MANAGER_REVIEW},
    }

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def can_transition(current_status: str, action: str) -> bool:
        return action in ApprovalService.VALID_TRANSITIONS.get(current_status, {})

    async def _count_appeals(self, evaluation_id: str) -> int:
        """统计某次评估已发起的申诉次数（基于审批动作记录）"""
        result = await self.session.execute(
            select(func.count())
            .select_from(ApprovalAction)
            .where(ApprovalAction.evaluation_id == evaluation_id)
            .where(ApprovalAction.action == "appeal")
            .where(ApprovalAction.tenant_id == get_current_tenant())
        )
        return int(result.scalar() or 0)

    @audit_action("transition_status", resource_type="approval")
    async def transition_status(
        self,
        evaluation_id: str,
        action: Literal[
            "approve",
            "reject",
            "request_hr_review",
            "request_manager_review",
            "appeal",
            "require_reeval",
        ],
        actor_id: str,
        actor_role: str,
        comment: Optional[str] = None,
        approver_id: Optional[str] = None,
    ) -> tuple[str, str]:
        """
        原子执行状态转换：使用 FOR UPDATE 查询 evaluation，校验当前状态合法性，
        更新 evaluation 状态，并写入审批记录。返回 (old_status, new_status)。
        不 commit，由调用方控制事务边界。

        挂 @audit_action 自动产生 transition_status 审计记录（resource_type="approval"，
        evaluation_id 落到 details.resource_id；actor_id 从 kwargs 或 contextvar 提取）。
        路由层若有手动 audit_service.log 调用，会产生重复审计记录——冗余优于缺失。
        """
        result = await self.session.execute(
            select(Evaluation)
            .where(
                Evaluation.evaluation_id == evaluation_id,
                Evaluation.tenant_id == get_current_tenant(),
            )
            .with_for_update()
        )
        evaluation = result.scalar_one_or_none()
        if not evaluation:
            raise ValueError(f"评估不存在: {evaluation_id}")

        current_status = evaluation.status
        if not self.can_transition(current_status, action):
            raise ValueError(f"非法状态转换: {current_status} -> {action}")

        # M2：申诉次数上限校验，防止员工无限申诉
        if action == "appeal":
            appeal_count = await self._count_appeals(evaluation_id)
            if appeal_count >= self.MAX_APPEALS:
                raise ValueError(
                    f"申诉次数已达上限 {self.MAX_APPEALS} 次，无法再次申诉"
                )

        new_status = self.VALID_TRANSITIONS[current_status][action]
        evaluation.status = new_status

        if new_status == EvaluationStatus.APPROVED:
            evaluation.approved_at = datetime.now(timezone.utc)
            if approver_id:
                evaluation.approver_id = approver_id
        elif (
            current_status == EvaluationStatus.APPROVED
            and new_status != EvaluationStatus.APPROVED
        ):
            evaluation.approved_at = None
            evaluation.approver_id = None
        elif approver_id:
            evaluation.approver_id = approver_id

        action_record = ApprovalAction(
            action_id=f"ACT-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{actor_id}-{uuid.uuid4().hex[:6]}",
            evaluation_id=evaluation_id,
            actor_id=actor_id,
            actor_role=actor_role,
            action=action,
            comment=comment,
            tenant_id=get_current_tenant(),
        )
        self.session.add(action_record)
        # 业务埋点:审批流转计数,埋点失败不影响状态机
        try:
            record_approval_transition(action, current_status, new_status)
        except Exception:
            logger.exception("record_approval_transition 埋点失败 action=%s", action)
        return current_status, new_status

    async def get_history(self, evaluation_id: str) -> List[ApprovalAction]:
        result = await self.session.execute(
            select(ApprovalAction)
            .where(
                ApprovalAction.evaluation_id == evaluation_id,
                ApprovalAction.tenant_id == get_current_tenant(),
            )
            .order_by(ApprovalAction.created_at.asc())
        )
        return result.scalars().all()

    @staticmethod
    def get_allowed_actions(current_status: str) -> List[str]:
        return list(ApprovalService.VALID_TRANSITIONS.get(current_status, {}).keys())
