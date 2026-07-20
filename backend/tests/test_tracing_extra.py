"""
core/tracing.py 补充单元测试
覆盖：NoOpTrace.span、LangfuseTracer 已启用路径（trace/span/generation）、
      is_enabled 各分支、init 失败降级、parent 为 None 等边界。
"""

import sys
import types
from types import SimpleNamespace

import pytest

from core.config import Settings
from core.tracing import LangfuseTracer, NoOpTrace


# ---------------- NoOpTrace ----------------


def test_noop_trace_span_returns_noop():
    """NoOpTrace.span() 应返回一个新的 NoOpTrace 实例"""
    parent = NoOpTrace()
    child = parent.span("some-span")
    assert isinstance(child, NoOpTrace)


def test_noop_trace_update_is_noop():
    """NoOpTrace.update 不应抛错"""
    t = NoOpTrace()
    t.update(output="x", metadata={"k": "v"})
    assert t.metadata == {}


def test_noop_trace_context_manager_yields_self():
    """NoOpTrace 作为上下文管理器应返回自身"""
    with NoOpTrace() as t:
        assert isinstance(t, NoOpTrace)


# ---------------- LangfuseTracer 已启用路径 ----------------


def _build_fake_langfuse_module(raise_on_init=False):
    """构造一个 fake langfuse 模块，Langfuse 类记录调用参数"""

    class FakeTrace:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.metadata = kwargs.get("metadata", {}) or {}
            self.spans = []
            self.generations = []
            self.name = kwargs.get("name")

        def span(self, name=None, input=None):
            s = FakeTrace(name=name)
            s.input = input
            self.spans.append(s)
            return s

        def generation(
            self,
            name=None,
            input=None,
            output=None,
            model=None,
            usage=None,
            metadata=None,
        ):
            g = SimpleNamespace(
                name=name,
                input=input,
                output=output,
                model=model,
                usage=usage,
                metadata=metadata,
            )
            self.generations.append(g)
            return g

        def update(self, *args, **kwargs):
            pass

    class FakeLangfuse:
        def __init__(self, public_key=None, secret_key=None, host=None):
            if raise_on_init:
                raise RuntimeError("langfuse init failed")
            self.public_key = public_key
            self.secret_key = secret_key
            self.host = host
            self.traces_created = []

        def trace(self, name=None, id=None, user_id=None, metadata=None):
            t = FakeTrace(name=name, id=id, user_id=user_id, metadata=metadata or {})
            self.traces_created.append(t)
            return t

    fake = types.ModuleType("langfuse")
    fake.Langfuse = FakeLangfuse  # type: ignore[attr-defined]
    return fake, FakeLangfuse


@pytest.fixture
def enabled_tracer(monkeypatch):
    """注入 fake langfuse 模块，构造一个已启用的 LangfuseTracer"""
    fake_module, _ = _build_fake_langfuse_module()
    monkeypatch.setitem(sys.modules, "langfuse", fake_module)
    settings = Settings(
        langfuse_public_key="pk-xxx",
        langfuse_secret_key="sk-xxx",
        langfuse_host="https://lf.example.com",
    )
    return LangfuseTracer(settings)


def test_enabled_tracer_is_enabled(enabled_tracer):
    """已配置三要素且 langfuse 可导入时应启用"""
    assert enabled_tracer.is_enabled() is True


def test_enabled_tracer_trace_yields_client_trace(enabled_tracer):
    """trace 上下文应调用 client.trace 并 yield 其返回值"""
    with enabled_tracer.trace(
        name="eval", evaluation_id="EVAL-1", employee_id="E1001", metadata={"k": "v"}
    ) as trace:
        # yield 的是 client.trace 返回的 FakeTrace
        assert trace.name == "eval"
        assert trace.kwargs["id"] == "EVAL-1"
        assert trace.kwargs["user_id"] == "E1001"
        assert trace.kwargs["metadata"] == {"k": "v"}
    # 验证 client 记录了一次 trace 调用
    assert len(enabled_tracer._client.traces_created) == 1


def test_enabled_tracer_trace_with_default_metadata(enabled_tracer):
    """metadata 为 None 时应传入空 dict"""
    with enabled_tracer.trace(name="t") as trace:
        assert trace.metadata == {}


