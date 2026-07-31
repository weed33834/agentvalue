"""
Anthropic Claude Provider 单元测试
验证 chat / stream / vision / function_calling / health_check 逻辑,避免真实网络调用。
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from core.providers.anthropic_provider import AnthropicProvider
from core.providers.base import ChatMessage, ProviderConfig

MODULE = "core.providers.anthropic_provider"


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
):
    """Patch httpx.AsyncClient,返回 mock client 供断言调用参数。"""
    mock_client = MagicMock()
    if post_response is not None:
        mock_client.post = AsyncMock(return_value=post_response)
    elif post_side_effect is not None:
        mock_client.post = AsyncMock(side_effect=post_side_effect)
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
        model_name="claude-3-5-sonnet",
        api_key="sk-test",
        base_url="https://api.anthropic.com",
    )
    cfg.update(overrides)
    return AnthropicProvider(ProviderConfig(**cfg))


# ============================================================
# Tests
# ============================================================


def test_init_and_headers():
    provider = _provider(api_key="sk-test", base_url="https://custom.example.com/")
    assert provider._api_key == "sk-test"
    # base_url 末尾斜杠应被 strip
    assert provider._api_base == "https://custom.example.com"
    headers = provider._headers
    assert headers["x-api-key"] == "sk-test"
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["content-type"] == "application/json"


def test_name():
    assert _provider().name() == "anthropic"


@pytest.mark.asyncio
async def test_chat_completion(monkeypatch):
    data = {
        "content": [{"type": "text", "text": "Hello"}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "stop_reason": "end_turn",
        "model": "claude-3-5-sonnet",
    }
    mock_client = _patch_client(monkeypatch, post_response=_make_response(data))
    provider = _provider()
    result = await provider.chat_completion([ChatMessage(role="user", content="hi")])
    assert result.content == "Hello"
    assert result.model == "claude-3-5-sonnet"
    assert result.usage == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }
    # 请求 URL 与 payload
    args, kwargs = mock_client.post.call_args
    assert args[0].endswith("/v1/messages")
    assert kwargs["json"]["model"] == "claude-3-5-sonnet"
    assert kwargs["json"]["messages"] == [{"role": "user", "content": "hi"}]
    # system 字段在有 system 消息时才出现
    assert "system" not in kwargs["json"]


@pytest.mark.asyncio
async def test_chat_completion_system_prompt_split(monkeypatch):
    """system 角色应拆出作为独立 system 字段(Anthropic API 要求)。"""
    data = {
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    mock_client = _patch_client(monkeypatch, post_response=_make_response(data))
    provider = _provider()
    await provider.chat_completion(
        [
            ChatMessage(role="system", content="You are helpful"),
            ChatMessage(role="user", content="hi"),
        ]
    )
    _, kwargs = mock_client.post.call_args
    payload = kwargs["json"]
    assert payload["system"] == "You are helpful"
    # user messages 不再包含 system
    assert all(m["role"] != "system" for m in payload["messages"])


@pytest.mark.asyncio
async def test_stream_chat_completion(monkeypatch):
    """SSE 流:多事件拼接 content,末尾 message_delta 携带 stop_reason 与 usage。"""
    lines = [
        'data: {"type":"message_start","message":{"id":"msg_1"}}',
        "",
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hello"}}',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":" World"}}',
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"input_tokens":10,"output_tokens":5}}',
        'data: {"type":"message_stop"}',
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
    assert finish[0].finish_reason == "end_turn"
    assert finish[0].usage == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }


@pytest.mark.asyncio
async def test_vision_completion(monkeypatch):
    """vision:请求体 content 数组应包含 image source(base64)。"""
    data = {"content": [{"type": "text", "text": "a cat"}]}
    mock_client = _patch_client(monkeypatch, post_response=_make_response(data))
    provider = _provider()
    result = await provider.vision_completion(
        prompt="describe", image_data="BASE64DATA", is_url=False
    )
    assert result == "a cat"
    _, kwargs = mock_client.post.call_args
    content = kwargs["json"]["messages"][0]["content"]
    image_part = content[0]
    assert image_part["type"] == "image"
    assert image_part["source"]["type"] == "base64"
    assert image_part["source"]["media_type"] == "image/jpeg"
    assert image_part["source"]["data"] == "BASE64DATA"
    text_part = content[1]
    assert text_part["type"] == "text"
    assert text_part["text"] == "describe"


@pytest.mark.asyncio
async def test_function_calling(monkeypatch):
    """function_calling:响应含 tool_use block,应解析出 tool_calls。"""
    data = {
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_01",
                "name": "get_weather",
                "input": {"city": "SF"},
            },
            {"type": "text", "text": "Calling tool"},
        ],
        "model": "claude-3-5-sonnet",
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
    assert result.content == "Calling tool"
    assert result.tool_calls == [
        {"id": "toolu_01", "name": "get_weather", "arguments": {"city": "SF"}}
    ]
    # payload tools 应转为 Anthropic 格式(name + input_schema)
    _, kwargs = mock_client.post.call_args
    converted = kwargs["json"]["tools"][0]
    assert converted["name"] == "get_weather"
    assert converted["input_schema"] == {"type": "object"}


@pytest.mark.asyncio
async def test_health_check_success(monkeypatch):
    mock_client = _patch_client(
        monkeypatch, post_response=_make_response({}, status_code=200)
    )
    provider = _provider()
    assert await provider.health_check() is True
    # health_check 走 /v1/messages
    args, _ = mock_client.post.call_args
    assert args[0].endswith("/v1/messages")


@pytest.mark.asyncio
async def test_health_check_failure(monkeypatch):
    """请求异常应被捕获并返回 False。"""
    _patch_client(monkeypatch, post_side_effect=httpx.ConnectError("conn lost"))
    provider = _provider()
    assert await provider.health_check() is False


@pytest.mark.asyncio
async def test_health_check_unauthorized(monkeypatch):
    """401 应视为不可用。"""
    _patch_client(monkeypatch, post_response=_make_response({}, status_code=401))
    provider = _provider()
    assert await provider.health_check() is False


def test_split_system():
    msgs = [
        ChatMessage(role="system", content="sys1"),
        ChatMessage(role="user", content="u"),
        ChatMessage(role="assistant", content="a"),
    ]
    system, user = AnthropicProvider._split_system(msgs)
    assert system == "sys1"
    assert user == [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"},
    ]


def test_split_system_multiple_concat():
    """多个 system 消息应拼成一段(换行分隔)。"""
    msgs = [
        ChatMessage(role="system", content="line1"),
        ChatMessage(role="system", content="line2"),
        ChatMessage(role="user", content="u"),
    ]
    system, user = AnthropicProvider._split_system(msgs)
    assert system == "line1\nline2"
    assert user == [{"role": "user", "content": "u"}]


def test_split_system_no_system():
    msgs = [ChatMessage(role="user", content="u")]
    system, user = AnthropicProvider._split_system(msgs)
    assert system is None
    assert user == [{"role": "user", "content": "u"}]


def test_convert_tools():
    """OpenAI tools 格式 → Anthropic tools 格式(name/description/input_schema)。"""
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
    converted = AnthropicProvider._convert_tools(tools)
    assert converted == [
        {
            "name": "get_weather",
            "description": "d",
            "input_schema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
            },
        }
    ]
