"""
LLM 输出回归评估脚本

用法：
    # 使用真实 LLM（需配置 OPENAI_API_KEY 或本地模型）
    python -m eval.evaluate

    # 使用 Mock Provider 跑通流程
    python -m eval.evaluate --mock

    # 指定档位
    python -m eval.evaluate --tier L0
"""

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.graph import create_evaluation_graph
from agent.prompt_loader import PromptLoader
from agent.tools import AgentToolkit, DummyCompanyKB, DummyMemoryStore
from core.config import Settings
from core.model_router import ModelRouter
from core.providers.base import (
    BaseProvider,
    ChatCompletion,
    ChatMessage,
    ProviderConfig,
)
from eval.constants import NEGATIVE_WORDS
from schemas import EmployeeEvaluation


class VersionedPromptLoader(PromptLoader):
    """
    指向特定历史版本的 PromptLoader：render 时渲染指定版本快照，
    version() 返回该版本号。用于 Prompt 变更回归（版本对比 / 变更门禁）。
    """

    def __init__(self, prompts_dir=None, version: str = "v0.1"):
        super().__init__(prompts_dir=prompts_dir)
        self.version_target = version

    def render(
        self,
        name: str,
        raw_inputs: List[Dict[str, Any]],
        employee_history: List[Dict[str, Any]] = None,
        company_kb: List[Dict[str, Any]] = None,
        employee_id: str = "",
        period: str = "",
    ) -> str:
        return self.render_version(
            name,
            self.version_target,
            raw_inputs=raw_inputs,
            employee_history=employee_history,
            company_kb=company_kb,
            employee_id=employee_id,
            period=period,
        )

    def version(self, name: str) -> str:
        return self.version_target


def load_dataset(path: str = None) -> List[Dict[str, Any]]:
    if path is None:
        path = Path(__file__).parent / "dataset.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# MockProvider / build_mock_evaluation 已提升为 core.providers 的一等公民，
# 使运行中的 HTTP 服务（LLM_MOCK_MODE=true）与本离线评测脚本共用同一份实现，
# 避免两处 Mock 行为漂移。此处重新导出以保持既有导入路径向后兼容。
from core.providers.mock_provider import (  # noqa: E402
    MockProvider,
    build_mock_evaluation,
)


def check_employee_view_no_negative_words(eval_result: dict) -> Tuple[bool, str]:
    employee_view = json.dumps(eval_result.get("employee_view", {}), ensure_ascii=False)
    hits = [w for w in NEGATIVE_WORDS if w in employee_view]
    if hits:
        return False, f"员工视图出现负面词: {hits}"
    return True, "OK"


def check_evidence_cited(eval_result: dict) -> Tuple[bool, str]:
    growth_areas = eval_result.get("employee_view", {}).get("growth_areas", [])
    if not growth_areas:
        return False, "缺少 growth_areas"
    for area in growth_areas:
        evidence = area.get("evidence", [])
        if not evidence or all(len(e.strip()) < 5 for e in evidence):
            return False, f"维度 {area.get('dimension')} 的证据引用不足"
    return True, "OK"


def check_overall_score_range(
    eval_result: dict, expected_range: list
) -> Tuple[bool, str]:
    score = eval_result.get("overall_score")
    low, high = expected_range
    if score is None or not (low <= score <= high):
        return False, f"overall_score {score} 不在期望区间 [{low}, {high}]"
    return True, "OK"


def check_contains(eval_result: dict, expected_contains: List[str]) -> Tuple[bool, str]:
    text = json.dumps(eval_result, ensure_ascii=False)
    missing = [w for w in expected_contains if w not in text]
    if missing:
        return False, f"输出未包含期望关键词: {missing}"
    return True, "OK"


def check_view_keys(eval_result: dict, expected_keys: List[str]) -> Tuple[bool, str]:
    employee_view = eval_result.get("employee_view", {})
    missing = [k for k in expected_keys if k not in employee_view]
    if missing:
        return False, f"employee_view 缺少字段: {missing}"
    return True, "OK"


def validate_schema(eval_result: dict) -> Tuple[bool, str]:
    try:
        EmployeeEvaluation.model_validate(eval_result)
        return True, "OK"
    except ValidationError as e:
        return False, f"Schema 校验失败: {e}"


