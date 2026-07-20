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


class MockProvider(BaseProvider):
    """Mock Provider：从 system prompt 提取原始输入并返回相关 JSON，用于流程验证"""

    # 用于从 prompt 中提取原始输入 JSON 的正则
    RAW_INPUTS_RE = re.compile(r"## 当前输入\s*```json\s*(\[.*?\])\s*```", re.DOTALL)

    def __init__(self, config: ProviderConfig):
        super().__init__(config)

    def name(self) -> str:
        return "mock/provider"

    async def chat_completion(
        self,
        messages: List[ChatMessage],
        response_format: Dict[str, str] = None,
    ) -> ChatCompletion:
        prompt = messages[0].content if messages else ""
        raw_content = self._extract_raw_content(prompt)

        tone, score = self._classify(raw_content)
        matched_keywords = self._extract_keywords(raw_content, tone)

        # 从 prompt 末尾的 employee_id / period 占位符简单提取
        employee_id = self._extract_tag(prompt, "employee_id") or "unknown"
        period = self._extract_tag(prompt, "period") or "unknown"

        mock_eval = build_mock_evaluation(
            score=score,
            tone=tone,
            employee_id=employee_id,
            period=period,
            keywords=matched_keywords,
        )
        return ChatCompletion(
            content=json.dumps(mock_eval, ensure_ascii=False),
            model="mock-model",
            usage={"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
        )

    async def health_check(self) -> bool:
        return True

    def _extract_raw_content(self, prompt: str) -> str:
        """从 system prompt 中提取原始输入内容"""
        match = self.RAW_INPUTS_RE.search(prompt)
        if match:
            try:
                raw_inputs = json.loads(match.group(1))
                return " ".join(str(inp.get("content", "")) for inp in raw_inputs)
            except json.JSONDecodeError:
                pass
        return ""

    @staticmethod
    def _extract_tag(prompt: str, tag: str) -> str:
        """简单提取 {tag} 替换后的值（取同一行等号或冒号后的内容）"""
        for line in prompt.splitlines():
            if tag in line:
                # 取行尾 10 个字符作为近似值
                return line.strip().split()[-1][:20]
        return ""

    @staticmethod
    def _classify(content: str) -> tuple[str, int]:
        """根据原始内容判断 tone 与分数"""
        negative_signals = [
            "延期",
            "崩溃",
            "迟到",
            "质量不高",
            "未自测",
            "差距较大",
            "阻塞",
        ]
        positive_signals = ["超额", "提前", "零Bug", "分享", "主导", "优化", "提升"]

        negative_count = sum(1 for s in negative_signals if s in content)
        positive_count = sum(1 for s in positive_signals if s in content)

        if positive_count > negative_count:
            return "positive", 88
        if negative_count > positive_count:
            return "negative", 52
        return "neutral", 72

    @staticmethod
    def _extract_keywords(content: str, tone: str) -> List[str]:
        """从原始输入中提取与设定 archetype 相关的关键词，用于 Mock 输出"""
        keyword_map = {
            "positive": ["高质量", "超额完成", "主导", "优化", "团队"],
            "negative": ["延期", "沟通不及时", "质量不高", "待改进", "未自测"],
            "neutral": ["加班", "独立", "沟通少", "稳健", "完成"],
        }
        candidates = keyword_map.get(tone, [])
        found = [kw for kw in candidates if kw in content]
        return found[:2] if found else candidates[:2]


def build_mock_evaluation(
    score: int,
    tone: str,
    employee_id: str,
    period: str,
    keywords: List[str] = None,
) -> Dict[str, Any]:
    keywords = keywords or []
    keyword_text = "、".join(keywords) if keywords else ""

    if tone == "positive":
        summary = f"本周期整体表现优秀，在{keyword_text}等多个维度上超出团队平均水平，值得继续保持并放大影响力。"
        strengths = [
            "主导完成核心模块重构，性能提升40%",
            "主动辅导新人，提升团队整体代码质量",
        ]
        growth = [
            {
                "dimension": "技术交付",
                "score": 90,
                "evidence": ["主导完成用户画像模块重构，性能提升40%"],
                "improvement_actions": ["继续保持技术影响力，挑战更高复杂度模块"],
            },
            {
                "dimension": "团队协作",
                "score": 86,
                "evidence": ["辅导两名新人完成Code Review"],
                "improvement_actions": ["多组织跨团队技术分享"],
            },
        ]
        risk_flags = []
        harsh = "该员工本周期交付质量与主动性均高于团队平均水平，是当前项目中的核心贡献者，建议继续赋予关键路径任务并纳入晋升观察名单。"
        hidden = ["无显著隐藏风险", "需关注长期高负荷是否可持续"]
    elif tone == "negative":
        summary = f"本周期在任务交付与代码质量方面出现明显问题，涉及{keyword_text}等情况，需要尽快制定改进计划并跟进。"
        strengths = ["能够完成部分基础任务", "日报提交较为规律"]
        growth = [
            {
                "dimension": "任务交付",
                "score": 45,
                "evidence": ["本周任务延期2天，未主动同步阻塞问题"],
                "improvement_actions": ["每日同步风险，避免最后时刻暴露问题"],
            },
            {
                "dimension": "代码质量",
                "score": 48,
                "evidence": ["提交代码未自测导致测试环境崩溃"],
                "improvement_actions": ["建立自测清单，提交前跑通核心用例"],
            },
        ]
        risk_flags = [
            {
                "level": "high",
                "category": "交付风险",
                "description": "多次延期且沟通不及时",
                "suggested_action": "主管需在本周内一对一沟通，明确下周交付承诺",
            }
        ]
        harsh = "该员工当前处于低效与低质量并行的状态，若未来两周无显著改善，建议调整其任务范围并启动绩效改进计划。"
        hidden = ["存在被动等待指令的倾向", "代码自测习惯尚未建立"]
    else:
        summary = f"本周期整体表现稳定，在{keyword_text}等方面交付可靠，但创新性和协作主动性仍有提升空间。"
        strengths = ["独立完成全部指派任务", "对细节把控严格，交付质量合格"]
        growth = [
            {
                "dimension": "工作投入",
                "score": 78,
                "evidence": ["本周独立完成全部指派任务，加班较多"],
                "improvement_actions": ["注意工作负荷分配，避免过度依赖个人加班"],
            },
            {
                "dimension": "协作沟通",
                "score": 68,
                "evidence": ["本周跨团队协作沟通偏少，建议主动同步关键信息"],
                "improvement_actions": ["主动同步关键信息，减少团队信息不对称"],
            },
        ]
        risk_flags = [
            {
                "level": "medium",
                "category": "协作风险",
                "description": "沟通主动性不足",
                "suggested_action": "鼓励参与跨团队会议并同步进展",
            }
        ]
        harsh = "该员工是一名可靠的执行者，但当前大包大揽的工作方式正在形成团队依赖，且缺乏主动分享与协作，长期价值受限。"
        hidden = ["团队对其存在隐性依赖", "加班多但产出边际效益在下降"]

    return {
        "evaluation_id": f"EV-{period}-{employee_id}-MOCK",
        "employee_id": employee_id,
        "period": period,
        "overall_score": score,
        "status": "ai_drafted",
        "employee_view": {
            "summary": summary,
            "strengths": strengths,
            "growth_areas": growth,
            "next_week_focus": [
                "继续保持优势项",
                "重点改进已识别短板",
                "主动同步关键进展",
            ],
        },
        "manager_view": {
            "harsh_assessment": harsh,
            "risk_flags": risk_flags,
            "roi_analysis": "从投入产出比看，该员工当前处于中等偏上水平，但成长曲线需要更明确的管理干预。",
            "reallocation_suggestion": "建议保持当前岗位，同时增加协作型任务比例。",
            "hidden_issues": hidden,
        },
        "audit": {
            "model_name": "mock-model",
            "model_tier": "L0",
            "confidence_score": 0.75,
            "raw_data_refs": ["daily-001"],
            "triggered_rules": ["evidence_first", "dual_view_separation"],
            "processing_time_ms": 500,
            "prompt_version": "v0.1",
        },
    }


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
    parser = argparse.ArgumentParser(description="AgentValue-AI LLM 回归评估")
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
        print(f"\n=== 对比报告 ===")
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
