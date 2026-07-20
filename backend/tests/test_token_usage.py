"""
H6: LLM token usage 指标埋点测试

验证：
- record_token_usage 辅助函数正确累加 prompt / completion 两个方向的 Counter；
- Counter label 包含 tier / model / direction；
- 0 值跳过对应方向（避免产生无意义样本）；
- chat_completion / chat_completion_structured / vision_completion 在返回前正确
  提取 resp.usage 并调用 record_token_usage。
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from prometheus_client import REGISTRY

from core.metrics import (
    LLM_TOKEN_USAGE_TOTAL,
    record_token_usage,
)
from core.providers.base import ChatMessage, ProviderConfig
from core.providers.openai_provider import OpenAICompatibleProvider


def _counter_value(tier: str, model: str, direction: str) -> float:
    """读取 Counter 当前值（按 label 组合查询）。

    若 label 未注册（即从未 inc 过），返回 0.0 而非抛 KeyError，便于断言。

    P3-5 增强: Counter 加了 tenant_id label,测试无 tenant context 时
    _tenant_label() 回退 "default",这里查询也按 tenant_id="default" 过滤。
    """
    try:
        return LLM_TOKEN_USAGE_TOTAL.labels(
            tier=tier, model=model, direction=direction, tenant_id="default"
        )._value.get()
    except KeyError:
        return 0.0


def _counter_samples_count() -> int:
    """统计当前 Counter 已注册的 label 组合数量。"""
    return len(LLM_TOKEN_USAGE_TOTAL._metrics)


def _reset_token_counter():
    """测试前清空 Counter，避免相互污染。

    prometheus_client Counter 不支持重置公开 API，这里直接清空 _metrics
    字典以彻底移除已注册的 label 组合，使后续断言可基于「是否注册」判断。
    """
    LLM_TOKEN_USAGE_TOTAL._metrics.clear()


@pytest.fixture(autouse=True)
def _clean_counter():
    """每个测试前后清空 Counter，确保增量断言可靠。"""
    _reset_token_counter()
    yield
    _reset_token_counter()


# ---------------- record_token_usage 辅助函数 ----------------


def test_record_token_usage_increments_prompt_and_completion():
    """应同时累加 prompt 和 completion 两个方向。"""
    record_token_usage("L0", "gpt-4o-mini", prompt_tokens=10, completion_tokens=5)
    assert _counter_value("L0", "gpt-4o-mini", "prompt") == 10.0
    assert _counter_value("L0", "gpt-4o-mini", "completion") == 5.0


def test_record_token_usage_accumulates_across_calls():
    """多次调用应累加，而非覆盖。"""
    record_token_usage("L1", "gpt-4o", prompt_tokens=10, completion_tokens=4)
    record_token_usage("L1", "gpt-4o", prompt_tokens=20, completion_tokens=6)
    assert _counter_value("L1", "gpt-4o", "prompt") == 30.0
    assert _counter_value("L1", "gpt-4o", "completion") == 10.0


def test_record_token_usage_distinguishes_tier_and_model():
    """不同 tier / model 应分别累加。"""
    record_token_usage("L0", "gpt-4o-mini", prompt_tokens=1, completion_tokens=1)
    record_token_usage("L1", "gpt-4o", prompt_tokens=2, completion_tokens=2)
    assert _counter_value("L0", "gpt-4o-mini", "prompt") == 1.0
    assert _counter_value("L1", "gpt-4o", "prompt") == 2.0
    assert _counter_value("L0", "gpt-4o", "prompt") == 0.0


def test_record_token_usage_zero_skipped():
    """入参为 0 时跳过对应方向，不产生样本。"""
    samples_before = _counter_samples_count()
    record_token_usage("L0", "gpt-4o-mini", prompt_tokens=0, completion_tokens=5)
    # completion 被记
    assert _counter_value("L0", "gpt-4o-mini", "completion") == 5.0
    # prompt 未被记：label 数量仅 +1（只有 completion 这一组新增）
    assert _counter_samples_count() == samples_before + 1
    assert _counter_value("L0", "gpt-4o-mini", "prompt") == 0.0


def test_record_token_usage_both_zero_noop():
    """两个方向都为 0 时不产生任何样本。"""
    samples_before = _counter_samples_count()
    record_token_usage("L0", "gpt-4o-mini", prompt_tokens=0, completion_tokens=0)
    # 没有任何 label 被注册
    assert _counter_samples_count() == samples_before
    assert _counter_value("L0", "gpt-4o-mini", "prompt") == 0.0
    assert _counter_value("L0", "gpt-4o-mini", "completion") == 0.0


# ---------------- chat_completion 集成 ----------------


def _fake_response_with_usage(
    prompt_tokens=15, completion_tokens=8, model="gpt-4o-mini"
):
    resp = MagicMock()
    resp.model = model
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = "ok"
    resp.usage.prompt_tokens = prompt_tokens
    resp.usage.completion_tokens = completion_tokens
    resp.usage.total_tokens = prompt_tokens + completion_tokens
    return resp


@pytest.mark.asyncio
async def test_chat_completion_records_token_usage(monkeypatch):
    """chat_completion 成功后应把 resp.usage 记到 Counter。"""
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_fake_response_with_usage(
            prompt_tokens=15, completion_tokens=8, model="gpt-4o-mini"
        )
    )

    provider = OpenAICompatibleProvider(
        ProviderConfig(model_name="gpt-4o-mini", api_key="fake-key", model_tier="L0")
    )
    provider.client = mock_client

    await provider.chat_completion([ChatMessage(role="user", content="hi")])

    assert _counter_value("L0", "gpt-4o-mini", "prompt") == 15.0
    assert _counter_value("L0", "gpt-4o-mini", "completion") == 8.0


@pytest.mark.asyncio
async def test_chat_completion_no_usage_skips_counter(monkeypatch):
    """resp.usage 为 None 时不应记 Counter（不产生无意义样本）。"""
    resp = _fake_response_with_usage()
    resp.usage = None  # 模拟未返回 usage
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=resp)

    provider = OpenAICompatibleProvider(
        ProviderConfig(model_name="gpt-4o-mini", api_key="fake-key", model_tier="L0")
    )
    provider.client = mock_client

    samples_before = _counter_samples_count()
    result = await provider.chat_completion([ChatMessage(role="user", content="hi")])
    # usage 仍写 0，但 Counter 不应被记
    assert result.usage["prompt_tokens"] == 0
    # 没有任何 label 被注册
    assert _counter_samples_count() == samples_before
    assert _counter_value("L0", "gpt-4o-mini", "prompt") == 0.0
    assert _counter_value("L0", "gpt-4o-mini", "completion") == 0.0


# ---------------- vision_completion 集成 ----------------


@pytest.mark.asyncio
async def test_vision_completion_records_token_usage(monkeypatch):
    """vision_completion 成功后应把 resp.usage 记到 Counter。"""
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_fake_response_with_usage(
            prompt_tokens=33, completion_tokens=7, model="gpt-4o-mini"
        )
    )

    provider = OpenAICompatibleProvider(
        ProviderConfig(
            model_name="gpt-4o-mini",
            api_key="fake-key",
            vision_model="gpt-4o-mini",
            model_tier="L0",
        )
    )
    provider.client = mock_client

    await provider.vision_completion(prompt="describe", image_data="BASE64DATA")

    assert _counter_value("L0", "gpt-4o-mini", "prompt") == 33.0
    assert _counter_value("L0", "gpt-4o-mini", "completion") == 7.0


@pytest.mark.asyncio
async def test_vision_completion_no_usage_skips_counter(monkeypatch):
    """vision_completion 在 resp.usage 为 None 时不应记 Counter。"""
    resp = _fake_response_with_usage()
    resp.usage = None
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=resp)

    provider = OpenAICompatibleProvider(
        ProviderConfig(model_name="gpt-4o-mini", api_key="fake-key", model_tier="L0")
    )
    provider.client = mock_client

    await provider.vision_completion(prompt="p", image_data="BASE64DATA")
    # resp.usage 为 None 时不应记任何样本
    assert _counter_value("L0", "gpt-4o-mini", "prompt") == 0.0
    assert _counter_value("L0", "gpt-4o-mini", "completion") == 0.0


# ---------------- 失败路径不应记 token usage ----------------


@pytest.mark.asyncio
async def test_chat_failure_does_not_record_token_usage(monkeypatch):
    """重试耗尽抛 RuntimeError 后不应记 token usage（因为根本没有 resp.usage）。"""
    import httpx
    from openai import InternalServerError

    request = httpx.Request("POST", "http://localhost/v1/chat/completions")
    response = httpx.Response(500, request=request)
    err = InternalServerError("boom", response=response, body=None)
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=err)
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    provider = OpenAICompatibleProvider(
        ProviderConfig(model_name="gpt-4o-mini", api_key="fake-key", model_tier="L0")
    )
    provider.client = mock_client

    with pytest.raises(RuntimeError):
        await provider.chat_completion([ChatMessage(role="user", content="hi")])

    # 失败时不应有任何 token usage 样本（值仍为 0）
    assert _counter_value("L0", "gpt-4o-mini", "prompt") == 0.0
    assert _counter_value("L0", "gpt-4o-mini", "completion") == 0.0
