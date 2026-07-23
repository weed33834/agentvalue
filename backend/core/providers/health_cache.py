"""Provider 健康检查结果缓存。

问题: ModelRouter.get_provider_with_fallback() 每次都调 health_check(),
而 health_check() 会打一次 /models 接口,生产环境高频评估时会浪费上游配额
与延迟。

方案: 缓存 health_check 结果 TTL=30s,30s 内重复调用直接返回缓存值。
失败结果缓存 TTL=10s(更短,避免故障 Provider 被缓存太久拖慢降级)。

参考:
- LiteLLM 用 Redis 缓存跨副本共享,本项目用进程内 TTL 缓存(单实例够用)
- 多副本共享留 P2,Redis 实现
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# 默认 TTL: 成功 30s,失败 10s(失败更短,让故障 Provider 更快重试)
_DEFAULT_SUCCESS_TTL = 30.0
_DEFAULT_FAILURE_TTL = 10.0


@dataclass
class _CacheEntry:
    healthy: bool
    response_time: float
    cached_at: float
    ttl: float

    def is_expired(self) -> bool:
        return time.monotonic() - self.cached_at > self.ttl


class HealthCheckCache:
    """进程内 TTL 缓存,按 provider key 隔离。

    线程安全: asyncio.Lock 保护单个 key 的并发刷新(避免雷群)。
    """

    def __init__(
        self,
        success_ttl: float = _DEFAULT_SUCCESS_TTL,
        failure_ttl: float = _DEFAULT_FAILURE_TTL,
    ):
        self.success_ttl = success_ttl
        self.failure_ttl = failure_ttl
        self._cache: dict[str, _CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def get(self, key: str) -> Optional[_CacheEntry]:
        """读缓存,过期或不存在返回 None"""
        entry = self._cache.get(key)
        if entry is None or entry.is_expired():
            return None
        return entry

    def set(self, key: str, healthy: bool, response_time: float) -> None:
        """写缓存,TTL 按成功/失败区分"""
        ttl = self.success_ttl if healthy else self.failure_ttl
        self._cache[key] = _CacheEntry(
            healthy=healthy,
            response_time=response_time,
            cached_at=time.monotonic(),
            ttl=ttl,
        )

    def invalidate(self, key: str) -> None:
        """显式失效某个 key(如手动重试后)"""
        self._cache.pop(key, None)

    def clear(self) -> None:
        """清空所有缓存(测试间状态隔离)"""
        self._cache.clear()

    async def get_or_refresh(
        self,
        key: str,
        refresh_fn,
    ) -> tuple[bool, float]:
        """读缓存,过期则调 refresh_fn 刷新。

        refresh_fn 是 async callable 返回 (healthy, response_time)。
        用锁避免并发刷新同一 key 的雷群。
        """
        cached = self.get(key)
        if cached is not None:
            return cached.healthy, cached.response_time

        async with self._get_lock(key):
            # 二次检查(可能其他协程已刷新)
            cached = self.get(key)
            if cached is not None:
                return cached.healthy, cached.response_time

            # 真正调 health_check
            start = time.monotonic()
            try:
                healthy = await refresh_fn()
            except Exception as e:
                logger.debug("health_check 异常 key=%s: %s", key, e)
                healthy = False
            elapsed = time.monotonic() - start
            self.set(key, healthy, elapsed)
            return healthy, elapsed


# 全局单例
_global_cache: Optional[HealthCheckCache] = None


def get_global_health_cache() -> HealthCheckCache:
    global _global_cache
    if _global_cache is None:
        _global_cache = HealthCheckCache()
    return _global_cache
