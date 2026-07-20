"""
Session Processor

移植自 opencode (TypeScript/Effect) 的 packages/opencode/src/session/processor.ts
消费 LLMEvent，转成 ChatPart 写入 DB（对齐 opencode SessionProcessor 的"事件→part"持久化逻辑）。

设计要点：
- text-delta 内存累加，每 N 个 delta 或 text-end 时 flush DB（减少写入，SSE 不受影响）
- tool-input-delta 累加到 tool_state.input
- tool-call 更新 tool_state.input 为完整 input
- tool-result / tool-error 更新 tool_state.status + output / error
- step-start / step-finish 写 boundary part
- finish 更新 assistant message 的 completed_at / finish_reason / tokens
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from core.llm_events import (
    Finish,
    ProviderError,
    ReasoningDelta,
    ReasoningEnd,
    ReasoningStart,
    StepFinish,
    StepStart,
    TextDelta,
    TextEnd,
    TextStart,
    ToolCall,
    ToolError,
    ToolInputDelta,
    ToolInputEnd,
    ToolInputStart,
    ToolResult,
    Usage,
)
from models.chat_models import ChatMessage, ChatSession
from services.chat_service import ChatService

logger = logging.getLogger(__name__)

# text-delta 累积多少个后 flush 一次 DB（平衡性能与持久化完整性）
_FLUSH_INTERVAL = 8


class SessionProcessor:
    """消费 LLMEvent，转成 ChatPart 写入 DB。

    对齐 opencode SessionProcessor.process 的 handleEvent 逻辑。
    """

    def __init__(
        self,
        db: AsyncSession,
        chat_svc: ChatService,
        assistant_msg: ChatMessage,
        session: ChatSession,
    ):
        self.db = db
        self.chat_svc = chat_svc
        self.assistant_msg = assistant_msg
        self.session = session

        self._seq: int = 0
        # text_id → {part, buffer, delta_count}
        self._text_parts: Dict[str, Dict[str, Any]] = {}
        self._reasoning_parts: Dict[str, Dict[str, Any]] = {}
        # tool_call_id → ChatPart（持久化的 tool part）
        self._tool_parts: Dict[str, Any] = {}

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def handle(self, event: Any) -> None:
        """处理一个 LLMEvent，持久化到 DB。"""
        if isinstance(event, StepStart):
            await self._handle_step_start(event)
        elif isinstance(event, TextStart):
            await self._handle_text_start(event)
        elif isinstance(event, TextDelta):
            await self._handle_text_delta(event)
        elif isinstance(event, TextEnd):
            await self._handle_text_end(event)
        elif isinstance(event, ReasoningStart):
            await self._handle_reasoning_start(event)
        elif isinstance(event, ReasoningDelta):
            await self._handle_reasoning_delta(event)
        elif isinstance(event, ReasoningEnd):
            await self._handle_reasoning_end(event)
        elif isinstance(event, ToolInputStart):
            await self._handle_tool_input_start(event)
        elif isinstance(event, ToolInputDelta):
            await self._handle_tool_input_delta(event)
        elif isinstance(event, ToolInputEnd):
            pass  # 等 ToolCall 拿完整 input
        elif isinstance(event, ToolCall):
            await self._handle_tool_call(event)
        elif isinstance(event, ToolResult):
            await self._handle_tool_result(event)
        elif isinstance(event, ToolError):
            await self._handle_tool_error(event)
        elif isinstance(event, StepFinish):
            await self._handle_step_finish(event)
        elif isinstance(event, Finish):
            await self._handle_finish(event)
        elif isinstance(event, ProviderError):
            await self._handle_provider_error(event)

    # ============================================================
    # Step 边界
    # ============================================================

    async def _handle_step_start(self, event: StepStart) -> None:
        await self.chat_svc.create_part(
            message_id=self.assistant_msg.id,
            session_id=self.session.id,
            part_type="step-start",
            sequence=self._next_seq(),
            step_index=event.index,
        )

    async def _handle_step_finish(self, event: StepFinish) -> None:
        await self.chat_svc.create_part(
            message_id=self.assistant_msg.id,
            session_id=self.session.id,
            part_type="step-finish",
            sequence=self._next_seq(),
            step_index=event.index,
            metadata={"reason": event.reason} if event.reason else None,
        )
        # 更新 message tokens
        if event.usage:
            tokens = {
                "input_tokens": event.usage.input_tokens,
                "output_tokens": event.usage.output_tokens,
                "total_tokens": event.usage.total_tokens,
                "step": event.index,
            }
            # 更新 assistant message 的 tokens 用量
            self.assistant_msg.tokens = tokens
            await self.chat_svc.update_message_tokens(
                self.assistant_msg.id, tokens
            )

    # ============================================================
    # Text
    # ============================================================

    async def _handle_text_start(self, event: TextStart) -> None:
        part = await self.chat_svc.create_part(
            message_id=self.assistant_msg.id,
            session_id=self.session.id,
            part_type="text",
            sequence=self._next_seq(),
            text="",
            metadata={"text_id": event.id},
        )
        self._text_parts[event.id] = {"part": part, "buffer": "", "delta_count": 0}

    async def _handle_text_delta(self, event: TextDelta) -> None:
        entry = self._text_parts.get(event.id)
        if entry is None:
            # 容错：未收到 text-start，创建一个
            part = await self.chat_svc.create_part(
                message_id=self.assistant_msg.id,
                session_id=self.session.id,
                part_type="text",
                sequence=self._next_seq(),
                text="",
                metadata={"text_id": event.id},
            )
            entry = {"part": part, "buffer": "", "delta_count": 0}
            self._text_parts[event.id] = entry
        entry["buffer"] += event.text
        entry["delta_count"] += 1
        # 每 N 个 delta flush 一次
        if entry["delta_count"] >= _FLUSH_INTERVAL:
            await self.chat_svc.update_part(entry["part"].id, text=entry["buffer"])
            entry["delta_count"] = 0

    async def _handle_text_end(self, event: TextEnd) -> None:
        entry = self._text_parts.get(event.id)
        if entry is not None:
            await self.chat_svc.update_part(entry["part"].id, text=entry["buffer"])
            entry["delta_count"] = 0

    # ============================================================
    # Reasoning（同 text 逻辑，provider 暂未产出，预留）
    # ============================================================

    async def _handle_reasoning_start(self, event: ReasoningStart) -> None:
        part = await self.chat_svc.create_part(
            message_id=self.assistant_msg.id,
            session_id=self.session.id,
            part_type="reasoning",
            sequence=self._next_seq(),
            text="",
            metadata={"reasoning_id": event.id},
        )
        self._reasoning_parts[event.id] = {"part": part, "buffer": "", "delta_count": 0}

    async def _handle_reasoning_delta(self, event: ReasoningDelta) -> None:
        entry = self._reasoning_parts.get(event.id)
        if entry is None:
            return
        entry["buffer"] += event.text
        entry["delta_count"] += 1
        if entry["delta_count"] >= _FLUSH_INTERVAL:
            await self.chat_svc.update_part(entry["part"].id, text=entry["buffer"])
            entry["delta_count"] = 0

    async def _handle_reasoning_end(self, event: ReasoningEnd) -> None:
        entry = self._reasoning_parts.get(event.id)
        if entry is not None:
            await self.chat_svc.update_part(entry["part"].id, text=entry["buffer"])

    # ============================================================
    # Tool
    # ============================================================

    async def _handle_tool_input_start(self, event: ToolInputStart) -> None:
        part = await self.chat_svc.create_part(
            message_id=self.assistant_msg.id,
            session_id=self.session.id,
            part_type="tool",
            sequence=self._next_seq(),
            tool_name=event.name,
            tool_call_id=event.id,
            tool_state={
                "status": "running",
                "input": "",
                "output": None,
                "error": None,
            },
        )
        self._tool_parts[event.id] = part

    async def _handle_tool_input_delta(self, event: ToolInputDelta) -> None:
        part = self._tool_parts.get(event.id)
        if part is None:
            return
        state = part.tool_state or {"status": "running", "input": ""}
        state["input"] = state.get("input", "") + event.text
        await self.chat_svc.update_part(part.id, tool_state=state)

    async def _handle_tool_call(self, event: ToolCall) -> None:
        """ToolCall 携带完整 input，更新 tool_state。"""
        part = self._tool_parts.get(event.id)
        if part is None:
            # 容错：未收到 tool-input-start，创建一个
            part = await self.chat_svc.create_part(
                message_id=self.assistant_msg.id,
                session_id=self.session.id,
                part_type="tool",
                sequence=self._next_seq(),
                tool_name=event.name,
                tool_call_id=event.id,
                tool_state={"status": "running", "input": "", "output": None, "error": None},
            )
            self._tool_parts[event.id] = part
        state = part.tool_state or {}
        state["status"] = "running"
        state["input"] = event.input
        await self.chat_svc.update_part(part.id, tool_state=state)

    async def _handle_tool_result(self, event: ToolResult) -> None:
        part = self._tool_parts.get(event.id)
        if part is None:
            return
        state = part.tool_state or {}
        state["status"] = "completed"
        state["output"] = event.output
        state["result"] = event.result
        await self.chat_svc.update_part(part.id, tool_state=state)

    async def _handle_tool_error(self, event: ToolError) -> None:
        part = self._tool_parts.get(event.id)
        if part is None:
            return
        state = part.tool_state or {}
        state["status"] = "error"
        state["error"] = event.message
        await self.chat_svc.update_part(part.id, tool_state=state)

    # ============================================================
    # Finish / Error
    # ============================================================

    async def _handle_finish(self, event: Finish) -> None:
        await self.chat_svc.complete_message(
            self.assistant_msg.id,
            finish_reason=event.reason or "stop",
            tokens=self._aggregate_tokens(),
        )

    async def _handle_provider_error(self, event: ProviderError) -> None:
        await self.chat_svc.complete_message(
            self.assistant_msg.id,
            error={"message": event.message, "retryable": event.retryable},
        )

    async def handle_error(self, error: Exception) -> None:
        """流式异常时的兜底处理。"""
        await self.chat_svc.complete_message(
            self.assistant_msg.id,
            error={"message": str(error)},
        )

    async def handle_finish(self, reason: str = "stop") -> None:
        """手动标记完成。"""
        await self.chat_svc.complete_message(
            self.assistant_msg.id,
            finish_reason=reason,
            tokens=self._aggregate_tokens(),
        )

    def _aggregate_tokens(self) -> Optional[Dict[str, Any]]:
        """从所有 step-finish 聚合 token 用量（简化版：返回空 dict 由 StepFinish 单独记录）"""
        return {}
