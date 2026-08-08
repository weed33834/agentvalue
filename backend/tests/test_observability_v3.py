"""
WS-1 原生可观测性测试

覆盖:
- core/pricing.py:  模型名归一化、定价查表、成本计算、未知模型兜底标记
- core/observe.py:  span 嵌套、trace 汇总回填、无 trace 时的 no-op、异常记录、
                    批量写入器 + flush_now、成本账本落库
- api/admin/trace_v2_routes.py: 瀑布图组装（嵌套 + relative_start_ms + 孤儿提升）
"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import core.database as core_database
from core.database import Base
from core.observe import (
    flush_now,
    record_llm_usage,
    span,
    start_trace_writer,
    stop_trace_writer,
    trace_context,
)
from core.pricing import (
    DEFAULT_PRICE,
    DEFAULT_PRICE_KEY,
    MODEL_PRICING,
    ModelPrice,
    calculate_cost,
    normalize_model_name,
)
from models.conversation_analytics import ConversationMetrics
from models.quota_models import BillingRecord
from models.trace_models import SpanRecord, TraceRecord


# ---------------- 公共夹具 ----------------


@pytest.fixture
async def trace_db(monkeypatch):
    """每个测试使用独立临时 SQLite 异步数据库，并把 observe 的写入指向它。

    core.observe._write_batch / record_llm_usage 都是在调用时才
    `from core.database import AsyncSessionLocal`，因此 monkeypatch 模块属性即可生效。
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    monkeypatch.setattr(core_database, "AsyncSessionLocal", SessionLocal)

    yield SessionLocal

    await engine.dispose()
    Path(tmp.name).unlink(missing_ok=True)


def _make_span(
    span_id, *, parent=None, trace_id="T1", name=None, offset_ms=0, duration_ms=10
):
    """构造未入库的 SpanRecord，用于瀑布图组装的纯函数测试"""
    started = datetime(2026, 8, 8, 10, 0, 0, tzinfo=timezone.utc) + timedelta(
        milliseconds=offset_ms
    )
    return SpanRecord(
        id=abs(hash(span_id)) % 100000,
        span_id=span_id,
        trace_id=trace_id,
        parent_span_id=parent,
        tenant_id="default",
        name=name or span_id,
        kind="llm",
        status="success",
        started_at=started,
        ended_at=started + timedelta(milliseconds=duration_ms),
        duration_ms=float(duration_ms),
    )


# ============================================================
# 1. 定价表 / 模型名归一化
# ============================================================


