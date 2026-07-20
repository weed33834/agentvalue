"""
Langfuse 可观测性集成
追踪 Agent 执行全链路：输入、Prompt、模型调用、输出、审批状态。
"""

import contextvars
import logging
import os
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Optional

from core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# P3-1：进程级 contextvar 保存当前 trace_id，供 logging_config 平铺到日志顶层。
# 未启用 Langfuse 时也写入（用本地生成的 uuid），让日志关联不依赖 Langfuse 可用性。
_current_trace_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_current_trace_id", default=None
)


class NoOpTrace:
    """当 Langfuse 未配置时的空实现"""

    def __init__(self):
        self.metadata = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def span(self, *args, **kwargs):
        return NoOpTrace()

    def update(self, *args, **kwargs):
        pass


class LangfuseTracer:
    """Langfuse 追踪器包装"""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self._client = None
        self._enabled = bool(
            self.settings.langfuse_public_key
            and self.settings.langfuse_secret_key
            and self.settings.langfuse_host
        )
        if self._enabled:
            try:
                from langfuse import Langfuse

                self._client = Langfuse(
                    public_key=self.settings.langfuse_public_key,
                    secret_key=self.settings.langfuse_secret_key,
                    host=self.settings.langfuse_host,
                )
            except Exception as e:
                logger.warning(f"Langfuse 初始化失败: {e}")
                self._enabled = False

    def is_enabled(self) -> bool:
        return self._enabled

    @contextmanager
    def trace(
        self,
        name: str,
        evaluation_id: Optional[str] = None,
        employee_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        # P3-1：进入 trace 时设置 contextvar，退出时恢复，让日志能关联当前 trace。
        # trace_id 优先用 evaluation_id（业务可读），否则生成 uuid。
        trace_id = evaluation_id or str(uuid.uuid4())
        token = _current_trace_id.set(trace_id)
        if not self._enabled or not self._client:
            try:
                yield NoOpTrace()
            finally:
                _current_trace_id.reset(token)
            return

        trace = self._client.trace(
            name=name,
            id=evaluation_id,
            user_id=employee_id,
            metadata=metadata or {},
        )
        try:
            yield trace
        finally:
            # Langfuse 客户端自动刷新，无需显式操作
            _current_trace_id.reset(token)

    @contextmanager
    def span(self, parent, name: str, input_data: Optional[Any] = None):
        if not self._enabled or not self._client or parent is None:
            yield NoOpTrace()
            return

        span = parent.span(
            name=name,
            input=input_data,
        )
        try:
            yield span
        finally:
            pass

    def generation(
        self,
        parent,
        name: str,
        prompt: Optional[str] = None,
        completion: Optional[str] = None,
        model: Optional[str] = None,
        usage: Optional[Dict[str, int]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        prompt_name: Optional[str] = None,
        prompt_version: Optional[int] = None,
        prompt_version_id: Optional[str] = None,
        prompt_labels: Optional[list] = None,
    ):
        """记录模型生成调用

        P1 调试增强: 支持 prompt 版本绑定,便于在 Langfuse UI 中追溯
        某次评估使用了哪个 prompt 版本 (对标 Langfuse Prompt Management 集成)。

        Args:
            prompt_name: Prompt 模板名 (如 "daily_evaluation")
            prompt_version: Prompt 版本号 (如 3)
            prompt_version_id: Prompt 版本 ID (DB 主键,可跳转到管理 UI)
            prompt_labels: 该版本携带的 label (如 ["production", "latest"])
        """
        if not self._enabled or not self._client or parent is None:
            return NoOpTrace()

        # P1 调试增强: 把 prompt 版本信息合并到 metadata,Langfuse UI 可按此过滤
        full_metadata = metadata or {}
        if prompt_name:
            full_metadata["prompt_name"] = prompt_name
        if prompt_version is not None:
            full_metadata["prompt_version"] = prompt_version
        if prompt_version_id:
            full_metadata["prompt_version_id"] = prompt_version_id
        if prompt_labels:
            full_metadata["prompt_labels"] = prompt_labels

        gen = parent.generation(
            name=name,
            input=prompt,
            output=completion,
            model=model,
            usage=usage,
            metadata=full_metadata,
        )
        return gen

    def current_trace_id(self) -> Optional[str]:
        """返回当前 contextvar 中的 trace_id，未处于 trace 上下文时返回 None。

        P3-1：供 logging_config.get_log_context() 平铺到日志顶层，实现日志与
        Langfuse trace 的关联查询。trace() 进入时 set，退出时 reset。
        """
        return _current_trace_id.get()

    def close(self) -> None:
        """刷新并关闭 Langfuse 客户端"""
        if self._enabled and self._client:
            try:
                self._client.flush()
                self._client.shutdown()
            except Exception as e:
                logger.warning(f"Langfuse 客户端关闭失败: {e}")
        self._enabled = False
        self._client = None


# 全局单例
tracer = LangfuseTracer()
