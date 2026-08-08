"""RAGAS 生成质量指标服务 (LLM-as-a-Judge 实现)

对标 Ragas / DeepEval / Braintrust 的 RAG 生成侧指标。补齐
``services/rag_eval_service.py`` 只有检索侧指标 (precision@k / recall@k / MRR / NDCG)
的空白 —— 检索命中不代表答案被上下文支撑, 生成质量必须单独度量。

五个指标 (取值均归一化到 [0, 1]):
- ``faithfulness``        答案是否被检索上下文支撑 (原子声明拆解 + 逐条 NLI 判定)
- ``answer_relevancy``    答案与问题的相关度 (反向提问 + embedding 余弦相似度)
- ``context_precision``   检索上下文信噪比 (逐块相关性判定 + 排序加权平均精度)
- ``context_recall``      检索是否覆盖 ground truth (标准答案声明拆解 + 归因判定)
- ``answer_correctness``  与标准答案的语义相似度 + 事实 TP/FP/FN F1 加权

调用约定
--------
LLM 调用完全复用 ``services/llm_judge_service.py`` 的方式:
``call_llm_with_fallback(model_router, messages=[ChatMessage(role="system", ...)])``
返回 ``(completion, tier)``, 由 ``core/llm_call.py`` 统一负责档位降级重试。
文本抽取直接复用 ``LLMJudgeService._extract_text``; JSON 解析沿用同一套
"先 json.loads, 失败再修复提取" 的容错策略 (``_parse_json_payload``)。

真实性约束 (v3 审计要求)
------------------------
任何指标在 LLM 或 embedding 不可用时**一律不编造数值**:
返回 ``score=None`` + ``status="unavailable"`` + ``reason`` 说明原因。
特别地 ``core/embeddings.py`` 在未配置 key 时会返回**零向量**(第 101 行),
本模块显式探测全零向量并判定为 unavailable, 而不是把余弦相似度当成 0 或 1 上报。
``answer_correctness`` 的两个分量中若仅 embedding 分量不可用, 返回
``status="partial"`` 并只用真实可得的事实 F1 分量计分 (权重重新归一化),
details 中记录被丢弃的分量, 同样不编造缺失分量的值。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from core.embeddings import EmbeddingClient
from core.llm_call import call_llm_with_fallback
from core.providers.base import ChatMessage
from services.llm_judge_service import LLMJudgeService

logger = logging.getLogger(__name__)


# ============================================================
# 常量
# ============================================================

# 指标名称 (对外暴露的稳定标识)
METRIC_FAITHFULNESS = "faithfulness"
METRIC_ANSWER_RELEVANCY = "answer_relevancy"
METRIC_CONTEXT_PRECISION = "context_precision"
METRIC_CONTEXT_RECALL = "context_recall"
METRIC_ANSWER_CORRECTNESS = "answer_correctness"

ALL_METRICS: List[str] = [
    METRIC_FAITHFULNESS,
    METRIC_ANSWER_RELEVANCY,
    METRIC_CONTEXT_PRECISION,
    METRIC_CONTEXT_RECALL,
    METRIC_ANSWER_CORRECTNESS,
]

# 需要 ground_truth 才能计算的指标
GROUND_TRUTH_METRICS = frozenset({METRIC_CONTEXT_RECALL, METRIC_ANSWER_CORRECTNESS})

# 需要 contexts 才能计算的指标
CONTEXT_METRICS = frozenset(
    {METRIC_FAITHFULNESS, METRIC_CONTEXT_PRECISION, METRIC_CONTEXT_RECALL}
)


class MetricStatus:
    """指标计算状态 (禁止用编造数值代替失败)"""

    OK = "ok"
    # 多分量指标中部分分量不可用, score 仅由真实可得的分量计算
    PARTIAL = "partial"
    # LLM / embedding 不可用或输入缺失, score 必须为 None
    UNAVAILABLE = "unavailable"


# 反向提问默认生成条数 (Ragas 默认 strictness=3)
DEFAULT_REVERSE_QUESTIONS = 3

# 单条声明拆解上限, 防止超长答案打爆上下文
MAX_CLAIMS = 30

# 上下文块判定上限
MAX_CONTEXTS = 20

# 数据集级并发上限
DEFAULT_CONCURRENCY = 4

# answer_correctness 默认权重 (事实 F1 : 语义相似度), 与 Ragas 默认一致
CORRECTNESS_WEIGHT_FACTUAL = 0.75
CORRECTNESS_WEIGHT_SEMANTIC = 0.25


# ============================================================
# 结果结构
# ============================================================


@dataclass
class MetricResult:
    """单个指标的计算结果

    Attributes:
        metric: 指标名。
        score: 归一化到 [0, 1] 的得分; 不可用时为 None (绝不填充默认值)。
        status: MetricStatus 之一。
        reason: 人类可读的说明 (成功时为评分依据, 失败时为不可用原因)。
        details: 原始子事实 (逐条声明判定 / 逐块相关性 / TP-FP-FN 等)。
    """

    metric: str
    score: Optional[float] = None
    status: str = MetricStatus.UNAVAILABLE
    reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可直接落 JSON 列的 dict"""
        return {
            "metric": self.metric,
            "score": self.score,
            "status": self.status,
            "reason": self.reason,
            "details": self.details,
        }