class TestPricing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("gpt-4o", "gpt-4o"),
            ("GPT-4o", "gpt-4o"),
            ("openai/gpt-4o", "gpt-4o"),
            ("openai/gpt-4o-2024-08-06", "gpt-4o"),
            ("anthropic/claude-3-5-sonnet-20241022", "claude-3-5-sonnet"),
            ("openrouter/anthropic/claude-3-5-haiku", "claude-3-5-haiku"),
            ("DeepSeek-V4-Flash", "deepseek-v4-flash"),
            ("Qwen3.5-397B-A17B", "qwen3.5-397b-a17b"),
            ("glm-5.2:prod", "glm-5.2"),
            ("gpt-4.1-mini-latest", "gpt-4.1-mini"),
            ("  gemini-2.5-pro  ", "gemini-2.5-pro"),
            (None, ""),
            ("", ""),
        ],
    )
    def test_normalize_model_name(self, raw, expected):
        assert normalize_model_name(raw) == expected

    @pytest.mark.parametrize(
        "model",
        [
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4.1",
            "gpt-4.1-mini",
            "o3-mini",
            "claude-sonnet-4",
            "claude-3-5-sonnet",
            "claude-3-5-haiku",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "deepseek-chat",
            "deepseek-reasoner",
            "DeepSeek-V4-Flash",
            "DeepSeek-V4-Pro",
            "glm-5.1",
            "glm-5.2",
            "Kimi-K2.6",
            "MiniMax-M3",
            "Qwen3.5-397B-A17B",
            "Qwen3.6-35B-A3B",
            "step-3.5-flash",
            "step-3.7-flash",
            "text-embedding-3-small",
            "text-embedding-3-large",
        ],
    )
    def test_required_models_are_priced(self, model):
        """spec 要求的模型必须全部命中内置表，不得走兜底"""
        breakdown = calculate_cost(model, 1000, 1000)
        assert breakdown.is_fallback is False, f"{model} 未命中定价表"
        assert breakdown.matched_model != DEFAULT_PRICE_KEY

    def test_cost_math_per_1m_tokens(self):
        """价格单位是 $/1M tokens：正好 1M 输入 + 1M 输出 = 单价之和"""
        price = MODEL_PRICING["gpt-4o"]
        b = calculate_cost("gpt-4o", 1_000_000, 1_000_000)
        assert b.input_cost == pytest.approx(price.input_per_1m)
        assert b.output_cost == pytest.approx(price.output_per_1m)
        assert b.total_cost == pytest.approx(price.input_per_1m + price.output_per_1m)
        assert b.currency == "USD"

    def test_cost_math_partial_tokens(self):
        b = calculate_cost("gpt-4o-mini", 1500, 800)
        assert b.input_cost == pytest.approx(1500 / 1_000_000 * 0.15)
        assert b.output_cost == pytest.approx(800 / 1_000_000 * 0.60)
        assert b.total_cost == pytest.approx(b.input_cost + b.output_cost)

    def test_unknown_model_flags_fallback_and_never_bills_zero(self):
        """未知模型必须标记 is_fallback 且成本 > 0（绝不静默计 0）"""
        b = calculate_cost("some-brand-new-model-2099", 10_000, 5_000)
        assert b.is_fallback is True
        assert b.matched_model == DEFAULT_PRICE_KEY
        assert b.total_cost > 0
        assert b.total_cost == pytest.approx(
            10_000 / 1_000_000 * DEFAULT_PRICE.input_per_1m
            + 5_000 / 1_000_000 * DEFAULT_PRICE.output_per_1m
        )

    def test_unknown_model_logs_warning(self, caplog):
        with caplog.at_level("WARNING", logger="core.pricing"):
            calculate_cost("mystery-model-xyz", 100, 100)
        assert any("未命中定价表" in r.getMessage() for r in caplog.records)

    def test_overrides_win_over_builtin_table(self):
        """DB / 管理页覆盖优先级高于内置兜底表"""
        overrides = {"gpt-4o": ModelPrice(99.0, 199.0)}
        b = calculate_cost("gpt-4o", 1_000_000, 1_000_000, overrides=overrides)
        assert b.is_fallback is False
        assert b.total_cost == pytest.approx(298.0)

    def test_overrides_rescue_unknown_model(self):
        overrides = {"brand-new-llm": ModelPrice(1.0, 2.0)}
        b = calculate_cost("brand-new-llm", 1_000_000, 0, overrides=overrides)
        assert b.is_fallback is False
        assert b.total_cost == pytest.approx(1.0)

    def test_longest_prefix_match_not_greedy(self):
        """gpt-4o-mini-high 应命中 gpt-4o-mini 而非更短的 gpt-4o"""
        b = calculate_cost("gpt-4o-mini-high", 1_000_000, 0)
        assert b.matched_model == "gpt-4o-mini"
        assert b.input_cost == pytest.approx(0.15)

    def test_negative_tokens_treated_as_zero(self):
        b = calculate_cost("gpt-4o", -100, -50)
        assert b.total_cost == 0.0
        assert b.is_fallback is False


# ============================================================
# 2. span 嵌套 + trace 汇总
# ============================================================


