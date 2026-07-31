"""离线 Mock Provider —— 无需任何 LLM 凭证即可跑通全链路。

**为什么需要它（企业落地视角）**

真实交付中有三类场景拿不到（或不该用）线上大模型：

1. **POC / 售前演示**：客户内网未开通外网，或尚未采购模型额度。
2. **CI / E2E 自动化**：每次流水线都真调 LLM 既慢又贵，且结果不确定，
   无法做断言。
3. **信创 / 涉密环境**：数据不得出域，交付初期只验证流程与权限。

本模块把原先只存在于离线评测脚本 ``eval/evaluate.py`` 中的 Mock 实现提升为
``core.providers`` 的一等公民，使**运行中的 HTTP 服务**也能以确定性方式跑通
「创建评估 → LangGraph 执行 → 落库 → 双视图读取」的完整因果链路。

启用方式::

    LLM_MOCK_MODE=true

安全护栏：``AGENTVALUE_ENV=production`` 时禁止开启（见 ``core/config.py``
的 validator），避免误把假数据当成真实绩效结论。

实现要点：
- 输出**严格贴合** ``agent/graph.py::_make_parse_output`` 期望的 JSON schema，
  含 employee_view / manager_view 双视图分离与 audit 审计字段。
- 依据输入文本的正负向信号做确定性打分，同一输入永远得到同一结果，
  便于断言与回归对比。
- 同时实现 ``stream_chat_completion``，使 Chat/Playground 的 SSE 打字机链路
  在 Mock 模式下同样可演示。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, AsyncIterator, Dict, List, Optional

from core.providers.base import (
    BaseProvider,
    ChatCompletion,
    ChatMessage,
    ProviderConfig,
    StreamChunk,
)

__all__ = ["MockProvider", "build_mock_evaluation"]

# 与 prompt 模板中的 "## 当前输入 ```json [...] ```" 段落对应
RAW_INPUTS_RE = re.compile(r"## 当前输入\s*```json\s*(\[.*?\])\s*```", re.DOTALL)

_NEGATIVE_SIGNALS = ("延期", "崩溃", "迟到", "质量不高", "未自测", "差距较大", "阻塞")
_POSITIVE_SIGNALS = ("超额", "提前", "零Bug", "分享", "主导", "优化", "提升")

_KEYWORD_MAP: Dict[str, List[str]] = {
    "positive": ["高质量", "超额完成", "主导", "优化", "团队"],
    "negative": ["延期", "沟通不及时", "质量不高", "待改进", "未自测"],
    "neutral": ["加班", "独立", "沟通少", "稳健", "完成"],
}


def build_mock_evaluation(
    score: int,
    tone: str,
    employee_id: str,
    period: str,
    keywords: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """构造符合评估 schema 的确定性 Mock 结果。

    Args:
        score: 总分（0-100）。
        tone: "positive" | "negative" | "neutral"，决定文案与风险标记。
        employee_id: 员工 ID，回填到结果中。
        period: 评估周期。
        keywords: 命中的关键词，用于让摘要文案随输入变化。

    Returns:
        与 ``agent/graph.py`` 解析节点期望一致的评估结果字典。
    """
    keywords = keywords or []
    keyword_text = "、".join(keywords) if keywords else ""

    if tone == "positive":
        summary = (
            f"本周期整体表现优秀，在{keyword_text}等多个维度上超出团队平均水平，"
            "值得继续保持并放大影响力。"
        )
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
        risk_flags: List[Dict[str, str]] = []
        harsh = (
            "该员工本周期交付质量与主动性均高于团队平均水平，是当前项目中的核心贡献者，"
            "建议继续赋予关键路径任务并纳入晋升观察名单。"
        )
        hidden = ["无显著隐藏风险", "需关注长期高负荷是否可持续"]
    elif tone == "negative":
        summary = (
            f"本周期在任务交付与代码质量方面出现明显问题，涉及{keyword_text}等情况，"
            "需要尽快制定改进计划并跟进。"
        )
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
        harsh = (
            "该员工当前处于低效与低质量并行的状态，若未来两周无显著改善，"
            "建议调整其任务范围并启动绩效改进计划。"
        )
        hidden = ["存在被动等待指令的倾向", "代码自测习惯尚未建立"]
    else:
        summary = (
            f"本周期整体表现稳定，在{keyword_text}等方面交付可靠，"
            "但创新性和协作主动性仍有提升空间。"
        )
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
        harsh = (
            "该员工是一名可靠的执行者，但当前大包大揽的工作方式正在形成团队依赖，"
            "且缺乏主动分享与协作，长期价值受限。"
        )
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
            "roi_analysis": (
                "从投入产出比看，该员工当前处于中等偏上水平，"
                "但成长曲线需要更明确的管理干预。"
            ),
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


class MockProvider(BaseProvider):
    """确定性 Mock Provider：从 prompt 抽取输入并返回结构合法的评估 JSON。"""

    RAW_INPUTS_RE = RAW_INPUTS_RE

    def name(self) -> str:
        return "mock/provider"

    async def health_check(self) -> bool:
        """Mock 恒定可用——这正是它能在无网络环境跑通链路的原因。"""
        return True

    async def chat_completion(
        self,
        messages: List[ChatMessage],
        response_format: Optional[Dict[str, str]] = None,
    ) -> ChatCompletion:
        prompt = self._flatten(messages[0].content) if messages else ""
        raw_content = self._extract_raw_content(prompt)

        tone, score = self._classify(raw_content)
        matched_keywords = self._extract_keywords(raw_content, tone)
        employee_id = self._extract_tag(prompt, "employee_id") or "unknown"
        period = self._extract_tag(prompt, "period") or "unknown"

        # 判定是否返回评估 JSON：
        # 1) 调用方显式要求 json_object（主链路 core/llm_call.py 默认如此）；或
        # 2) prompt 本身是评估类 prompt（含 "## 当前输入" JSON 段落，
        #    eval/evaluate.py 离线评测与历史契约均不传 response_format）。
        # 仅当两者都不满足（纯聊天/Playground）才返回自然语言占位回复，
        # 避免把结构化产物塞进聊天气泡。
        wants_json = (
            (response_format or {}).get("type") == "json_object"
            or self.RAW_INPUTS_RE.search(prompt) is not None
        )
        if not wants_json:
            content = (
                "【Mock 模式】当前未配置真实模型凭证，此为占位回复，"
                "用于验证链路连通性。请配置 CLOUD_API_KEY 或 LOCAL_BASE_URL 后关闭 LLM_MOCK_MODE。"
            )
        else:
            content = json.dumps(
                build_mock_evaluation(
                    score=score,
                    tone=tone,
                    employee_id=employee_id,
                    period=period,
                    keywords=matched_keywords,
                ),
                ensure_ascii=False,
            )

        return ChatCompletion(
            content=content,
            model="mock-model",
            usage={"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
        )

    async def stream_chat_completion(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[StreamChunk]:
        """按字符切片模拟打字机效果，让 SSE 链路在 Mock 模式下同样可演示。"""
        completion = await self.chat_completion(messages)
        text = completion.content
        step = 12
        for i in range(0, len(text), step):
            yield StreamChunk(content=text[i : i + step])
            await asyncio.sleep(0.01)
        yield StreamChunk(finish_reason="stop", usage=completion.usage)

    async def vision_completion(
        self,
        prompt: str,
        image_data: str,
        is_url: bool = False,
        model: Optional[str] = None,
    ) -> str:
        return "【Mock 模式】图像理解占位结果：检测到一张与工作产出相关的截图。"

    # ------------------------------------------------------------- internals
    @staticmethod
    def _flatten(content: Any) -> str:
        """兼容多模态 content（list[dict]）与纯文本 content（str）。"""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        return str(content)

    def _extract_raw_content(self, prompt: str) -> str:
        match = self.RAW_INPUTS_RE.search(prompt)
        if match:
            try:
                raw_inputs = json.loads(match.group(1))
                return " ".join(str(inp.get("content", "")) for inp in raw_inputs)
            except json.JSONDecodeError:
                pass
        # 兜底：正则未命中时直接用整段 prompt 做信号判定，保证仍有区分度
        return prompt

    @staticmethod
    def _extract_tag(prompt: str, tag: str) -> str:
        for line in prompt.splitlines():
            if tag in line:
                parts = line.strip().split()
                if parts:
                    return parts[-1][:20]
        return ""

    @staticmethod
    def _classify(content: str) -> tuple[str, int]:
        negative_count = sum(1 for s in _NEGATIVE_SIGNALS if s in content)
        positive_count = sum(1 for s in _POSITIVE_SIGNALS if s in content)
        if positive_count > negative_count:
            return "positive", 88
        if negative_count > positive_count:
            return "negative", 52
        return "neutral", 72

    @staticmethod
    def _extract_keywords(content: str, tone: str) -> List[str]:
        candidates = _KEYWORD_MAP.get(tone, [])
        found = [kw for kw in candidates if kw in content]
        return found[:2] if found else candidates[:2]


def make_mock_provider(model_name: str = "mock-model") -> MockProvider:
    """便捷工厂：供 ModelRouter / 测试快速构造实例。"""
    return MockProvider(ProviderConfig(model_name=model_name))