@dataclass
class RagasResult:
    """单条样本的 RAGAS 评测结果"""

    metrics: Dict[str, MetricResult] = field(default_factory=dict)
    sample_id: Optional[str] = None
    duration_ms: int = 0

    @property
    def scores(self) -> Dict[str, float]:
        """仅包含真实可得得分的 {metric: score} 映射 (unavailable 的不出现)"""
        return {
            name: r.score
            for name, r in self.metrics.items()
            if r.score is not None and r.status != MetricStatus.UNAVAILABLE
        }

    def to_dict(self) -> Dict[str, Any]:
        """序列化"""
        return {
            "sample_id": self.sample_id,
            "duration_ms": self.duration_ms,
            "scores": self.scores,
            "metrics": {k: v.to_dict() for k, v in self.metrics.items()},
        }


# ============================================================
# 提示词模板
# ============================================================

CLAIM_DECOMPOSE_PROMPT = """你是一个严谨的信息抽取器。请把下面的文本拆解为若干条**原子声明**(atomic claim)。

原子声明的要求:
1. 每条只陈述一个独立的事实, 不能包含 "并且 / 同时" 之类的复合结构;
2. 用完整句子表达, 代词要还原为具体指代对象;
3. 只拆解文本中真实出现的信息, 不要补充任何外部知识;
4. 最多输出 {max_claims} 条。

## 待拆解文本
{text}

## 输出要求
仅返回 JSON 对象, 格式:
{{"claims": ["声明1", "声明2"]}}"""


CLAIM_VERDICT_PROMPT = """你是一个严格的事实核查员。请判断每条声明能否**仅依据给定上下文**得到支撑。

判定标准:
- 上下文中有直接或可直接推出的依据 -> verdict = 1
- 上下文没有提及, 或与上下文矛盾 -> verdict = 0
- 不允许使用上下文之外的常识或外部知识

## 上下文
{contexts}

## 待判定声明
{claims}

## 输出要求
仅返回 JSON 对象, verdicts 数组长度必须与声明条数一致, 顺序一一对应:
{{"verdicts": [{{"index": 0, "verdict": 1, "reason": "简短中文理由"}}]}}"""


REVERSE_QUESTION_PROMPT = """你是一个提问生成器。请针对下面这段"答案", 反推出 {n} 个该答案最可能在回答的问题。

要求:
1. 问题必须只依据答案内容本身生成, 不要引入答案以外的信息;
2. {n} 个问题应表述各异但语义指向同一答案;
3. 若答案是逃避性回答(如"我不知道""无法回答"), 也照实反推。

## 答案
{answer}

## 输出要求
仅返回 JSON 对象:
{{"questions": ["问题1", "问题2"], "noncommittal": 0}}
其中 noncommittal 为 1 表示该答案是逃避性回答, 否则为 0。"""


CONTEXT_RELEVANCE_PROMPT = """你是一个检索质量评审员。请逐块判断下面的检索上下文对回答给定问题**是否有用**。

判定标准:
- 该块包含回答问题所需的信息(哪怕只是一部分) -> useful = 1
- 该块与问题无关, 或只是话题相近但不提供任何有效信息 -> useful = 0

## 问题
{question}
{ground_truth_block}
## 检索上下文块
{contexts}

## 输出要求
仅返回 JSON 对象, verdicts 数组长度必须与上下文块数一致, 顺序一一对应:
{{"verdicts": [{{"index": 0, "useful": 1, "reason": "简短中文理由"}}]}}"""


CORRECTNESS_PROMPT = """你是一个严格的答案比对员。请把"待评答案"与"标准答案"的事实陈述做集合比对。

分类定义:
- TP (true positive): 待评答案中出现、且标准答案也支持的事实
- FP (false positive): 待评答案中出现、但标准答案不支持或与之矛盾的事实
- FN (false negative): 标准答案中出现、但待评答案遗漏的事实

## 待评答案
{answer}

## 标准答案
{ground_truth}

## 输出要求
仅返回 JSON 对象:
{{"TP": ["..."], "FP": ["..."], "FN": ["..."], "reason": "简短中文说明"}}"""


# ============================================================
# 服务
# ============================================================


