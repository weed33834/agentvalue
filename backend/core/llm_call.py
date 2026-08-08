"""
公共 LLM 调用 helper（P1-3 提取自 agent/graph.py._call_llm_with_fallback）。

将「调用 LLM + 失败时触发 runtime_reselect 档位降级并重试一次」的逻辑集中到此处，
供 agent/graph.py（评估工作流）与 eval/llm_judge.py（LLM-as-Judge）复用，
避免兜底/降级逻辑散落多处导致行为漂移。

设计要点：
- 支持两种入参：prompt(str) 自动包装为 [system, user] 消息对；messages(list[ChatMessage]) 直接使用。
- 失败时调 model_router.runtime_reselect（若存在）触发档位降级，再用降级档位重试一次。
  最多 1 次降级重试，避免无限重试。重试仍失败抛出聚合异常由调用方处理。
- 返回 (completion, tier)：completion 为 ChatCompletion 对象（含 content/model/usage），
  tier 为实际使用的模型档位。与原 agent/graph._call_llm_with_fallback 签名保持一致，
  便于 graph.py 与 test_graph_fallback.py 无缝替换。
- runtime_reselect / get_health_score / get_provider 不存在时安全降级（getattr 容错），
  兼容 MockModelRouter 等测试替身。
"""

import logging
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional, Tuple

from core.metrics import record_evaluation_failure
from core.observe import current_trace_id, record_llm_usage, span, trace_context
from core.providers.base import ChatCompletion, ChatMessage
from core.tracing import tracer

logger = logging.getLogger(__name__)


async def _invoke_provider(
    provider: Any,
    messages: List[ChatMessage],
    response_format: Optional[Dict[str, str]],
    *,
    tier: str,
    employee_id: Optional[str],
    period: Optional[str],
) -> ChatCompletion:
    """WS-1：带观测埋点地调用 provider.chat_completion。

    在原调用外套一层 ``span(kind="llm")``，并在返回后：
    1. 从 ``completion.usage`` 取 token 用量 → ``span.set_usage`` 计算成本；
    2. 落 ``ConversationMetrics`` + ``BillingRecord``（成本账本唯一写入点）。

    无活跃 trace 时自动开一条隐式 trace（对标 Langfuse 的 orphan generation），
    保证自托管部署下链路数据不会因为上游没埋点而全部丢失。

    埋点与账本写入的任何失败都只告警，不影响补全结果与异常语义：
    provider 抛出的异常会被 span 记录后原样向上抛，由调用方走降级重试逻辑。
    """
    model_name = (
        getattr(getattr(provider, "config", None), "model_name", None) or "unknown"
    )
    provider_name = None
    try:
        name_attr = getattr(provider, "name", None)
        provider_name = name_attr() if callable(name_attr) else name_attr
    except Exception:
        logger.debug("provider 名称获取失败", exc_info=True)

    async with AsyncExitStack() as stack:
        if current_trace_id() is None:
            await stack.enter_async_context(
                trace_context(
                    f"llm_call:{model_name}",
                    kind="chat",
                    user_id=employee_id,
                    metadata={"period": period, "tier": tier},
                )
            )
        llm_span = span(
            "llm.chat_completion",
            kind="llm",
            model=model_name,
            provider=provider_name,
            input={"messages": [getattr(m, "__dict__", m) for m in messages]},
            attributes={"tier": tier, "employee_id": employee_id, "period": period},
        )
        try:
            async with llm_span:
                completion = await provider.chat_completion(
                    messages=messages,
                    response_format=response_format,
                )
                # 兼容部分 Provider 在结果上挂 error 字段表示非异常失败
                if getattr(completion, "error", None):
                    raise RuntimeError(f"provider 返回错误: {completion.error}")
                usage = getattr(completion, "usage", None) or {}
                llm_span.set_usage(
                    usage.get("prompt_tokens", 0),
                    usage.get("completion_tokens", 0),
                    model=getattr(completion, "model", None) or model_name,
                    total_tokens=usage.get("total_tokens"),
                )
                llm_span.set_output(getattr(completion, "content", None))
        except Exception as exc:
            # 失败也入账本（status=error），否则 /analytics-v2/error-rate 永远为空
            await record_llm_usage(
                model=llm_span.model,
                prompt_tokens=llm_span.prompt_tokens,
                completion_tokens=llm_span.completion_tokens,
                cost=llm_span.cost,
                latency_ms=llm_span.duration_ms,
                conversation_id=llm_span.trace_id,
                user_id=employee_id,
                status="error",
                error_message=str(exc)[:1000],
            )
            raise

        await record_llm_usage(
            model=llm_span.model,
            prompt_tokens=llm_span.prompt_tokens,
            completion_tokens=llm_span.completion_tokens,
            cost=llm_span.cost,
            latency_ms=llm_span.duration_ms,
            conversation_id=llm_span.trace_id,
            user_id=employee_id,
            status="success",
        )
        return completion