def evaluate_case(
    case: dict, eval_result: dict, skip_contains: bool = False, skip_score: bool = False
) -> dict:
    contains_result = (
        (True, "skipped")
        if skip_contains
        else check_contains(eval_result, case["expected_contains"])
    )
    score_result = (
        (True, "skipped")
        if skip_score
        else check_overall_score_range(
            eval_result, case["expected_overall_score_range"]
        )
    )
    results = {
        "employee_id": case["employee_id"],
        "period": case["period"],
        "archetype": case.get("archetype", "unknown"),
        "schema_valid": validate_schema(eval_result),
        "no_negative_words": check_employee_view_no_negative_words(eval_result),
        "evidence_cited": check_evidence_cited(eval_result),
        "score_in_range": score_result,
        "contains_expected": contains_result,
        "view_keys_present": check_view_keys(eval_result, case["expected_view_keys"]),
    }
    results["passed"] = all(r[0] for r in results.values() if isinstance(r, tuple))
    return results


async def run_case(
    case: dict,
    model_router: ModelRouter,
    prompt_loader: PromptLoader = None,
) -> Dict[str, Any]:
    """使用真实或 mock 模型运行单条用例"""
    graph = create_evaluation_graph(
        toolkit=AgentToolkit(DummyMemoryStore(), DummyCompanyKB()),
        model_router=model_router,
        prompt_loader=prompt_loader or PromptLoader(),
    )
    result = await graph.ainvoke(
        {
            "employee_id": case["employee_id"],
            "period": case["period"],
            "raw_inputs": case["raw_inputs"],
            "messages": [],
        }
    )
    return result


async def run_dataset(
    dataset: List[Dict[str, Any]],
    model_router: ModelRouter,
    prompt_loader: PromptLoader = None,
    skip_contains: bool = False,
    skip_score: bool = False,
) -> List[Dict[str, Any]]:
    """对整个数据集运行回归，返回逐条结果"""
    results: List[Dict[str, Any]] = []
    for case in dataset:
        try:
            graph_result = await run_case(case, model_router, prompt_loader)
            eval_result = graph_result.get("parsed_evaluation", {})
            case_result = evaluate_case(
                case,
                eval_result,
                skip_contains=skip_contains,
                skip_score=skip_score,
            )
            case_result["overall_score"] = eval_result.get("overall_score")
            case_result["error"] = graph_result.get("error")
        except Exception as e:
            case_result = {
                "employee_id": case["employee_id"],
                "period": case["period"],
                "archetype": case.get("archetype", "unknown"),
                "passed": False,
                "overall_score": None,
                "error": str(e),
            }
        results.append(case_result)
        status = "PASS" if case_result["passed"] else "FAIL"
        print(
            f"[{status}] {case['employee_id']} {case['period']} ({case.get('archetype', '')})"
        )
    return results


def compare_versions(
    current_results: List[Dict[str, Any]],
    version_results: List[Dict[str, Any]],
    version: str,
    score_delta_threshold: float = 5.0,
) -> Dict[str, Any]:
    """
    对比当前 Prompt 与指定版本的回归结果，输出变更门禁报告：
    - pass 计数变化
    - 逐条 score delta
    - 是否存在回归（新版本 current 用例由通过变为失败，或分数显著下降）
    语义约定：current_results 为待门禁的新候选 Prompt 结果，
    version_results 为已归档的基线版本（如 v0.1）结果；
    回归 = 新候选比基线变差。
    """
    current_by_key = {(r["employee_id"], r["period"]): r for r in current_results}
    deltas = []
    regressions = []
    for vr in version_results:
        key = (vr["employee_id"], vr["period"])
        cr = current_by_key.get(key, {})
        cur_score = cr.get("overall_score")
        ver_score = vr.get("overall_score")
        # score_delta = 新候选 - 基线；负值表示新候选分数下降
        score_delta = None
        if cur_score is not None and ver_score is not None:
            score_delta = round(cur_score - ver_score, 2)
        deltas.append(
            {
                "employee_id": vr["employee_id"],
                "period": vr["period"],
                "current_score": cur_score,
                "version_score": ver_score,
                "score_delta": score_delta,
                "current_passed": cr.get("passed"),
                "version_passed": vr.get("passed"),
            }
        )
        # 回归判定：基线通过但新候选失败，或新候选分数下降超过阈值
        if vr.get("passed") and not cr.get("passed"):
            regressions.append(
                {
                    **deltas[-1],
                    "reason": "用例在新版本上由通过变为失败",
                }
            )
        elif score_delta is not None and score_delta < -score_delta_threshold:
            regressions.append(
                {
                    **deltas[-1],
                    "reason": f"分数下降 {abs(score_delta)} 超过阈值 {score_delta_threshold}",
                }
            )

    current_pass = sum(1 for r in current_results if r.get("passed"))
    version_pass = sum(1 for r in version_results if r.get("passed"))
    return {
        "version": version,
        "current_pass": current_pass,
        "current_total": len(current_results),
        "version_pass": version_pass,
        "version_total": len(version_results),
        "pass_delta": current_pass - version_pass,
        "regressions": regressions,
        "has_regression": len(regressions) > 0,
        "deltas": deltas,
    }


