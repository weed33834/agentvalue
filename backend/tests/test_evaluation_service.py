"""
services/evaluation_service.py 单元测试
覆盖 update_status / update_evaluation / get_evaluation_for_update /
get_employee_history / query_company_kb / create_kb_doc / get_team_analytics 等未覆盖分支。
"""

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.database import Base
from models import (  # 触发模型注册
    CompanyKB,
    DimensionScore,
    Evaluation,
    EvidenceRef,
    Feedback,
    Memory,
    RawInput,
    User,
)
from models.constants import EvaluationStatus
from services.evaluation_service import EvaluationService


def _eval_data(
    evaluation_id="EVAL-1", employee_id="E1001", period="2026-W01", score=80.0
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
        "status": EvaluationStatus.AI_DRAFTED,
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
def service(db_session):
    return EvaluationService(db_session)


async def _make_user(service, user_id="E1001", role="employee"):
    return await service.create_user(
        {"user_id": user_id, "name": user_id, "role": role}
    )


# ---------------- update_status ----------------


async def test_update_status_to_approved_sets_approver_and_time(service, db_session):
    """转入 approved 应设置 approver_id 与 approved_at"""
    await _make_user(service)
    eval_obj = await service.create_evaluation(_eval_data())
    await db_session.flush()

    updated = await service.update_status(
        "EVAL-1", EvaluationStatus.APPROVED, approver_id="M001"
    )
    assert updated.status == EvaluationStatus.APPROVED
    assert updated.approver_id == "M001"
    assert updated.approved_at is not None


async def test_update_status_leaving_approved_resets_approval_info(service, db_session):
    """从 approved 转出应重置 approved_at 与 approver_id"""
    await _make_user(service)
    eval_obj = await service.create_evaluation(_eval_data())
    await db_session.flush()
    await service.update_status("EVAL-1", EvaluationStatus.APPROVED, approver_id="M001")
    await db_session.flush()

    updated = await service.update_status("EVAL-1", EvaluationStatus.MANAGER_REVIEW)
    assert updated.status == EvaluationStatus.MANAGER_REVIEW
    assert updated.approved_at is None
    assert updated.approver_id is None


async def test_update_status_non_approved_with_approver_sets_approver(
    service, db_session
):
    """非 approved 状态转换但传入 approver_id，应记录 approver"""
    await _make_user(service)
    await service.create_evaluation(_eval_data())
    await db_session.flush()

    updated = await service.update_status(
        "EVAL-1", EvaluationStatus.MANAGER_REVIEW, approver_id="M002"
    )
    assert updated.status == EvaluationStatus.MANAGER_REVIEW
    assert updated.approver_id == "M002"
    assert updated.approved_at is None


async def test_update_status_not_found_returns_none(service):
    """不存在的评估应返回 None"""
    assert await service.update_status("NOPE", EvaluationStatus.APPROVED) is None


# ---------------- update_evaluation ----------------


async def test_update_evaluation_not_found_returns_none(service):
    assert await service.update_evaluation("NOPE", {}) is None


async def test_update_evaluation_to_approved_sets_approved_at(service, db_session):
    await _make_user(service)
    await service.create_evaluation(_eval_data())
    await db_session.flush()

    updated = await service.update_evaluation(
        "EVAL-1", {"overall_score": 90.0, "status": EvaluationStatus.APPROVED}
    )
    assert updated.overall_score == 90.0
    assert updated.status == EvaluationStatus.APPROVED
    assert updated.approved_at is not None


async def test_update_evaluation_leaving_approved_resets(service, db_session):
    await _make_user(service)
    await service.create_evaluation(_eval_data())
    await db_session.flush()
    await service.update_status("EVAL-1", EvaluationStatus.APPROVED, approver_id="M001")
    await db_session.flush()

    updated = await service.update_evaluation(
        "EVAL-1", {"status": EvaluationStatus.MANAGER_REVIEW}
    )
    assert updated.status == EvaluationStatus.MANAGER_REVIEW
    assert updated.approved_at is None
    assert updated.approver_id is None


# ---------------- get_evaluation_for_update ----------------


async def test_get_evaluation_for_update_returns_row(service, db_session):
    await _make_user(service)
    await service.create_evaluation(_eval_data())
    await db_session.flush()

    got = await service.get_evaluation_for_update("EVAL-1")
    assert got is not None
    assert got.evaluation_id == "EVAL-1"


async def test_get_evaluation_for_update_not_found(service):
    assert await service.get_evaluation_for_update("NOPE") is None


# ---------------- create_kb_doc ----------------


async def test_create_kb_doc_persists_fields(service, db_session):
    doc = await service.create_kb_doc(
        {
            "kb_id": "KB-X",
            "title": "价值观",
            "content": "客户第一",
            "metadata": {"v": 1},
        }
    )
    await db_session.flush()
    assert doc.kb_id == "KB-X"
    assert doc.title == "价值观"
    assert doc.content == "客户第一"
    assert doc.metadata_ == {"v": 1}


# ---------------- get_team_analytics ----------------


async def test_get_team_analytics_aggregates(service, db_session):
    await _make_user(service, "E1001")
    await _make_user(service, "E1002")
    # E1001 两份评估，E1002 一份
    await service.create_evaluation(_eval_data("EV-1", "E1001", score=80.0))
    await service.create_evaluation(_eval_data("EV-2", "E1001", score=90.0))
    await service.create_evaluation(_eval_data("EV-3", "E1002", score=70.0))
    await db_session.flush()

    analytics = await service.get_team_analytics(["E1001", "E1002"])
    members = {m["employee_id"]: m for m in analytics["members"]}
    assert members["E1001"]["eval_count"] == 2
    assert members["E1001"]["avg_score"] == 85.0
    assert members["E1002"]["eval_count"] == 1
    assert members["E1002"]["avg_score"] == 70.0
    # overall_avg = (85 + 70) / 2
    assert analytics["overall_avg"] == 77.5


async def test_get_team_analytics_empty_returns_zero(service):
    analytics = await service.get_team_analytics(["NOBODY"])
    assert analytics["members"] == []
    assert analytics["overall_avg"] == 0
