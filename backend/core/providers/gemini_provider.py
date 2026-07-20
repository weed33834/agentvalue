"""
Google Gemini Provider

对标 Dify Gemini Provider:
https://github.com/langgenius/dify/tree/main/api/core/model_runtime/model_providers/google

支持:
- chat_completion: generateContent
- stream_chat_completion: streamGenerateContent (SSE)
- vision_completion: 图片输入
- function_calling: functionDeclarations
- health_check: GET /v1beta/models

Gemini API 文档: https://ai.google.dev/api/rest/v1beta/models/generateContent
"""

import base64
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

_API_BASE = "https://generativelanguage.googleapis.com"
_API_VERSION = "v1beta"
_MAX_RETRIES = 3
_REQUEST_TIMEOUT = 60.0

# Gemini finish_reason → OpenAI 风格
_FINISH_MAP = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
}


class GeminiProvider(BaseProvider):
    """Google Gemini Provider"""

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self._api_key = config.api_key or ""
        self._api_base = (config.base_url or _API_BASE).rstrip("/")
        self._timeout = config.request_timeout or _REQUEST_TIMEOUT
        self._tier = config.model_tier or "unknown"

    def name(self) -> str:
        return "gemini"

    def _headers(self) -> Dict[str, str]:
        return {"content-type": "application/json"}

    async def chat_completion(
        self,
        messages: List[ChatMessage],
        response_format: Optional[Dict[str, str]] = None,
    ) -> ChatCompletion:
        """非流式聊天补全(对标 Gemini generateContent)"""
        system_prompt, contents = self._convert_messages(messages)
        payload: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": self.config.temperature,
                "maxOutputTokens": self.config.max_tokens,
            },
        }
        if system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}
        if response_format and response_format.get("type") == "json_object":
            payload["generationConfig"]["responseMimeType"] = "application/json"

        url = f"{self._api_base}/{_API_VERSION}/models/{self.config.model_name}:generateContent?key={self._api_key}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, headers=self._headers(), json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            record_llm_request(self._tier, "error")
            raise RuntimeError(
                f"Gemini API 错误: {e.response.status_code} {e.response.text}"
            )

        content, tool_calls = self._extract_response(data)
        usage_meta = data.get("usageMetadata", {})
        record_llm_request(self._tier, "success")
        # 修复: record_token_usage 签名为 (tier, model, prompt_tokens, completion_tokens)
        # 之前漏传 model 参数导致埋点丢失。Gemini 不返回实际 model,用配置的 model_name
        record_token_usage(
            self._tier,
            self.config.model_name,
            usage_meta.get("promptTokenCount", 0),
            usage_meta.get("candidatesTokenCount", 0),
        )
        return ChatCompletion(
            content=content,
            model=self.config.model_name,
            usage={
                "prompt_tokens": usage_meta.get("promptTokenCount", 0),
                "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
                "total_tokens": usage_meta.get("totalTokenCount", 0),
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
        """流式聊天补全(对标 Gemini streamGenerateContent)。

        Gemini 流式响应每条 data: 是一个完整的 GenerateContentResponse,
        candidates[0].content.parts[] 内含 text 或 functionCall。
        """
        system_prompt, contents = self._convert_messages(messages)
        payload: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature if temperature is not None else self.config.temperature,
                "maxOutputTokens": max_tokens or self.config.max_tokens,
            },
        }
        if system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}
        if tools:
            payload["tools"] = [{"functionDeclarations": self._convert_tools(tools)}]

        url = (
            f"{self._api_base}/{_API_VERSION}/models/{self.config.model_name}"
            f":streamGenerateContent?alt=sse&key={self._api_key}"
        )
        current_tool_index = 0
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream(
                    "POST", url, headers=self._headers(), json=payload
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:].strip()
                        if not data_str:
                            continue
                        try:
                            event = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        candidates = event.get("candidates", [])
                        if not candidates:
                            # 可能只携带 usageMetadata
                            usage_meta = event.get("usageMetadata")
                            if usage_meta:
                                yield StreamChunk(
                                    usage={
                                        "prompt_tokens": usage_meta.get("promptTokenCount", 0),
                                        "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
                                        "total_tokens": usage_meta.get("totalTokenCount", 0),
                                    }
                                )
                            continue

                        candidate = candidates[0]
                        content_part = candidate.get("content", {})
                        for part in content_part.get("parts", []):
                            if "text" in part:
                                yield StreamChunk(content=part["text"])
                            elif "functionCall" in part:
                                fc = part["functionCall"]
                                args_str = json.dumps(fc.get("args", {}), ensure_ascii=False)
                                yield StreamChunk(
                                    tool_calls=[
                                        ToolCallDelta(
                                            index=current_tool_index,
                                            name=fc.get("name", ""),
                                            id=fc.get("name", ""),
                                            arguments=args_str,
                                        )
                                    ]
                                )
                                current_tool_index += 1

                        finish = candidate.get("finishReason")
                        if finish:
                            usage_meta = event.get("usageMetadata", {})
                            yield StreamChunk(
                                finish_reason=_FINISH_MAP.get(finish, finish.lower()),
                                usage={
                                    "prompt_tokens": usage_meta.get("promptTokenCount", 0),
                                    "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
                                    "total_tokens": usage_meta.get("totalTokenCount", 0),
                                },
                            )
            record_llm_request(self._tier, "success")
        except Exception as e:
            record_llm_request(self._tier, "error")
            logger.warning("Gemini stream_chat_completion 失败: %s", e)
            raise

    async def vision_completion(
        self,
        prompt: str,
        image_data: str,
        is_url: bool = False,
        model: Optional[str] = None,
    ) -> str:
        """视觉模型: 图片 + 文本 → 文本"""
        if is_url:
            # Gemini 不支持任意 URL,需要 fetch 后 base64;这里报错提示用户传 base64
            raise NotImplementedError("Gemini vision 仅支持 base64,请传 is_url=False")
        parts = [
            {"text": prompt},
            {"inlineData": {"mimeType": "image/jpeg", "data": image_data}},
        ]
        payload = {"contents": [{"role": "user", "parts": parts}]}
        model_name = model or self.config.model_name
        url = f"{self._api_base}/{_API_VERSION}/models/{model_name}:generateContent?key={self._api_key}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, headers=self._headers(), json=payload)
                resp.raise_for_status()
                data = resp.json()
            from core.metrics import record_llm_vision_call

            record_llm_vision_call(self._tier)
            parts = data["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        except Exception as e:
            logger.warning("Gemini vision_completion 失败: %s", e)
            raise

    async def function_calling(
        self,
        messages: List[ChatMessage],
        tools: List[Dict[str, Any]],
        tool_choice: str = "auto",
        response_format: Optional[Dict[str, str]] = None,
    ) -> ChatCompletion:
        """function calling(Gemini functionDeclarations)"""
        system_prompt, contents = self._convert_messages(messages)
        payload: Dict[str, Any] = {
            "contents": contents,
            "tools": [{"functionDeclarations": self._convert_tools(tools)}],
            "generationConfig": {
                "temperature": self.config.temperature,
                "maxOutputTokens": self.config.max_tokens,
            },
        }
        if system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

        url = f"{self._api_base}/{_API_VERSION}/models/{self.config.model_name}:generateContent?key={self._api_key}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, headers=self._headers(), json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            record_llm_request(self._tier, "error")
            raise RuntimeError(f"Gemini API 错误: {e.response.status_code}")

        content, tool_calls = self._extract_response(data)
        record_llm_request(self._tier, "success")
        return ChatCompletion(
            content=content,
            model=self.config.model_name,
            tool_calls=tool_calls if tool_calls else None,
        )

    async def health_check(self) -> bool:
        """检查 Provider 是否可用(GET /v1beta/models 验证 API Key)"""
        try:
            url = f"{self._api_base}/{_API_VERSION}/models?key={self._api_key}"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                return resp.status_code == 200
        except Exception:
            return False

    # ============================================================
    # Helpers
    # ============================================================

    @staticmethod
    def _convert_messages(
        messages: List[ChatMessage],
    ) -> tuple[Optional[str], List[Dict[str, Any]]]:
        """将 ChatMessage 列表转换为 Gemini contents 格式。

        Gemini 角色只有 user / model,system 拆出作为 systemInstruction。
        assistant → model,user 保留。

        支持多模态 content：当 m.content 为 list 时（OpenAI 风格 multi-modal），
        将其转换为 Gemini parts 格式：
            [{"type":"text","text":"..."},
             {"type":"image_url","image_url":{"url":"data:...;base64,..."}}]
        →
            [{"text":"..."},
             {"inlineData":{"mimeType":"image/jpeg","data":"<base64>"}}]
        """
        system_parts: List[str] = []
        contents: List[Dict[str, Any]] = []
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
                role = "model" if m.role == "assistant" else "user"
                parts = GeminiProvider._content_to_gemini_parts(m.content)
                if parts:
                    contents.append({"role": role, "parts": parts})
        return ("\n".join(system_parts) if system_parts else None, contents)

    @staticmethod
    def _content_to_gemini_parts(content: Any) -> List[Dict[str, Any]]:
        """将 ChatMessage.content（str 或 multi-modal list）转为 Gemini parts。"""
        if isinstance(content, str):
            return [{"text": content}] if content else []
        if not isinstance(content, list):
            return []
        parts: List[Dict[str, Any]] = []
        for p in content:
            if not isinstance(p, dict):
                continue
            ptype = p.get("type")
            if ptype == "text" and p.get("text"):
                parts.append({"text": p["text"]})
            elif ptype == "image_url":
                url = (p.get("image_url") or {}).get("url", "")
                mime, data = GeminiProvider._parse_data_url(url)
                if data:
                    parts.append({"inlineData": {"mimeType": mime, "data": data}})
        return parts

    @staticmethod
    def _parse_data_url(url: str) -> tuple[str, str]:
        """从 data:image/...;base64,... 中解析出 (mimeType, base64Data)。"""
        if not url or not url.startswith("data:"):
            return "image/jpeg", ""
        try:
            header, _, data = url.partition(",")
            # header 形如 "data:image/png;base64"
            mime = "image/jpeg"
            if ";" in header and "/" in header:
                mime = header[5:].split(";", 1)[0]
            return mime, data
        except Exception:
            return "image/jpeg", ""

    @staticmethod
    def _convert_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """OpenAI tools 格式 → Gemini functionDeclarations 格式"""
        declarations = []
        for t in tools:
            if t.get("type") == "function":
                fn = t.get("function", {})
                declarations.append(
                    {
                        "name": fn.get("name", ""),
                        "description": fn.get("description", ""),
                        "parameters": fn.get(
                            "parameters", {"type": "object", "properties": {}}
                        ),
                    }
                )
        return declarations

    @staticmethod
    def _extract_response(data: Dict[str, Any]) -> tuple[str, List[Dict[str, Any]]]:
        """提取 generateContent 响应的文本与 tool_calls"""
        content_parts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        candidates = data.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            for part in parts:
                if "text" in part:
                    content_parts.append(part["text"])
                elif "functionCall" in part:
                    fc = part["functionCall"]
                    tool_calls.append(
                        {
                            "id": fc.get("name", ""),
                            "name": fc.get("name", ""),
                            "arguments": fc.get("args", {}),
                        }
                    )
        return "".join(content_parts), tool_calls
