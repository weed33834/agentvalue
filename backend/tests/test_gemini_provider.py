"""
Google Gemini Provider 单元测试
验证 chat / stream / vision / 消息转换 / health_check 逻辑,避免真实网络调用。
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from core.providers.base import ChatMessage, ProviderConfig
from core.providers.gemini_provider import GeminiProvider

MODULE = "core.providers.gemini_provider"


# ============================================================
# Helpers
# ============================================================


async def _aiter(lines):
    """把字符串列表变成异步生成器,模拟 httpx aiter_lines。"""
    for line in lines:
        yield line


def _make_response(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


def _make_stream_ctx(lines):
    stream_resp = MagicMock()
    stream_resp.raise_for_status = MagicMock()
    stream_resp.aiter_lines = MagicMock(return_value=_aiter(lines))
    stream_ctx = MagicMock()
    stream_ctx.__aenter__ = AsyncMock(return_value=stream_resp)
    stream_ctx.__aexit__ = AsyncMock(return_value=None)
    return stream_ctx


def _patch_client(
    monkeypatch,
    *,
    post_response=None,
    post_side_effect=None,
    stream_lines=None,
    get_response=None,
    get_side_effect=None,
):
    """Patch httpx.AsyncClient,返回 mock client 供断言调用参数。"""
    mock_client = MagicMock()
    if post_response is not None:
        mock_client.post = AsyncMock(return_value=post_response)
    elif post_side_effect is not None:
        mock_client.post = AsyncMock(side_effect=post_side_effect)
    if get_response is not None:
        mock_client.get = AsyncMock(return_value=get_response)
    elif get_side_effect is not None:
        mock_client.get = AsyncMock(side_effect=get_side_effect)
    if stream_lines is not None:
        mock_client.stream = MagicMock(return_value=_make_stream_ctx(stream_lines))
    ctx_manager = MagicMock()
    ctx_manager.__aenter__ = AsyncMock(return_value=mock_client)
    ctx_manager.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr("httpx.AsyncClient", MagicMock(return_value=ctx_manager))
    return mock_client


@pytest.fixture(autouse=True)
def _stub_metrics(monkeypatch):
    """屏蔽 Prometheus 埋点(避免签名/副作用干扰,聚焦 provider 逻辑)。"""
    monkeypatch.setattr(f"{MODULE}.record_llm_request", lambda *a, **kw: None)
    monkeypatch.setattr(f"{MODULE}.record_token_usage", lambda *a, **kw: None)
    monkeypatch.setattr("core.metrics.record_llm_vision_call", lambda *a, **kw: None)


def _provider(**overrides):
    cfg = dict(
        model_name="gemini-1.5-pro",
        api_key="gem-key",
        base_url="https://generativelanguage.googleapis.com",
    )
    cfg.update(overrides)
    return GeminiProvider(ProviderConfig(**cfg))


# ============================================================
# Tests
# ============================================================


def test_init():
    provider = _provider(api_key="gem-key", base_url="https://custom.example.com/")
    assert provider._api_key == "gem-key"
    # base_url 末尾斜杠应被 strip
    assert provider._api_base == "https://custom.example.com"


def test_name():
    assert _provider().name() == "gemini"


@pytest.mark.asyncio
async def test_chat_completion(monkeypatch):
    data = {
        "candidates": [
            {"content": {"parts": [{"text": "Hi"}]}, "finishReason": "STOP"}
        ],
        "usageMetadata": {
            "promptTokenCount": 5,
            "candidatesTokenCount": 2,
            "totalTokenCount": 7,
        },
    }
    mock_client = _patch_client(monkeypatch, post_response=_make_response(data))
    provider = _provider()
    result = await provider.chat_completion([ChatMessage(role="user", content="hi")])
    assert result.content == "Hi"
    assert result.model == "gemini-1.5-pro"
    assert result.usage == {
        "prompt_tokens": 5,
        "completion_tokens": 2,
        "total_tokens": 7,
    }
    # URL 应携带 model 与 key
    args, kwargs = mock_client.post.call_args
    assert "gemini-1.5-pro:generateContent" in args[0]
    assert "key=gem-key" in args[0]
    assert kwargs["json"]["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]


@pytest.mark.asyncio
async def test_chat_completion_system_instruction(monkeypatch):
    """system 角色应拆出作为 systemInstruction(而非放进 contents)。"""
    data = {
        "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
        "usageMetadata": {},
    }
    mock_client = _patch_client(monkeypatch, post_response=_make_response(data))
    provider = _provider()
    await provider.chat_completion(
        [
            ChatMessage(role="system", content="sys"),
            ChatMessage(role="user", content="hi"),
        ]
    )
    _, kwargs = mock_client.post.call_args
    payload = kwargs["json"]
    assert payload["systemInstruction"] == {"parts": [{"text": "sys"}]}
    # contents 不含 system 角色
    assert all(c["role"] != "system" for c in payload["contents"])


@pytest.mark.asyncio
async def test_stream_chat_completion(monkeypatch):
    """SSE 流:多 chunk 拼接 content,finishReason=STOP → finish_reason=stop。"""
    lines = [
        'data: {"candidates":[{"content":{"parts":[{"text":"Hello"}]}}]}',
        'data: {"candidates":[{"content":{"parts":[{"text":" World"}]},'
        '"finishReason":"STOP"}],'
        '"usageMetadata":{"promptTokenCount":5,"candidatesTokenCount":2,'
        '"totalTokenCount":7}}',
    ]
    _patch_client(monkeypatch, stream_lines=lines)
    provider = _provider()
    chunks = []
    async for chunk in provider.stream_chat_completion(
        [ChatMessage(role="user", content="hi")]
    ):
        chunks.append(chunk)
    contents = "".join(c.content for c in chunks if c.content)
    assert contents == "Hello World"
    finish = [c for c in chunks if c.finish_reason]
    assert len(finish) == 1
    # STOP → stop (OpenAI 风格映射)
    assert finish[0].finish_reason == "stop"
    assert finish[0].usage == {
        "prompt_tokens": 5,
        "completion_tokens": 2,
        "total_tokens": 7,
    }


def test_convert_messages():
    """system → systemInstruction;assistant → model;user 保留。"""
    msgs = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="u"),
        ChatMessage(role="assistant", content="a"),
    ]
    system, contents = GeminiProvider._convert_messages(msgs)
    assert system == "sys"
    assert contents == [
        {"role": "user", "parts": [{"text": "u"}]},
        {"role": "model", "parts": [{"text": "a"}]},
    ]


def test_convert_messages_no_system():
    msgs = [ChatMessage(role="user", content="u")]
    system, contents = GeminiProvider._convert_messages(msgs)
    assert system is None
    assert contents == [{"role": "user", "parts": [{"text": "u"}]}]


def test_convert_tools():
    """OpenAI tools 格式 → Gemini functionDeclarations 格式。"""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "d",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                },
            },
        }
    ]
    decls = GeminiProvider._convert_tools(tools)
    assert decls == [
        {
            "name": "get_weather",
            "description": "d",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
            },
        }
    ]


@pytest.mark.asyncio
async def test_health_check_success(monkeypatch):
    mock_client = _patch_client(
        monkeypatch, get_response=_make_response({"models": []}, status_code=200)
    )
    provider = _provider()
    assert await provider.health_check() is True
    # URL 应命中 /v1beta/models 且携带 key
    args, _ = mock_client.get.call_args
    assert "/v1beta/models" in args[0]
    assert "key=gem-key" in args[0]


@pytest.mark.asyncio
async def test_health_check_failure(monkeypatch):
    """请求异常应被捕获并返回 False。"""
    _patch_client(monkeypatch, get_side_effect=httpx.ConnectError("down"))
    provider = _provider()
    assert await provider.health_check() is False


@pytest.mark.asyncio
async def test_health_check_non_200(monkeypatch):
    """非 200 响应应视为不可用。"""
    _patch_client(monkeypatch, get_response=_make_response({}, status_code=500))
    provider = _provider()
    assert await provider.health_check() is False


@pytest.mark.asyncio
async def test_vision_completion(monkeypatch):
    """vision:请求体 parts 应包含 inlineData(base64)。"""
    data = {"candidates": [{"content": {"parts": [{"text": "vision result"}]}}]}
    mock_client = _patch_client(monkeypatch, post_response=_make_response(data))
    provider = _provider()
    result = await provider.vision_completion(
        prompt="describe", image_data="BASE64DATA", is_url=False
    )
    assert result == "vision result"
    _, kwargs = mock_client.post.call_args
    parts = kwargs["json"]["contents"][0]["parts"]
    assert {"text": "describe"} in parts
    assert {"inlineData": {"mimeType": "image/jpeg", "data": "BASE64DATA"}} in parts


@pytest.mark.asyncio
async def test_vision_completion_url_not_supported():
    """Gemini vision 仅支持 base64,is_url=True 应报 NotImplementedError。"""
    provider = _provider()
    with pytest.raises(NotImplementedError):
        await provider.vision_completion(
            prompt="p", image_data="https://x/y.png", is_url=True
        )


@pytest.mark.asyncio
async def test_function_calling(monkeypatch):
    """function_calling:响应含 functionCall,应解析出 tool_calls。"""
    data = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "functionCall": {
                                "name": "get_weather",
                                "args": {"city": "SF"},
                            }
                        }
                    ]
                }
            }
        ]
    }
    mock_client = _patch_client(monkeypatch, post_response=_make_response(data))
    provider = _provider()
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "d",
                "parameters": {"type": "object"},
            },
        }
    ]
    result = await provider.function_calling(
        [ChatMessage(role="user", content="weather?")], tools
    )
    assert result.tool_calls == [
        {"id": "get_weather", "name": "get_weather", "arguments": {"city": "SF"}}
    ]
    # payload tools 应包裹为 functionDeclarations(含 name/description/parameters)
    _, kwargs = mock_client.post.call_args
    decls = kwargs["json"]["tools"][0]["functionDeclarations"]
    assert decls[0]["name"] == "get_weather"
    assert decls[0]["parameters"] == {"type": "object"}
