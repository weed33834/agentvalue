"""
Ollama 本地模型 Provider 单元测试
验证 chat / stream / 工具转换 / health_check 逻辑,避免真实网络调用。
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from core.providers.base import ChatMessage, ProviderConfig
from core.providers.ollama_provider import OllamaProvider

MODULE = "core.providers.ollama_provider"


# ============================================================
# Helpers
# ============================================================


async def _aiter(lines):
    """把字符串列表变成异步生成器,模拟 httpx aiter_lines(NDJSON 每行一个 JSON)。"""
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
    cfg = dict(model_name="llama3", base_url="http://localhost:11434")
    cfg.update(overrides)
    return OllamaProvider(ProviderConfig(**cfg))


# ============================================================
# Tests
# ============================================================


def test_init():
    provider = _provider(base_url="http://custom:11434/")
    # base_url 末尾斜杠应被 strip
    assert provider._api_base == "http://custom:11434"


def test_name():
    assert _provider().name() == "ollama"


@pytest.mark.asyncio
async def test_chat_completion(monkeypatch):
    data = {
        "message": {"content": "Hi"},
        "model": "llama3",
        "prompt_eval_count": 10,
        "eval_count": 5,
    }
    mock_client = _patch_client(monkeypatch, post_response=_make_response(data))
    provider = _provider()
    result = await provider.chat_completion([ChatMessage(role="user", content="hi")])
    assert result.content == "Hi"
    assert result.model == "llama3"
    assert result.usage == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }
    # 请求 URL 与 payload
    args, kwargs = mock_client.post.call_args
    assert args[0].endswith("/api/chat")
    payload = kwargs["json"]
    assert payload["model"] == "llama3"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["stream"] is False


@pytest.mark.asyncio
async def test_stream_chat_completion(monkeypatch):
    """NDJSON 流:多行 content 拼接,done=true 携带 usage。"""
    lines = [
        '{"message":{"content":"Hello"},"done":false}',
        '{"message":{"content":" World"},"done":false}',
        '{"message":{"content":""},"done":true,"prompt_eval_count":10,"eval_count":5}',
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
    assert finish[0].finish_reason == "stop"
    assert finish[0].usage == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }


def test_convert_tools():
    """Ollama 兼容 OpenAI tools 结构,直接透传重建(type+function)。"""
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
    converted = OllamaProvider._convert_tools(tools)
    assert converted == [
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


@pytest.mark.asyncio
async def test_health_check_model_present(monkeypatch):
    """目标模型已 pull 时应返回 True。"""
    data = {
        "models": [
            {"name": "llama3:latest"},
            {"name": "mistral:latest"},
        ]
    }
    mock_client = _patch_client(
        monkeypatch, get_response=_make_response(data, status_code=200)
    )
    provider = _provider(model_name="llama3")
    assert await provider.health_check() is True
    args, _ = mock_client.get.call_args
    assert args[0].endswith("/api/tags")


@pytest.mark.asyncio
async def test_health_check_model_missing(monkeypatch):
    """目标模型未 pull 时应返回 False。"""
    data = {"models": [{"name": "mistral:latest"}]}
    _patch_client(monkeypatch, get_response=_make_response(data, status_code=200))
    provider = _provider(model_name="llama3")
    assert await provider.health_check() is False


@pytest.mark.asyncio
async def test_health_check_with_tag_suffix(monkeypatch):
    """model_name 带 :tag 后缀时应按主干名匹配。"""
    data = {"models": [{"name": "llama3:8b"}]}
    _patch_client(monkeypatch, get_response=_make_response(data, status_code=200))
    provider = _provider(model_name="llama3:8b")
    assert await provider.health_check() is True


@pytest.mark.asyncio
async def test_health_check_status_error(monkeypatch):
    """非 200 响应应返回 False。"""
    _patch_client(monkeypatch, get_response=_make_response({}, status_code=500))
    provider = _provider(model_name="llama3")
    assert await provider.health_check() is False


@pytest.mark.asyncio
async def test_health_check_connection_error(monkeypatch):
    """连接异常应返回 False。"""
    _patch_client(monkeypatch, get_side_effect=httpx.ConnectError("down"))
    provider = _provider(model_name="llama3")
    assert await provider.health_check() is False


@pytest.mark.asyncio
async def test_vision_completion(monkeypatch):
    """vision:请求体 messages[0] 应携带 images 字段(base64)。"""
    data = {"message": {"content": "a cat"}}
    mock_client = _patch_client(monkeypatch, post_response=_make_response(data))
    provider = _provider()
    result = await provider.vision_completion(
        prompt="describe", image_data="BASE64DATA", is_url=False
    )
    assert result == "a cat"
    _, kwargs = mock_client.post.call_args
    msg = kwargs["json"]["messages"][0]
    assert msg["role"] == "user"
    assert msg["content"] == "describe"
    assert msg["images"] == ["BASE64DATA"]