def test_enabled_tracer_span_yields_parent_span(enabled_tracer):
    """span 上下文应调用 parent.span 并 yield 其返回值"""
    with enabled_tracer.trace(name="root") as parent:
        with enabled_tracer.span(parent, name="child", input_data={"x": 1}) as span:
            assert span.name == "child"
            assert span.input == {"x": 1}
        # parent 应记录一次 span 调用
        assert len(parent.spans) == 1


def test_enabled_tracer_generation_returns_parent_generation(enabled_tracer):
    """generation 应调用 parent.generation 并返回其结果"""
    with enabled_tracer.trace(name="root") as parent:
        gen = enabled_tracer.generation(
            parent,
            name="llm-call",
            prompt="hello",
            completion="world",
            model="gpt-4o-mini",
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            metadata={"temp": 0.1},
        )
        assert gen.name == "llm-call"
        assert gen.input == "hello"
        assert gen.output == "world"
        assert gen.model == "gpt-4o-mini"
        assert gen.usage["total_tokens"] == 2
        assert gen.metadata == {"temp": 0.1}
        assert len(parent.generations) == 1


def test_enabled_tracer_generation_with_default_metadata(enabled_tracer):
    """generation metadata 为 None 时应传入空 dict"""
    with enabled_tracer.trace(name="root") as parent:
        gen = enabled_tracer.generation(parent, name="g", prompt="p")
        assert gen.metadata == {}


# ---------------- is_enabled 各分支 ----------------


def test_is_enabled_false_when_missing_secret():
    """缺少 secret_key 时应禁用"""
    settings = Settings(langfuse_public_key="pk", langfuse_secret_key=None)
    assert LangfuseTracer(settings).is_enabled() is False


def test_is_enabled_false_when_missing_public():
    """缺少 public_key 时应禁用"""
    settings = Settings(langfuse_public_key=None, langfuse_secret_key="sk")
    assert LangfuseTracer(settings).is_enabled() is False


def test_is_enabled_false_when_missing_host(monkeypatch):
    """缺少 host 时应禁用"""
    settings = Settings(
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        langfuse_host="",  # 空字符串视为未配置
    )
    assert LangfuseTracer(settings).is_enabled() is False


def test_init_failure_disables_tracer(monkeypatch, caplog):
    """langfuse 初始化抛异常时应降级为禁用并记录 warning"""
    fake_module, _ = _build_fake_langfuse_module(raise_on_init=True)
    monkeypatch.setitem(sys.modules, "langfuse", fake_module)
    settings = Settings(
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        langfuse_host="https://lf.example.com",
    )
    with caplog.at_level("WARNING", logger="core.tracing"):
        tracer = LangfuseTracer(settings)
    assert tracer.is_enabled() is False
    assert tracer._client is None
    assert any("Langfuse 初始化失败" in r.message for r in caplog.records)


# ---------------- disabled / parent=None 边界 ----------------


def test_disabled_tracer_generation_returns_noop():
    """未启用时 generation 应返回 NoOpTrace"""
    settings = Settings(langfuse_public_key=None, langfuse_secret_key=None)
    tracer = LangfuseTracer(settings)
    gen = tracer.generation(object(), name="g", prompt="p")
    assert isinstance(gen, NoOpTrace)


def test_disabled_tracer_span_yields_noop():
    """未启用时 span 应 yield NoOpTrace（即使 parent 非 None）"""
    settings = Settings(langfuse_public_key=None, langfuse_secret_key=None)
    tracer = LangfuseTracer(settings)
    with tracer.span(object(), name="s") as span:
        assert isinstance(span, NoOpTrace)


def test_disabled_tracer_trace_yields_noop():
    """未启用时 trace 应 yield NoOpTrace"""
    settings = Settings(langfuse_public_key=None, langfuse_secret_key=None)
    tracer = LangfuseTracer(settings)
    with tracer.trace(name="t", evaluation_id="E1") as trace:
        assert isinstance(trace, NoOpTrace)


def test_enabled_tracer_span_with_none_parent_yields_noop(enabled_tracer):
    """已启用但 parent 为 None 时 span 应 yield NoOpTrace"""
    with enabled_tracer.span(None, name="s") as span:
        assert isinstance(span, NoOpTrace)


def test_enabled_tracer_generation_with_none_parent_returns_noop(enabled_tracer):
    """已启用但 parent 为 None 时 generation 应返回 NoOpTrace"""
    gen = enabled_tracer.generation(None, name="g", prompt="p")
    assert isinstance(gen, NoOpTrace)
