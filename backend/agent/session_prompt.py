"""
Session Prompt

移植自 opencode (TypeScript/Effect) 的 packages/opencode/src/session/prompt.ts SessionPrompt.runLoop
会话级 Agent 主循环：写入用户消息 → while True 取最新消息 → stream LLM →
消费 LLMEvent → 持久化 part → 执行 tool → 回填 → 直到 finish 或 max_steps。

对齐 opencode runLoop 的核心语义，但简化：
- 去掉 compaction / subtask / structured output（MVP 不需要）
- 手动 ReAct 循环（不依赖 LangGraph ToolNode），便于精确推送 LLMEvent
- 工具调用结果作为新 user message 追加（provider ChatMessage 只有 role+content）
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from agent.session_processor import SessionProcessor
from agent.tool_registry import ToolRegistry
from core.config import Settings
from core.llm_events import (
    Finish,
    ProviderError,
    StepStart,
    ToolCall,
    ToolResult,
    stream_chunks_to_events,
)
from core.provider_resolver import get_provider_for_model
from core.providers.base import ChatMessage
from models.chat_models import ChatSession
from services.chat_service import ChatService

logger = logging.getLogger(__name__)


class Interrupted(BaseModel):
    """用户中断生成事件（对齐 LLMEvent 接口，供 event_to_sse_dict 序列化）。"""

    type: str = "interrupted"
    message: str = "用户已停止生成"


# 系统提示（对齐 opencode SystemPrompt.Service，简化版）
_DEFAULT_SYSTEM_PROMPT = """你是 AgentValue AI 助手，一个专业、友好的员工价值评估系统助手。

你的职责：
1. 回答用户关于员工价值评估、能力维度、绩效管理的问题
2. 当需要查询公司数据时，主动调用可用工具（如计算器、日期、情感分析、知识库查询等）
3. 回答时引用具体数据，保持客观、专业
4. 用清晰、结构化的中文回答，支持 Markdown 格式

注意：
- 如果不确定，坦诚告知并建议查询工具
- 不要编造数据，优先使用工具获取真实信息

## 可用工具

你拥有一组强大的内置工具，应在用户请求执行实际任务时主动使用它们：

- **calculator**: 计算数学表达式（支持 + - * / **, sqrt, sin, cos 等）
- **get_current_datetime**: 获取当前日期和时间（可指定时区偏移）
- **bash**: 执行 shell 命令（运行脚本、查看系统状态、文件操作等）
- **read_file**: 读取文件内容（支持 offset/limit 分页读取）
- **write_file**: 写入或创建文件（支持追加模式）
- **list_directory**: 列出目录内容（支持 glob 模式过滤）
- **web_fetch**: 抓取网页内容并返回纯文本
- **get_employee_history**: 查询员工历史评估记录与记忆
- **query_company_kb**: 搜索公司知识库（评估标准、价值观、政策等）

## 工具使用准则