class TestSpanNestingAndRollup:
    async def test_nesting_and_rollup_totals(self, trace_db):
        async with trace_context(
            "eval_flow", kind="evaluation", user_id="U1", session_id="S1"
        ) as t:
            async with span("outer", kind="chain") as outer:
                async with span("llm-1", kind="llm", model="gpt-4o") as s1:
                    s1.set_usage(1000, 500)
                    s1.set_output("答案1")
                async with span("llm-2", kind="llm", model="gpt-4o-mini") as s2:
                    s2.set_usage(200, 100)

            # 父子关系：两个 llm span 都挂在 outer 之下
            assert s1.parent_span_id == outer.span_id
            assert s2.parent_span_id == outer.span_id
            assert outer.parent_span_id is None

        # trace 汇总回填：3 个 span，token 与成本按 span 累加
        assert t.total_spans == 3
        assert t.total_prompt_tokens == 1200
        assert t.total_completion_tokens == 600
        assert t.total_cost == pytest.approx(s1.cost + s2.cost)
        assert t.total_cost > 0
        assert t.status == "success"
        assert t.duration_ms is not None and t.duration_ms >= 0

    async def test_records_persisted_to_db(self, trace_db):
        async with trace_context("persist_me", kind="chat") as t:
            async with span("llm", kind="llm", model="claude-3-5-sonnet") as s:
                s.set_usage(300, 150)

        async with trace_db() as session:
            trace = (
                await session.execute(
                    select(TraceRecord).where(TraceRecord.trace_id == t.trace_id)
                )
            ).scalar_one()
            spans = (
                (
                    await session.execute(
                        select(SpanRecord).where(SpanRecord.trace_id == t.trace_id)
                    )
                )
                .scalars()
                .all()
            )

        assert trace.status == "success"
        assert trace.total_spans == 1
        assert trace.total_prompt_tokens == 300
        assert trace.total_completion_tokens == 150
        assert trace.total_cost == pytest.approx(s.cost)
        assert len(spans) == 1
        assert spans[0].model == "claude-3-5-sonnet"
        assert spans[0].total_tokens == 450
        assert spans[0].cost > 0

    async def test_span_without_active_trace_is_noop(self, trace_db):
        """没有活跃 trace 时 span 不落库也不报错，成本仍可算出供日志用"""
        async with span("orphan", kind="llm", model="gpt-4o") as s:
            s.set_usage(100, 50)
            s.set_output("x")
            s.set_attribute("k", "v")

        assert s.enabled is False
        assert s.trace_id is None
        assert s.cost > 0

        async with trace_db() as session:
            rows = (await session.execute(select(SpanRecord))).scalars().all()
        assert rows == []

    async def test_exception_recorded_and_reraised(self, trace_db):
        """业务异常必须原样抛出，同时 span / trace 标记为 error"""
        with pytest.raises(ValueError, match="业务炸了"):
            async with trace_context("boom", kind="chat") as t:
                async with span("bad", kind="tool") as s:
                    raise ValueError("业务炸了")

        assert s.status == "error"
        assert "业务炸了" in s.error
        assert t.status == "error"

        async with trace_db() as session:
            trace = (
                await session.execute(
                    select(TraceRecord).where(TraceRecord.trace_id == t.trace_id)
                )
            ).scalar_one()
        assert trace.status == "error"
        assert "ValueError" in trace.error

    async def test_unknown_model_span_marked_as_estimated(self, trace_db):
        async with trace_context("unknown_model") as _t:
            async with span("llm", kind="llm", model="nobody-knows-me") as s:
                s.set_usage(1000, 1000)
        assert s.attributes.get("price_is_fallback") is True
        assert s.cost > 0

    async def test_sampling_zero_disables_collection(self, trace_db, monkeypatch):
        from core.config import get_settings

        monkeypatch.setattr(get_settings(), "trace_sample_rate", 0.0)
        async with trace_context("dropped") as t:
            async with span("llm", kind="llm", model="gpt-4o") as s:
                s.set_usage(10, 10)

        assert t.sampled is False
        assert s.enabled is False
        async with trace_db() as session:
            traces = (await session.execute(select(TraceRecord))).scalars().all()
            spans = (await session.execute(select(SpanRecord))).scalars().all()
        assert traces == [] and spans == []

    async def test_background_writer_flush(self, trace_db):
        """启用后台写入器后记录先进队列，flush_now 后才可见"""
        start_trace_writer()
        try:
            async with trace_context("queued", kind="workflow") as t:
                async with span("step", kind="chain") as s:
                    s.set_output("ok")
            await flush_now()

            async with trace_db() as session:
                trace = (
                    await session.execute(
                        select(TraceRecord).where(TraceRecord.trace_id == t.trace_id)
                    )
                ).scalar_one()
                spans = (
                    (
                        await session.execute(
                            select(SpanRecord).where(SpanRecord.trace_id == t.trace_id)
                        )
                    )
                    .scalars()
                    .all()
                )
            assert trace.status == "success"
            assert trace.total_spans == 1
            assert len(spans) == 1
        finally:
            await stop_trace_writer()

    async def test_write_failure_never_raises(self, monkeypatch, caplog):
        """DB 不可用时采集层只告警，绝不把异常抛回业务链路"""

        class _BrokenSessionMaker:
            def __call__(self):
                raise RuntimeError("db down")

        monkeypatch.setattr(core_database, "AsyncSessionLocal", _BrokenSessionMaker())
        with caplog.at_level("WARNING", logger="core.observe"):
            async with trace_context("broken") as t:
                async with span("llm", kind="llm", model="gpt-4o") as s:
                    s.set_usage(1, 1)
        assert t.status == "success"
        assert any("落库失败" in r.getMessage() for r in caplog.records)