async def call_llm_with_fallback(
    model_router: Any,
    prompt: Optional[str] = None,
    *,
    messages: Optional[List[ChatMessage]] = None,
    employee_id: Optional[str] = None,
    period: Optional[str] = None,
    response_format: Optional[Dict[str, str]] = None,
) -> Tuple[ChatCompletion, str]:
    """调用 LLM 生成评估，失败时触发 runtime_reselect 档位降级并重试一次。

    Args:
        model_router: ModelRouter 实例（或兼容的测试替身），需提供
            get_provider_with_fallback() async 方法；可选 runtime_reselect /
            get_health_score / get_provider 用于降级重试。
        prompt: System prompt 字符串。与 messages 二选一；提供 prompt 时自动包装为
            [system:prompt, user:"请根据以上输入生成评估 JSON。"]。
        messages: 直接使用的消息列表（llm_judge 用 [system:prompt] 单条）。
        employee_id: 员工 ID，用于 Langfuse 追踪与降级重试 trace。
        period: 评估周期，用于 Langfuse 追踪 metadata。
        response_format: 传给 provider.chat_completion 的 response_format，
            默认 {"type": "json_object"}。

    Returns:
        (completion, tier)：completion 为 ChatCompletion 对象（含 content/model/usage），
        tier 为实际使用的模型档位。

    Raises:
        RuntimeError: 首次调用与降级重试均失败时抛出聚合异常。
        ValueError: 既未提供 prompt 也未提供 messages。
    """
    if messages is None:
        if prompt is None:
            raise ValueError("call_llm_with_fallback 必须提供 prompt 或 messages")
        messages = [
            ChatMessage(role="system", content=prompt),
            ChatMessage(role="user", content="请根据以上输入生成评估 JSON。"),
        ]
    if response_format is None:
        response_format = {"type": "json_object"}

    provider, tier = await model_router.get_provider_with_fallback()
    try:
        # WS-1：埋点 + 成本账本落库在 _invoke_provider 内完成，
        # provider 返回 error 字段的非异常失败同样在其中转为 RuntimeError
        completion = await _invoke_provider(
            provider,
            messages,
            response_format,
            tier=tier,
            employee_id=employee_id,
            period=period,
        )
        return completion, tier
    except Exception as first_err:
        # LLM 调用失败，触发 runtime_reselect 档位降级并重试一次
        logger.warning(
            "LLM 调用失败 (tier=%s): %s，尝试 runtime_reselect 降级重试",
            tier,
            first_err,
        )
        new_tier = None
        reselect = getattr(model_router, "runtime_reselect", None)
        if reselect is not None:
            try:
                get_health_score = getattr(model_router, "get_health_score", None)
                health_score = (
                    get_health_score(tier) if get_health_score is not None else 100.0
                )
                new_tier = reselect(tier, health_score)
            except Exception as re_err:
                logger.warning("runtime_reselect 调用失败: %s", re_err)
                new_tier = None

        # 若 runtime_reselect 返回 None（已无可降级档位）或无该方法，
        # 直接走原失败逻辑：记录指标并抛出首次异常
        if new_tier is None or new_tier == tier:
            try:
                record_evaluation_failure("fallback_exhausted")
            except Exception:
                logger.debug("record_evaluation_failure 埋点失败", exc_info=True)
            raise first_err

        # 降级重试一次（最多 1 次）
        try:
            with tracer.trace(
                name="fallback_retry",
                employee_id=employee_id,
                metadata={
                    "period": period,
                    "from_tier": tier,
                    "to_tier": new_tier,
                },
            ):
                # get_provider 为同步方法
                retry_provider = model_router.get_provider(new_tier)
                retry_completion = await _invoke_provider(
                    retry_provider,
                    messages,
                    response_format,
                    tier=new_tier,
                    employee_id=employee_id,
                    period=period,
                )
            if getattr(retry_completion, "error", None):
                raise RuntimeError(f"provider 返回错误: {retry_completion.error}")
            logger.info("降级重试成功: %s -> %s", tier, new_tier)
            return retry_completion, new_tier
        except Exception as retry_err:
            logger.error("降级重试仍失败 (tier=%s): %s", new_tier, retry_err)
            try:
                record_evaluation_failure("fallback_exhausted")
            except Exception:
                logger.debug("record_evaluation_failure 埋点失败", exc_info=True)
            raise RuntimeError(
                f"降级重试失败: {retry_err}（首次失败: {first_err}）"
            ) from retry_err
