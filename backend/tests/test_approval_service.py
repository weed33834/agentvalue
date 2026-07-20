"""
services/approval_service.py 单元测试

覆盖 transition_status 的合法/非法转换、不存在评估、状态机校验，
以及 get_allowed_actions / get_history / 状态更新副作用（approved_at、approver_id）。
目标：把 approval_service 覆盖率从 88% 提升到 ≥95%，补齐异常分支。
"""

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.database import Base
from models import Evaluation  # 触发模型注册
from models.constants import EvaluationStatus
from services.approval_service import ApprovalService
from services.evaluation_service import EvaluationService


def _eval_data(
    evaluation_id="EVAL-1",
    employee_id="E1001",
    period="2026-W01",
    score=80.0,
    status=EvaluationStatus.AI_DRAFTED,
):
    """构造一份合法的评估数据字典"""
    return {
        "evaluation_id": evaluation_id,
        "employee_id": employee_id,
        "period": period,
        "overall_score": score,
        "employee_view": {
            "summary": "表现稳定",
            "strengths": ["执行力强"],
            "growth_areas": [
                {
                    "dimension": "执行力",
                    "score": 85,
                    "evidence": ["按时完成日报"],
                    "improvement_actions": ["继续保持"],
                }
            ],
            "next_week_focus": ["保持节奏"],
        },
        "manager_view": {
            "harsh_assessment": "稳定但缺乏突破",
            "risk_flags": [],
            "roi_analysis": "ROI 正常",
            "reallocation_suggestion": "维持现状",
            "hidden_issues": [],
        },
        "audit": {
            "model_name": "qwen2.5-7b",
            "model_tier": "L2",
            "confidence_score": 0.8,
            "raw_data_refs": ["input-1"],
            "triggered_rules": [],
            "processing_time_ms": 1200,
            "prompt_version": "v0.1",
        },
        "status": status,
    }