# ============================================================
# 3. 成本账本（ConversationMetrics + BillingRecord）
# ============================================================


class TestCostLedger:
    async def test_record_llm_usage_writes_both_tables(self, trace_db):
        ok = await record_llm_usage(
            model="gpt-4o",
            prompt_tokens=1000,
            completion_tokens=500,
            cost=0.0075,
            latency_ms=123.4,
            conversation_id="conv-1",
            user_id="U1",
            tenant_id="default",
        )
        assert ok is True

        async with trace_db() as session:
            metric = (
                await session.execute(select(ConversationMetrics))
            ).scalar_one()
            billing = (await session.execute(select(BillingRecord))).scalar_one()

        assert metric.model == "gpt-4o"
        assert metric.input_tokens == 1000
        assert metric.output_tokens == 500
        assert metric.total_tokens == 1500
        assert metric.cost == pytest.approx(0.0075)
        assert metric.latency_ms == pytest.approx(123.4)
        assert metric.status == "success"

        assert billing.user_id == "U1"
        assert billing.tokens_used == 1500
        assert billing.cost_usd == pytest.approx(0.0075)
        assert billing.api_endpoint == "llm/gpt-4o"
        assert billing.invoice_period == datetime.now(timezone.utc).strftime("%Y-%m")

    async def test_record_llm_usage_failure_is_swallowed(self, monkeypatch):
        class _BrokenSessionMaker:
            def __call__(self):
                raise RuntimeError("db down")

        monkeypatch.setattr(core_database, "AsyncSessionLocal", _BrokenSessionMaker())
        ok = await record_llm_usage(
            model="gpt-4o", prompt_tokens=1, completion_tokens=1, cost=0.1
        )
        assert ok is False

    async def test_llm_call_writes_ledger_and_span(self, trace_db):
        """call_llm_with_fallback 成功后必须落 span + 会话度量 + 计费记录"""
        from core.llm_call import call_llm_with_fallback
        from core.providers.base import ChatCompletion, ProviderConfig

        class _Provider:
            config = ProviderConfig(model_name="gpt-4o-mini")

            def name(self):
                return "openai"

            async def chat_completion(self, messages, response_format=None):
                return ChatCompletion(
                    content='{"ok": true}',
                    model="gpt-4o-mini",
                    usage={
                        "prompt_tokens": 800,
                        "completion_tokens": 200,
                        "total_tokens": 1000,
                    },
                )

        class _Router:
            async def get_provider_with_fallback(self):
                return _Provider(), "L1"

        completion, tier = await call_llm_with_fallback(
            _Router(), prompt="打分", employee_id="E1001", period="2026-08"
        )
        # 返回签名保持 (completion, tier)
        assert isinstance(completion, ChatCompletion)
        assert tier == "L1"

        async with trace_db() as session:
            spans = (await session.execute(select(SpanRecord))).scalars().all()
            metric = (
                await session.execute(select(ConversationMetrics))
            ).scalar_one()
            billing = (await session.execute(select(BillingRecord))).scalar_one()
            traces = (await session.execute(select(TraceRecord))).scalars().all()

        assert len(spans) == 1
        assert spans[0].kind == "llm"
        assert spans[0].model == "gpt-4o-mini"
        assert spans[0].prompt_tokens == 800
        assert spans[0].completion_tokens == 200
        assert spans[0].cost == pytest.approx(
            calculate_cost("gpt-4o-mini", 800, 200).total_cost
        )
        # 无外层 trace 时自动开隐式 trace，链路不丢
        assert len(traces) == 1
        assert traces[0].total_spans == 1

        assert metric.input_tokens == 800 and metric.output_tokens == 200
        assert metric.cost > 0
        assert billing.tokens_used == 1000
        assert billing.cost_usd > 0


