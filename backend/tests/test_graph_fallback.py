"""
P3-3: _call_llm_with_fallback 降级重试测试

注意:Agent 1 会把 agent.graph._call_llm_with_fallback 提取到 core/llm_call.py。
本测试优先 import core.llm_call.call_llm_with_fallback;若该模块尚未提取,
回退到 agent.graph._call_llm_with_fallback,保证测试在两种状态下都能跑。

覆盖:
- 主档调用失败 → runtime_reselect 降级 → fallback 档成功,返回 (completion, new_tier)
- 主档失败 + fallback 档也失败 → 聚合异常(RuntimeError)
- runtime_reselect 返回 None(无可降级档位)→ 抛出首次异常
"""

import json

import pytest

from core.providers.base import (
    BaseProvider,
    ChatCompletion,
    ChatMessage,
    ProviderConfig,
)


# 优先用 core.llm_call(若 Agent 1 已提取);否则回退到 agent.graph 原位置
try:
    from core.llm_call import call_llm_with_fallback as _call_llm_with_fallback
except ImportError:  # pragma: no cover - 兼容 Agent 1 未提取的场景
    from agent.graph import _call_llm_with_fallback


# ====== 可控行为的 Provider ======


class _StubProvider(BaseProvider):
    """可控 Provider:按 raise_on / return_content 行为执行 chat_completion"""

    def __init__(
        self, *, model_name="stub", raise_on=False, return_content='{"ok": true}'
    ):
        super().__init__(ProviderConfig(model_name=model_name))
        self._raise_on = raise_on
        self._return_content = return_content
        self.call_count = 0

    def name(self) -> str:
        return "stub"

    async def chat_completion(self, messages, response_format=None):
        self.call_count += 1
        if self._raise_on:
            raise RuntimeError(f"provider {self.config.model_name} 不可用")
        return ChatCompletion(
            content=self._return_content,
            model=self.config.model_name,
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )

    async def health_check(self) -> bool:
        return True


# ====== 可控行为的 ModelRouter ======


class _FakeRouter:
    """可控行为的 ModelRouter stub。

    main_provider: get_provider_with_fallback 返回的(主档 provider, tier)
    fallback_provider: get_provider(new_tier) 返回的重试 provider
    reselect_result: runtime_reselect 返回的 tier(None 表示无可降级)
    """

    def __init__(
        self,
        main_provider,
        main_tier,
        fallback_provider=None,
        reselect_result="L1",
        health_score=10.0,
    ):
        self._main_provider = main_provider
        self._main_tier = main_tier
        self._fallback_provider = fallback_provider
        self._reselect_result = reselect_result
        self._health_score = health_score
        # 记录调用,便于断言
        self.calls = {
            "get_provider_with_fallback": 0,
            "get_health_score": [],
            "runtime_reselect": [],
            "get_provider": [],
        }

    async def get_provider_with_fallback(self):
        self.calls["get_provider_with_fallback"] += 1
        return self._main_provider, self._main_tier

    def get_health_score(self, tier):
        self.calls["get_health_score"].append(tier)
        return self._health_score

    def runtime_reselect(self, current_tier, current_health_score):
        self.calls["runtime_reselect"].append((current_tier, current_health_score))
        return self._reselect_result

    def get_provider(self, tier):
        self.calls["get_provider"].append(tier)
        if self._fallback_provider is None:
            # 默认回退到一个成功的 provider
            return _StubProvider(model_name=f"fallback-{tier}")
        return self._fallback_provider


# ====== 测试用例 ======


@pytest.mark.asyncio
async def test_main_fails_fallback_succeeds_returns_fallback_tier():
    """主档失败 → runtime_reselect → fallback 档成功,返回 (completion, new_tier)"""
    main = _StubProvider(model_name="L2-main", raise_on=True)
    fallback = _StubProvider(
        model_name="L1-fallback", raise_on=False, return_content='{"score": 80}'
    )
    router = _FakeRouter(
        main_provider=main,
        main_tier="L2",
        fallback_provider=fallback,
        reselect_result="L1",
        health_score=10.0,
    )

    completion, tier = await _call_llm_with_fallback(
        router, prompt="prompt", employee_id="E1", period="2026-W25"
    )

    assert tier == "L1"
    assert json.loads(completion.content)["score"] == 80
    assert completion.model == "L1-fallback"
    # 主档调用过一次
    assert main.call_count == 1
    # fallback 档调用过一次
    assert fallback.call_count == 1
    # runtime_reselect 被调,入参为 (主档 tier, health_score)
    assert router.calls["runtime_reselect"] == [("L2", 10.0)]
    # get_provider(L1) 被调取 fallback provider
    assert router.calls["get_provider"] == ["L1"]


@pytest.mark.asyncio
async def test_both_main_and_fallback_fail_raises_aggregate_exception():
    """主档失败 + fallback 档也失败 → 抛出聚合 RuntimeError"""
    main = _StubProvider(model_name="L2-main", raise_on=True)
    fallback = _StubProvider(model_name="L1-fallback", raise_on=True)
    router = _FakeRouter(
        main_provider=main,
        main_tier="L2",
        fallback_provider=fallback,
        reselect_result="L1",
        health_score=10.0,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await _call_llm_with_fallback(
            router, prompt="prompt", employee_id="E1", period="2026-W25"
        )

    msg = str(exc_info.value)
    # 聚合异常应同时包含"降级重试失败"语义与两次失败信息
    assert (
        "降级重试失败" in msg or "fallback" in msg.lower()
    ), f"异常消息不含降级语义: {msg}"
    # 主档与 fallback 都被调用过
    assert main.call_count == 1
    assert fallback.call_count == 1


@pytest.mark.asyncio
async def test_no_fallback_available_reselect_returns_none_raises_first_error():
    """runtime_reselect 返回 None(无可降级档位)→ 直接抛首次异常,不重试"""
    main = _StubProvider(model_name="L0-main", raise_on=True)
    router = _FakeRouter(
        main_provider=main,
        main_tier="L0",
        fallback_provider=None,
        reselect_result=None,  # 无可降级
        health_score=5.0,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await _call_llm_with_fallback(
            router, prompt="prompt", employee_id="E1", period="2026-W25"
        )

    # 应抛出首次异常(provider 不可用),不进入重试
    assert "L0-main" in str(exc_info.value) or "不可用" in str(exc_info.value)
    # 主档调用过一次
    assert main.call_count == 1
    # runtime_reselect 被调用,但因返回 None 未触发 get_provider 重试
    assert router.calls["runtime_reselect"] == [("L0", 5.0)]
    assert router.calls["get_provider"] == []


@pytest.mark.asyncio
async def test_main_succeeds_no_fallback_triggered():
    """主档成功时不触发降级重试"""
    main = _StubProvider(
        model_name="L2-main", raise_on=False, return_content='{"ok": 1}'
    )
    router = _FakeRouter(
        main_provider=main,
        main_tier="L2",
        fallback_provider=None,
        reselect_result="L1",
        health_score=90.0,
    )

    completion, tier = await _call_llm_with_fallback(
        router, prompt="prompt", employee_id="E1", period="2026-W25"
    )

    assert tier == "L2"
    assert json.loads(completion.content)["ok"] == 1
    # 主档成功,runtime_reselect 不应被调用
    assert router.calls["runtime_reselect"] == []
    assert router.calls["get_provider"] == []
    assert main.call_count == 1