- 当用户要求执行计算、读取/写入文件、运行命令、抓取网页、查询数据等任务时，**主动调用相应工具**，不要仅凭记忆作答。
- 工具调用结果以 `[工具调用结果]` 形式回传给你，请基于真实结果继续回答。
- 若工具返回错误或失败信息，告知用户失败原因，并尝试其他方式或建议人工介入。
- 对涉及文件系统、shell 命令、网络请求等有副作用的操作，先确认用户意图后再执行。
- 工具返回内容可能被截断，必要时可分页或分批调用获取完整信息。
"""


class SessionPrompt:
    """会话级 Agent 主循环（移植 opencode SessionPrompt.runLoop）"""

    def __init__(
        self,
        db: AsyncSession,
        chat_svc: ChatService,
        session: ChatSession,
        settings: Optional[Settings] = None,
        tool_registry: Optional[ToolRegistry] = None,
        max_steps: int = 10,
    ):
        self.db = db
        self.chat_svc = chat_svc
        self.session = session
        self.settings = settings
        self.max_steps = max_steps
        self.tool_registry = tool_registry or ToolRegistry(settings=settings)

    async def run_loop(
        self,
        user_text: str,
        attachments: Optional[List[dict]] = None,
        event_callback: Optional[Callable[[Any], Awaitable[None]]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        resume: bool = False,
    ) -> Any:
        """主循环。

        Args:
            user_text: 用户输入文本
            attachments: 附件列表，每项形如
                {"name","size","type","dataUrl"(仅图片)}
                图片附件将作为 image part 持久化并在构建 LLM 请求时
                以 multi-modal content 格式发送给支持 Vision 的模型。
            event_callback: LLMEvent 回调（推 SSE 用）
            should_stop: 中断检查回调，返回 True 时中断当前生成。
                在每次循环迭代和 token 流式输出时都会被检查。
            resume: 是否为恢复模式。True 时不写入新 user message，
                直接从历史消息继续上次中断的对话。

        Returns:
            最后一条 assistant ChatMessage
        """
        # 1. 写入 user message + text part (resume 模式跳过，继续上次中断的对话)
        if not resume:
            user_msg = await self.chat_svc.create_message(
                session_id=self.session.id, role="user"
            )
            await self.chat_svc.create_part(
                message_id=user_msg.id,
                session_id=self.session.id,
                part_type="text",
                sequence=0,
                text=user_text,
            )
            # 1.1 持久化附件 parts（图片以 image part 存 base64 dataUrl）
            if attachments:
                seq = 1
                for att in attachments:
                    a_type = att.get("type") or ""
                    if a_type.startswith("image/") and att.get("dataUrl"):
                        await self.chat_svc.create_part(
                            message_id=user_msg.id,
                            session_id=self.session.id,
                            part_type="image",
                            sequence=seq,
                            metadata={
                                "name": att.get("name"),
                                "size": att.get("size"),
                                "mime": a_type,
                                "dataUrl": att["dataUrl"],
                            },
                        )
                        seq += 1
                    else:
                        # 非图片附件：仅记录元数据（MVP 不解析内容）
                        await self.chat_svc.create_part(
                            message_id=user_msg.id,
                            session_id=self.session.id,
                            part_type="file",
                            sequence=seq,
                            metadata={
                                "name": att.get("name"),
                                "size": att.get("size"),
                                "mime": a_type,
                            },
                        )
                        seq += 1
            await self.chat_svc.commit()

        # 1.2 加载 MCP 工具（如果配置了），合并到 tool_registry
        mcp_tools = []
        try:
            _settings = self.settings
            if _settings is None:
                from core.config import get_settings

                _settings = get_settings()
            if getattr(_settings, "mcp_servers", None):
                from agent.mcp_client import get_global_mcp_manager

                manager = get_global_mcp_manager(_settings.mcp_servers)
                mcp_tools = await manager.get_tools()
                if mcp_tools:
                    logger.info("加载 %d 个 MCP 工具", len(mcp_tools))
                    # 注入到 tool_registry 使其可被 resolve_schemas / execute_tool 使用
                    await self.tool_registry.resolve()
                    for _t in mcp_tools:
                        _tname = getattr(_t, "name", None)
                        if _tname and _tname not in self.tool_registry._tools_by_name:
                            self.tool_registry._tools.append(_t)
                            self.tool_registry._tools_by_name[_tname] = _t
        except Exception as e:
            logger.warning("加载 MCP 工具失败: %s", e)

        # 2. 主循环
        assistant_msg = None
        step = 0
        while step < self.max_steps:
            # 2.0 中断检查（循环级）
            if should_stop and should_stop():
                await self._emit(event_callback, Interrupted())
                await self.chat_svc.commit()
                return assistant_msg

            await self._emit(event_callback, StepStart(index=step))

            # 2.1 取历史消息组装成 provider ChatMessage[]
            history = await self._build_history()

            # 2.2 resolve provider
            try:
                provider = await get_provider_for_model(self.session.model_name)
            except Exception as e:
                logger.warning("get_provider_for_model 失败: %s", e, exc_info=True)
                provider = None

            if provider is None:
                err = ProviderError(
                    message=f"无法获取模型 '{self.session.model_name}' 的 Provider，请检查配置"
                )
                await self._emit(event_callback, err)
                assistant_msg = await self.chat_svc.create_message(
                    session_id=self.session.id,
                    role="assistant",
                    model_id=self.session.model_name,
                )
                await self.chat_svc.complete_message(
                    assistant_msg.id, error={"message": err.message}
                )
                await self.chat_svc.commit()
                break

            # 2.3 resolve tools
            try:
                tool_schemas = await self.tool_registry.resolve_schemas()
            except Exception as e:
                logger.warning("resolve_schemas 失败: %s", e, exc_info=True)
                tool_schemas = []

            # 2.3.1 根据会话工具配置（session.metadata_["tools_config"]）过滤工具
            try:
                _meta = getattr(self.session, "metadata_", None) or {}
                _tools_config = (
                    _meta.get("tools_config", {}) if isinstance(_meta, dict) else {}
                )
                if _tools_config:
                    _filtered = []
                    for _s in tool_schemas:
                        _fn = _s.get("function", {}) if isinstance(_s, dict) else {}
                        _tname = _fn.get("name")
                        # 未在 config 中显式配置的工具默认启用
                        if _tools_config.get(_tname, True):
                            _filtered.append(_s)
                    tool_schemas = _filtered
            except Exception as e:
                logger.warning("过滤工具配置失败: %s", e)

            # 2.4 创建 assistant message + processor
            assistant_msg = await self.chat_svc.create_message(
                session_id=self.session.id,
                role="assistant",
                model_id=self.session.model_name,
                provider_id=provider.name() if hasattr(provider, "name") else None,
            )
            processor = SessionProcessor(
                self.db, self.chat_svc, assistant_msg, self.session
            )

            # 2.5 stream LLM + 消费事件
            tool_calls: List[ToolCall] = []
            try:
                chunk_stream = provider.stream_chat_completion(
                    messages=history,
                    tools=tool_schemas or None,
                )
                async for event in stream_chunks_to_events(
                    chunk_stream, step_index=step
                ):
                    # token 级中断检查
                    if should_stop and should_stop():
                        await self._emit(event_callback, Interrupted())
                        await self.chat_svc.commit()
                        return assistant_msg
                    await self._emit(event_callback, event)
                    await processor.handle(event)
                    if isinstance(event, ToolCall):
                        tool_calls.append(event)
            except Exception as e:
                logger.warning("stream LLM 异常: %s", e, exc_info=True)
                err = ProviderError(message=f"LLM 流式调用失败: {e}")
                await self._emit(event_callback, err)
                await processor.handle_error(e)
                await self.chat_svc.commit()
                break

            await self.chat_svc.commit()

            # 2.6 若有 tool_calls，执行后继续循环
            if tool_calls:
                tool_outputs: List[str] = []
                for tc in tool_calls:
                    result = await self.tool_registry.execute_tool(
                        tc.name, tc.input if isinstance(tc.input, dict) else {}
                    )
                    tool_result_event = ToolResult(
                        id=tc.id,
                        name=tc.name,
                        result={
                            "output": result.get("output"),
                            "error": result.get("error"),
                        },
                        output=result.get("output"),
                    )
                    await self._emit(event_callback, tool_result_event)
                    await processor.handle(tool_result_event)

                    # 收集结果用于追加 user message
                    if result.get("error"):
                        tool_outputs.append(
                            f"工具 {tc.name} 执行失败: {result['error']}"
                        )
                    else:
                        tool_outputs.append(
                            f"工具 {tc.name} 执行结果:\n{result.get('output', '')}"
                        )

                await self.chat_svc.commit()

                # 把 tool 结果追加为新 user message（provider ChatMessage 只有 role+content）
                tool_text = "\n\n".join(tool_outputs)
                tool_msg = await self.chat_svc.create_message(
                    session_id=self.session.id,
                    role="user",
                    parent_id=assistant_msg.id,
                )
                await self.chat_svc.create_part(
                    message_id=tool_msg.id,
                    session_id=self.session.id,
                    part_type="text",
                    sequence=0,
                    text=tool_text,
                    metadata={"role": "tool_result"},
                )
                await self.chat_svc.commit()

                step += 1
                continue
            else:
                # 无 tool_calls，完成
                finish_event = Finish(reason="stop")
                await self._emit(event_callback, finish_event)
                await processor.handle(finish_event)
                await self.chat_svc.commit()
                break

        # 达到 max_steps
        if step >= self.max_steps and assistant_msg is not None:
            finish_event = Finish(reason="max_steps")
            await self._emit(event_callback, finish_event)
            await processor.handle(finish_event)
            await self.chat_svc.commit()

        return assistant_msg

    # ============================================================
    # 历史消息组装
    # ============================================================

    async def _build_history(self) -> List[ChatMessage]:
        """从 DB 取 ChatMessage + ChatPart，组装成 provider ChatMessage 列表。

        对齐 opencode MessageV2.toModelMessagesEffect 的语义：
        - user message: role="user", content=text parts 拼接
        - assistant message: role="assistant", content=text parts 拼接
        - tool result message: role="user", content=tool output（标注 [tool result]）

        当 user message 含 image part 时，使用 multi-modal content 列表：
            [
                {"type": "text", "text": "..."},
                {"type": "image_url", "image_url": {"url": "data:image/...;base64,..."}}
            ]
        """
        messages_with_parts = await self.chat_svc.list_messages_with_parts(
            self.session.id
        )
        history: List[ChatMessage] = []

        # 系统提示
        history.append(ChatMessage(role="system", content=_DEFAULT_SYSTEM_PROMPT))

        for msg in messages_with_parts:
            role = msg["role"]
            parts = msg.get("parts", [])
            # 拼接 text parts
            text_parts = [p for p in parts if p.get("type") == "text"]
            image_parts = [p for p in parts if p.get("type") == "image"]

            text_content = "\n".join(
                p.get("text", "") for p in text_parts if p.get("text")
            )

            # tool 结果消息标注
            metadata = (text_parts[0].get("metadata") or {}) if text_parts else {}
            if metadata.get("role") == "tool_result":
                text_content = f"[工具调用结果]\n{text_content}"

            # 含图片：构建 multi-modal content 列表
            if image_parts:
                content_parts: List[Dict[str, Any]] = []
                if text_content:
                    content_parts.append({"type": "text", "text": text_content})
                for img in image_parts:
                    img_meta = img.get("metadata") or {}
                    url = img_meta.get("dataUrl") or ""
                    if not url:
                        continue
                    content_parts.append(
                        {"type": "image_url", "image_url": {"url": url}}
                    )
                if not content_parts:
                    # 图片 URL 都缺失则跳过
                    continue
                history.append(ChatMessage(role=role, content=content_parts))
                continue

            # 纯文本：保持原有逻辑
            if not text_parts:
                continue
            if not text_content:
                continue

            history.append(ChatMessage(role=role, content=text_content))

        return history

    def _format_tool_results(self, tool_calls: List[ToolCall]) -> str:
        """格式化工具调用结果为一条 user message。"""
        lines: List[str] = []
        for tc in tool_calls:
            result = self.tool_registry.execute_tool  # noqa
            # 结果已在 run_loop 中执行，这里只格式化 input
            try:
                input_str = json.dumps(tc.input, ensure_ascii=False, default=str)
            except Exception:
                input_str = str(tc.input)
            lines.append(f"工具 {tc.name} 被调用，参数: {input_str}")
        lines.append("（请根据工具执行结果继续回答用户问题）")
        return "\n".join(lines)

    async def _emit(
        self,
        callback: Optional[Callable[[Any], Awaitable[None]]],
        event: Any,
    ) -> None:
        """推送事件到回调（SSE 端点用）。"""
        if callback is not None:
            try:
                await callback(event)
            except Exception:
                logger.debug("event_callback 异常", exc_info=True)
