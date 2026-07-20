"""
LLM-as-Judge 评估框架

用 LLM 对评估输出做质量打分（证据引用准确率、语气分离、幻觉率），
补充现有 Mock Provider 只验流程的不足。

- LLMJudge：接受一个 ChatProvider 实例，调用真实 LLM 对三个维度打分；
- MockJudge：不调真实 LLM，用规则启发式返回分数，便于离线/CI 验证。

用法：
    # 使用 MockJudge（规则启发式，不调真实 LLM）
    python -m eval.llm_judge --dataset eval/dataset.json --mock

    # 输出报告到文件
    python -m eval.llm_judge --dataset eval/dataset.json --mock --output report.json
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.llm_call import call_llm_with_fallback
from core.providers.base import BaseProvider, ChatMessage
from eval.constants import NEGATIVE_WORDS
from eval.evaluate import build_mock_model_router, load_dataset, run_case


class _SingleProviderRouter:
    """Adapter：将单个 Provider 包装成 model_router 接口供 call_llm_with_fallback 使用。

    P1-3：LLMJudge 接受单个 provider 而非 ModelRouter，调用公共 helper 时需要一个
    model_router 形状的对象（提供 get_provider_with_fallback）。此 adapter 仅做形状
    适配，不实现 runtime_reselect（getattr 取不到则 helper 走「无降级」分支直接抛首次异常）。
    """

    def __init__(self, provider: BaseProvider, tier: str = "L0"):
        self._provider = provider
        self._tier = tier

    async def get_provider_with_fallback(self):
        return self._provider, self._tier


class LLMJudge:
    """
    LLM-as-Judge：用一个 ChatProvider 实例对评估输出做质量打分。

    三个维度：
    - 证据引用准确率（evidence）：证据是否真实引用自 raw_inputs；
    - 语气分离（tone_separation）：员工视图是否建设性、管理视图是否尖锐；
    - 幻觉率（hallucination）：是否存在无证据支撑的结论。
    """

    def __init__(self, provider: Optional[BaseProvider]):
        # 接受一个 ChatProvider 实例；MockJudge 不使用它
        self.provider = provider

    async def judge_evidence(self, eval_result: dict, raw_inputs: list) -> dict:
        """判断证据是否真实引用自 raw_inputs。返回 {score: 0-100, reason: str}"""
        prompt = self._build_evidence_prompt(eval_result, raw_inputs)
        return await self._score_with_llm(prompt, dimension="evidence")

    async def judge_tone_separation(self, eval_result: dict) -> dict:
        """判断员工视图是否建设性、管理视图是否尖锐。返回 {score, reason}"""
        prompt = self._build_tone_prompt(eval_result)
        return await self._score_with_llm(prompt, dimension="tone")

    async def judge_hallucination(self, eval_result: dict, raw_inputs: list) -> dict:
        """判断是否有幻觉（无证据结论）。返回 {score, reason}"""
        prompt = self._build_hallucination_prompt(eval_result, raw_inputs)
        return await self._score_with_llm(prompt, dimension="hallucination")

    async def judge_all(self, eval_result: dict, raw_inputs: list) -> dict:
        """聚合三个维度，返回 {evidence, tone, hallucination, overall_score}"""
        evidence = await self.judge_evidence(eval_result, raw_inputs)
        tone = await self.judge_tone_separation(eval_result)
        hallucination = await self.judge_hallucination(eval_result, raw_inputs)
        overall_score = round(
            (evidence["score"] + tone["score"] + hallucination["score"]) / 3, 2
        )
        return {
            "evidence": evidence,
            "tone": tone,
            "hallucination": hallucination,
            "overall_score": overall_score,
        }

    # LLMJudge 内部辅助：构造 prompt + 调用并解析 LLM 返回

    def _build_evidence_prompt(self, eval_result: dict, raw_inputs: list) -> str:
        evidence_items = self._collect_evidence(eval_result)
        return (
            "你是一个评估质量审核员。请判断以下评估结果中引用的证据是否真实来源于原始输入。\n\n"
            f"## 原始输入\n{json.dumps(raw_inputs, ensure_ascii=False)}\n\n"
            f"## 评估中引用的证据\n{json.dumps(evidence_items, ensure_ascii=False)}\n\n"
            '请返回 JSON：{"score": 0-100 的整数, "reason": 简短中文说明}。'
            "score 越高表示证据引用越准确（全部可在原始输入中找到依据则高分）。"
        )

    def _build_tone_prompt(self, eval_result: dict) -> str:
        employee_view = eval_result.get("employee_view", {})
        manager_view = eval_result.get("manager_view", {})
        return (
            "你是一个评估质量审核员。请判断评估输出的语气分离是否合理：\n"
            "- 员工视图应建设性、正向引导，避免打击性负面词；\n"
            "- 管理视图应尖锐、直击风险，包含 risk_flags。\n\n"
            f"## 员工视图\n{json.dumps(employee_view, ensure_ascii=False)}\n\n"
            f"## 管理视图\n{json.dumps(manager_view, ensure_ascii=False)}\n\n"
            '请返回 JSON：{"score": 0-100 的整数, "reason": 简短中文说明}。'
        )

    def _build_hallucination_prompt(self, eval_result: dict, raw_inputs: list) -> str:
        conclusions = self._collect_conclusions(eval_result)
        return (
            "你是一个评估质量审核员。请判断以下结论是否都有原始输入中的证据支撑，"
            "识别无证据支撑的幻觉结论。\n\n"
            f"## 原始输入\n{json.dumps(raw_inputs, ensure_ascii=False)}\n\n"
            f"## 评估结论\n{json.dumps(conclusions, ensure_ascii=False)}\n\n"
            '请返回 JSON：{"score": 0-100 的整数, "reason": 简短中文说明}。'
            "score 越高表示幻觉越少（全部结论有证据支撑则高分）。"
        )

    async def _score_with_llm(self, prompt: str, dimension: str) -> dict:
        """调用 provider 获取打分，解析 JSON 返回 {score, reason}

        P1-3：复用 core.llm_call.call_llm_with_fallback 公共 helper，与 agent/graph.py
        共享「LLM 调用 + 失败降级」逻辑。LLMJudge 持有单个 provider 而非 ModelRouter，
        通过 _SingleProviderRouter 适配为 model_router 形状传入 helper。
        """
        if self.provider is None:
            raise RuntimeError(f"LLMJudge 需要 provider 才能进行 {dimension} 打分")
        router = _SingleProviderRouter(self.provider)
        messages = [ChatMessage(role="system", content=prompt)]
        completion, _tier = await call_llm_with_fallback(router, messages=messages)
        return self._parse_score(completion.content, dimension)

    @staticmethod
    def _parse_score(content: str, dimension: str) -> dict:
        """从 LLM 返回中解析 score 与 reason，做范围裁剪与容错"""
        try:
            data = json.loads(content)
            score = int(data.get("score", 0))
            reason = str(data.get("reason", ""))
        except (json.JSONDecodeError, ValueError, TypeError):
            score = 0
            reason = f"{dimension} 打分解析失败：{content[:80]}"
        score = max(0, min(100, score))
        return {"score": score, "reason": reason}

    @staticmethod
    def _collect_evidence(eval_result: dict) -> list:
        """收集评估结果中所有 growth_areas 下的 evidence 字段"""
        items: List[Any] = []
        for area in eval_result.get("employee_view", {}).get("growth_areas", []) or []:
            items.extend(area.get("evidence", []) or [])
        return items

    @staticmethod
    def _collect_conclusions(eval_result: dict) -> list:
        """收集评估结果中的结论性文本（summary + harsh_assessment + evidence）"""
        items: List[str] = []
        emp = eval_result.get("employee_view", {})
        items.append(emp.get("summary", ""))
        for area in emp.get("growth_areas", []) or []:
            items.extend(area.get("evidence", []) or [])
        mgr = eval_result.get("manager_view", {})
        items.append(mgr.get("harsh_assessment", ""))
        return [c for c in items if c]


class MockJudge(LLMJudge):
    """
    MockJudge：不调真实 LLM，用规则启发式返回分数。

    启发式规则：
    - judge_evidence：检查 evidence 是否作为子串真实出现在 raw_inputs 中，
      全部可溯源则 100 分，按可溯源比例线性打分；
    - judge_tone_separation：员工视图无负面词 +50，管理视图有 risk_flags +30，
      有 harsh_assessment +20，三者叠加得到 0-100；
    - judge_hallucination：每个 growth_areas 维度需有 evidence 且可溯源，
      按有据维度比例线性打分，缺证据或不可溯源均扣分。
    """

    async def judge_evidence(self, eval_result: dict, raw_inputs: list) -> dict:
        evidence_items = self._collect_evidence(eval_result)
        if not evidence_items:
            return {"score": 20, "reason": "评估结果中未引用任何证据"}
        raw_text = " ".join(str(inp.get("content", "")) for inp in raw_inputs)
        matched = sum(
            1
            for e in evidence_items
            if isinstance(e, str) and e.strip() and e in raw_text
        )
        ratio = matched / len(evidence_items)
        score = int(round(ratio * 100))
        if ratio == 1.0:
            reason = f"全部 {len(evidence_items)} 条证据均可溯源至原始输入"
        elif ratio >= 0.5:
            reason = f"{matched}/{len(evidence_items)} 条证据可在原始输入中找到依据"
        else:
            reason = (
                f"仅 {matched}/{len(evidence_items)} 条证据可溯源，存在编造证据风险"
            )
        return {"score": score, "reason": reason}

    async def judge_tone_separation(self, eval_result: dict) -> dict:
        employee_view = eval_result.get("employee_view", {})
        manager_view = eval_result.get("manager_view", {})
        emp_text = json.dumps(employee_view, ensure_ascii=False)
        negative_hits = [w for w in NEGATIVE_WORDS if w in emp_text]
        risk_flags = manager_view.get("risk_flags", []) or []
        harsh = manager_view.get("harsh_assessment", "")

        score = 0
        reasons: List[str] = []
        # 员工视图建设性：无负面词
        if not negative_hits:
            score += 50
        else:
            reasons.append(f"员工视图出现负面词 {negative_hits}")
        # 管理视图尖锐：有 risk_flags
        if risk_flags:
            score += 30
        else:
            reasons.append("管理视图缺少 risk_flags")
        # 管理视图尖锐：有 harsh_assessment
        if harsh:
            score += 20
        else:
            reasons.append("管理视图缺少 harsh_assessment")
        reason = (
            "；".join(reasons)
            if reasons
            else "员工视图建设性良好，管理视图风险标注完整"
        )
        return {"score": score, "reason": reason}

    async def judge_hallucination(self, eval_result: dict, raw_inputs: list) -> dict:
        growth_areas = (
            eval_result.get("employee_view", {}).get("growth_areas", []) or []
        )
        if not growth_areas:
            return {"score": 30, "reason": "缺少 growth_areas，无法判定结论是否有据"}
        raw_text = " ".join(str(inp.get("content", "")) for inp in raw_inputs)
        no_evidence_count = 0
        unsupported_count = 0
        for area in growth_areas:
            evidence = area.get("evidence", []) or []
            if not evidence:
                no_evidence_count += 1
                continue
            # 任一证据可溯源则该维度视为有据
            if not any(
                isinstance(e, str) and e.strip() and e in raw_text for e in evidence
            ):
                unsupported_count += 1
        total = len(growth_areas)
        supported = total - no_evidence_count - unsupported_count
        ratio = supported / total if total else 0
        score = int(round(ratio * 100))
        if no_evidence_count == 0 and unsupported_count == 0:
            reason = f"全部 {total} 个维度的结论均有证据支撑且可溯源"
        else:
            reason = (
                f"{total} 个维度中 {supported} 个有据可查，"
                f"{no_evidence_count} 个缺证据，{unsupported_count} 个证据不可溯源"
            )
        return {"score": score, "reason": reason}


async def run_dataset_with_judge(
    dataset: list, judge: LLMJudge, tier: str = "L0"
) -> List[Dict[str, Any]]:
    """对整个数据集用 MockProvider 生成评估，再用 judge 打分"""
    router = build_mock_model_router(tier=tier)
    results: List[Dict[str, Any]] = []
    for case in dataset:
        try:
            graph_result = await run_case(case, router)
            eval_result = graph_result.get("parsed_evaluation", {}) or {}
            judge_result = await judge.judge_all(
                eval_result, case.get("raw_inputs", [])
            )
            entry = {
                "employee_id": case.get("employee_id"),
                "period": case.get("period"),
                "archetype": case.get("archetype", "unknown"),
                "overall_score": eval_result.get("overall_score"),
                "judge": judge_result,
                "error": graph_result.get("error"),
            }
        except Exception as e:
            entry = {
                "employee_id": case.get("employee_id"),
                "period": case.get("period"),
                "archetype": case.get("archetype", "unknown"),
                "judge": None,
                "error": str(e),
            }
        results.append(entry)
        tag = "OK" if entry.get("judge") else "ERR"
        score = entry["judge"]["overall_score"] if entry.get("judge") else "-"
        print(f"[{tag}] {entry['employee_id']} {entry['period']} judge_score={score}")
    return results


async def main():
    parser = argparse.ArgumentParser(description="AgentValue-AI LLM-as-Judge 评估")
    parser.add_argument(
        "--dataset", default=None, help="数据集路径，默认 eval/dataset.json"
    )
    parser.add_argument(
        "--mock", action="store_true", help="使用 MockJudge（规则启发式，不调真实 LLM）"
    )
    parser.add_argument("--output", default=None, help="报告输出路径")
    args = parser.parse_args()

    dataset = load_dataset(args.dataset)
    print(f"加载了 {len(dataset)} 条用例")

    if args.mock:
        judge: LLMJudge = MockJudge(None)
    else:
        # 真实 LLM 模式：构造默认 Provider（需配置 API Key）
        from core.config import Settings
        from core.model_router import ModelRouter

        settings = Settings()
        router = ModelRouter(settings)
        provider, _ = await router.get_provider_with_fallback()
        judge = LLMJudge(provider)

    results = await run_dataset_with_judge(dataset, judge)

    valid = [r for r in results if r.get("judge")]
    if valid:
        avg = round(sum(r["judge"]["overall_score"] for r in valid) / len(valid), 2)
    else:
        avg = 0
    print(f"\nJudge 平均分: {avg} （{len(valid)}/{len(results)} 条有效）")

    if args.output:
        payload = {
            "total": len(results),
            "valid": len(valid),
            "average_judge_score": avg,
            "results": results,
        }
        Path(args.output).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"报告已保存: {args.output}")

    return 0 if valid else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