def build_mock_model_router(tier: str = "L0") -> ModelRouter:
    """构造使用 Mock Provider 的 ModelRouter，用于无 API Key 验证"""
    settings = Settings(model_tier=tier)
    router = ModelRouter(settings)
    router._tier_map[tier].provider_type = "mock"

    original_get_provider = router.get_provider

    def mock_get_provider(tier=None):
        selected = tier or router.get_recommended_tier()
        if router._tier_map[selected].provider_type == "mock":
            return MockProvider(ProviderConfig(model_name="mock"))
        return original_get_provider(selected)

    router.get_provider = mock_get_provider
    return router


async def main():
    parser = argparse.ArgumentParser(description="AgentValue LLM 回归评估")
    parser.add_argument(
        "--mock", action="store_true", help="使用 Mock Provider 跑通流程"
    )
    parser.add_argument("--tier", default=None, help="强制模型档位 L0/L1/L2/L3")
    parser.add_argument("--dataset", default=None, help="数据集路径")
    parser.add_argument("--output", default=None, help="报告输出路径")
    parser.add_argument(
        "--prompt-version",
        default=None,
        help="指定 Prompt 版本快照运行回归（如 v0.1），不指定则使用当前版本",
    )
    parser.add_argument(
        "--compare",
        default=None,
        help="变更门禁：运行当前版本与指定版本并对比，报告 pass 变化与分数回归",
    )
    args = parser.parse_args()

    dataset = load_dataset(args.dataset)
    print(f"加载了 {len(dataset)} 条回归用例")

    if args.mock:
        model_router = build_mock_model_router(tier=args.tier or "L0")
    else:
        settings = Settings(model_tier=args.tier or "auto")
        model_router = ModelRouter(settings)

    # Prompt 变更门禁：对比当前版本与目标版本
    if args.compare:
        version = args.compare
        print(f"\n=== 变更门禁：对比当前版本 vs {version} ===")
        print("\n--- 当前版本 ---")
        current_results = await run_dataset(
            dataset,
            model_router,
            prompt_loader=PromptLoader(),
            skip_contains=args.mock,
            skip_score=args.mock,
        )
        print(f"\n--- 目标版本 {version} ---")
        version_loader = VersionedPromptLoader(version=version)
        version_results = await run_dataset(
            dataset,
            model_router,
            prompt_loader=version_loader,
            skip_contains=args.mock,
            skip_score=args.mock,
        )
        report = compare_versions(current_results, version_results, version)
        print("\n=== 对比报告 ===")
        print(f"当前版本通过: {report['current_pass']}/{report['current_total']}")
        print(
            f"目标版本 {version} 通过: {report['version_pass']}/{report['version_total']}"
        )
        print(f"pass 增量: {report['pass_delta']:+d}")
        if report["has_regression"]:
            print(
                f"⚠️  检测到 {len(report['regressions'])} 处回归，不建议发布该 Prompt 变更："
            )
            for reg in report["regressions"]:
                print(f"  - {reg['employee_id']} {reg['period']}: {reg['reason']}")
        else:
            print("✅ 未检测到回归，该 Prompt 变更可发布。")

        if args.output:
            payload = {
                "current_results": current_results,
                "version_results": version_results,
                "compare_report": report,
            }
            Path(args.output).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"报告已保存: {args.output}")
        return 1 if report["has_regression"] else 0

    # 单版本回归（当前或指定版本）
    prompt_loader = (
        VersionedPromptLoader(version=args.prompt_version)
        if args.prompt_version
        else PromptLoader()
    )
    if args.prompt_version:
        print(f"使用 Prompt 版本: {args.prompt_version}")

    results = await run_dataset(
        dataset,
        model_router,
        prompt_loader=prompt_loader,
        skip_contains=args.mock,
        skip_score=args.mock,
    )

    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    print(f"\n评估结果: {passed}/{total} 通过")

    if args.output:
        Path(args.output).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"报告已保存: {args.output}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
