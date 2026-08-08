"""
零侵入链路采集层（WS-1 可观测性）

提供两个基于 ``contextvars`` 的异步上下文管理器，把业务链路落到
``trace_records`` / ``span_records``：

    async with trace_context("evaluate_employee", kind="evaluation") as t:
        async with span("retrieve_context", kind="retriever") as s:
            s.set_output({"docs": 5})
        async with span("chat_completion", kind="llm", model="gpt-4o") as s:
            s.set_usage(1200, 300, model="gpt-4o")   # 自动算成本

设计红线
--------
1. **绝不影响业务链路**。span 内部任何异常都被吞掉并打日志/埋点；
   业务代码抛出的异常会被记录到 span.error 后**原样 re-raise**。
2. **绝不阻塞**。记录进 ``asyncio.Queue``，由后台任务批量落库
   （满 ``_BATCH_SIZE`` 条或每 ``_FLUSH_INTERVAL`` 秒）。
   队列打满时丢弃最旧策略改为丢弃当前记录并告警——观测数据可丢，业务不可卡。
3. **无 trace 时可用**。没有活跃 trace（例如某个 service 被单测直接调用）时，
   ``span()`` 退化为内存对象，所有方法可正常调用，只是不落库、不报错。

与 core/tracing.py 的分工：后者对接 Langfuse / OTel 外发，本模块负责本地存储。
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import random
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import update

from core.config import get_settings
from models.trace_models import (
    STATUS_ERROR,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    SpanRecord,
    TraceRecord,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 上下文变量
# ---------------------------------------------------------------------------

# 当前活跃 trace_id（无活跃 trace 时为 None）
_current_trace_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_current_trace_id", default=None
)
# 当前活跃 span_id，新建 span 以此为 parent（trace 根层为 None）
_current_span_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_current_span_id", default=None
)
# 当前活跃 trace 句柄，供 span 回写汇总（未采样时为 None）
_current_trace: contextvars.ContextVar[Optional["Trace"]] = contextvars.ContextVar(
    "_current_trace", default=None
)

# ---------------------------------------------------------------------------
# 写入器参数（写死为模块常量，避免为观测细节污染共享的 Settings）
# ---------------------------------------------------------------------------

# 队列容量：约等于 5 秒的峰值写入量，超出即丢弃并告警
_QUEUE_MAXSIZE = 10_000
# 单批最大条数
_BATCH_SIZE = 100
# 最长驻留时间（秒），保证低流量下也能及时可见
_FLUSH_INTERVAL = 1.0
# input / output 序列化后的最大字符数，超出截断（防止单条 span 撑爆 DB）
_MAX_PAYLOAD_CHARS = 8000

_queue: Optional["asyncio.Queue[Dict[str, Any]]"] = None
_writer_task: Optional[asyncio.Task] = None
# 直写降级只在首次告警，避免刷屏
_direct_write_warned = False


try:  # pragma: no cover - 取决于运行环境是否安装 prometheus_client
    from prometheus_client import Counter as _Counter

    TRACE_RECORDS_DROPPED_TOTAL = _Counter(
        "agentvalue_trace_records_dropped_total",
        "因写入队列打满而丢弃的 trace/span 记录数",
        ["record_type"],
    )
    TRACE_WRITE_FAILURES_TOTAL = _Counter(
        "agentvalue_trace_write_failures_total",
        "trace/span 落库失败次数",
    )
except Exception as _exc:  # pragma: no cover
    TRACE_RECORDS_DROPPED_TOTAL = None
    TRACE_WRITE_FAILURES_TOTAL = None
    logger.debug("trace 写入埋点注册失败，降级为仅日志: %s", _exc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


def _default_tenant() -> Optional[str]:
    """取当前租户；tenant_context 未初始化时返回 None（不阻断采集）。"""
    try:
        from core.tenant_context import get_current_tenant

        return get_current_tenant()
    except Exception:
        return None


def _truncate_payload(value: Any) -> Optional[Any]:
    """把任意 input/output 规整为可 JSON 序列化且长度受控的值。

    不可序列化对象退化为 ``repr``；超长内容截断并追加省略标记，
    保证不会因为一条大 payload 让整批写入失败。
    """
    if value is None:
        return None
    try:
        serialized = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        serialized = repr(value)
    if len(serialized) <= _MAX_PAYLOAD_CHARS:
        try:
            return json.loads(serialized)
        except Exception:
            return serialized
    return {
        "_truncated": True,
        "_original_chars": len(serialized),
        "preview": serialized[:_MAX_PAYLOAD_CHARS],
    }


# ---------------------------------------------------------------------------
# 采集句柄
# ---------------------------------------------------------------------------


class Span:
    """单个执行片段句柄，同时是 async context manager。

    未采样 / 无活跃 trace 时 ``enabled=False``，全部方法仍可正常调用，
    只是退出时不入队（no-op），因此业务代码无需做任何判空。
    """

    __slots__ = (
        "span_id",
        "trace_id",
        "parent_span_id",
        "tenant_id",
        "name",
        "kind",
        "status",
        "started_at",
        "ended_at",
        "duration_ms",
        "input",
        "output",
        "model",
        "provider",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cost",
        "error",
        "attributes",
        "enabled",
        "_perf_start",
        "_trace",
        "_token_span",
        "_finished",
    )

    def __init__(
        self,
        name: str,
        *,
        kind: str = "chain",
        model: Optional[str] = None,
        provider: Optional[str] = None,
        input: Any = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.span_id = _new_id()
        self.name = name
        self.kind = kind
        self.model = model
        self.provider = provider
        self.input = _truncate_payload(input)
        self.attributes: Dict[str, Any] = dict(attributes or {})
        self.output: Any = None
        self.status = STATUS_RUNNING
        self.error: Optional[str] = None
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.cost = 0.0
        self.started_at = _now()
        self.ended_at: Optional[datetime] = None
        self.duration_ms: Optional[float] = None
        self._perf_start = time.perf_counter()
        self._finished = False
        self._token_span: Optional[contextvars.Token] = None

        self._trace = _current_trace.get()
        self.trace_id = _current_trace_id.get()
        self.parent_span_id = _current_span_id.get()
        self.tenant_id = self._trace.tenant_id if self._trace else _default_tenant()
        # 有 trace_id 才有落库意义（无 trace 时退化为 no-op）
        self.enabled = self.trace_id is not None

    # ---------------- 采集 API ----------------

    def set_output(self, output: Any) -> "Span":
        """记录输出（自动截断超长内容）。"""
        self.output = _truncate_payload(output)
        return self

    def set_usage(
        self,
        prompt_tokens: Optional[int],
        completion_tokens: Optional[int],
        *,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        total_tokens: Optional[int] = None,
    ) -> "Span":
        """记录 token 用量并按 core/pricing 计算成本。

        model 未传时沿用创建 span 时的 model。未知模型会走 DEFAULT_PRICE
        兜底（``pricing`` 内部已打日志与埋点），成本绝不静默为 0。
        """
        if model:
            self.model = model
        if provider:
            self.provider = provider
        self.prompt_tokens = max(0, int(prompt_tokens or 0))
        self.completion_tokens = max(0, int(completion_tokens or 0))
        self.total_tokens = (
            int(total_tokens)
            if total_tokens is not None
            else self.prompt_tokens + self.completion_tokens
        )
        try:
            from core.pricing import calculate_cost

            breakdown = calculate_cost(
                self.model, self.prompt_tokens, self.completion_tokens
            )
            self.cost = breakdown.total_cost
            self.attributes["cost_currency"] = breakdown.currency
            self.attributes["priced_as"] = breakdown.matched_model
            if breakdown.is_fallback:
                # 让瀑布图能标出"该条成本是估算值"
                self.attributes["price_is_fallback"] = True
        except Exception as exc:  # 成本算不出来也不能拖垮业务
            logger.warning("span 成本计算失败 (model=%s): %s", self.model, exc)
        return self

    def set_attribute(self, key: str, value: Any) -> "Span":
        """写入一条自定义属性（对齐 OTel span attributes）。"""
        self.attributes[key] = value
        return self

    def set_error(self, error: Any) -> "Span":
        """显式标记本 span 失败（不抛异常的业务失败场景）。"""
        self.status = STATUS_ERROR
        self.error = str(error)[:2000]
        return self

    # ---------------- 上下文管理 ----------------

    async def __aenter__(self) -> "Span":
        self._token_span = _current_span_id.set(self.span_id)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self._token_span is not None:
            try:
                _current_span_id.reset(self._token_span)
            except ValueError:
                # 跨任务退出时 token 不属于当前 context，直接置回父 span
                _current_span_id.set(self.parent_span_id)
            self._token_span = None

        if exc is not None:
            self.status = STATUS_ERROR
            self.error = f"{exc_type.__name__}: {exc}"[:2000]
        elif self.status == STATUS_RUNNING:
            self.status = STATUS_SUCCESS

        self.ended_at = _now()
        self.duration_ms = (time.perf_counter() - self._perf_start) * 1000.0

        await self._finish()
        # 返回 False：业务异常原样向上抛，观测层不吞业务异常
        return False

    async def _finish(self) -> None:
        """结算并入队（幂等）。"""
        if self._finished:
            return
        self._finished = True
        if self._trace is not None:
            self._trace._rollup(self)
        if not self.enabled:
            return
        await _enqueue(
            {
                "op": "insert_span",
                "values": {
                    "span_id": self.span_id,
                    "trace_id": self.trace_id,
                    "parent_span_id": self.parent_span_id,
                    "tenant_id": self.tenant_id,
                    "name": self.name[:256],
                    "kind": self.kind,
                    "status": self.status,
                    "started_at": self.started_at,
                    "ended_at": self.ended_at,
                    "duration_ms": self.duration_ms,
                    "input": self.input,
                    "output": self.output,
                    "model": self.model,
                    "provider": self.provider,
                    "prompt_tokens": self.prompt_tokens,
                    "completion_tokens": self.completion_tokens,
                    "total_tokens": self.total_tokens,
                    "cost": self.cost,
                    "error": self.error,
                    "attributes": self.attributes or None,
                },
            }
        )


class Trace:
    """一次完整业务链路句柄，同时是 async context manager。"""

    __slots__ = (
        "trace_id",
        "name",
        "kind",
        "tenant_id",
        "user_id",
        "session_id",
        "tags",
        "trace_metadata",
        "status",
        "error",
        "started_at",
        "ended_at",
        "duration_ms",
        "total_spans",
        "total_prompt_tokens",
        "total_completion_tokens",
        "total_cost",
        "sampled",
        "_perf_start",
        "_token_trace",
        "_token_trace_id",
        "_token_span",
        "_finished",
    )

    def __init__(
        self,
        name: str,
        *,
        kind: str = "chat",
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        trace_id: Optional[str] = None,
    ) -> None:
        self.trace_id = trace_id or _new_id()
        self.name = name
        self.kind = kind
        self.tenant_id = tenant_id if tenant_id is not None else _default_tenant()
        self.user_id = user_id
        self.session_id = session_id
        self.tags: List[str] = list(tags or [])
        self.trace_metadata: Dict[str, Any] = dict(metadata or {})
        self.status = STATUS_RUNNING
        self.error: Optional[str] = None
        self.started_at = _now()
        self.ended_at: Optional[datetime] = None
        self.duration_ms: Optional[float] = None
        self.total_spans = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_cost = 0.0
        self.sampled = _should_sample()
        self._perf_start = time.perf_counter()
        self._finished = False
        self._token_trace: Optional[contextvars.Token] = None
        self._token_trace_id: Optional[contextvars.Token] = None
        self._token_span: Optional[contextvars.Token] = None

    # ---------------- 采集 API ----------------

    def add_tag(self, tag: str) -> "Trace":
        if tag not in self.tags:
            self.tags.append(tag)
        return self

    def set_metadata(self, key: str, value: Any) -> "Trace":
        self.trace_metadata[key] = value
        return self

    def set_error(self, error: Any) -> "Trace":
        self.status = STATUS_ERROR
        self.error = str(error)[:2000]
        return self

    def _rollup(self, span: Span) -> None:
        """由子 span 结束时回调，累加汇总指标。"""
        self.total_spans += 1
        self.total_prompt_tokens += span.prompt_tokens
        self.total_completion_tokens += span.completion_tokens
        self.total_cost += span.cost

    # ---------------- 上下文管理 ----------------

    async def __aenter__(self) -> "Trace":
        if self.sampled:
            self._token_trace_id = _current_trace_id.set(self.trace_id)
            self._token_trace = _current_trace.set(self)
            # 新 trace 的第一层 span 无父节点
            self._token_span = _current_span_id.set(None)
            await _enqueue(
                {"op": "insert_trace", "values": self._insert_values()}
            )
        else:
            # 未采样：显式清空上下文，避免子 span 挂到外层 trace 上
            self._token_trace_id = _current_trace_id.set(None)
            self._token_trace = _current_trace.set(None)
            self._token_span = _current_span_id.set(None)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        for var, token, fallback in (
            (_current_span_id, self._token_span, None),
            (_current_trace, self._token_trace, None),
            (_current_trace_id, self._token_trace_id, None),
        ):
            if token is None:
                continue
            try:
                var.reset(token)
            except ValueError:
                var.set(fallback)
        self._token_span = self._token_trace = self._token_trace_id = None

        if exc is not None:
            self.status = STATUS_ERROR
            self.error = f"{exc_type.__name__}: {exc}"[:2000]
        elif self.status == STATUS_RUNNING:
            self.status = STATUS_SUCCESS

        self.ended_at = _now()
        self.duration_ms = (time.perf_counter() - self._perf_start) * 1000.0

        await self._finish()
        return False

    def _insert_values(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "tenant_id": self.tenant_id,
            "name": self.name[:256],
            "kind": self.kind,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "total_spans": self.total_spans,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_cost": self.total_cost,
            "error": self.error,
            "tags": self.tags or None,
            "trace_metadata": self.trace_metadata or None,
        }

    async def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        if not self.sampled:
            return
        await _enqueue({"op": "finish_trace", "values": self._insert_values()})


def trace_context(
    name: str,
    *,
    kind: str = "chat",
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    trace_id: Optional[str] = None,
) -> Trace:
    """开启一条链路。

        async with trace_context("chat", kind="chat", user_id="u1") as t:
            ...

    退出时自动结算 duration / status，并把其下所有 span 的 token 与成本
    汇总回填到 TraceRecord。采样未命中时整条链路（含 span）不落库。
    """
    return Trace(
        name,
        kind=kind,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        tags=tags,
        metadata=metadata,
        trace_id=trace_id,
    )


def span(
    name: str,
    *,
    kind: str = "chain",
    model: Optional[str] = None,
    provider: Optional[str] = None,
    input: Any = None,
    attributes: Optional[Dict[str, Any]] = None,
) -> Span:
    """在当前链路下开启一个 span，自动挂到当前 span（或 trace 根）之下。

    无活跃 trace 时返回一个 no-op span：方法照常可调用，不落库也不报错。
    """
    return Span(
        name,
        kind=kind,
        model=model,
        provider=provider,
        input=input,
        attributes=attributes,
    )


def current_trace_id() -> Optional[str]:
    """返回当前活跃 trace_id，供日志关联 / 响应头回传。"""
    return _current_trace_id.get()


def current_span_id() -> Optional[str]:
    """返回当前活跃 span_id。"""
    return _current_span_id.get()


def _should_sample() -> bool:
    """按 settings.trace_sample_rate 判定是否采集本条链路。"""
    try:
        rate = float(getattr(get_settings(), "trace_sample_rate", 1.0))
    except Exception:
        rate = 1.0
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    return random.random() < rate


# ---------------------------------------------------------------------------
# 异步批量写入器
# ---------------------------------------------------------------------------


async def _enqueue(op: Dict[str, Any]) -> None:
    """把一条写操作放入队列；写入器未运行时降级为直写。

    任何情况下都不向调用方抛异常。
    """
    global _direct_write_warned
    queue = _queue
    if queue is None or _writer_task is None or _writer_task.done():
        if not _direct_write_warned:
            _direct_write_warned = True
            logger.info(
                "trace 写入器未运行，本进程 trace/span 采用直写模式"
                "（单测/脚本场景正常；服务进程请确认 lifespan 调用了 start_trace_writer）"
            )
        await _write_batch([op])
        return
    try:
        queue.put_nowait(op)
    except asyncio.QueueFull:
        record_type = "trace" if op["op"] != "insert_span" else "span"
        logger.warning(
            "trace 写入队列已满(%d)，丢弃一条 %s 记录", _QUEUE_MAXSIZE, record_type
        )
        if TRACE_RECORDS_DROPPED_TOTAL is not None:
            try:
                TRACE_RECORDS_DROPPED_TOTAL.labels(record_type=record_type).inc()
            except Exception:
                logger.debug("trace 丢弃埋点失败", exc_info=True)
    except Exception:
        logger.warning("trace 入队失败", exc_info=True)


async def _write_batch(ops: List[Dict[str, Any]]) -> None:
    """把一批写操作落库。失败只告警不抛出。

    批内顺序固定为：trace 插入 → span 插入 → trace 收尾更新，
    保证同批内"先建后更"，跨批则由队列 FIFO 保证。
    """
    if not ops:
        return
    trace_inserts = [o["values"] for o in ops if o["op"] == "insert_trace"]
    span_inserts = [o["values"] for o in ops if o["op"] == "insert_span"]
    trace_finishes = [o["values"] for o in ops if o["op"] == "finish_trace"]

    try:
        from core.database import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            try:
                if trace_inserts:
                    session.add_all([TraceRecord(**v) for v in trace_inserts])
                if span_inserts:
                    session.add_all([SpanRecord(**v) for v in span_inserts])
                if trace_inserts or span_inserts:
                    await session.flush()

                for values in trace_finishes:
                    payload = {k: v for k, v in values.items() if k != "trace_id"}
                    result = await session.execute(
                        update(TraceRecord)
                        .where(TraceRecord.trace_id == values["trace_id"])
                        .values(**payload)
                    )
                    if (result.rowcount or 0) == 0:
                        # 起始记录丢失（队列溢出/直写失败），补insert 保证不丢链路
                        session.add(TraceRecord(**values))
                        await session.flush()

                await session.commit()
            except Exception:
                await session.rollback()
                raise
    except Exception as exc:
        logger.warning(
            "trace/span 落库失败，丢弃 %d 条记录: %s", len(ops), exc, exc_info=True
        )
        if TRACE_WRITE_FAILURES_TOTAL is not None:
            try:
                TRACE_WRITE_FAILURES_TOTAL.inc()
            except Exception:
                logger.debug("trace 写入失败埋点失败", exc_info=True)


# ---------------------------------------------------------------------------
# 成本账本落库（ConversationMetrics + BillingRecord）
# ---------------------------------------------------------------------------


async def record_llm_usage(
    *,
    model: Optional[str],
    prompt_tokens: int,
    completion_tokens: int,
    cost: float,
    latency_ms: Optional[float] = None,
    conversation_id: Optional[str] = None,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    status: str = STATUS_SUCCESS,
    error_message: Optional[str] = None,
) -> bool:
    """把一次 LLM 调用写入成本账本（会话度量 + 计费记录）。

    审计结论 2.1：``conversation_metrics`` / ``billing_records`` 两张表定义完整
    却零调用方，导致所有成本看板永远空白。本函数是它们唯一的写入入口。

    两张表在**同一个事务**内写入，避免出现"有度量无账单"的半截数据。
    失败只告警不抛出——账本写不进去也绝不能让 LLM 调用失败。

    Returns:
        True 表示写入成功，False 表示已降级（已打 WARNING 日志）。
    """
    try:
        from core.database import AsyncSessionLocal
        from models.quota_models import BillingRecord
        from services.analytics_service_v2 import AnalyticsServiceV2

        tid = tenant_id or _default_tenant()
        p_tokens = max(0, int(prompt_tokens or 0))
        c_tokens = max(0, int(completion_tokens or 0))
        now = _now()

        async with AsyncSessionLocal() as session:
            try:
                # 1) 会话度量（趋势 / 延迟分位 / 错误率 / 成本分解看板的数据源）
                await AnalyticsServiceV2(session).record_metrics(
                    conversation_id=conversation_id or _new_id(),
                    user_id=user_id,
                    agent_id=agent_id,
                    model=model,
                    input_tokens=p_tokens,
                    output_tokens=c_tokens,
                    cost=float(cost or 0.0),
                    latency_ms=latency_ms,
                    status=status,
                    error_message=error_message,
                    tenant_id=tid,
                )
                # 2) 计费记录（账单汇总 / 配额预算告警的数据源）
                session.add(
                    BillingRecord(
                        tenant_id=tid,
                        user_id=user_id or "system",
                        api_endpoint=f"llm/{model or 'unknown'}"[:512],
                        method="POST",
                        tokens_used=p_tokens + c_tokens,
                        cost_usd=float(cost or 0.0),
                        billed_at=now,
                        invoice_period=now.strftime("%Y-%m"),
                    )
                )
                await session.commit()
                return True
            except Exception:
                await session.rollback()
                raise
    except Exception as exc:
        logger.warning(
            "成本账本写入失败 (model=%s, tokens=%s/%s): %s",
            model,
            prompt_tokens,
            completion_tokens,
            exc,
            exc_info=True,
        )
        return False


def _drain(queue: "asyncio.Queue[Dict[str, Any]]", limit: int) -> List[Dict[str, Any]]:
    """非阻塞取出至多 limit 条待写记录。"""
    batch: List[Dict[str, Any]] = []
    while len(batch) < limit:
        try:
            batch.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return batch


async def _writer_loop(queue: "asyncio.Queue[Dict[str, Any]]") -> None:
    """后台写入循环：满 _BATCH_SIZE 条或每 _FLUSH_INTERVAL 秒落一批。"""
    logger.info(
        "trace 写入器已启动 (batch=%d, interval=%.1fs, queue=%d)",
        _BATCH_SIZE,
        _FLUSH_INTERVAL,
        _QUEUE_MAXSIZE,
    )
    while True:
        try:
            batch: List[Dict[str, Any]] = []
            try:
                first = await asyncio.wait_for(queue.get(), timeout=_FLUSH_INTERVAL)
                batch.append(first)
            except asyncio.TimeoutError:
                pass
            batch.extend(_drain(queue, _BATCH_SIZE - len(batch)))
            if batch:
                await _write_batch(batch)
        except asyncio.CancelledError:
            # 停机前尽力把剩余记录刷完
            remaining = _drain(queue, _QUEUE_MAXSIZE)
            if remaining:
                await _write_batch(remaining)
            raise
        except Exception:
            # 循环自身异常绝不允许终止写入器
            logger.warning("trace 写入循环异常，继续运行", exc_info=True)
            await asyncio.sleep(_FLUSH_INTERVAL)


def start_trace_writer() -> None:
    """启动后台批量写入任务（由 FastAPI lifespan 调用）。

    重复调用安全：已有存活任务时直接返回。
    """
    global _queue, _writer_task, _direct_write_warned
    if _writer_task is not None and not _writer_task.done():
        return
    _queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    _writer_task = asyncio.create_task(_writer_loop(_queue), name="trace-writer")
    _direct_write_warned = False


async def stop_trace_writer() -> None:
    """停止写入任务并把队列里剩余记录刷完（由 FastAPI lifespan 调用）。"""
    global _queue, _writer_task
    task, queue = _writer_task, _queue
    _writer_task = None
    _queue = None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("trace 写入器停止时异常", exc_info=True)
    if queue is not None:
        remaining = _drain(queue, _QUEUE_MAXSIZE)
        if remaining:
            await _write_batch(remaining)
    logger.info("trace 写入器已停止")


async def flush_now() -> None:
    """立即把队列中已有记录落库（测试/需要强一致读取的场景使用）。

    写入器未运行时为空操作——此时记录已在 ``_enqueue`` 阶段直写完成。
    """
    queue = _queue
    if queue is None:
        return
    while True:
        batch = _drain(queue, _BATCH_SIZE)
        if not batch:
            return
        await _write_batch(batch)


__all__ = [
    "Trace",
    "Span",
    "trace_context",
    "span",
    "current_trace_id",
    "current_span_id",
    "record_llm_usage",
    "start_trace_writer",
    "stop_trace_writer",
    "flush_now",
]
