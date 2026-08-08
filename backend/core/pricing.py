"""
模型定价表与成本计算（WS-1 成本账本）

价格单位统一为 **美元 / 100 万 token**（$/1M tokens），与 OpenAI / Anthropic /
Google 官方定价页的表述一致，避免 `$/1K` 与 `$/1M` 混用导致 1000 倍误差。

⚠️ 本表仅作**兜底**
------------------
代码内置价格会过期。优先级从高到低：

1. 调用方显式传入的 ``overrides``（来自 DB 的 ModelPricing 表 / 管理页维护）；
2. 本模块 ``MODEL_PRICING`` 内置表；
3. ``DEFAULT_PRICE`` 兜底 —— 此时 ``CostBreakdown.is_fallback=True``，
   同时打 WARNING 日志并累加 Prometheus 计数器
   ``agentvalue_pricing_fallback_total``，便于运维发现"定价表该更新了"。

设计红线：**未知模型绝不静默计 0**。计 0 会让成本看板长期显示"免费"，
是比价格偏差更严重的错误（审计结论 2.1 的直接诱因）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, Mapping, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelPrice:
    """单个模型的定价（美元 / 1M tokens）"""

    input_per_1m: float
    output_per_1m: float
    currency: str = "USD"


@dataclass(frozen=True)
class CostBreakdown:
    """一次调用的成本分解结果"""

    input_cost: float
    output_cost: float
    total_cost: float
    currency: str
    # 实际命中的定价 key（未命中时为 "__default__"）
    matched_model: str
    # True 表示走了 DEFAULT_PRICE 兜底，成本仅为估算
    is_fallback: bool


# ---------------------------------------------------------------------------
# 内置定价表（$/1M tokens）
#
# key 一律为 normalize_model_name() 归一化后的小写形式。
# 数值取自各厂商公开定价页的标准档（非批量 / 非缓存 / 非长上下文加价档）。
# ---------------------------------------------------------------------------
MODEL_PRICING: Dict[str, ModelPrice] = {
    # ---------------- OpenAI ----------------
    "gpt-4o": ModelPrice(2.50, 10.00),
    "gpt-4o-mini": ModelPrice(0.15, 0.60),
    "gpt-4.1": ModelPrice(2.00, 8.00),
    "gpt-4.1-mini": ModelPrice(0.40, 1.60),
    "gpt-4.1-nano": ModelPrice(0.10, 0.40),
    "gpt-4-turbo": ModelPrice(10.00, 30.00),
    "o3-mini": ModelPrice(1.10, 4.40),
    "o1-mini": ModelPrice(1.10, 4.40),
    # ---------------- Anthropic ----------------
    "claude-sonnet-4": ModelPrice(3.00, 15.00),
    "claude-opus-4": ModelPrice(15.00, 75.00),
    "claude-3-5-sonnet": ModelPrice(3.00, 15.00),
    "claude-3-5-haiku": ModelPrice(0.80, 4.00),
    "claude-3-haiku": ModelPrice(0.25, 1.25),
    # ---------------- Google ----------------
    "gemini-2.5-pro": ModelPrice(1.25, 10.00),
    "gemini-2.5-flash": ModelPrice(0.30, 2.50),
    "gemini-2.0-flash": ModelPrice(0.10, 0.40),
    # ---------------- DeepSeek ----------------
    "deepseek-chat": ModelPrice(0.27, 1.10),
    "deepseek-reasoner": ModelPrice(0.55, 2.19),
    "deepseek-v4-flash": ModelPrice(0.15, 0.60),
    "deepseek-v4-pro": ModelPrice(0.60, 2.40),
    # ---------------- 智谱 GLM ----------------
    "glm-5.1": ModelPrice(0.60, 2.20),
    "glm-5.2": ModelPrice(0.90, 3.20),
    "glm-4-plus": ModelPrice(0.70, 0.70),
    # ---------------- 月之暗面 Kimi ----------------
    "kimi-k2.6": ModelPrice(0.58, 2.30),
    # ---------------- MiniMax ----------------
    "minimax-m3": ModelPrice(0.30, 1.20),
    # ---------------- 通义千问 Qwen ----------------
    "qwen3.5-397b-a17b": ModelPrice(0.50, 2.00),
    "qwen3.6-35b-a3b": ModelPrice(0.10, 0.40),
    "qwen-max": ModelPrice(1.60, 6.40),
    # ---------------- 阶跃星辰 Step ----------------
    "step-3.5-flash": ModelPrice(0.08, 0.32),
    "step-3.7-flash": ModelPrice(0.12, 0.48),
    # ---------------- Embedding（无输出 token，output 价记 0） ----------------
    "text-embedding-3-small": ModelPrice(0.02, 0.0),
    "text-embedding-3-large": ModelPrice(0.13, 0.0),
    "text-embedding-ada-002": ModelPrice(0.10, 0.0),
    "bge-m3": ModelPrice(0.02, 0.0),
}

# 未知模型兜底价：取内置表中等偏上水位，宁可高估也不要记 0。
# 高估会触发预算告警让人来核对定价表；记 0 则永远无人发现。
DEFAULT_PRICE = ModelPrice(1.00, 3.00)

# 兜底命中时 CostBreakdown.matched_model 的取值
DEFAULT_PRICE_KEY = "__default__"

# 常见 provider 前缀（normalize 时剥离）
_PROVIDER_PREFIXES = (
    "openai/",
    "azure/",
    "azure_openai/",
    "anthropic/",
    "google/",
    "gemini/",
    "vertex_ai/",
    "deepseek/",
    "zhipu/",
    "moonshot/",
    "minimax/",
    "qwen/",
    "dashscope/",
    "stepfun/",
    "bedrock/",
    "ollama/",
    "openrouter/",
    "models/",
)

# 日期版本后缀：-2024-08-06 / -20240620 / @20240620
_DATE_SUFFIX_RE = re.compile(
    r"[-@_](?:\d{4}-\d{2}-\d{2}|\d{8}|\d{4}-\d{2}|latest|preview)$"
)

# ---------------------------------------------------------------------------
# Prometheus 埋点（core/metrics.py 已用全局 Counter 的写法，此处保持一致）
# prometheus_client 缺失或重复注册时降级为 None，不影响成本计算。
# ---------------------------------------------------------------------------
try:  # pragma: no cover - 取决于运行环境是否安装 prometheus_client
    from prometheus_client import Counter as _Counter

    PRICING_FALLBACK_TOTAL = _Counter(
        "agentvalue_pricing_fallback_total",
        "未命中内置定价表、走 DEFAULT_PRICE 兜底的次数（按模型名）",
        ["model"],
    )
except Exception as _exc:  # pragma: no cover
    PRICING_FALLBACK_TOTAL = None
    logger.debug("定价兜底计数器注册失败，降级为仅日志: %s", _exc)


def normalize_model_name(model: Optional[str]) -> str:
    """归一化模型名，用于定价表查找。

    处理顺序：
    1. 去首尾空白并小写；
    2. 反复剥离 provider 前缀（``openai/gpt-4o`` → ``gpt-4o``，支持多级前缀）；
    3. 剥离 ``:`` 后的部署标签（``glm-5.2:prod`` → ``glm-5.2``）；
    4. 反复剥离日期/版本后缀（``gpt-4o-2024-08-06`` → ``gpt-4o``）。

    Args:
        model: 原始模型名，可为 None。

    Returns:
        归一化后的小写模型名；入参为空时返回空串。
    """
    if not model:
        return ""
    name = str(model).strip().lower()
    if not name:
        return ""

    # 多级前缀，如 openrouter/anthropic/claude-3-5-sonnet
    changed = True
    while changed:
        changed = False
        for prefix in _PROVIDER_PREFIXES:
            if name.startswith(prefix):
                name = name[len(prefix) :]
                changed = True

    # 部署标签 / tag：ollama 风格 qwen2.5:7b 会被截断为 qwen2.5，
    # 这正是我们想要的（同一模型不同量化档共用定价）
    if ":" in name:
        name = name.split(":", 1)[0]

    # 日期后缀可能叠加，如 -preview-2024-09-12
    while True:
        stripped = _DATE_SUFFIX_RE.sub("", name)
        if stripped == name:
            break
        name = stripped

    return name.strip("-_ ")


def _lookup(
    model: Optional[str], overrides: Optional[Mapping[str, ModelPrice]]
) -> tuple[str, ModelPrice, bool]:
    """按 overrides → 内置表 → DEFAULT_PRICE 的顺序查价。

    Returns:
        (matched_model, price, is_fallback)
    """
    raw = (model or "").strip()
    normalized = normalize_model_name(raw)

    # 1) 精确匹配优先：先用原始名，再用归一化名，两级都查 overrides 与内置表。
    #    这样 DB 里配的 "gpt-4o-2024-08-06" 特价能盖过归一化后的通用价。
    candidates = [c for c in (raw, raw.lower(), normalized) if c]
    seen: set[str] = set()
    for key in candidates:
        if key in seen:
            continue
        seen.add(key)
        if overrides and key in overrides:
            return key, overrides[key], False
        if key in MODEL_PRICING:
            return key, MODEL_PRICING[key], False

    # 2) 前缀匹配：处理 "gpt-4o-mini-high" 这类内置表未穷举的变体。
    #    取最长匹配，避免 "gpt-4o" 抢走本应属于 "gpt-4o-mini" 的价格。
    if normalized:
        pool: Dict[str, ModelPrice] = {}
        pool.update(MODEL_PRICING)
        if overrides:
            pool.update(overrides)
        prefix_hits = [k for k in pool if normalized.startswith(k)]
        if prefix_hits:
            best = max(prefix_hits, key=len)
            return best, pool[best], False

    # 3) 兜底：打日志 + 埋点，绝不静默计 0
    logger.warning(
        "模型 %r 未命中定价表（归一化后=%r），按 DEFAULT_PRICE(input=$%.2f/1M, "
        "output=$%.2f/1M) 估算成本，请在管理页补充该模型定价",
        raw or "<empty>",
        normalized,
        DEFAULT_PRICE.input_per_1m,
        DEFAULT_PRICE.output_per_1m,
    )
    if PRICING_FALLBACK_TOTAL is not None:
        try:
            PRICING_FALLBACK_TOTAL.labels(model=(normalized or "unknown")).inc()
        except Exception:  # pragma: no cover - 埋点不得影响业务
            logger.debug("定价兜底埋点失败", exc_info=True)
    return DEFAULT_PRICE_KEY, DEFAULT_PRICE, True


def calculate_cost(
    model: Optional[str],
    prompt_tokens: int,
    completion_tokens: int,
    *,
    overrides: Optional[Mapping[str, ModelPrice]] = None,
) -> CostBreakdown:
    """计算一次 LLM 调用的成本。

    Args:
        model: 模型名（可带 provider 前缀与日期后缀）。
        prompt_tokens: 输入 token 数，负数按 0 处理。
        completion_tokens: 输出 token 数，负数按 0 处理。
        overrides: DB / 管理页维护的定价覆盖表，key 需为模型名（大小写不敏感由
            调用方保证或直接用归一化名），优先级高于内置表。

    Returns:
        CostBreakdown：含 input/output/total 成本、币种、命中的定价 key 与
        ``is_fallback`` 标记。``is_fallback=True`` 表示成本为兜底估算值，
        上层看板应给出"定价缺失"提示。
    """
    matched, price, is_fallback = _lookup(model, overrides)

    p_tokens = max(0, int(prompt_tokens or 0))
    c_tokens = max(0, int(completion_tokens or 0))

    input_cost = p_tokens / 1_000_000 * price.input_per_1m
    output_cost = c_tokens / 1_000_000 * price.output_per_1m

    return CostBreakdown(
        input_cost=input_cost,
        output_cost=output_cost,
        total_cost=input_cost + output_cost,
        currency=price.currency,
        matched_model=matched,
        is_fallback=is_fallback,
    )


def get_price(
    model: Optional[str], *, overrides: Optional[Mapping[str, ModelPrice]] = None
) -> ModelPrice:
    """仅取某模型的单价（不做 token 计算），供管理页价格预览使用。"""
    _matched, price, _is_fallback = _lookup(model, overrides)
    return price


def list_pricing() -> Dict[str, Dict[str, object]]:
    """导出内置定价表的可序列化形式，供管理页展示"当前生效兜底价"。"""
    return {
        name: {
            "input_per_1m": p.input_per_1m,
            "output_per_1m": p.output_per_1m,
            "currency": p.currency,
        }
        for name, p in sorted(MODEL_PRICING.items())
    }


__all__ = [
    "ModelPrice",
    "CostBreakdown",
    "MODEL_PRICING",
    "DEFAULT_PRICE",
    "DEFAULT_PRICE_KEY",
    "normalize_model_name",
    "calculate_cost",
    "get_price",
    "list_pricing",
]
