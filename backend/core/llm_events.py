"""
LLMEvent 流式协议

移植自 opencode (TypeScript/Effect) 的 packages/llm/src/schema/events.ts
16 种事件类型，用 Pydantic discriminated union 表达，跨 provider 统一契约。

设计要点：
- 每个事件都有 `type` 字段作为 discriminator
- 与现有 core.providers.base.StreamChunk 适配（stream_chunks_to_events）
- 前后端共享同一套事件名，SSE 端点直接用 event.type 作为 SSE event 名
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Annotated, Any, AsyncIterator, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field

from core.providers.base import StreamChunk
from core.providers.stream_buffer import ToolCallAggregator

logger = logging.getLogger(__name__)


def _new_id() -> str:
    """生成事件/ part 唯一 ID（对齐 opencode 的 ContentBlockID/ToolCallID 语义）"""
    return uuid.uuid4().hex


# ============================================================
# Usage 统计
# ============================================================


class Usage(BaseModel):
    """token 用量统计（对齐 opencode Usage schema）"""

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    non_cached_input_tokens: Optional[int] = None
    cache_read_input_tokens: Optional[int] = None
    cache_write_input_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    provider_metadata: Optional[Dict[str, Any]] = None


# ============================================================
# 16 类 LLMEvent（对齐 opencode packages/llm/src/schema/events.ts）
# ============================================================


class StepStart(BaseModel):
    """一步 LLM 调用开始（一个 step 可能含多轮 tool 调用）"""

    type: Literal["step-start"] = "step-start"
    index: int


class TextStart(BaseModel):
    """文本输出块开始"""

    type: Literal["text-start"] = "text-start"
    id: str
    provider_metadata: Optional[Dict[str, Any]] = None


class TextDelta(BaseModel):
    """文本增量（逐 token）"""

    type: Literal["text-delta"] = "text-delta"
    id: str
    text: str
    provider_metadata: Optional[Dict[str, Any]] = None


class TextEnd(BaseModel):
    """文本输出块结束"""

    type: Literal["text-end"] = "text-end"
    id: str
    provider_metadata: Optional[Dict[str, Any]] = None


class ReasoningStart(BaseModel):
    """推理（thinking）块开始 - Anthropic extended thinking / OpenAI o-series"""

    type: Literal["reasoning-start"] = "reasoning-start"
    id: str
    provider_metadata: Optional[Dict[str, Any]] = None


class ReasoningDelta(BaseModel):
    """推理增量"""

    type: Literal["reasoning-delta"] = "reasoning-delta"
    id: str
    text: str


class ReasoningEnd(BaseModel):
    """推理块结束"""

    type: Literal["reasoning-end"] = "reasoning-end"
    id: str


class ToolInputStart(BaseModel):
    """工具调用输入开始（首个 chunk 携带 name + id）"""

    type: Literal["tool-input-start"] = "tool-input-start"
    id: str
    name: str


class ToolInputDelta(BaseModel):
    """工具调用输入增量（arguments 字符串逐片补全）"""

    type: Literal["tool-input-delta"] = "tool-input-delta"
    id: str
    name: str
    text: str


class ToolInputEnd(BaseModel):
    """工具调用输入收完（arguments 已完整）"""

    type: Literal["tool-input-end"] = "tool-input-end"
    id: str
    name: str


class ToolCall(BaseModel):
    """工具调用决策完成（含完整 input 参数）"""

    type: Literal["tool-call"] = "tool-call"
    id: str
    name: str
    input: Any = None
    provider_executed: Optional[bool] = None


class ToolResult(BaseModel):
    """工具执行结果"""

    type: Literal["tool-result"] = "tool-result"
    id: str
    name: str
    result: Dict[str, Any] = Field(default_factory=dict)
    output: Optional[str] = None


class ToolError(BaseModel):
    """工具执行错误"""

    type: Literal["tool-error"] = "tool-error"
    id: str
    name: str
    message: str


class StepFinish(BaseModel):
    """一步 LLM 调用结束（含 usage）"""

    type: Literal["step-finish"] = "step-finish"
    index: int
    reason: Optional[str] = None
    usage: Optional[Usage] = None


class Finish(BaseModel):
    """整个 agent loop 结束"""

    type: Literal["finish"] = "finish"
    reason: Optional[str] = None
    usage: Optional[Usage] = None


class ProviderError(BaseModel):
    """Provider 调用错误"""

    type: Literal["provider-error"] = "provider-error"
    message: str
    retryable: Optional[bool] = None


# discriminated union - Pydantic v2 用 Field(discriminator=...)
LLMEvent = Annotated[
    Union[
        StepStart,
        TextStart,
        TextDelta,
        TextEnd,
        ReasoningStart,
        ReasoningDelta,
        ReasoningEnd,
        ToolInputStart,
        ToolInputDelta,
        ToolInputEnd,
        ToolCall,
        ToolResult,
        ToolError,
        StepFinish,
        Finish,
        ProviderError,
    ],
    Field(discriminator="type"),
]


# 事件类型名集合，供校验/文档用
EVENT_TYPES = {
    "step-start",
    "text-start",
    "text-delta",
    "text-end",
    "reasoning-start",
    "reasoning-delta",
    "reasoning-end",
    "tool-input-start",
    "tool-input-delta",
    "tool-input-end",
    "tool-call",
    "tool-result",
    "tool-error",
    "step-finish",
    "finish",
    "provider-error",
}


# ============================================================
# StreamChunk → LLMEvent adapter
# ============================================================


class _StreamState:
    """stream_chunks_to_events 的内部状态机"""

    def __init__(self, step_index: int):
        self.step_index = step_index
        self.text_id: Optional[str] = None
        self.text_started: bool = False
        self.aggregator = ToolCallAggregator()
        # tool index → {id, name, input_started}
        self.tool_meta: Dict[int, Dict[str, Any]] = {}
        self.last_usage: Optional[Dict[str, int]] = None
        self.finish_reason: Optional[str] = None


async def stream_chunks_to_events(
    chunks: AsyncIterator[StreamChunk],
    step_index: int = 0,
) -> AsyncIterator[Any]:
    """把 provider StreamChunk 流转换成 LLMEvent 流。

    对齐 opencode processor.ts 的事件产出逻辑，但简化为纯函数式 adapter：
    - 首个 content → TextStart
    - 每个 content → TextDelta
    - 流结束 → TextEnd（如果有 text）
    - tool_calls 首片（name+id）→ ToolInputStart
    - tool_calls arguments → ToolInputDelta
    - 流结束 finalize → ToolInputEnd + ToolCall（含完整 input）
    - finish_reason / usage → StepFinish

    Args:
        chunks: provider.stream_chat_completion() 返回的 StreamChunk 异步迭代器
        step_index: 当前 step 索引（多步 agent loop 用）

    Yields:
        LLMEvent 实例
    """
    state = _StreamState(step_index)

    try:
        async for chunk in chunks:
            # 1. 文本增量
            if chunk.content:
                if not state.text_started:
                    state.text_id = _new_id()
                    state.text_started = True
                    yield TextStart(id=state.text_id)
                yield TextDelta(id=state.text_id, text=chunk.content)

            # 2. tool_calls delta
            if chunk.tool_calls:
                deltas = state.aggregator.feed(chunk)
                for tc in deltas:
                    meta = state.tool_meta.setdefault(
                        tc.index, {"id": None, "name": None, "input_started": False}
                    )
                    # 首片携带 name + id → ToolInputStart
                    if tc.name and tc.id and not meta["input_started"]:
                        meta["id"] = tc.id
                        meta["name"] = tc.name
                        meta["input_started"] = True
                        yield ToolInputStart(id=tc.id, name=tc.name)
                    # arguments 增量 → ToolInputDelta
                    if tc.arguments and meta["id"]:
                        yield ToolInputDelta(
                            id=meta["id"], name=meta["name"] or "", text=tc.arguments
                        )

            # 3. usage（最后一个 chunk）
            if chunk.usage:
                state.last_usage = chunk.usage

            # 4. finish_reason
            if chunk.finish_reason:
                state.finish_reason = chunk.finish_reason

    except Exception as e:
        logger.warning("stream_chunks_to_events 流式异常: %s", e, exc_info=True)
        yield ProviderError(message=f"LLM 流式调用失败: {e}", retryable=False)
        return

    # 流结束：收尾 text
    if state.text_started and state.text_id:
        yield TextEnd(id=state.text_id)

    # 流结束：finalize tool_calls → ToolInputEnd + ToolCall
    final_calls = state.aggregator.finalize()
    for call in final_calls:
        call_id = call.get("id") or _new_id()
        call_name = call.get("name") or ""
        yield ToolInputEnd(id=call_id, name=call_name)
        yield ToolCall(
            id=call_id,
            name=call_name,
            input=call.get("arguments", {}),
        )

    # StepFinish（含 usage + reason）
    usage = None
    if state.last_usage:
        usage = Usage(
            input_tokens=state.last_usage.get("prompt_tokens")
            or state.last_usage.get("input_tokens"),
            output_tokens=state.last_usage.get("completion_tokens")
            or state.last_usage.get("output_tokens"),
            total_tokens=state.last_usage.get("total_tokens"),
            provider_metadata={
                k: v
                for k, v in state.last_usage.items()
                if k
                not in (
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "input_tokens",
                    "output_tokens",
                )
            },
        )
    yield StepFinish(
        index=state.step_index,
        reason=state.finish_reason,
        usage=usage,
    )


def event_to_sse_dict(event: Any) -> Dict[str, str]:
    """把 LLMEvent 转成 SSE 事件 dict（event + data）。

    供 sse-starlette EventSourceResponse 直接 yield 使用。
    """
    return {
        "event": event.type,
        "data": json.dumps(
            event.model_dump(exclude_none=True), ensure_ascii=False, default=str
        ),
    }