@pytest.fixture
async def db_session():
    """每个测试使用独立临时 SQLite 异步数据库"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_url = f"sqlite+aiosqlite:///{tmp.name}"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    async with SessionLocal() as session:
        yield session
    await engine.dispose()
    Path(tmp.name).unlink(missing_ok=True)


@pytest.fixture
def approval_service(db_session):
    return ApprovalService(db_session)


@pytest.fixture
def eval_service(db_session):
    return EvaluationService(db_session)


async def _seed_eval(
    eval_service, db_session, evaluation_id="EVAL-1", status=EvaluationStatus.AI_DRAFTED
):
    """创建用户并写入一份评估，返回 evaluation_id"""
    await eval_service.create_user(
        {"user_id": "E1001", "name": "E1001", "role": "employee"}
    )
    await eval_service.create_evaluation(_eval_data(evaluation_id, status=status))
    await db_session.flush()
    return evaluation_id


# ---------------- can_transition / get_allowed_actions ----------------


def test_can_transition_returns_true_for_valid_action():
    """ai_drafted 状态下 approve 是合法转换"""
    assert (
        ApprovalService.can_transition(EvaluationStatus.AI_DRAFTED, "approve") is True
    )


def test_can_transition_returns_false_for_invalid_action():
    """非法 action 应返回 False"""
    assert (
        ApprovalService.can_transition(EvaluationStatus.AI_DRAFTED, "foobar") is False
    )


def test_can_transition_returns_false_for_unknown_status():
    """未知状态任何 action 都不可转换"""
    assert ApprovalService.can_transition("unknown_status", "approve") is False


def test_get_allowed_actions_returns_correct_actions():
    """各状态应返回其支持的动作列表"""
    assert set(ApprovalService.get_allowed_actions(EvaluationStatus.AI_DRAFTED)) == {
        "approve",
        "reject",
        "request_hr_review",
    }
    assert set(ApprovalService.get_allowed_actions(EvaluationStatus.HR_AUDIT)) == {
        "approve",
        "reject",
        "request_manager_review",
        "require_reeval",
    }
    # approved 只能申诉
    assert ApprovalService.get_allowed_actions(EvaluationStatus.APPROVED) == ["appeal"]
    assert ApprovalService.get_allowed_actions(EvaluationStatus.REJECTED) == ["appeal"]


def test_get_allowed_actions_unknown_status_returns_empty():
    """未知状态返回空列表"""
    assert ApprovalService.get_allowed_actions("unknown_status") == []


# ---------------- transition_status：成功路径 ----------------


async def test_transition_approve_updates_status_and_approval_info(
    approval_service, eval_service, db_session
):
    """approve 成功后 status 更新为 approved，并设置 approved_at 与 approver_id"""
    evaluation_id = await _seed_eval(
        eval_service, db_session, status=EvaluationStatus.AI_DRAFTED
    )

    old_status, new_status = await approval_service.transition_status(
        evaluation_id=evaluation_id,
        action="approve",
        actor_id="M001",
        actor_role="manager",
        comment="同意",
        approver_id="M001",
    )

    assert old_status == EvaluationStatus.AI_DRAFTED
    assert new_status == EvaluationStatus.APPROVED

    # 校验 evaluation.status 已被更新
    evaluation = await eval_service.get_evaluation(evaluation_id)
    assert evaluation.status == EvaluationStatus.APPROVED
    assert evaluation.approved_at is not None
    assert evaluation.approver_id == "M001"


async def test_transition_reject_updates_status(
    approval_service, eval_service, db_session
):
    """reject 成功后 status 更新为 rejected，approved_at 仍为 None"""
    evaluation_id = await _seed_eval(
        eval_service, db_session, status=EvaluationStatus.AI_DRAFTED
    )

    old_status, new_status = await approval_service.transition_status(
        evaluation_id=evaluation_id,
        action="reject",
        actor_id="M001",
        actor_role="manager",
        comment="不通过",
    )

    assert old_status == EvaluationStatus.AI_DRAFTED
    assert new_status == EvaluationStatus.REJECTED
    evaluation = await eval_service.get_evaluation(evaluation_id)
    assert evaluation.status == EvaluationStatus.REJECTED
    assert evaluation.approved_at is None


async def test_transition_request_hr_review_with_approver_sets_approver(
    approval_service, eval_service, db_session
):
    """非 approved 转换但传入 approver_id，应记录 approver（覆盖 elif approver_id 分支）"""
    evaluation_id = await _seed_eval(
        eval_service, db_session, status=EvaluationStatus.AI_DRAFTED
    )

    old_status, new_status = await approval_service.transition_status(
        evaluation_id=evaluation_id,
        action="request_hr_review",
        actor_id="M001",
        actor_role="manager",
        approver_id="M002",
    )

    assert old_status == EvaluationStatus.AI_DRAFTED
    assert new_status == EvaluationStatus.HR_AUDIT
    evaluation = await eval_service.get_evaluation(evaluation_id)
    assert evaluation.status == EvaluationStatus.HR_AUDIT
    # 非 approved 状态下 approver_id 仍被记录
    assert evaluation.approver_id == "M002"
    assert evaluation.approved_at is None


async def test_appeal_from_approved_resets_approval_info(
    approval_service, eval_service, db_session
):
    """approved → appeal 回到 manager_review，应重置 approved_at 与 approver_id"""
    evaluation_id = await _seed_eval(
        eval_service, db_session, status=EvaluationStatus.AI_DRAFTED
    )
    # 先 approve 进入 approved 状态（设置 approved_at / approver_id）
    await approval_service.transition_status(
        evaluation_id=evaluation_id,
        action="approve",
        actor_id="M001",
        actor_role="manager",
        approver_id="M001",
    )
    await db_session.flush()
    evaluation = await eval_service.get_evaluation(evaluation_id)
    assert evaluation.approved_at is not None

    # 再 appeal 回到 manager_review
    old_status, new_status = await approval_service.transition_status(
        evaluation_id=evaluation_id,
        action="appeal",
        actor_id="E1001",
        actor_role="employee",
        comment="对结果有异议",
    )

    assert old_status == EvaluationStatus.APPROVED
    assert new_status == EvaluationStatus.MANAGER_REVIEW
    evaluation = await eval_service.get_evaluation(evaluation_id)
    assert evaluation.status == EvaluationStatus.MANAGER_REVIEW
    # 离开 approved 后审批信息应被重置
    assert evaluation.approved_at is None
    assert evaluation.approver_id is None


# ---------------- transition_status：异常分支 ----------------


async def test_transition_nonexistent_evaluation_raises(approval_service):
    """不存在的 evaluation_id 应抛 ValueError"""
    with pytest.raises(ValueError, match="评估不存在"):
        await approval_service.transition_status(
            evaluation_id="EVAL-NOPE",
            action="approve",
            actor_id="M001",
            actor_role="manager",
        )


async def test_transition_illegal_action_raises(
    approval_service, eval_service, db_session
):
    """非法 action（不在任何状态机的合法动作里）应抛 ValueError"""
    evaluation_id = await _seed_eval(
        eval_service, db_session, status=EvaluationStatus.AI_DRAFTED
    )

    with pytest.raises(ValueError, match="非法状态转换"):
        await approval_service.transition_status(
            evaluation_id=evaluation_id,
            action="foobar",
            actor_id="M001",
            actor_role="manager",
        )


async def test_transition_legal_action_unsupported_by_status_raises(
    approval_service, eval_service, db_session
):
    """合法 action 但当前状态不支持该 action 应抛 ValueError"""
    evaluation_id = await _seed_eval(
        eval_service, db_session, status=EvaluationStatus.AI_DRAFTED
    )
    # appeal 是合法 action（approved/rejected 支持），但 ai_drafted 不支持
    with pytest.raises(ValueError, match="非法状态转换"):
        await approval_service.transition_status(
            evaluation_id=evaluation_id,
            action="appeal",
            actor_id="E1001",
            actor_role="employee",
        )


async def test_duplicate_approve_on_approved_raises(
    approval_service, eval_service, db_session
):
    """已 approved 再 approve 应抛 ValueError（approved 只支持 appeal）"""
    evaluation_id = await _seed_eval(
        eval_service, db_session, status=EvaluationStatus.AI_DRAFTED
    )
    await approval_service.transition_status(
        evaluation_id=evaluation_id,
        action="approve",
        actor_id="M001",
        actor_role="manager",
        approver_id="M001",
    )
    await db_session.flush()

    with pytest.raises(ValueError, match="非法状态转换"):
        await approval_service.transition_status(
            evaluation_id=evaluation_id,
            action="approve",
            actor_id="M001",
            actor_role="manager",
        )


async def test_reject_on_approved_raises(approval_service, eval_service, db_session):
    """approved 状态下 reject 不合法，应抛 ValueError"""
    evaluation_id = await _seed_eval(
        eval_service, db_session, status=EvaluationStatus.AI_DRAFTED
    )
    await approval_service.transition_status(
        evaluation_id=evaluation_id,
        action="approve",
        actor_id="M001",
        actor_role="manager",
        approver_id="M001",
    )
    await db_session.flush()

    with pytest.raises(ValueError, match="非法状态转换"):
        await approval_service.transition_status(
            evaluation_id=evaluation_id,
            action="reject",
            actor_id="M001",
            actor_role="manager",
        )


# ---------------- get_history ----------------


async def test_get_history_returns_ordered_actions(
    approval_service, eval_service, db_session
):
    """get_history 应返回按时间正序的审批动作记录"""
    evaluation_id = await _seed_eval(
        eval_service, db_session, status=EvaluationStatus.AI_DRAFTED
    )
    await approval_service.transition_status(
        evaluation_id=evaluation_id,
        action="request_hr_review",
        actor_id="M001",
        actor_role="manager",
        comment="送 HR",
    )
    await db_session.flush()
    await approval_service.transition_status(
        evaluation_id=evaluation_id,
        action="approve",
        actor_id="HR001",
        actor_role="hr",
        comment="HR 通过",
        approver_id="HR001",
    )
    await db_session.flush()

    history = await approval_service.get_history(evaluation_id)
    assert len(history) == 2
    assert history[0].action == "request_hr_review"
    assert history[0].actor_id == "M001"
    assert history[1].action == "approve"
    assert history[1].actor_id == "HR001"
    # action_id 应唯一
    assert history[0].action_id != history[1].action_id


async def test_get_history_empty_when_no_actions(
    approval_service, eval_service, db_session
):
    """无审批动作时 get_history 返回空列表"""
    evaluation_id = await _seed_eval(
        eval_service, db_session, status=EvaluationStatus.AI_DRAFTED
    )
    history = await approval_service.get_history(evaluation_id)
    assert history == []


# ---------------- M2：申诉次数上限 + HR 退回重评 ----------------


async def _seed_eval_in_status(eval_service, db_session, evaluation_id, status):
    """构造一份评估并直接置为指定状态（用于申诉/HR 复核测试）"""
    await _seed_eval(
        eval_service, db_session, evaluation_id=evaluation_id, status=status
    )
    return evaluation_id


async def test_appeal_count_limit_blocks_excessive_appeals(
    approval_service, eval_service, db_session
):
    """M2：申诉次数达到 MAX_APPEALS 后再次申诉应被拒绝"""
    evaluation_id = await _seed_eval_in_status(
        eval_service, db_session, "EVAL-APPEAL-LIMIT", EvaluationStatus.APPROVED
    )
    # 第一次申诉：approved -> manager_review
    await approval_service.transition_status(
        evaluation_id=evaluation_id,
        action="appeal",
        actor_id="E1001",
        actor_role="employee",
        comment="第一次申诉",
    )
    await db_session.flush()
    # 主管再 approve
    await approval_service.transition_status(
        evaluation_id=evaluation_id,
        action="approve",
        actor_id="M001",
        actor_role="manager",
        approver_id="M001",
    )
    await db_session.flush()
    # 第二次申诉：approved -> manager_review
    await approval_service.transition_status(
        evaluation_id=evaluation_id,
        action="appeal",
        actor_id="E1001",
        actor_role="employee",
        comment="第二次申诉",
    )
    await db_session.flush()
    # 主管再 approve
    await approval_service.transition_status(
        evaluation_id=evaluation_id,
        action="approve",
        actor_id="M001",
        actor_role="manager",
        approver_id="M001",
    )
    await db_session.flush()

    # 第三次申诉应被拒绝（已达上限 2 次）
    with pytest.raises(ValueError, match="申诉次数已达上限"):
        await approval_service.transition_status(
            evaluation_id=evaluation_id,
            action="appeal",
            actor_id="E1001",
            actor_role="employee",
            comment="第三次申诉",
        )


async def test_appeal_allowed_below_limit(approval_service, eval_service, db_session):
    """M2：未达上限时申诉应正常通过"""
    evaluation_id = await _seed_eval_in_status(
        eval_service, db_session, "EVAL-APPEAL-OK", EvaluationStatus.APPROVED
    )
    old_status, new_status = await approval_service.transition_status(
        evaluation_id=evaluation_id,
        action="appeal",
        actor_id="E1001",
        actor_role="employee",
        comment="首次申诉",
    )
    assert old_status == EvaluationStatus.APPROVED
    assert new_status == EvaluationStatus.MANAGER_REVIEW


async def test_hr_audit_require_reeval_transitions_to_ai_drafted(
    approval_service, eval_service, db_session
):
    """M2：HR_AUDIT 状态下 require_reeval 应回到 ai_drafted"""
    evaluation_id = await _seed_eval_in_status(
        eval_service, db_session, "EVAL-REEVAL", EvaluationStatus.HR_AUDIT
    )

    old_status, new_status = await approval_service.transition_status(
        evaluation_id=evaluation_id,
        action="require_reeval",
        actor_id="HR001",
        actor_role="hr",
        comment="需要重新评估",
    )

    assert old_status == EvaluationStatus.HR_AUDIT
    assert new_status == EvaluationStatus.AI_DRAFTED
    evaluation = await eval_service.get_evaluation(evaluation_id)
    assert evaluation.status == EvaluationStatus.AI_DRAFTED


async def test_require_reeval_rejected_from_non_hr_audit(
    approval_service, eval_service, db_session
):
    """M2：非 HR_AUDIT 状态下 require_reeval 应抛 ValueError"""
    evaluation_id = await _seed_eval_in_status(
        eval_service, db_session, "EVAL-REEVAL-BAD", EvaluationStatus.AI_DRAFTED
    )
    with pytest.raises(ValueError, match="非法状态转换"):
        await approval_service.transition_status(
            evaluation_id=evaluation_id,
            action="require_reeval",
            actor_id="HR001",
            actor_role="hr",
        )
