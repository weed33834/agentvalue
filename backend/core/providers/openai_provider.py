"""
OpenAI 兼容 Provider
同时支持：OpenAI 官方、Azure、LM Studio、Ollama、DeepSeek、阿里云百炼等。
"""

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from openai import APIConnectionError, AsyncOpenAI, InternalServerError, RateLimitError

from core.metrics import (
    record_llm_request,
    record_llm_vision_call,
    record_token_usage,
)
from .base import (
    BaseProvider,
    ChatCompletion,
    ChatMessage,
    ProviderConfig,
    StreamChunk,
    ToolCallDelta,
)

logger = logging.getLogger(__name__)

# 最大重试次数（不含首次调用）与请求超时（秒）
MAX_RETRIES = 3
REQUEST_TIMEOUT = 30.0
# Settings 未注入时的兜底模型名
_DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
_DEFAULT_VISION_MODEL = "gpt-4o-mini"

# 仅这三种异常触发重试，其余立即上抛
_RETRYABLE = (APIConnectionError, RateLimitError, InternalServerError)


class OpenAICompatibleProvider(BaseProvider):
    """OpenAI 兼容 API Provider"""

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        kwargs: Dict[str, str] = {}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        # 允许无 key 初始化（本地模型/测试场景），实际调用时若仍未配置会失败
        kwargs["api_key"] = config.api_key or "dummy-key"
        self.client = AsyncOpenAI(**kwargs)

    def name(self) -> str:
        return f"openai-compatible/{self.config.model_name}"

    @property
    def _tier(self) -> str:
        """模型档位，未注入时记为 unknown，避免埋点 label 缺失"""
        return self.config.model_tier or "unknown"

    @property
    def _timeout(self) -> float:
        """单次请求超时（秒）：优先用 ProviderConfig 注入值，否则兜底常量"""
        return getattr(self.config, "request_timeout", None) or REQUEST_TIMEOUT

    @property
    def embedding_model(self) -> str:
        """Embedding 模型名：优先用 ProviderConfig 注入值，否则兜底硬编码默认。"""
        return self.config.embedding_model or _DEFAULT_EMBEDDING_MODEL

    @property
    def vision_model(self) -> str:
        """视觉模型名：优先用 ProviderConfig 注入值，否则兜底硬编码默认。"""
        return self.config.vision_model or _DEFAULT_VISION_MODEL

    # ====== 内部 helper：重试与埋点 ======

    async def _retry(self, coro_fn: Callable[[], Any], action_label: str) -> Any:
        """对 OpenAI 兼容 API 调用做指数退避重试，耗尽抛 RuntimeError。

        仅 ``_RETRYABLE`` 三类异常重试，其余立即上抛。action_label 同时用于
        重试 warning 与耗尽 RuntimeError 消息，便于日志检索定位。
        """
        last_error: Optional[Exception] = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                return await coro_fn()
            except _RETRYABLE as e:
                last_error = e
                logger.warning(
                    f"{action_label}（第 {attempt + 1}/{MAX_RETRIES + 1} 次）: {e}"
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(2**attempt)
        raise RuntimeError(
            f"{action_label}，已重试 {MAX_RETRIES} 次仍未恢复。最后错误: {last_error}"
        ) from last_error

    @staticmethod
    def _safe_metric(fn: Callable, *args: Any) -> None:
        """埋点调用，失败仅记日志不阻断主流程。"""
        try:
            fn(*args)
        except Exception:
            logger.exception("埋点失败")

    def _record_usage(self, resp: Any, model_override: Optional[str] = None) -> None:
        """token usage 埋点，仅在 resp.usage 存在时记录。"""
        if resp.usage:
            self._safe_metric(
                record_token_usage,
                self._tier,
                resp.model or model_override or self.config.model_name,
                resp.usage.prompt_tokens,
                resp.usage.completion_tokens,
            )

    # ====== 对外能力 ======

    async def chat_completion(
        self,
        messages: List[ChatMessage],
        response_format: Optional[Dict[str, str]] = None,
    ) -> ChatCompletion:
        payload = {
            "model": self.config.model_name,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "timeout": self._timeout,
        }
        if response_format:
            payload["response_format"] = response_format

        label = f"模型 {self.config.model_name} 调用失败"
        try:
            resp = await self._retry(
                lambda: self.client.chat.completions.create(**payload), label
            )
        except RuntimeError:
            self._safe_metric(record_llm_request, self._tier, "error")
            raise

        content = self._strip_markdown_json(resp.choices[0].message.content or "")
        self._safe_metric(record_llm_request, self._tier, "success")
        self._record_usage(resp)
        return ChatCompletion(
            content=content,
            model=resp.model or self.config.model_name,
            usage={
                "prompt_tokens": resp.usage.prompt_tokens if resp.usage else 0,
                "completion_tokens": resp.usage.completion_tokens if resp.usage else 0,
                "total_tokens": resp.usage.total_tokens if resp.usage else 0,
            },
        )

    async def health_check(self) -> bool:
        try:
            models = await self.client.models.list()
            model_ids = [m.id for m in models.data]
            return self.config.model_name in model_ids
        except Exception as e:
            logger.debug(f"健康检查失败: {e}")
            return False

    async def stream_chat_completion(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ):
        """流式聊天补全(对标 OpenAI stream=True)。

        P2 深水区: yield StreamChunk,消费方按需拼接 content 或 tool_call arguments。
        参考: https://docs.langchain.com/oss/python/langchain/streaming/overview
        """
        payload: Dict[str, Any] = {
            "model": self.config.model_name,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_tokens": max_tokens or self.config.max_tokens,
            "timeout": self._timeout,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools

        try:
            stream = await self.client.chat.completions.create(**payload)
            async for chunk in stream:
                if not chunk.choices:
                    # 最后一个 chunk 可能只有 usage 没有 choices
                    if hasattr(chunk, "usage") and chunk.usage:
                        yield StreamChunk(
                            usage={
                                "prompt_tokens": chunk.usage.prompt_tokens,
                                "completion_tokens": chunk.usage.completion_tokens,
                                "total_tokens": chunk.usage.total_tokens,
                            }
                        )
                    continue
                delta = chunk.choices[0].delta
                content = delta.content if hasattr(delta, "content") else None
                tool_calls = None
                if delta.tool_calls:
                    tool_calls = [
                        ToolCallDelta(
                            index=tc.index,
                            name=tc.function.name if tc.function and tc.function.name else None,
                            id=tc.id,
                            arguments=tc.function.arguments if tc.function else None,
                        )
                        for tc in delta.tool_calls
                    ]
                finish = chunk.choices[0].finish_reason
                yield StreamChunk(
                    content=content,
                    tool_calls=tool_calls,
                    finish_reason=finish,
                )
            self._safe_metric(record_llm_request, self._tier, "success")
        except Exception as e:
            self._safe_metric(record_llm_request, self._tier, "error")
            logger.warning("stream_chat_completion 失败: %s", e)
            raise

    @staticmethod
    def _strip_markdown_json(content: str) -> str:
        content = content.strip()
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        return content.strip()

    # ====== 能力扩展：vision ======

    async def vision_completion(
        self,
        prompt: str,
        image_data: str,  # base64 编码或 URL
        is_url: bool = False,
        model: Optional[str] = None,
    ) -> str:
        """视觉模型：图片 + 文本 → 文本。

        image_data 为 URL 时直接透传；为 base64 时拼成 data URI。
        """
        used_model = model or self.vision_model
        url = image_data if is_url else f"data:image/jpeg;base64,{image_data}"
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }
        ]
        payload = {
            "model": used_model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "timeout": self._timeout,
        }

        label = f"模型 {used_model} vision 调用失败"
        try:
            resp = await self._retry(
                lambda: self.client.chat.completions.create(**payload), label
            )
        except RuntimeError:
            self._safe_metric(record_llm_vision_call, self._tier, "error")
            raise

        content = resp.choices[0].message.content or ""
        # 视觉调用单独统计，不重复调 record_llm_request（避免与 chat_completion
        # 的 LLM 调用计数双埋点造成指标膨胀）
        self._safe_metric(record_llm_vision_call, self._tier, "success")
        self._record_usage(resp, used_model)
        return content

    # ====== 能力扩展：function calling（P1） ======

    async def function_calling(
        self,
        messages: List[ChatMessage],
        tools: List[Dict[str, Any]],
        tool_choice: str = "auto",
        response_format: Optional[Dict[str, str]] = None,
    ) -> ChatCompletion:
        """OpenAI 兼容 function calling 实现。

        P1 增强: 透传 tools / tool_choice 给 OpenAI Chat Completions API,
        让模型自行决定是否调用工具及参数。返回 ChatCompletion.tool_calls
        (OpenAI tool_calls 格式: [{"id","type":"function","function":{"name","arguments"}}])

        上层 LangGraph ToolNode 会解析这个格式并执行对应工具。

        参考:
        - LangGraph ToolNode 官方: https://github.langchain.ac.cn/langgraph/how-tos/tool-calling/
        - OpenAI tool calling: https://platform.openai.com/docs/guides/function-calling
        """
        payload: Dict[str, Any] = {
            "model": self.config.model_name,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "timeout": self._timeout,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        if response_format:
            payload["response_format"] = response_format

        label = f"模型 {self.config.model_name} function calling 失败"
        try:
            resp = await self._retry(
                lambda: self.client.chat.completions.create(**payload), label
            )
        except RuntimeError:
            self._safe_metric(record_llm_request, self._tier, "error")
            raise

        msg = resp.choices[0].message
        # tool_calls 可能为 None(模型决定不调工具,直接出文本)或 list
        raw_tool_calls = getattr(msg, "tool_calls", None)
        tool_calls_list: List[Dict[str, Any]] = []
        if raw_tool_calls:
            for tc in raw_tool_calls:
                # 透传 OpenAI tool_calls 结构,LangGraph ToolNode 直接可消费
                tool_calls_list.append(
                    {
                        "id": getattr(tc, "id", ""),
                        "type": getattr(tc.type, "type", "function")
                        if hasattr(tc, "type")
                        else "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                )

        self._safe_metric(record_llm_request, self._tier, "success")
        self._record_usage(resp)
        return ChatCompletion(
            content=msg.content or "",
            model=resp.model or self.config.model_name,
            usage={
                "prompt_tokens": resp.usage.prompt_tokens if resp.usage else 0,
                "completion_tokens": resp.usage.completion_tokens if resp.usage else 0,
                "total_tokens": resp.usage.total_tokens if resp.usage else 0,
            },
            tool_calls=tool_calls_list or None,
        )