# ============================================================
# 4. 瀑布图组装
# ============================================================


class TestWaterfallAssembly:
    def _trace(self, duration_ms=100.0):
        return TraceRecord(
            id=1,
            trace_id="T1",
            tenant_id="default",
            name="wf",
            kind="chat",
            status="success",
            started_at=datetime(2026, 8, 8, 10, 0, 0, tzinfo=timezone.utc),
            duration_ms=duration_ms,
        )

    def test_nested_tree_and_relative_offsets(self):
        from api.admin.trace_v2_routes import build_waterfall

        spans = [
            _make_span("root", offset_ms=0, duration_ms=100),
            _make_span("child-a", parent="root", offset_ms=10, duration_ms=30),
            _make_span("child-b", parent="root", offset_ms=50, duration_ms=40),
            _make_span("grandchild", parent="child-a", offset_ms=15, duration_ms=10),
        ]
        roots, timeline = build_waterfall(self._trace(), spans)

        assert len(roots) == 1
        root = roots[0]
        assert root.span_id == "root"
        assert root.relative_start_ms == pytest.approx(0.0)
        assert [c.span_id for c in root.children] == ["child-a", "child-b"]
        assert root.children[0].relative_start_ms == pytest.approx(10.0)
        assert root.children[1].relative_start_ms == pytest.approx(50.0)

        grandchild = root.children[0].children[0]
        assert grandchild.span_id == "grandchild"
        assert grandchild.relative_start_ms == pytest.approx(15.0)
        assert timeline >= 100.0

    def test_orphan_span_promoted_to_root(self):
        """父节点缺失（写入丢失）的 span 提升为根，绝不静默丢数据"""
        from api.admin.trace_v2_routes import build_waterfall

        spans = [
            _make_span("root", offset_ms=0),
            _make_span("orphan", parent="ghost-span-id", offset_ms=20),
        ]
        roots, _timeline = build_waterfall(self._trace(), spans)
        assert {r.span_id for r in roots} == {"root", "orphan"}

    def test_cycle_is_broken_not_infinite(self):
        """异常数据成环时打断为根节点，不递归爆栈"""
        from api.admin.trace_v2_routes import build_waterfall

        spans = [
            _make_span("a", parent="b", offset_ms=0),
            _make_span("b", parent="a", offset_ms=5),
        ]
        roots, _timeline = build_waterfall(self._trace(), spans)
        total = 0
        stack = list(roots)
        while stack:
            node = stack.pop()
            total += 1
            stack.extend(node.children)
        assert total == 2

    def test_timeline_falls_back_to_span_extent(self):
        """trace.duration_ms 缺失时用 span 最大结束偏移兜底"""
        from api.admin.trace_v2_routes import build_waterfall

        spans = [_make_span("root", offset_ms=0, duration_ms=250)]
        roots, timeline = build_waterfall(self._trace(duration_ms=None), spans)
        assert len(roots) == 1
        assert timeline == pytest.approx(250.0)

    def test_empty_spans(self):
        from api.admin.trace_v2_routes import build_waterfall

        roots, timeline = build_waterfall(self._trace(duration_ms=42.0), [])
        assert roots == []
        assert timeline == pytest.approx(42.0)
