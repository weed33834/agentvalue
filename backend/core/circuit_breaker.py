"""LLM Provider 状态机熔断器。

参考: LiteLLM Redis Circuit Breaker (https://docs.litellm.ai/blog/redis-circuit-breaker)

核心模式:
    CLOSED → 5 次连续失败 → OPEN(0ms fast-fail) → 60s 后 HALF-OPEN(放探针)
    → 探针成功 → CLOSED; 探针失败 → OPEN

设计要点:
- 进程内状态(单实例够用),Redis 共享状态留 P2 多实例场景实现
- 线程安全: 用 asyncio.Lock 保护状态切换
- 装饰器用法: @circuit_breaker_guard(circuit) 包装 async 调用
- 失败判定: 由调用方在 except 中显式 record_failure,成功时 record_success
- 与 OpenAICompatibleProvider._retry 解耦: _retry 处理瞬时错误(连接/限流),
  熔断器处理持续性故障(整个 Provider 不可达)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class CircuitState:
    """熔断器状态(进程内单例,按 key 隔离)"""

    # LiteLLM 默认值: 5 次失败熔断,60s 后探活
    failure_threshold: int = 5
    recovery_timeout: float = 60.0
    # 当前状态: closed / open / half_open
    state: str = "closed"
    # 连续失败计数(成功时清零)
    _failure_count: int = 0
    # 进入 OPEN 状态的时间戳(用于判断是否到 recovery_timeout)
    _opened_at: float = 0.0
    # HALF_OPEN 状态下是否已派探针(避免并发多个探针)
    _probe_in_flight: bool = False
    # 保护状态切换的锁
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def is_open(self) -> bool:
        """是否处于熔断打开状态(调用方应 fast-fail)。

        HALF_OPEN 状态返回 False(允许探针通过),由 acquire_probe 确保只有一个探针。
        """
        if self.state == "open":
            # 检查是否到恢复时间
            if time.monotonic() - self._opened_at > self.recovery_timeout:
                # 状态转 HALF_OPEN(由 record_success/failure 完成最终转换)
                self.state = "half_open"
                return False
            return True
        return False

    async def acquire_probe(self) -> bool:
        """HALF_OPEN 状态下尝试获取探针资格。

        返回 True 表示当前调用是探针,可放行;False 表示已被其他并发请求抢走探针资格,
        当前请求应 fast-fail。
        """
        async with self._lock:
            if self.state != "half_open":
                return False
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
            return True

    async def record_success(self) -> None:
        """记录一次成功调用,重置失败计数 + 关闭熔断"""
        async with self._lock:
            self._failure_count = 0
            self._probe_in_flight = False
            if self.state in ("open", "half_open"):
                logger.info(
                    "熔断器恢复: %s → closed (success probe)", self.state
                )
            self.state = "closed"

    async def record_failure(self) -> None:
        """记录一次失败调用,达到阈值则熔断"""
        async with self._lock:
            self._probe_in_flight = False
            self._failure_count += 1
            if self.state == "half_open":
                # 探针失败: 重新打开熔断
                self.state = "open"
                self._opened_at = time.monotonic()
                logger.warning("熔断器探针失败: half_open → open")
                return
            if self._failure_count >= self.failure_threshold:
                self.state = "open"
                self._opened_at = time.monotonic()
                logger.warning(
                    "熔断器打开: closed → open (连续失败 %d 次,阈值 %d)",
                    self._failure_count,
                    self.failure_threshold,
                )


class CircuitBreakerRegistry:
    """按 key 维护多个熔断器实例(如按 provider_name / tier 分组)。

    用法:
        registry = CircuitBreakerRegistry()
        circuit = registry.get_or_create("openai/L0")
        if circuit.is_open():
            raise RuntimeError("provider 熔断中,请稍后再试")
        try:
            result = await provider.chat_completion(...)
            await circuit.record_success()
        except Exception:
            await circuit.record_failure()
            raise
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._circuits: dict[str, CircuitState] = {}

    def get_or_create(self, key: str) -> CircuitState:
        """按 key 获取或创建熔断器(每个 provider/tier 独立熔断)"""
        if key not in self._circuits:
            self._circuits[key] = CircuitState(
                failure_threshold=self.failure_threshold,
                recovery_timeout=self.recovery_timeout,
            )
        return self._circuits[key]

    def all_states(self) -> dict[str, str]:
        """返回所有熔断器的当前状态,供 /admin/model-status 暴露"""
        return {k: v.state for k, v in self._circuits.items()}


# 全局单例(进程内)。多副本共享状态留 P2,Redis 实现)
_global_registry: Optional[CircuitBreakerRegistry] = None


def get_global_registry() -> CircuitBreakerRegistry:
    """获取全局熔断器注册表单例。

    默认参数参考 LiteLLM 生产调优建议:
    - failure_threshold=5 (LiteLLM 默认值,生产环境可改 2 加快熔断)
    - recovery_timeout=60s (探活间隔)
    """
    global _global_registry
    if _global_registry is None:
        _global_registry = CircuitBreakerRegistry(
            failure_threshold=5,
            recovery_timeout=60.0,
        )
    return _global_registry


async def call_with_circuit(
    circuit: CircuitState,
    coro_fn: Callable[[], Awaitable[Any]],
    *,
    fast_fail_exc: type[Exception] = RuntimeError,
) -> Any:
    """用熔断器包装一次 async 调用。

    用法:
        result = await call_with_circuit(circuit, lambda: provider.chat_completion(...))

    逻辑:
    1. CLOSED: 直接调,成功 record_success,失败 record_failure
    2. OPEN: 抛 fast_fail_exc(0ms,不发网络请求)
    3. HALF_OPEN: acquire_probe 抢探针资格,抢到才调;抢不到 fast_fail
    """
    if circuit.is_open():
        raise fast_fail_exc(
            f"熔断器处于 OPEN 状态,fast-fail (state={circuit.state})"
        )
    if circuit.state == "half_open":
        # 仅放一个探针
        if not await circuit.acquire_probe():
            raise fast_fail_exc("熔断器 HALF_OPEN 状态,已有探针在飞行中")
    try:
        result = await coro_fn()
        await circuit.record_success()
        return result
    except Exception as e:
        await circuit.record_failure()
        raise
