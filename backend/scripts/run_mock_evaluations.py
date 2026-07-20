"""
Mock 员工画像评估脚本
使用 5 类典型员工画像跑通 LangGraph 评估流程，验证系统端到端能力。

用法：
    cd backend
    python scripts/run_mock_evaluations.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.graph import create_evaluation_graph
from agent.prompt_loader import PromptLoader
from agent.tools import AgentToolkit, DummyCompanyKB, DummyMemoryStore
from core.config import Settings
from core.model_router import ModelRouter
from data.loader import ProfileLoader


async def main():
    settings = Settings(model_tier="L0")
    loader = ProfileLoader()
    toolkit = AgentToolkit(DummyMemoryStore(), DummyCompanyKB())
    router = ModelRouter(settings)
    prompt_loader = PromptLoader()
    graph = create_evaluation_graph(toolkit, router, prompt_loader)

    for profile in loader.list_profiles():
        employee_id = profile["employee_id"]
        period = loader.get_latest_period(employee_id)
        raw_inputs = loader.get_inputs(employee_id, period)

        print(f"\n{'=' * 60}")
        print(f"员工: {profile['name']} ({profile['archetype']})  周期: {period}")
        print(f"{'=' * 60}")

        initial_state = {
            "employee_id": employee_id,
            "period": period,
            "raw_inputs": raw_inputs,
            "messages": [],
        }

        try:
            result = await graph.ainvoke(initial_state)
            if result.get("error"):
                print(f"[ERROR] {result['error']}")
                continue

            evaluation = result.get("parsed_evaluation", {})
            print(f"状态: {result.get('status')}")
            print(f"综合得分: {evaluation.get('overall_score')}")
            print(f"模型档位: {evaluation.get('audit', {}).get('model_tier')}")
            print(
                f"员工视图总结: {evaluation.get('employee_view', {}).get('summary', '')[:80]}..."
            )
        except Exception as e:
            print(f"[EXCEPTION] {e}")


if __name__ == "__main__":
    asyncio.run(main())
