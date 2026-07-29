"""Langfuse 追踪 + OpenTelemetry 分布式追踪集成

提供两层追踪能力:
1. Langfuse 应用层追踪: 评估流程/LLM 调用/节点执行 trace → Langfuse Cloud/UI
2. OpenTelemetry 基础设施追踪: HTTP/DB/Redis 自动埋点 → Jaeger/Tempo/OTLP collector

设计原则:
- 优雅降级: 未安装 langfuse/opentelemetry 时跳过,不影响应用启动
- 配置驱动: Langfuse 通过 settings 配置, OTel 通过环境变量配置
- trace_id 关联: RequestContextMiddleware 的 trace_id 与日志/追踪打通

使用方式:
    # Langfuse (自动初始化全局 tracer):
    from core.tracing import tracer
    with tracer.trace(name="eval", employee_id="E001") as trace:
        tracer.generation(parent=trace, ...)

    # OpenTelemetry (main.py lifespan 中调用):
    from core.tracing import setup_tracing
    setup_tracing(app, engine)

    # 环境变量:
    OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4317
    OTEL_SERVICE_NAME=agentvalue-backend
"""

from __future__ import annotations

import contextvars
import logging
from contextlib import contextmanager
from typing import Any, Dict, Generator, Optional

logger = logging.getLogger(__name__)

# ============================================================
# trace_id contextvar — 供 RequestContextMiddleware 和日志 Filter 共享
# ============================================================

_current_trace_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "trace_id", default=None
)


def get_current_trace_id() -> Optional[str]:
    """获取当前 trace_id(供日志关联使用)"""
    return _current_trace_id.get()


def set_trace_id(trace_id: str):
    """设置当前 trace_id"""
    return _current_trace_id.set(trace_id)


def reset_trace_id(token):
    """重置 trace_id"""
    if token is not None:
        try:
            _current_trace_id.reset(token)
        except Exception:
            pass


# ============================================================
# NoOpTrace — 未启用 Langfuse 时的空操作追踪
# ============================================================