class RagasMetricsService:
    """RAGAS 生成质量指标服务

    Args:
        model_router: ModelRouter 实例 (或兼容测试替身), 传给 call_llm_with_fallback。
        embedding_client: EmbeddingClient 实例; 缺省时惰性创建。
        reverse_questions: answer_relevancy 反向提问条数。
    """

    def __init__(
        self,
        model_router: Any,
        *,
        embedding_client: Optional[EmbeddingClient] = None,
        reverse_questions: int = DEFAULT_REVERSE_QUESTIONS,
    ):
        self.model_router = model_router
        self._embedding_client = embedding_client
        self.reverse_questions = max(1, reverse_questions)

    # ===================== 底层调用 =====================

    @property
    def embedding_client(self) -> EmbeddingClient:
        """惰性创建 EmbeddingClient (构造期不触碰配置, 便于测试注入)"""
        if self._embedding_client is None:
            self._embedding_client = EmbeddingClient()
        return self._embedding_client

    async def _call_json(self, prompt: str) -> Dict[str, Any]:
        """调用 LLM 并解析 JSON 响应

        沿用 llm_judge_service 的调用约定: 单条 system 消息 + call_llm_with_fallback。

        Returns:
            {"ok": True, "data": {...}} 或 {"ok": False, "error": "...."}
            —— 绝不返回伪造的默认数据, 调用方据此判定 unavailable。
        """
        try:
            messages = [ChatMessage(role="system", content=prompt)]
            completion, _tier = await call_llm_with_fallback(
                self.model_router, messages=messages
            )
        except Exception as e:  # noqa: BLE001 - 需要把失败原因原样上报
            logger.warning("RAGAS LLM 调用失败: %s", e)
            return {"ok": False, "error": f"LLM 调用失败: {e}"}

        data = _parse_json_payload(completion.content or "")
        if data is None:
            snippet = (completion.content or "")[:200]
            return {"ok": False, "error": f"LLM 响应非法 JSON: {snippet}"}
        return {"ok": True, "data": data}

    async def _embed(self, texts: Sequence[str]) -> Dict[str, Any]:
        """批量 embedding, 并显式探测零向量降级

        ``core/embeddings.py`` 在无 key 或调用失败时返回全零向量, 若直接拿去算余弦
        会得到 0/NaN 并被误当成"相关度为 0"。这里探测到全零即判 unavailable。

        Returns:
            {"ok": True, "vectors": [[...]]} 或 {"ok": False, "error": "..."}
        """
        if not texts:
            return {"ok": False, "error": "embedding 输入为空"}
        try:
            vectors = await self.embedding_client.embed(list(texts))
        except Exception as e:  # noqa: BLE001
            logger.warning("RAGAS embedding 调用失败: %s", e)
            return {"ok": False, "error": f"embedding 调用失败: {e}"}

        if not vectors or len(vectors) != len(texts):
            return {"ok": False, "error": "embedding 返回数量与输入不匹配"}
        for i, vec in enumerate(vectors):
            if _is_zero_vector(vec):
                return {
                    "ok": False,
                    "error": (
                        f"embedding 返回全零向量 (第 {i} 条), "
                        "通常意味着未配置 embedding key 或调用失败, 拒绝据此计算相似度"
                    ),
                }
        return {"ok": True, "vectors": vectors}

    async def _decompose_claims(self, text: str) -> Dict[str, Any]:
        """把文本拆成原子声明

        Returns:
            {"ok": True, "claims": [...]} 或 {"ok": False, "error": "..."}
        """
        prompt = CLAIM_DECOMPOSE_PROMPT.format(max_claims=MAX_CLAIMS, text=text)
        resp = await self._call_json(prompt)
        if not resp["ok"]:
            return resp
        claims = _as_str_list(resp["data"].get("claims"))[:MAX_CLAIMS]
        if not claims:
            return {"ok": False, "error": "LLM 未能拆解出任何原子声明"}
        return {"ok": True, "claims": claims}

    async def _judge_claims(
        self, claims: List[str], contexts: List[str]
    ) -> Dict[str, Any]:
        """判定每条声明能否被上下文支撑

        Returns:
            {"ok": True, "verdicts": [{"claim", "verdict", "reason"}]} 或 error。
        """
        prompt = CLAIM_VERDICT_PROMPT.format(
            contexts=_format_numbered(contexts),
            claims=_format_numbered(claims),
        )
        resp = await self._call_json(prompt)
        if not resp["ok"]:
            return resp

        raw = resp["data"].get("verdicts")
        mapped = _map_verdicts(raw, len(claims), key="verdict")
        if mapped is None:
            return {"ok": False, "error": "LLM 判定结果无法与声明对齐"}

        verdicts = [
            {
                "claim": claims[i],
                "verdict": mapped[i]["value"],
                "reason": mapped[i]["reason"],
            }
            for i in range(len(claims))
        ]
        return {"ok": True, "verdicts": verdicts}

    # ===================== 指标: faithfulness =====================

    async def faithfulness(self, answer: str, contexts: List[str]) -> MetricResult:
        """答案忠实度: 答案中的原子声明有多少比例被检索上下文支撑

        算法 (对齐 Ragas Faithfulness):
        1. 把答案拆解为原子声明;
        2. 逐条判定该声明能否**仅依据上下文**推出;
        3. score = 支撑数 / 总声明数。

        Args:
            answer: 待评答案。
            contexts: 检索到的上下文块列表。

        Returns:
            MetricResult, details 含逐条声明判定 (verdicts)。
        """
        metric = METRIC_FAITHFULNESS
        answer = (answer or "").strip()
        ctxs = _clean_contexts(contexts)
        if not answer:
            return _unavailable(metric, "答案为空, 无法计算忠实度")
        if not ctxs:
            return _unavailable(metric, "上下文为空, 无法判定答案是否被支撑")

        decomposed = await self._decompose_claims(answer)
        if not decomposed["ok"]:
            return _unavailable(metric, decomposed["error"])
        claims: List[str] = decomposed["claims"]

        judged = await self._judge_claims(claims, ctxs)
        if not judged["ok"]:
            return _unavailable(metric, judged["error"])

        verdicts = judged["verdicts"]
        supported = sum(1 for v in verdicts if v["verdict"] == 1)
        total = len(verdicts)
        score = _clamp01(supported / total) if total else None
        if score is None:
            return _unavailable(metric, "声明数为 0, 无法计算比例")

        return MetricResult(
            metric=metric,
            score=score,
            status=MetricStatus.OK,
            reason=f"{total} 条原子声明中 {supported} 条被上下文支撑",
            details={
                "verdicts": verdicts,
                "supported": supported,
                "total_claims": total,
            },
        )

    # ===================== 指标: answer_relevancy =====================

    async def answer_relevancy(self, question: str, answer: str) -> MetricResult:
        """答案相关度: 由答案反推问题, 与原问题的平均语义相似度

        算法 (对齐 Ragas AnswerRelevancy):
        1. 用 LLM 从答案反向生成 N 个问题;
        2. 对原问题与 N 个反向问题做 embedding;
        3. score = 平均余弦相似度 (逃避性回答直接判 0)。

        Args:
            question: 原始问题。
            answer: 待评答案。

        Returns:
            MetricResult, details 含反向问题与逐条相似度。
        """
        metric = METRIC_ANSWER_RELEVANCY
        question = (question or "").strip()
        answer = (answer or "").strip()
        if not question:
            return _unavailable(metric, "问题为空, 无法计算答案相关度")
        if not answer:
            return _unavailable(metric, "答案为空, 无法计算答案相关度")

        prompt = REVERSE_QUESTION_PROMPT.format(n=self.reverse_questions, answer=answer)
        resp = await self._call_json(prompt)
        if not resp["ok"]:
            return _unavailable(metric, resp["error"])

        questions = _as_str_list(resp["data"].get("questions"))[
            : self.reverse_questions
        ]
        noncommittal = _as_binary(resp["data"].get("noncommittal"))
        if not questions:
            return _unavailable(metric, "LLM 未能从答案反推出任何问题")

        if noncommittal == 1:
            # 逃避性回答: Ragas 语义下相关度确为 0, 这是判定结论而非降级填充
            return MetricResult(
                metric=metric,
                score=0.0,
                status=MetricStatus.OK,
                reason="答案为逃避性回答(noncommittal), 相关度判为 0",
                details={"reverse_questions": questions, "noncommittal": True},
            )

        embedded = await self._embed([question] + questions)
        if not embedded["ok"]:
            return _unavailable(metric, embedded["error"])

        vectors = embedded["vectors"]
        base = vectors[0]
        sims = [_cosine(base, v) for v in vectors[1:]]
        sims = [s for s in sims if s is not None]
        if not sims:
            return _unavailable(metric, "余弦相似度计算失败 (向量模长为 0)")

        score = _clamp01(sum(sims) / len(sims))
        return MetricResult(
            metric=metric,
            score=score,
            status=MetricStatus.OK,
            reason=f"{len(sims)} 个反向问题与原问题的平均余弦相似度 {score:.4f}",
            details={
                "reverse_questions": questions,
                "similarities": [round(s, 6) for s in sims],
                "noncommittal": False,
            },
        )

    # ===================== 指标: context_precision =====================

    async def context_precision(
        self,
        question: str,
        contexts: List[str],
        ground_truth: Optional[str] = None,
    ) -> MetricResult:
        """上下文精度: 相关块是否排在前面 (排序加权平均精度)

        算法 (对齐 Ragas ContextPrecision):
        1. 逐块判定该上下文对回答问题是否有用;
        2. AP = Σ_k (precision@k × rel_k) / 相关块总数 —— 越靠前的相关块权重越大。

        Args:
            question: 问题。
            contexts: 按检索排序的上下文块列表。
            ground_truth: 可选标准答案, 提供时判定更准。

        Returns:
            MetricResult, details 含逐块判定与 precision@k。
        """
        metric = METRIC_CONTEXT_PRECISION
        question = (question or "").strip()
        ctxs = _clean_contexts(contexts)
        if not question:
            return _unavailable(metric, "问题为空, 无法判定上下文相关性")
        if not ctxs:
            return _unavailable(metric, "上下文为空, 无法计算上下文精度")

        gt_block = ""
        if ground_truth and ground_truth.strip():
            gt_block = f"\n## 标准答案(判定参考)\n{ground_truth.strip()}\n"

        prompt = CONTEXT_RELEVANCE_PROMPT.format(
            question=question,
            ground_truth_block=gt_block,
            contexts=_format_numbered(ctxs),
        )
        resp = await self._call_json(prompt)
        if not resp["ok"]:
            return _unavailable(metric, resp["error"])

        mapped = _map_verdicts(resp["data"].get("verdicts"), len(ctxs), key="useful")
        if mapped is None:
            return _unavailable(metric, "LLM 判定结果无法与上下文块对齐")

        relevances = [mapped[i]["value"] for i in range(len(ctxs))]
        total_relevant = sum(relevances)
        if total_relevant == 0:
            # 无相关块是真实结论 (AP 定义为 0), 不是降级
            return MetricResult(
                metric=metric,
                score=0.0,
                status=MetricStatus.OK,
                reason=f"{len(ctxs)} 个上下文块均被判定为无关, 平均精度为 0",
                details={
                    "verdicts": _verdict_rows(ctxs, mapped, "useful"),
                    "total_relevant": 0,
                    "precision_at_k": [],
                },
            )

        hits = 0
        precision_at_k: List[float] = []
        weighted_sum = 0.0
        for k, rel in enumerate(relevances, start=1):
            if rel == 1:
                hits += 1
            p_at_k = hits / k
            precision_at_k.append(round(p_at_k, 6))
            if rel == 1:
                weighted_sum += p_at_k

        score = _clamp01(weighted_sum / total_relevant)
        return MetricResult(
            metric=metric,
            score=score,
            status=MetricStatus.OK,
            reason=(
                f"{len(ctxs)} 个上下文块中 {total_relevant} 个相关, "
                f"排序加权平均精度 {score:.4f}"
            ),
            details={
                "verdicts": _verdict_rows(ctxs, mapped, "useful"),
                "total_relevant": total_relevant,
                "precision_at_k": precision_at_k,
            },
        )

    # ===================== 指标: context_recall =====================

    async def context_recall(
        self, ground_truth: str, contexts: List[str]
    ) -> MetricResult:
        """上下文召回: 标准答案的声明有多少能归因到检索上下文

        算法 (对齐 Ragas ContextRecall):
        1. 把 ground_truth 拆解为原子声明;
        2. 逐条判定能否归因到上下文;
        3. score = 可归因数 / 总声明数。

        Args:
            ground_truth: 标准答案。
            contexts: 检索上下文块列表。

        Returns:
            MetricResult, details 含逐条归因判定。
        """
        metric = METRIC_CONTEXT_RECALL
        ground_truth = (ground_truth or "").strip()
        ctxs = _clean_contexts(contexts)
        if not ground_truth:
            return _unavailable(metric, "缺少 ground_truth, 无法计算上下文召回")
        if not ctxs:
            return _unavailable(metric, "上下文为空, 无法计算上下文召回")

        decomposed = await self._decompose_claims(ground_truth)
        if not decomposed["ok"]:
            return _unavailable(metric, decomposed["error"])
        claims: List[str] = decomposed["claims"]

        judged = await self._judge_claims(claims, ctxs)
        if not judged["ok"]:
            return _unavailable(metric, judged["error"])

        verdicts = judged["verdicts"]
        attributed = sum(1 for v in verdicts if v["verdict"] == 1)
        total = len(verdicts)
        if total == 0:
            return _unavailable(metric, "标准答案声明数为 0, 无法计算比例")

        score = _clamp01(attributed / total)
        return MetricResult(
            metric=metric,
            score=score,
            status=MetricStatus.OK,
            reason=f"标准答案 {total} 条声明中 {attributed} 条可归因到检索上下文",
            details={
                "verdicts": verdicts,
                "attributed": attributed,
                "total_claims": total,
            },
        )

    # ===================== 指标: answer_correctness =====================

    async def answer_correctness(self, answer: str, ground_truth: str) -> MetricResult:
        """答案正确性: 事实 F1 + 语义相似度加权

        算法 (对齐 Ragas AnswerCorrectness):
        1. LLM 把答案与标准答案的事实做 TP / FP / FN 集合比对, F1 = TP / (TP + 0.5(FP+FN));
        2. embedding 计算答案与标准答案的余弦相似度;
        3. score = 0.75 × F1 + 0.25 × 相似度。

        embedding 不可用时返回 status="partial", 权重归一化到事实分量,
        并在 details 中记录被丢弃的分量 —— 不为缺失分量编造数值。

        Args:
            answer: 待评答案。
            ground_truth: 标准答案。

        Returns:
            MetricResult, details 含 TP/FP/FN 与两个分量得分。
        """
        metric = METRIC_ANSWER_CORRECTNESS
        answer = (answer or "").strip()
        ground_truth = (ground_truth or "").strip()
        if not answer:
            return _unavailable(metric, "答案为空, 无法计算答案正确性")
        if not ground_truth:
            return _unavailable(metric, "缺少 ground_truth, 无法计算答案正确性")

        prompt = CORRECTNESS_PROMPT.format(answer=answer, ground_truth=ground_truth)
        factual_task = self._call_json(prompt)
        embed_task = self._embed([answer, ground_truth])
        factual_resp, embed_resp = await asyncio.gather(factual_task, embed_task)

        factual_f1: Optional[float] = None
        tp: List[str] = []
        fp: List[str] = []
        fn: List[str] = []
        factual_error = ""
        if factual_resp["ok"]:
            data = factual_resp["data"]
            tp = _as_str_list(data.get("TP") or data.get("tp"))
            fp = _as_str_list(data.get("FP") or data.get("fp"))
            fn = _as_str_list(data.get("FN") or data.get("fn"))
            denom = len(tp) + 0.5 * (len(fp) + len(fn))
            if denom > 0:
                factual_f1 = _clamp01(len(tp) / denom)
            elif not tp and not fp and not fn:
                factual_error = "LLM 未给出任何 TP/FP/FN 事实, 无法计算事实 F1"
        else:
            factual_error = factual_resp["error"]

        semantic: Optional[float] = None
        semantic_error = ""
        if embed_resp["ok"]:
            sim = _cosine(embed_resp["vectors"][0], embed_resp["vectors"][1])
            if sim is None:
                semantic_error = "余弦相似度计算失败 (向量模长为 0)"
            else:
                semantic = _clamp01(sim)
        else:
            semantic_error = embed_resp["error"]

        details: Dict[str, Any] = {
            "TP": tp,
            "FP": fp,
            "FN": fn,
            "factual_f1": factual_f1,
            "semantic_similarity": semantic,
            "weights": {
                "factual": CORRECTNESS_WEIGHT_FACTUAL,
                "semantic": CORRECTNESS_WEIGHT_SEMANTIC,
            },
        }
        if factual_error:
            details["factual_error"] = factual_error
        if semantic_error:
            details["semantic_error"] = semantic_error

        if factual_f1 is None and semantic is None:
            return _unavailable(
                metric,
                f"事实分量与语义分量均不可用: {factual_error or '-'} / {semantic_error or '-'}",
                details=details,
            )

        if factual_f1 is not None and semantic is not None:
            score = _clamp01(
                CORRECTNESS_WEIGHT_FACTUAL * factual_f1
                + CORRECTNESS_WEIGHT_SEMANTIC * semantic
            )
            return MetricResult(
                metric=metric,
                score=score,
                status=MetricStatus.OK,
                reason=(
                    f"事实 F1={factual_f1:.4f} (TP={len(tp)}, FP={len(fp)}, FN={len(fn)}), "
                    f"语义相似度={semantic:.4f}"
                ),
                details=details,
            )

        # 单分量可用: 权重归一化, 显式标注 partial
        if factual_f1 is not None:
            details["applied_weights"] = {"factual": 1.0, "semantic": 0.0}
            return MetricResult(
                metric=metric,
                score=factual_f1,
                status=MetricStatus.PARTIAL,
                reason=(
                    f"语义分量不可用({semantic_error}), 仅按事实 F1 计分: {factual_f1:.4f}"
                ),
                details=details,
            )

        details["applied_weights"] = {"factual": 0.0, "semantic": 1.0}
        return MetricResult(
            metric=metric,
            score=semantic,
            status=MetricStatus.PARTIAL,
            reason=f"事实分量不可用({factual_error}), 仅按语义相似度计分: {semantic:.4f}",
            details=details,
        )

    # ===================== 样本 / 数据集级编排 =====================

    async def evaluate_sample(
        self,
        *,
        question: str,
        answer: str,
        contexts: Optional[List[str]] = None,
        ground_truth: Optional[str] = None,
        metrics: Optional[List[str]] = None,
        sample_id: Optional[str] = None,
    ) -> RagasResult:
        """并发评测单条样本的指定指标子集

        所有请求的指标通过 asyncio.gather 并发执行; 单个指标抛异常不影响其他指标,
        异常会被转换为 status="unavailable" 的 MetricResult。

        Args:
            question: 问题。
            answer: 待评答案。
            contexts: 检索上下文块列表。
            ground_truth: 标准答案 (context_recall / answer_correctness 必需)。
            metrics: 指标子集, None 表示按输入条件自动选取可算的全部指标。
            sample_id: 样本标识, 原样回填到结果。

        Returns:
            RagasResult。
        """
        started = time.monotonic()
        ctxs = _clean_contexts(contexts or [])
        gt = (ground_truth or "").strip()

        requested = (
            list(metrics)
            if metrics
            else self.applicable_metrics(contexts=ctxs, ground_truth=gt)
        )

        results: Dict[str, MetricResult] = {}
        runnable: List[str] = []
        for name in requested:
            if name not in ALL_METRICS:
                results[name] = _unavailable(name, f"未知指标: {name}")
                continue
            runnable.append(name)

        coros = [self._dispatch(name, question, answer, ctxs, gt) for name in runnable]
        gathered = await asyncio.gather(*coros, return_exceptions=True)

        for name, outcome in zip(runnable, gathered):
            if isinstance(outcome, BaseException):
                logger.warning("RAGAS 指标 %s 计算异常: %s", name, outcome)
                results[name] = _unavailable(name, f"指标计算异常: {outcome}")
            else:
                results[name] = outcome

        return RagasResult(
            metrics=results,
            sample_id=sample_id,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    async def evaluate_dataset(
        self,
        samples: List[Dict[str, Any]],
        *,
        metrics: Optional[List[str]] = None,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> List[RagasResult]:
        """带并发上限地批量评测样本

        Args:
            samples: 样本列表, 每项支持 question / answer / contexts /
                ground_truth / sample_id 字段。
            metrics: 指标子集。
            concurrency: 并发上限 (信号量控制), 避免打爆下游 LLM 配额。

        Returns:
            与输入等长、顺序一致的 RagasResult 列表。
        """
        if not samples:
            return []
        sem = asyncio.Semaphore(max(1, concurrency))

        async def _one(idx: int, sample: Dict[str, Any]) -> RagasResult:
            async with sem:
                try:
                    return await self.evaluate_sample(
                        question=str(sample.get("question") or ""),
                        answer=str(sample.get("answer") or ""),
                        contexts=sample.get("contexts") or [],
                        ground_truth=sample.get("ground_truth"),
                        metrics=metrics,
                        sample_id=str(
                            sample.get("sample_id") or sample.get("id") or idx
                        ),
                    )
                except Exception as e:  # noqa: BLE001 - 单样本失败不拖垮整批
                    logger.warning("RAGAS 样本 %s 评测失败: %s", idx, e)
                    names = metrics or ALL_METRICS
                    return RagasResult(
                        metrics={
                            n: _unavailable(n, f"样本评测失败: {e}") for n in names
                        },
                        sample_id=str(sample.get("sample_id") or idx),
                    )

        return list(await asyncio.gather(*(_one(i, s) for i, s in enumerate(samples))))

    @staticmethod
    def applicable_metrics(
        *, contexts: Optional[List[str]] = None, ground_truth: Optional[str] = None
    ) -> List[str]:
        """按输入条件筛出可计算的指标 (缺 contexts / ground_truth 的直接排除)"""
        has_ctx = bool(_clean_contexts(contexts or []))
        has_gt = bool((ground_truth or "").strip())
        selected = []
        for name in ALL_METRICS:
            if name in CONTEXT_METRICS and not has_ctx:
                continue
            if name in GROUND_TRUTH_METRICS and not has_gt:
                continue
            selected.append(name)
        return selected

    async def _dispatch(
        self,
        metric: str,
        question: str,
        answer: str,
        contexts: List[str],
        ground_truth: str,
    ) -> MetricResult:
        """按指标名分发到具体实现"""
        if metric == METRIC_FAITHFULNESS:
            return await self.faithfulness(answer, contexts)
        if metric == METRIC_ANSWER_RELEVANCY:
            return await self.answer_relevancy(question, answer)
        if metric == METRIC_CONTEXT_PRECISION:
            return await self.context_precision(
                question, contexts, ground_truth or None
            )
        if metric == METRIC_CONTEXT_RECALL:
            return await self.context_recall(ground_truth, contexts)
        return await self.answer_correctness(answer, ground_truth)


# ============================================================
# 模块级辅助函数
# ============================================================


def _unavailable(
    metric: str, reason: str, *, details: Optional[Dict[str, Any]] = None
) -> MetricResult:
    """构造不可用结果 (score 恒为 None, 杜绝编造数值)"""
    return MetricResult(
        metric=metric,
        score=None,
        status=MetricStatus.UNAVAILABLE,
        reason=reason,
        details=details or {},
    )


def _parse_json_payload(content: str) -> Optional[Dict[str, Any]]:
    """解析 LLM 返回的 JSON 对象, 带修复策略

    与 ``LLMJudgeService._parse_judge_response`` 同源的容错思路:
    1. 直接 json.loads;
    2. 剥离 ```json 代码围栏后重试;
    3. 正则截取最外层 {...} 后重试;
    4. 均失败返回 None (调用方据此判 unavailable, 不返回空 dict 冒充成功)。
    """
    if not content:
        return None
    text = content.strip()

    for candidate in _json_candidates(text):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            # 少数模型直接返回数组, 包装成统一 dict 便于上层取值
            return {"items": data, "claims": data, "questions": data, "verdicts": data}
    return None


def _json_candidates(text: str) -> List[str]:
    """依次产出可尝试解析的 JSON 片段"""
    candidates = [text]
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fence:
        candidates.append(fence.group(1).strip())
    obj = re.search(r"\{.*\}", text, re.S)
    if obj:
        candidates.append(obj.group(0))
    arr = re.search(r"\[.*\]", text, re.S)
    if arr:
        candidates.append(arr.group(0))
    return candidates


def _as_str_list(value: Any) -> List[str]:
    """把 LLM 返回的任意结构规整为非空字符串列表"""
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = str(
                item.get("claim")
                or item.get("question")
                or item.get("text")
                or item.get("statement")
                or ""
            ).strip()
        else:
            text = str(item).strip()
        if text:
            out.append(text)
    return out


def _as_binary(value: Any) -> int:
    """把布尔 / 0-1 / "yes"-"no" 规整为 0 或 1 (无法识别按 0 处理)"""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if value >= 0.5 else 0
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"1", "yes", "true", "y", "是", "支持", "相关", "有用"}:
            return 1
        if low in {"0", "no", "false", "n", "否", "不支持", "无关", "无用"}:
            return 0
    return 0


def _map_verdicts(
    raw: Any, expected: int, *, key: str
) -> Optional[List[Dict[str, Any]]]:
    """把 LLM 的 verdicts 数组对齐到 expected 条

    支持带 index 字段的乱序返回; 条数不足或完全无法解析时返回 None,
    由调用方判 unavailable —— 不用默认值补齐(那等于编造判定)。
    """
    if not isinstance(raw, list) or not raw:
        return None

    slots: List[Optional[Dict[str, Any]]] = [None] * expected
    fallback: List[Dict[str, Any]] = []

    for pos, item in enumerate(raw):
        if isinstance(item, dict):
            value = _as_binary(
                item.get(key)
                if key in item
                else item.get("verdict", item.get("useful", item.get("score")))
            )
            reason = str(item.get("reason") or "")
            idx = item.get("index")
            parsed = {"value": value, "reason": reason}
            if isinstance(idx, (int, float)) and 0 <= int(idx) < expected:
                slots[int(idx)] = parsed
            else:
                fallback.append(parsed)
        else:
            fallback.append({"value": _as_binary(item), "reason": ""})
            if pos < expected and slots[pos] is None:
                slots[pos] = fallback[-1]

    # 用顺序返回的结果填补未定位的槽位
    cursor = 0
    for i in range(expected):
        if slots[i] is None and cursor < len(fallback):
            slots[i] = fallback[cursor]
            cursor += 1

    if any(s is None for s in slots):
        return None
    return [s for s in slots if s is not None]


def _verdict_rows(
    texts: List[str], mapped: List[Dict[str, Any]], key: str
) -> List[Dict[str, Any]]:
    """把判定结果与原文本拼成可读的逐条明细"""
    return [
        {
            "index": i,
            "context": texts[i][:500],
            key: mapped[i]["value"],
            "reason": mapped[i]["reason"],
        }
        for i in range(len(texts))
    ]


def _format_numbered(items: Sequence[str]) -> str:
    """把列表格式化为带序号的多行文本 (序号从 0 起, 与 verdicts.index 对齐)"""
    return "\n".join(f"[{i}] {text}" for i, text in enumerate(items))


def _clean_contexts(contexts: Sequence[Any]) -> List[str]:
    """规整上下文列表: 支持 str / {"content"|"text"|"page_content"} 结构"""
    out: List[str] = []
    for c in contexts or []:
        if isinstance(c, str):
            text = c.strip()
        elif isinstance(c, dict):
            text = str(
                c.get("content") or c.get("text") or c.get("page_content") or ""
            ).strip()
        else:
            text = str(c).strip()
        if text:
            out.append(text)
        if len(out) >= MAX_CONTEXTS:
            break
    return out


def _is_zero_vector(vec: Sequence[float]) -> bool:
    """探测全零向量 (core/embeddings.py 无 key / 调用失败时的降级返回值)"""
    if not vec:
        return True
    return all(abs(float(v)) < 1e-12 for v in vec)


def _cosine(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    """余弦相似度; 任一向量模长为 0 或维度不匹配时返回 None (不返回 0 冒充结果)"""
    if not a or not b or len(a) != len(b):
        return None
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        fx = float(x)
        fy = float(y)
        dot += fx * fy
        na += fx * fx
        nb += fy * fy
    if na <= 0.0 or nb <= 0.0:
        return None
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _clamp01(value: float) -> float:
    """把得分裁剪到 [0, 1] 并保留 6 位小数"""
    return round(max(0.0, min(1.0, float(value))), 6)


def extract_text(data: Any) -> str:
    """从 dict/list/str 中抽取纯文本

    直接复用 ``LLMJudgeService._extract_text``, 保证与 LLM-as-Judge 路径
    对 dataset item 的 input/expected_output 解析行为完全一致。
    """
    return LLMJudgeService._extract_text(data)


__all__ = [
    "ALL_METRICS",
    "CONTEXT_METRICS",
    "GROUND_TRUTH_METRICS",
    "METRIC_ANSWER_CORRECTNESS",
    "METRIC_ANSWER_RELEVANCY",
    "METRIC_CONTEXT_PRECISION",
    "METRIC_CONTEXT_RECALL",
    "METRIC_FAITHFULNESS",
    "MetricResult",
    "MetricStatus",
    "RagasMetricsService",
    "RagasResult",
    "extract_text",
]
