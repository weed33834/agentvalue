"""
进程内事件总线

移植自 opencode (TypeScript/Effect) 的 packages/opencode/src/event-v2-bridge.ts
opencode 用 EventV2Bridge + GlobalBus 做跨实例事件广播，这里简化为进程内 asyncio 事件总线。

用途：
- 多个 SSE 客户端订阅同一 session 的事件（后续扩展）
- 后台任务推送事件给前端（与 SSE 解耦）
- MVP 阶段可不用，单 SSE 直接从 queue 推；留接口供后续接入

设计要点：
- subscribe 返回 unsubscribe 函数，方便清理
- publish 是 async，handler 异常不阻断其他订阅者
- 按 channel 隔离（如 session:{id}）
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable, Dict, List

logger = logging.getLogger(__name__)

Handler = Callable[[Any], Awaitable[None]]


class EventBus:
    """进程内事件总线（对齐 opencode EventV2Bridge 的 publish/listen 语义）"""

    def __init__(self) -> None:
        self._subscribers: Dict[str, List[Handler]] = defaultdict(list)
        self._lock = asyncio.Lock()

    def subscribe(self, channel: str, handler: Handler) -> Callable[[], None]:
        """订阅 channel，返回 unsubscribe 函数。"""
        self._subscribers[channel].append(handler)

        def unsubscribe() -> None:
            try:
                self._subscribers[channel].remove(handler)
            except ValueError:
                pass  # 已移除

        return unsubscribe

    async def publish(self, channel: str, payload: Any) -> None:
        """向 channel 发布事件，所有订阅者异步收到。"""
        handlers = list(self._subscribers.get(channel, []))
        for handler in handlers:
            try:
                await handler(payload)
            except Exception:
                logger.exception("event bus handler 异常 channel=%s", channel)

    def subscriber_count(self, channel: str) -> int:
        return len(self._subscribers.get(channel, []))


# 进程级单例
_global_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """获取进程级 EventBus 单例。"""
    global _global_bus
    if _global_bus is None:
        _global_bus = EventBus()
    return _global_bus
