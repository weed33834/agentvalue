"""
Anthropic Claude Provider

对标 Dify Anthropic Provider:
https://github.com/langgenius/dify/tree/main/api/core/model_runtime/model_providers/anthropic

支持:
- chat_completion: messages API
- stream_chat_completion: 流式 SSE (对标 OpenAI stream=True)
- vision_completion: 图片 + 文本
- function_calling: tool_use

Anthropic API 文档: https://docs.anthropic.com/en/api/messages
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from core.metrics import record_llm_request, record_token_usage
from .base import (
    BaseProvider,
    ChatCompletion,
    ChatMessage,
    ProviderConfig,
    StreamChunk,
    ToolCallDelta,
)

logger = logging.getLogger(__name__)

_API_BASE = "https://api.anthropic.com"
_API_VERSION = "2023-06-01"
_MAX_RETRIES = 3
_REQUEST_TIMEOUT = 60.0


class AnthropicProvider(BaseProvider):
    """Anthropic Claude Provider"""

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self._api_key = config.api_key or ""
        self._api_base = (config.base_url or _API_BASE).rstrip("/")
        self._timeout = config.request_timeout or _REQUEST_TIMEOUT
        self._tier = config.model_tier or "unknown"

    @property
    def _headers(self) -> Dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }

    def name(self) -> str:
        return "anthropic"

    async def chat_completion(
        self,
        messages: List[ChatMessage],
        response_format: Optional[Dict[str, str]] = None,
    ) -> ChatCompletion:
        """非流式聊天补全(对标 Anthropic messages API)"""
        system_prompt, user_messages = self._split_system(messages)
        payload = {
            "model": self.config.model_name,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "messages": user_messages,
        }
        if system_prompt:
            payload["system"] = system_prompt

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._api_base}/v1/messages",
                    headers=self._headers,
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            record_llm_request(self._tier, "error")
            raise RuntimeError(f"Anthropic API 错误: {e.response.status_code} {e.response.text}")

        # 提取文本内容
        content_parts = []
        tool_calls = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                content_parts.append(block["text"])
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "id": block["id"],
                        "name": block["name"],
                        "arguments": block["input"],
                    }
                )

        content = "".join(content_parts)
        usage = data.get("usage", {})
        record_llm_request(self._tier, "success")
        # 修复: record_token_usage 签名为 (tier, model, prompt_tokens, completion_tokens)
        # 之前漏传 model 参数导致埋点丢失。优先用 API 返回的 model,回退到配置的 model_name
        record_token_usage(
            self._tier,
            data.get("model", self.config.model_name),
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
        )
        return ChatCompletion(
            content=content,
            model=data.get("model", self.config.model_name),
            usage={
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("input_tokens", 0)
                + usage.get("output_tokens", 0),
            },
            tool_calls=tool_calls if tool_calls else None,
        )

    async def stream_chat_completion(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ):
        """流式聊天补全(对标 Anthropic streaming messages API)。

        Anthropic SSE 事件:
        - message_start: 消息开始
        - content_block_start: 内容块开始(text / tool_use)
        - content_block_delta: 内容增量(text_delta / input_json_delta)
        - content_block_stop: 内容块结束
        - message_delta: 消息级增量(usage)
        - message_stop: 消息结束
        """
        system_prompt, user_messages = self._split_system(messages)
        payload: Dict[str, Any] = {
            "model": self.config.model_name,
            "max_tokens": max_tokens or self.config.max_tokens,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "messages": user_messages,
            "stream": True,
        }
        if system_prompt:
            payload["system"] = system_prompt
        if tools:
            payload["tools"] = self._convert_tools(tools)

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream(
                    "POST",
                    f"{self._api_base}/v1/messages",
                    headers=self._headers,
                    json=payload,
                ) as resp:
                    resp.raise_for_status()
                    # 当前 tool_use 块的 index 跟踪
                    current_tool_index = 0
                    tool_name = ""
                    tool_id = ""
                    tool_args_buffer = ""

                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if not data_str.strip():
                            continue
                        try:
                            event = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        event_type = event.get("type")

                        if event_type == "content_block_start":
                            block = event.get("content_block", {})
                            if block.get("type") == "tool_use":
                                tool_name = block.get("name", "")
                                tool_id = block.get("id", "")
                                tool_args_buffer = ""
                                yield StreamChunk(
                                    tool_calls=[
                                        ToolCallDelta(
                                            index=current_tool_index,
                                            name=tool_name,
                                            id=tool_id,
                                        )
                                    ]
                                )

                        elif event_type == "content_block_delta":
                            delta = event.get("delta", {})
                            if delta.get("type") == "text_delta":
                                yield StreamChunk(content=delta.get("text", ""))
                            elif delta.get("type") == "input_json_delta":
                                partial = delta.get("partial_json", "")
                                tool_args_buffer += partial
                                yield StreamChunk(
                                    tool_calls=[
                                        ToolCallDelta(
                                            index=current_tool_index,
                                            arguments=partial,
                                        )
                                    ]
                                )

                        elif event_type == "content_block_stop":
                            if tool_name:
                                current_tool_index += 1
                                tool_name = ""

                        elif event_type == "message_delta":
                            delta = event.get("delta", {})
                            usage = event.get("usage", {})
                            if delta.get("stop_reason"):
                                yield StreamChunk(
                                    finish_reason=delta["stop_reason"],
                                    usage={
                                        "prompt_tokens": usage.get("input_tokens", 0),
                                        "completion_tokens": usage.get("output_tokens", 0),
                                        "total_tokens": usage.get("input_tokens", 0)
                                        + usage.get("output_tokens", 0),
                                    },
                                )

            record_llm_request(self._tier, "success")
        except Exception as e:
            record_llm_request(self._tier, "error")
            logger.warning("Anthropic stream_chat_completion 失败: %s", e)
            raise

    async def vision_completion(
        self,
        prompt: str,
        image_data: str,
        is_url: bool = False,
        model: Optional[str] = None,
    ) -> str:
        """视觉模型: 图片 + 文本 → 文本"""
        import base64

        if is_url:
            # Anthropic 支持 URL
            source = {"type": "url", "url": image_data}
        else:
            # base64
            source = {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": image_data,
            }

        payload = {
            "model": model or self.config.model_name,
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": source},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._api_base}/v1/messages",
                    headers=self._headers,
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
            from core.metrics import record_llm_vision_call

            record_llm_vision_call(self._tier)
            return data["content"][0]["text"]
        except Exception as e:
            logger.warning("Anthropic vision_completion 失败: %s", e)
            raise

    async def function_calling(
        self,
        messages: List[ChatMessage],
        tools: List[Dict[str, Any]],
        tool_choice: str = "auto",
        response_format: Optional[Dict[str, str]] = None,
    ) -> ChatCompletion:
        """function calling(Anthropic tool_use)"""
        system_prompt, user_messages = self._split_system(messages)
        payload: Dict[str, Any] = {
            "model": self.config.model_name,
            "max_tokens": self.config.max_tokens,
            "messages": user_messages,
            "tools": self._convert_tools(tools),
        }
        if system_prompt:
            payload["system"] = system_prompt

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._api_base}/v1/messages",
                    headers=self._headers,
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Anthropic API 错误: {e.response.status_code}")

        content_parts = []
        tool_calls = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                content_parts.append(block["text"])
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "id": block["id"],
                        "name": block["name"],
                        "arguments": block["input"],
                    }
                )

        record_llm_request(self._tier, "success")
        return ChatCompletion(
            content="".join(content_parts),
            model=data.get("model", self.config.model_name),
            tool_calls=tool_calls if tool_calls else None,
        )

    async def health_check(self) -> bool:
        """检查 Provider 是否可用(发一个最小请求)"""
        try:
            payload = {
                "model": self.config.model_name,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self._api_base}/v1/messages",
                    headers=self._headers,
                    json=payload,
                )
                return resp.status_code != 401
        except Exception:
            return False

    # ============================================================
    # Helpers
    # ============================================================

    @staticmethod
    def _split_system(
        messages: List[ChatMessage],
    ) -> tuple[Optional[str], List[Dict[str, Any]]]:
        """分离 system prompt 与 user messages (Anthropic API 要求)

        支持多模态 content：当 m.content 为 list（OpenAI 风格 multi-modal）时，
        转换为 Anthropic content blocks 格式：
            [{"type":"text","text":"..."},
             {"type":"image_url","image_url":{"url":"data:...;base64,..."}}]
        →
            [{"type":"text","text":"..."},
             {"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":"<base64>"}}]
        """
        system_parts: List[str] = []
        user_messages: List[Dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                # system 仅支持文本
                if isinstance(m.content, list):
                    system_parts.extend(
                        p.get("text", "") for p in m.content if p.get("type") == "text"
                    )
                else:
                    system_parts.append(m.content)
            else:
                content = AnthropicProvider._content_to_anthropic(m.content)
                if content is not None:
                    user_messages.append({"role": m.role, "content": content})
        return ("\n".join(system_parts) if system_parts else None, user_messages)

    @staticmethod
    def _content_to_anthropic(content: Any) -> Any:
        """将 ChatMessage.content 转为 Anthropic messages content。

        - str → 原样返回
        - list（OpenAI multi-modal）→ Anthropic content blocks 列表
        """
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return None
        blocks: List[Dict[str, Any]] = []
        for p in content:
            if not isinstance(p, dict):
                continue
            ptype = p.get("type")
            if ptype == "text" and p.get("text"):
                blocks.append({"type": "text", "text": p["text"]})
            elif ptype == "image_url":
                url = (p.get("image_url") or {}).get("url", "")
                mime, data = AnthropicProvider._parse_data_url(url)
                if data:
                    blocks.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime,
                                "data": data,
                            },
                        }
                    )
        return blocks if blocks else None

    @staticmethod
    def _parse_data_url(url: str) -> tuple[str, str]:
        """从 data:image/...;base64,... 中解析出 (mimeType, base64Data)。"""
        if not url or not url.startswith("data:"):
            return "image/jpeg", ""
        try:
            header, _, data = url.partition(",")
            mime = "image/jpeg"
            if ";" in header and "/" in header:
                mime = header[5:].split(";", 1)[0]
            return mime, data
        except Exception:
            return "image/jpeg", ""

    @staticmethod
    def _convert_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """OpenAI tools 格式 → Anthropic tools 格式"""
        converted = []
        for t in tools:
            if t.get("type") == "function":
                fn = t.get("function", {})
                converted.append(
                    {
                        "name": fn.get("name", ""),
                        "description": fn.get("description", ""),
                        "input_schema": fn.get(
                            "parameters", {"type": "object", "properties": {}}
                        ),
                    }
                )
        return converted
