"""
Phase 9.2 高级分析服务测试
覆盖团队 ROI、员工成长路径、离职风险预测三类能力的关键场景与边界。
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
from services.analytics_service import AnalyticsService
from services.evaluation_service import EvaluationService


# ---------------- 公共夹具与工厂 ----------------


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


@pytest.fixture
def analytics(service):
    return AnalyticsService(service)


async def _make_user(service, user_id="E1001", role="employee"):
    return await service.create_user(
        {"user_id": user_id, "name": user_id, "role": role}
    )


def _view(summary="表现稳定", strengths=None, growth_areas=None):
    """构造 employee_view"""
    return {
        "summary": summary,
        "strengths": strengths or [],
        "growth_areas": growth_areas or [],
        "next_week_focus": [],
    }


def _manager_view(risk_flags=None):
    return {
        "harsh_assessment": "",
        "risk_flags": risk_flags or [],
        "roi_analysis": "",
        "reallocation_suggestion": "",
        "hidden_issues": [],
    }


def _audit(processing_ms=1000):
    return {
        "model_name": "mock",
        "model_tier": "L2",
        "confidence_score": 0.8,
        "raw_data_refs": [],
        "triggered_rules": [],
        "processing_time_ms": processing_ms,
        "prompt_version": "v0.1",
    }


async def _make_eval(
    service,
    employee_id,
    period,
    score,
    summary="表现稳定",
    strengths=None,
    growth_areas=None,
    risk_flags=None,
    processing_ms=1000,
    status=EvaluationStatus.APPROVED,
    evaluation_id=None,
):
    """落库一条评估"""
    data = {
        "evaluation_id": evaluation_id or f"EVAL-{employee_id}-{period}",
        "employee_id": employee_id,
        "period": period,
        "overall_score": score,
        "employee_view": _view(summary, strengths, growth_areas),
        "manager_view": _manager_view(risk_flags),
        "audit": _audit(processing_ms),
        "status": status,
    }
    return await service.create_evaluation(data)


# ---------------- 9.2.1 团队 ROI ----------------


async def test_team_roi_empty_team(analytics):
    """空团队返回零值结构，不抛异常"""
    result = await analytics.get_team_roi([])
    assert result["team_size"] == 0
    assert result["members"] == []
    assert result["trend"] == []
    assert result["summary"]["roi"] == 0
    # 九宫格结构完整
    assert result["nine_box"]["total"] == 0
    assert len(result["nine_box"]["cells"]) == 9


async def test_team_roi_member_without_evals(analytics, service):
    """成员无评估数据时仍返回空成员统计"""
    await _make_user(service, "E2001")
    result = await analytics.get_team_roi(["E2001"])
    assert result["team_size"] == 1
    assert result["members"][0]["eval_count"] == 0
    assert result["members"][0]["avg_score"] == 0
    assert result["summary"]["roi"] == 0


async def test_team_roi_single_member(analytics, service):
    """单员工 ROI：评估次数与平均分计算正确"""
    await _make_user(service, "E3001")
    await _make_eval(service, "E3001", "2026-W20", 70)
    await _make_eval(service, "E3001", "2026-W21", 80)
    result = await analytics.get_team_roi(["E3001"])
    m = result["members"][0]
    assert m["eval_count"] == 2
    assert m["avg_score"] == 75.0
    assert m["first_score"] == 70
    assert m["latest_score"] == 80
    assert m["score_slope"] == 10.0  # (80-70)/(2-1)
    assert result["summary"]["total_evaluations"] == 2
    # 产出提升为正
    assert result["summary"]["improvement"] >= 0


async def test_team_roi_multiple_members_nine_box(analytics, service):
    """多员工九宫格分布与 top/bottom"""
    for eid in ("E4001", "E4002", "E4003"):
        await _make_user(service, eid)
    # 高绩效高潜力
    await _make_eval(service, "E4001", "2026-W20", 80)
    await _make_eval(service, "E4001", "2026-W21", 90)
    # 中绩效平潜力
    await _make_eval(service, "E4002", "2026-W20", 70)
    await _make_eval(service, "E4002", "2026-W21", 72)
    # 低绩效下降
    await _make_eval(service, "E4003", "2026-W20", 70)
    await _make_eval(service, "E4003", "2026-W21", 55)

    result = await analytics.get_team_roi(["E4001", "E4002", "E4003"])
    assert result["nine_box"]["total"] == 3
    # E4001 均分 85 → high，斜率 10 → high
    cell = result["nine_box"]["cells"]["high-high"]
    assert "E4001" in cell["employees"]
    # top 第一名应为 E4001
    assert result["top_employees"][0]["employee_id"] == "E4001"
    # bottom 第一名应为最低分 E4003
    assert result["bottom_employees"][0]["employee_id"] == "E4003"


async def test_team_roi_period_range_filters_evals(analytics, service):
    """周期范围过滤：范围外的评估不计入"""
    await _make_user(service, "E5001")
    await _make_eval(service, "E5001", "2026-W18", 60)
    await _make_eval(service, "E5001", "2026-W20", 70)
    await _make_eval(service, "E5001", "2026-W21", 80)
    await _make_eval(service, "E5001", "2026-W25", 90)

    result = await analytics.get_team_roi(
        ["E5001"], period_range=("2026-W20", "2026-W21")
    )
    m = result["members"][0]
    assert m["eval_count"] == 2  # 仅 W20/W21 计入
    assert m["first_score"] == 70
    assert m["latest_score"] == 80
    # 趋势覆盖范围内的周
    weeks = [t["week"] for t in result["trend"]]
    assert "2026-W20" in weeks
    assert "2026-W21" in weeks
    assert "2026-W18" not in weeks


async def test_team_roi_trend_weekly_values(analytics, service):
    """周度趋势：平均分与评估次数按周聚合"""
    await _make_user(service, "E6001")
    await _make_eval(service, "E6001", "2026-W20", 80, processing_ms=60000)
    await _make_eval(service, "E6001", "2026-W21", 90, processing_ms=60000)
    result = await analytics.get_team_roi(
        ["E6001"], period_range=("2026-W20", "2026-W21")
    )
    trend_by_week = {t["week"]: t for t in result["trend"]}
    assert trend_by_week["2026-W20"]["avg_score"] == 80
    assert trend_by_week["2026-W20"]["eval_count"] == 1
    assert trend_by_week["2026-W21"]["avg_score"] == 90
    # 有评估的周 ROI 为正
    assert trend_by_week["2026-W20"]["roi"] > 0


# ---------------- 9.2.2 成长路径 ----------------


async def test_growth_path_no_data(analytics, service):
    """无评估数据时降级返回 no_data"""
    await _make_user(service, "E7001")
    result = await analytics.get_growth_path("E7001")
    assert result["status"] == "no_data"
    assert result["window_weeks"] == 0
    assert result["growth_trend"] == []
    assert result["suggested_actions"] == []


async def test_growth_path_insufficient_history(analytics, service):
    """历史不足 4 周时降级为 insufficient_data，但仍返回可用数据"""
    await _make_user(service, "E7002")
    await _make_eval(
        service,
        "E7002",
        "2026-W20",
        70,
        strengths=["执行力强"],
        growth_areas=[
            {
                "dimension": "代码质量",
                "score": 70,
                "evidence": [],
                "improvement_actions": ["重构"],
            }
        ],
    )
    await _make_eval(service, "E7002", "2026-W21", 75)
    result = await analytics.get_growth_path("E7002")
    assert result["status"] == "insufficient_data"
    assert result["window_weeks"] == 2
    assert len(result["growth_trend"]) == 2


async def test_growth_path_trend_recognition(analytics, service):
    """成长趋势按周期记录得分"""
    await _make_user(service, "E7003")
    for i, score in enumerate([60, 65, 70, 75, 80]):
        await _make_eval(service, "E7003", f"2026-W{20 + i:02d}", score)
    result = await analytics.get_growth_path("E7003")
    assert result["status"] == "ok"
    assert result["window_weeks"] == 5
    scores = [t["score"] for t in result["growth_trend"]]
    assert scores == [60, 65, 70, 75, 80]


async def test_growth_path_direction_tech(analytics, service):
    """成长领域全为技术类 → 推荐技术深耕"""
    await _make_user(service, "E7004")
    for i in range(4):
        await _make_eval(
            service,
            "E7004",
            f"2026-W{20 + i:02d}",
            70 + i,
            growth_areas=[
                {
                    "dimension": "代码质量",
                    "score": 70,
                    "evidence": [],
                    "improvement_actions": ["补充单测"],
                },
                {
                    "dimension": "系统架构",
                    "score": 65,
                    "evidence": [],
                    "improvement_actions": ["梳理模块"],
                },
            ],
        )
    result = await analytics.get_growth_path("E7004")
    assert result["recommended_direction"]["direction"] == "技术深耕"
    assert result["recommended_direction"]["tech_signal"] > 0
    assert result["recommended_direction"]["management_signal"] == 0


async def test_growth_path_direction_management(analytics, service):
    """成长领域以管理类为主且具备技术基础 → 推荐管理转型"""
    await _make_user(service, "E7005")
    for i in range(4):
        await _make_eval(
            service,
            "E7005",
            f"2026-W{20 + i:02d}",
            70 + i,
            growth_areas=[
                {
                    "dimension": "代码工程",
                    "score": 70,
                    "evidence": [],
                    "improvement_actions": [],
                },
                {
                    "dimension": "团队协作",
                    "score": 60,
                    "evidence": [],
                    "improvement_actions": ["主持周会"],
                },
                {
                    "dimension": "沟通能力",
                    "score": 62,
                    "evidence": [],
                    "improvement_actions": ["提升表达"],
                },
            ],
        )
    result = await analytics.get_growth_path("E7005")
    d = result["recommended_direction"]
    assert d["direction"] == "管理转型"
    assert d["management_signal"] > d["tech_signal"]
    assert d["tech_signal"] > 0


async def test_growth_path_direction_cross_domain(analytics, service):
    """成长领域技术与管理均衡 → 推荐跨领域"""
    await _make_user(service, "E7006")
    for i in range(4):
        await _make_eval(
            service,
            "E7006",
            f"2026-W{20 + i:02d}",
            70 + i,
            growth_areas=[
                {
                    "dimension": "代码质量",
                    "score": 70,
                    "evidence": [],
                    "improvement_actions": [],
                },
                {
                    "dimension": "团队协作",
                    "score": 60,
                    "evidence": [],
                    "improvement_actions": [],
                },
            ],
        )
    result = await analytics.get_growth_path("E7006")
    d = result["recommended_direction"]
    # 技术 1 次/周 × 4 = 4，管理 1 次/周 × 4 = 4，均衡 → 跨领域
    assert d["direction"] == "跨领域"
    assert d["tech_signal"] == d["management_signal"]


async def test_growth_path_capability_radar_delta(analytics, service):
    """能力雷达：当前 vs 历史的维度差值正确"""
    await _make_user(service, "E7007")
    await _make_eval(
        service,
        "E7007",
        "2026-W20",
        60,
        growth_areas=[
            {
                "dimension": "执行力",
                "score": 60,
                "evidence": [],
                "improvement_actions": [],
            },
            {
                "dimension": "沟通",
                "score": 50,
                "evidence": [],
                "improvement_actions": [],
            },
        ],
    )
    for i in range(1, 4):
        await _make_eval(service, "E7007", f"2026-W{20 + i:02d}", 70)
    await _make_eval(
        service,
        "E7007",
        "2026-W24",
        85,
        growth_areas=[
            {
                "dimension": "执行力",
                "score": 80,
                "evidence": [],
                "improvement_actions": [],
            },
            {
                "dimension": "沟通",
                "score": 55,
                "evidence": [],
                "improvement_actions": [],
            },
        ],
    )
    result = await analytics.get_growth_path("E7007")
    cap = result["capability_change"]
    assert "执行力" in cap["dimensions"]
    idx = cap["dimensions"].index("执行力")
    assert cap["current"][idx] == 80
    assert cap["history"][idx] == 60
    assert cap["delta"][idx] == 20.0


# ---------------- 9.2.3 离职风险 ----------------


async def test_attrition_risk_empty_team(analytics):
    """空团队：分布全零，平均风险 0"""
    result = await analytics.get_attrition_risk([])
    assert result["team_size"] == 0
    assert result["distribution"] == {"low": 0, "medium": 0, "high": 0}
    assert result["avg_risk_score"] == 0
    assert result["members"] == []


async def test_attrition_risk_no_evals(analytics, service):
    """无评估数据：低风险，给出基线建议"""
    await _make_user(service, "E8001")
    result = await analytics.get_attrition_risk(["E8001"])
    m = result["members"][0]
    assert m["risk_level"] == "low"
    assert m["risk_score"] == 0
    assert m["recent_scores"] == []
    assert m["suggestions"]  # 有基线建议


async def test_attrition_risk_low(analytics, service):
    """低风险：得分上升、无申诉、成长维度改善"""
    await _make_user(service, "E8002")
    for i, score in enumerate([60, 65, 70, 75]):
        await _make_eval(
            service,
            "E8002",
            f"2026-W{20 + i:02d}",
            score,
            summary="本周积极完成所有任务，主动承担",
            growth_areas=[
                {
                    "dimension": "执行力",
                    "score": 60 + i,
                    "evidence": [],
                    "improvement_actions": [],
                },
            ],
        )
    result = await analytics.get_attrition_risk(["E8002"])
    m = result["members"][0]
    assert m["risk_level"] == "low"
    assert m["risk_score"] < 30
    assert result["distribution"]["low"] == 1


async def test_attrition_risk_medium(analytics, service):
    """中风险：申诉反馈频次高 + 成长维度停滞，但无连续下滑"""
    await _make_user(service, "E8003")
    evals = []
    for i, score in enumerate([75, 75, 72, 72]):
        ev = await _make_eval(
            service,
            "E8003",
            f"2026-W{20 + i:02d}",
            score,
            summary="表现稳定",
            growth_areas=[
                {
                    "dimension": "执行力",
                    "score": 60,
                    "evidence": [],
                    "improvement_actions": [],
                },
            ],
        )
        evals.append(ev)
    # 4 条申诉/反馈 → 25 分；叠加 1 个停滞成长维度 → +8，合计 33 落入中风险
    for i in range(4):
        await service.create_feedback(
            {
                "feedback_id": f"FB-E8003-{i}",
                "evaluation_id": evals[0].evaluation_id,
                "employee_id": "E8003",
                "type": "appeal",
                "content": "对评估有异议",
            }
        )
    result = await analytics.get_attrition_risk(["E8003"])
    m = result["members"][0]
    assert m["risk_level"] == "medium"
    assert 30 <= m["risk_score"] < 70
    factor_names = {f["factor"] for f in m["factors"]}
    assert "申诉反馈频次高" in factor_names
    assert "成长领域无改善" in factor_names


async def test_attrition_risk_high(analytics, service):
    """高风险：连续下滑 + 投入度下降 + 申诉 + 成长停滞"""
    await _make_user(service, "E8004")
    # 5 周连续下滑：streak=4 → min(30,32)=30
    summaries = [
        "本周积极完成，主动突破",
        "本周主动完成多项任务",
        "本周完成基本任务",
        "本周任务一般",
        "本周进展有限",
    ]
    for i, score in enumerate([80, 75, 70, 65, 60]):
        ev = await _make_eval(
            service,
            "E8004",
            f"2026-W{20 + i:02d}",
            score,
            summary=summaries[i],
            growth_areas=[
                {
                    "dimension": "执行力",
                    "score": 60,
                    "evidence": [],
                    "improvement_actions": [],
                },
            ],
        )
        if i == 0:
            base_eval = ev
    # 3 条申诉 → min(25, 24)=24
    for i in range(3):
        await service.create_feedback(
            {
                "feedback_id": f"FB-E8004-{i}",
                "evaluation_id": base_eval.evaluation_id,
                "employee_id": "E8004",
                "type": "appeal",
                "content": "对评估强烈不满",
            }
        )
    result = await analytics.get_attrition_risk(["E8004"])
    m = result["members"][0]
    assert m["risk_level"] == "high"
    assert m["risk_score"] > 70
    factor_names = {f["factor"] for f in m["factors"]}
    assert "评分持续下降" in factor_names
    assert "投入度词频下降" in factor_names
    assert "申诉反馈频次高" in factor_names
    assert result["distribution"]["high"] == 1


async def test_attrition_risk_mixed_team_distribution(analytics, service):
    """混合团队：低/中/高风险分布计数正确"""
    # 低风险员工
    await _make_user(service, "E9001")
    for i in range(4):
        await _make_eval(service, "E9001", f"2026-W{20 + i:02d}", 60 + i)

    # 高风险员工
    await _make_user(service, "E9002")
    for i, score in enumerate([80, 75, 70, 65, 60]):
        ev = await _make_eval(
            service,
            "E9002",
            f"2026-W{20 + i:02d}",
            score,
            summary="积极" if i == 0 else "一般",
        )
        if i == 0:
            base = ev
    for i in range(3):
        await service.create_feedback(
            {
                "feedback_id": f"FB-E9002-{i}",
                "evaluation_id": base.evaluation_id,
                "employee_id": "E9002",
                "type": "appeal",
                "content": "申诉",
            }
        )

    result = await analytics.get_attrition_risk(["E9001", "E9002"])
    assert result["team_size"] == 2
    assert result["distribution"]["low"] == 1
    assert result["distribution"]["high"] == 1
    # 平均风险分在 0-100
    assert 0 <= result["avg_risk_score"] <= 100