class NoOpTrace:
    """空操作 Trace,所有方法均为 no-op。

    作为 context manager 使用时 yield self,
    span/update 方法安全无副作用。
    """

    def __init__(self, **kwargs: Any) -> None:
        self.metadata: Dict[str, Any] = {}

    def span(self, name: Optional[str] = None, **kwargs: Any) -> "NoOpTrace":
        return NoOpTrace()

    def generation(self, **kwargs: Any) -> "NoOpTrace":
        return NoOpTrace()

    def update(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> "NoOpTrace":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


# ============================================================
# LangfuseTracer — 应用层追踪(Langfuse)
# ============================================================


class LangfuseTracer:
    """Langfuse 追踪器,封装 trace/span/generation 三层 API。

    未配置 Langfuse 凭据或 langfuse 包未安装时自动降级为 NoOpTrace,
    确保业务代码无需感知追踪是否启用。
    """

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._client: Any = None

        # 检查 settings 是否配置了三要素(不依赖 _client,避免循环依赖)
        if not (
            getattr(settings, "langfuse_public_key", None)
            and getattr(settings, "langfuse_secret_key", None)
            and getattr(settings, "langfuse_host", None)
        ):
            return

        try:
            from langfuse import Langfuse

            self._client = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                host=settings.langfuse_host,
            )
            logger.info("Langfuse 追踪已启用: host=%s", settings.langfuse_host)
        except Exception as e:
            logger.warning("Langfuse 初始化失败,降级为 NoOp: %s", e)
            self._client = None

    def is_enabled(self) -> bool:
        """检查 Langfuse 是否已配置且可用(settings 三要素齐全 + client 初始化成功)"""
        s = self._settings
        return bool(
            getattr(s, "langfuse_public_key", None)
            and getattr(s, "langfuse_secret_key", None)
            and getattr(s, "langfuse_host", None)
            and self._client is not None
        )

    @contextmanager
    def trace(
        self,
        name: Optional[str] = None,
        evaluation_id: Optional[str] = None,
        employee_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Generator[Any, None, None]:
        """创建顶层 trace,返回 Langfuse trace 对象(上下文管理器)。

        未启用时 yield NoOpTrace。
        """
        if not self.is_enabled():
            yield NoOpTrace()
            return

        trace_kwargs: Dict[str, Any] = {"name": name, "metadata": metadata or {}}
        if evaluation_id:
            trace_kwargs["id"] = evaluation_id
        if employee_id:
            trace_kwargs["user_id"] = employee_id
        trace_kwargs.update(kwargs)

        _trace = self._client.trace(**trace_kwargs)
        try:
            yield _trace
        except Exception:
            # trace 异常不应阻断业务逻辑,但需记录
            logger.debug("Langfuse trace 异常", exc_info=True)

    @contextmanager
    def span(
        self,
        parent: Any,
        name: Optional[str] = None,
        input_data: Optional[Any] = None,
        **kwargs: Any,
    ) -> Generator[Any, None, None]:
        """在 parent trace 下创建子 span。

        parent 为 None 或未启用时 yield NoOpTrace。
        """
        if not self.is_enabled() or parent is None:
            yield NoOpTrace()
            return

        _span = parent.span(name=name, input=input_data)
        try:
            yield _span
        except Exception:
            logger.debug("Langfuse span 异常", exc_info=True)

    def generation(
        self,
        parent: Any,
        name: Optional[str] = None,
        prompt: Optional[str] = None,
        completion: Optional[str] = None,
        model: Optional[str] = None,
        usage: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        """记录 LLM 生成调用到 Langfuse。

        parent 为 None 或未启用时返回 NoOpTrace。
        额外的 prompt_name/prompt_version 等 kwargs 透传到 metadata。
        """
        if not self.is_enabled() or parent is None:
            return NoOpTrace()

        gen_metadata = metadata or {}
        # P1 调试增强: prompt 版本信息绑定到 generation metadata
        for k in ("prompt_name", "prompt_version", "prompt_version_id", "prompt_labels"):
            v = kwargs.pop(k, None)
            if v is not None:
                gen_metadata[k] = v

        try:
            return parent.generation(
                name=name,
                input=prompt,
                output=completion,
                model=model,
                usage=usage,
                metadata=gen_metadata,
            )
        except Exception:
            logger.debug("Langfuse generation 记录失败", exc_info=True)
            return NoOpTrace()

    def current_trace_id(self) -> Optional[str]:
        """获取当前请求的 trace_id(供日志关联)"""
        return get_current_trace_id()


# ============================================================
# 全局 tracer 实例 — 延迟初始化(首次访问 settings 时)
# ============================================================

_tracer_instance: Optional[LangfuseTracer] = None


def _get_tracer() -> LangfuseTracer:
    """延迟初始化全局 tracer(避免模块加载时依赖 settings)"""
    global _tracer_instance
    if _tracer_instance is not None:
        return _tracer_instance

    try:
        from core.config import get_settings

        settings = get_settings()
        _tracer_instance = LangfuseTracer(settings)
    except Exception as e:
        logger.debug("tracer 初始化失败,使用 NoOp: %s", e)
        # 创建一个禁用的 tracer 作为兜底
        _tracer_instance = LangfuseTracer.__new__(LangfuseTracer)
        _tracer_instance._settings = None
        _tracer_instance._client = None
    return _tracer_instance


class _TracerProxy:
    """代理对象,首次属性访问时延迟初始化全局 tracer。

    避免在模块加载阶段就触发 settings 读取(可能尚未配置)。
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(_get_tracer(), name)


tracer = _TracerProxy()


# ============================================================
# OpenTelemetry 分布式追踪 — 基础设施层(HTTP/DB/Redis 自动埋点)
# ============================================================

_tracer_provider = None


def setup_tracing(app: Any, engine: Any = None) -> bool:
    """初始化 OpenTelemetry 追踪

    Args:
        app: FastAPI 应用实例
        engine: SQLAlchemy 异步引擎(可选,用于 DB 追踪)

    Returns:
        True=成功启用, False=降级跳过(未安装/未配置)
    """
    global _tracer_provider

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.info("OpenTelemetry 未安装,跳过分布式追踪(降级为无追踪)")
        return False

    import os

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        logger.info("OTEL_EXPORTER_OTLP_ENDPOINT 未配置,跳过分布式追踪")
        return False

    service_name = os.environ.get("OTEL_SERVICE_NAME", "agentvalue-backend")

    # 创建 TracerProvider
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": getattr(app, "version", "unknown"),
        }
    )

    _tracer_provider = TracerProvider(resource=resource)

    # OTLP exporter → Jaeger/Tempo/collector
    exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    _tracer_provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(_tracer_provider)

    # 自动埋点: FastAPI
    FastAPIInstrumentor.instrument_app(app)

    # 自动埋点: SQLAlchemy
    if engine is not None:
        try:
            from opentelemetry.instrumentation.sqlalchemy import (
                SQLAlchemyInstrumentor,
            )

            SQLAlchemyInstrumentor.instrument(
                engine=engine.sync_engine,
                enable_commenter=True,
                commenter_options={},
            )
            logger.info("SQLAlchemy 追踪已启用")
        except Exception as e:
            logger.warning("SQLAlchemy 追踪启用失败: %s", e)

    # 自动埋点: Redis
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor

        RedisInstrumentor.instrument()
        logger.info("Redis 追踪已启用")
    except Exception as e:
        logger.warning("Redis 追踪启用失败: %s", e)

    logger.info(
        "OpenTelemetry 分布式追踪已启用: service=%s, endpoint=%s",
        service_name,
        endpoint,
    )
    return True


def shutdown_tracing() -> None:
    """关闭追踪(在应用关闭时调用)"""
    global _tracer_provider
    if _tracer_provider is not None:
        try:
            _tracer_provider.shutdown()
        except Exception:
            pass
        _tracer_provider = None
