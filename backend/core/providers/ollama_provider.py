"""
Ollama 本地模型 Provider

对标 Dify Ollama Provider:
https://github.com/langgenius/dify/tree/main/api/core/model_runtime/model_providers/ollama

支持:
- chat_completion: POST /api/chat
- stream_chat_completion: POST /api/chat stream=true (NDJSON)
- vision_completion: 多模态(llava 系列, images 字段)
- function_calling: 通过 OpenAI 兼容端点 /v1/chat/completions
- health_check: GET /api/tags

Ollama API 文档: https://github.com/ollama/ollama/blob/main/docs/api.md
"""

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

_DEFAULT_BASE = "http://localhost:11434"
_REQUEST_TIMEOUT = 120.0  # 本地模型首 token 延迟可能较大,放宽超时


class OllamaProvider(BaseProvider):
    """Ollama 本地模型 Provider"""

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self._api_base = (config.base_url or _DEFAULT_BASE).rstrip("/")
        self._timeout = config.request_timeout or _REQUEST_TIMEOUT
        self._tier = config.model_tier or "unknown"

    def name(self) -> str:
        return "ollama"

    async def chat_completion(
        self,
        messages: List[ChatMessage],
        response_format: Optional[Dict[str, str]] = None,
    ) -> ChatCompletion:
        """非流式聊天补全(对标 Ollama /api/chat)"""
        payload = {
            "model": self.config.model_name,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "options": {
                "temperature": self.config.temperature,
                "num_predict": self.config.max_tokens,
            },
        }
        if response_format and response_format.get("type") == "json_object":
            payload["format"] = "json"

        url = f"{self._api_base}/api/chat"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            record_llm_request(self._tier, "error")
            raise RuntimeError(
                f"Ollama API 错误: {e.response.status_code} {e.response.text}"
            )

        content = data.get("message", {}).get("content", "")
        record_llm_request(self._tier, "success")
        # Ollama 在 prompt_eval_count / eval_count 字段给出 token 用量
        # 修复: record_token_usage 签名为 (tier, model, prompt_tokens, completion_tokens)
        # 之前漏传 model 参数导致埋点丢失。优先用 API 返回的 model,回退到配置的 model_name
        record_token_usage(
            self._tier,
            data.get("model", self.config.model_name),
            data.get("prompt_eval_count", 0),
            data.get("eval_count", 0),
        )
        return ChatCompletion(
            content=content,
            model=data.get("model", self.config.model_name),
            usage={
                "prompt_tokens": data.get("prompt_eval_count", 0),
                "completion_tokens": data.get("eval_count", 0),
                "total_tokens": data.get("prompt_eval_count", 0)
                + data.get("eval_count", 0),
            },
        )

    async def stream_chat_completion(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ):
        """流式聊天补全(对标 Ollama /api/chat stream=true NDJSON)。

        Ollama 流式返回 NDJSON(每行一个 JSON 对象),字段:
        - message.content: 文本增量
        - done: 是否结束(false 多次,true 最后)
        - total_duration / load_duration / prompt_eval_count / eval_count: 仅 done=true 时携带
        """
        payload: Dict[str, Any] = {
            "model": self.config.model_name,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": True,
            "options": {
                "temperature": temperature if temperature is not None else self.config.temperature,
                "num_predict": max_tokens or self.config.max_tokens,
            },
        }
        # Ollama 0.3.0+ 支持 tools(走原生 function calling)
        if tools:
            payload["tools"] = self._convert_tools(tools)

        url = f"{self._api_base}/api/chat"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream("POST", url, json=payload) as resp:
                    resp.raise_for_status()
                    tool_index = 0
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        message = data.get("message", {})
                        # 文本增量
                        content = message.get("content", "")
                        if content:
                            yield StreamChunk(content=content)

                        # tool_calls(若模型返回)
                        tool_calls = message.get("tool_calls")
                        if tool_calls:
                            for tc in tool_calls:
                                fn = tc.get("function", {})
                                args = fn.get("arguments", {})
                                args_str = (
                                    json.dumps(args, ensure_ascii=False)
                                    if isinstance(args, dict)
                                    else str(args)
                                )
                                yield StreamChunk(
                                    tool_calls=[
                                        ToolCallDelta(
                                            index=tool_index,
                                            name=fn.get("name", ""),
                                            id=fn.get("name", ""),
                                            arguments=args_str,
                                        )
                                    ]
                                )
                                tool_index += 1

                        if data.get("done"):
                            yield StreamChunk(
                                finish_reason="stop",
                                usage={
                                    "prompt_tokens": data.get("prompt_eval_count", 0),
                                    "completion_tokens": data.get("eval_count", 0),
                                    "total_tokens": data.get("prompt_eval_count", 0)
                                    + data.get("eval_count", 0),
                                },
                            )
            record_llm_request(self._tier, "success")
        except Exception as e:
            record_llm_request(self._tier, "error")
            logger.warning("Ollama stream_chat_completion 失败: %s", e)
            raise

    async def vision_completion(
        self,
        prompt: str,
        image_data: str,
        is_url: bool = False,
        model: Optional[str] = None,
    ) -> str:
        """视觉模型: 图片 + 文本 → 文本(llava 系列)"""
        if is_url:
            raise NotImplementedError("Ollama vision 仅支持 base64")
        payload = {
            "model": model or self.config.model_name,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [image_data],
                }
            ],
            "stream": False,
        }
        url = f"{self._api_base}/api/chat"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
            from core.metrics import record_llm_vision_call

            record_llm_vision_call(self._tier)
            return data.get("message", {}).get("content", "")
        except Exception as e:
            logger.warning("Ollama vision_completion 失败: %s", e)
            raise

    async def function_calling(
        self,
        messages: List[ChatMessage],
        tools: List[Dict[str, Any]],
        tool_choice: str = "auto",
        response_format: Optional[Dict[str, str]] = None,
    ) -> ChatCompletion:
        """function calling(Ollama 0.3.0+ 原生 tools 字段)"""
        payload = {
            "model": self.config.model_name,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "tools": self._convert_tools(tools),
            "stream": False,
            "options": {
                "temperature": self.config.temperature,
                "num_predict": self.config.max_tokens,
            },
        }
        url = f"{self._api_base}/api/chat"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            record_llm_request(self._tier, "error")
            raise RuntimeError(f"Ollama API 错误: {e.response.status_code}")

        message = data.get("message", {})
        content = message.get("content", "")
        tool_calls_raw = message.get("tool_calls", [])
        tool_calls = []
        for tc in tool_calls_raw:
            fn = tc.get("function", {})
            tool_calls.append(
                {
                    "id": fn.get("name", ""),
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments", {}),
                }
            )
        record_llm_request(self._tier, "success")
        return ChatCompletion(
            content=content,
            model=data.get("model", self.config.model_name),
            tool_calls=tool_calls if tool_calls else None,
        )

    async def health_check(self) -> bool:
        """检查 Provider 是否可用(GET /api/tags 列出本地模型)"""
        try:
            url = f"{self._api_base}/api/tags"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    return False
                data = resp.json()
                model_names = [m.get("name", "") for m in data.get("models", [])]
                # 检查目标模型是否已 pull(支持带 :tag 后缀)
                target = self.config.model_name.split(":")[0]
                return any(name.split(":")[0] == target for name in model_names)
        except Exception:
            return False

    # ============================================================
    # Helpers
    # ============================================================

    @staticmethod
    def _convert_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """OpenAI tools 格式 → Ollama tools 格式(Ollama 兼容 OpenAI tools 结构)"""
        converted = []
        for t in tools:
            if t.get("type") == "function":
                fn = t.get("function", {})
                converted.append(
                    {
                        "type": "function",
                        "function": {
                            "name": fn.get("name", ""),
                            "description": fn.get("description", ""),
                            "parameters": fn.get(
                                "parameters", {"type": "object", "properties": {}}
                            ),
                        },
                    }
                )
        return converted
