"""
开发演示数据种子脚本
插入一条已审批的评估和对应原始输入，用于前端联调。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import get_settings
from core.database import AsyncSessionLocal, Base, engine
from memory.vector_store import ChromaCompanyKB, ChromaMemoryStore
from models.models import Evaluation, RawInput, User
from sqlalchemy import select


SAMPLE_EVALUATION = {
    "evaluation_id": "EV-DEMO-001",
    "employee_id": "E1001",
    "period": "2026-W25",
    "overall_score": 76.75,
    "status": "approved",
    "employee_view": {
        "summary": "本周你在订单中心核心接口重构及推动跨团队协作方面取得了积极成果，并主动输出了可复用的团队资产。在测试自检环节可以进一步优化，以提升交付稳定性。",
        "strengths": [
            "成功完成订单中心核心接口重构，客户反馈接口延迟下降40%，体现了扎实的技术能力",
            "主动组织跨团队对齐会，成功推动阻塞两天的JIRA-2051进入联调阶段，展现了出色的协作与问题解决能力",
            "积极进行Code Review并辅导新人，输出《接口变更 checklist》供团队复用",
        ],
        "growth_areas": [
            {
                "dimension": "交付质量与风险控制",
                "score": 65.0,
                "evidence": [
                    "周一完成了订单中心核心接口重构，但自测不够充分，导致周二预发环境出现3处回归Bug，花了大半天定位修复"
                ],
                "improvement_actions": [
                    "在提交核心功能改动前，可增加一个简化的自测Checklist，覆盖关键回归场景",
                    "考虑在重构任务完成后，预留固定时间进行自我代码审查和关键路径测试",
                ],
            },
            {
                "dimension": "需求管理与沟通",
                "score": 70.0,
                "evidence": [
                    "与产品经理在排期上发生争执，员工坚持要先补全测试用例再排新需求，双方最终达成一致由测试同学介入把关"
                ],
                "improvement_actions": [
                    "在与产品方沟通时，可以尝试更早同步风险与约束",
                    "将“测试用例补全”作为一项明确的前置任务纳入计划",
                ],
            },
        ],
        "next_week_focus": [
            "根据客户要求，补齐重构接口的幂等性和超时熔断文档",
            "将本次重构中遇到的回归Bug模式进行简单归类",
            "继续关注JIRA-2051的联调进展，确保推动落地",
        ],
    },
    "manager_view": {
        "harsh_assessment": "该员工本周展现了高价值的个人技术产出与团队协作杠杆效应，但其交付流程的严谨性存在明显短板。",
        "risk_flags": [
            {
                "level": "medium",
                "category": "交付质量风险",
                "description": "核心接口重构因自测不充分，导致预发环境出现3处回归Bug，消耗了额外半天的排障与修复时间。",
                "suggested_action": "要求其为关键重构任务制定并遵守一个简单的自测-提交清单。",
            }
        ],
        "roi_analysis": "本周投入产出呈现高波动性。正面产出明确，负面产出是低效的救火成本。",
        "reallocation_suggestion": "建议继续保持其在核心接口重构、跨团队协调及技术赋能方面的任务分配。",
        "hidden_issues": [
            "员工在与产品方争执时表现出的技术理想主义倾向，在高压时可能演变为协作障碍。"
        ],
    },
    "audit": {
        "model_name": "gpt-4o-mini",
        "model_tier": "L0",
        "confidence_score": 0.7,
        "raw_data_refs": [
            "daily-001",
            "daily-002",
            "daily-003",
            "task-001",
            "meeting-001",
        ],
        "triggered_rules": ["evidence_first", "dual_view_separation"],
        "processing_time_ms": 40954,
        "prompt_version": "v0.1",
    },
}


async def main():
    settings = get_settings()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_id == "E1001"))
        user = result.scalar_one_or_none()
        if not user:
            session.add(
                User(user_id="E1001", name="张三", role="employee", department="技术部")
            )

        # 按 evaluation_id 唯一列查询（主键是自增 id，不能用 session.get 查 evaluation_id）
        existing = await session.execute(
            select(Evaluation).where(Evaluation.evaluation_id == "EV-DEMO-001")
        )
        if existing.scalar_one_or_none():
            print("演示数据已存在，跳过")
            return

        raw_inputs = [
            RawInput(
                input_id="daily-001",
                employee_id="E1001",
                period="2026-W25",
                type="daily_report",
                content="周一完成了订单中心核心接口重构，但自测不够充分，导致周二预发环境出现3处回归Bug，花了大半天定位修复。",
            ),
            RawInput(
                input_id="daily-002",
                employee_id="E1001",
                period="2026-W25",
                type="daily_report",
                content="周三主动组织跨团队对齐会，推动支付依赖方确认接口文档，最终把阻塞2天的JIRA-2051推进到联调阶段。",
            ),
            RawInput(
                input_id="daily-003",
                employee_id="E1001",
                period="2026-W25",
                type="daily_report",
                content="周四周五集中做Code Review，辅导两名新人完成3个PR合并，并输出《接口变更 checklist》供团队复用。",
            ),
            RawInput(
                input_id="task-001",
                employee_id="E1001",
                period="2026-W25",
                type="task_progress",
                content="客户侧反馈本次重构后接口延迟下降40%，但要求下周补齐幂等性和超时熔断文档。",
            ),
            RawInput(
                input_id="meeting-001",
                employee_id="E1001",
                period="2026-W25",
                type="meeting_note",
                content="与产品经理在排期上发生争执，员工坚持要先补全测试用例再排新需求，双方最终达成一致由测试同学介入把关。",
            ),
        ]
        session.add_all(raw_inputs)

        evaluation = Evaluation(
            evaluation_id=SAMPLE_EVALUATION["evaluation_id"],
            employee_id=SAMPLE_EVALUATION["employee_id"],
            period=SAMPLE_EVALUATION["period"],
            overall_score=SAMPLE_EVALUATION["overall_score"],
            status=SAMPLE_EVALUATION["status"],
            employee_view=SAMPLE_EVALUATION["employee_view"],
            manager_view=SAMPLE_EVALUATION["manager_view"],
            audit=SAMPLE_EVALUATION["audit"],
            approver_id="M001",
        )
        session.add(evaluation)
        await session.commit()

        # 同步写入真实向量记忆与知识库
        memory_store = ChromaMemoryStore(settings=settings)
        await memory_store.add_memory(
            SAMPLE_EVALUATION["employee_id"],
            {
                "period": SAMPLE_EVALUATION["period"],
                "summary": SAMPLE_EVALUATION["employee_view"]["summary"],
                "overall_score": SAMPLE_EVALUATION["overall_score"],
                "status": SAMPLE_EVALUATION["status"],
            },
        )

        kb = ChromaCompanyKB(settings=settings)
        await kb.add_document(
            kb_id="kb-001",
            title="绩效评估维度定义",
            content="技术能力：代码质量、架构设计、技术选型与重构能力。协作能力：跨团队沟通、Code Review、新人辅导。交付质量：自测充分度、回归 Bug 控制、文档完整性。",
        )
        await kb.add_document(
            kb_id="kb-002",
            title="公司价值观",
            content="务实：以真实数据和可验证结果说话，反对做样子。成长：把每次任务当作学习机会，主动复盘。协作：优先解决阻塞，推动整体目标达成。",
        )
        print("演示数据已插入: EV-DEMO-001（含向量记忆与知识库）")


if __name__ == "__main__":
    asyncio.run(main())
