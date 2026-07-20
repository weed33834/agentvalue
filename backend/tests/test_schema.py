"""
Schema 单元测试：验证 Pydantic 模型的校验行为。
"""

import pytest
from pydantic import ValidationError

from schemas import (
    DimensionScore,
    EmployeeView,
    RiskFlag,
    ManagerView,
    AuditInfo,
    EmployeeEvaluation,
)


def build_employee_view():
    return EmployeeView(
        summary="本周整体表现稳健，在交付和协作方面均有可取之处。",
        strengths=["高质量完成登录模块重构", "主动帮助新同学解决问题"],
        growth_areas=[
            DimensionScore(
                dimension="业务影响",
                score=72.0,
                evidence=["本周主要处理维护性工单，未参与核心业务需求"],
                improvement_actions=["下周主动申请参与一个高优先级业务需求"],
            )
        ],
        next_week_focus=["完成 JIRA-2051 剩余 40%", "参加一次技术分享"],
    )


def build_manager_view():
    return ManagerView(
        harsh_assessment="该员工本周交付稳定，但业务价值产出偏低，成长斜率有放缓迹象。",
        risk_flags=[
            RiskFlag(
                level="medium",
                category="成长瓶颈",
                description="连续两周以低优先级维护任务为主，未见技术突破",
                suggested_action="主管与其沟通下一阶段发展目标，适当分配有挑战的任务",
            )
        ],
        roi_analysis="当前投入产出比中等，若持续停留在维护性工作，ROI 会进一步下降。",
        reallocation_suggestion="建议分配至有技术挑战的新项目，或让其承担部分技术方案设计工作。",
        hidden_issues=[
            "该员工技术能力尚可，但主动性在下降，需观察是否对当前工作失去兴趣"
        ],
    )


def build_audit():
    return AuditInfo(
        model_name="qwen2.5-7b-instruct",
        model_tier="L2",
        confidence_score=0.82,
        raw_data_refs=["daily-001", "task-001"],
        triggered_rules=["evidence_first", "dual_view_separation"],
        processing_time_ms=1250,
        prompt_version="v0.1",
    )


def test_valid_evaluation():
    ev = EmployeeEvaluation(
        evaluation_id="EV-2026-W25-E1001",
        employee_id="E1001",
        period="2026-W25",
        overall_score=78.5,
        employee_view=build_employee_view(),
        manager_view=build_manager_view(),
        audit=build_audit(),
        status="ai_drafted",
    )
    assert ev.overall_score == 78.5
    assert ev.employee_view.summary
    assert len(ev.manager_view.risk_flags) == 1


def test_score_out_of_range():
    with pytest.raises(ValidationError):
        DimensionScore(
            dimension="执行力",
            score=150,
            evidence=["完成了登录模块重构"],
            improvement_actions=["继续保持"],
        )


def test_evidence_too_short_filtered():
    """过短证据应被过滤（替换为占位文本），而非抛出 ValidationError 阻断评估"""
    ds = DimensionScore(
        dimension="执行力",
        score=80,
        evidence=["完成"],
        improvement_actions=["继续保持"],
    )
    assert len(ds.evidence) >= 1
    assert all(len(e.strip()) >= 5 for e in ds.evidence)


def test_missing_evidence():
    with pytest.raises(ValidationError):
        DimensionScore(
            dimension="执行力",
            score=80,
            evidence=[],
            improvement_actions=["继续保持"],
        )


def test_overall_score_rounding():
    ev = EmployeeEvaluation(
        evaluation_id="EV-2026-W25-E1001",
        employee_id="E1001",
        period="2026-W25",
        overall_score=78.55555,
        employee_view=build_employee_view(),
        manager_view=build_manager_view(),
        audit=build_audit(),
        status="ai_drafted",
    )
    assert ev.overall_score == 78.56
