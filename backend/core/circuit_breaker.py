"""LLM Provider 状态机熔断器。

参考: LiteLLM Redis Circuit Breaker (https://docs.litellm.ai/blog/redis-circuit-breaker)

核心模式:
    CLOSED → 5 次连续失败 → OPEN(0ms fast-fail) → 60s 后 HALF-OPEN(放探针)
    → 探针成功 → CLOSED; 探针失败 → OPEN

设计要点:
- 支持 Redis 分布式状态(多实例共享) + 进程内降级(Redis 不可用时回退)
- 线程安全: 用 asyncio.Lock 保护状态切换(进程内) / Redis 原子操作(分布式)
- 装饰器用法: @circuit_breaker_guard(circuit) 包装 async 调用
- 失败判定: 由调用方在 except 中显式 record_failure,成功时 record_success
- 与 OpenAICompatibleProvider._retry 解耦: _retry 处理瞬时错误(连接/限流),
  熔断器处理持续性故障(整个 Provider 不可达)
"""

from __future__ import annotations

import asyncio
import json
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


class RedisCircuitState:
    """P0: 基于 Redis 的分布式熔断器状态

    与 CircuitState 接口兼容,但状态存储在 Redis 中实现多实例共享:
    - failure_count: Redis INCR 原子递增,成功时 DEL 清零
    - state: Redis GET/SET,包含 opened_at 时间戳
    - probe: Redis SET NX 实现分布式探针锁

    降级策略: Redis 操作失败时回退到进程内状态(使用内部 CircuitState)
    """

    def __init__(
        self,
        redis_client: Any,
        key: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ):
        self._redis = redis_client
        self._key = f"cb:{key}"
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        # 进程内降级实例(Redis 不可用时使用)
        self._fallback = CircuitState(
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
        )
        # 缓存状态供 all_states() 同步读取
        self._cached_state = "closed"

    def is_open(self) -> bool:
        """同步检查: 基于 _cached_state 判断(异步更新在 record_success/failure 中)"""
        return self._cached_state == "open"

    async def acquire_probe(self) -> bool:
        """HALF_OPEN 状态下尝试获取探针资格(Redis 分布式锁)"""
        try:
            # 用 SET NX 实现分布式探针锁(60s 过期防死锁)
            result = await self._redis.set(
                f"{self._key}:probe", "1", nx=True, ex=int(self.recovery_timeout)
            )
            if result:
                self._cached_state = "half_open"
                return True
            return False
        except Exception as e:
            logger.warning("Redis 探针锁失败,降级进程内: %s", e)
            return await self._fallback.acquire_probe()

    async def record_success(self) -> None:
        """记录成功: 清零失败计数, 状态转 closed"""
        try:
            pipe = self._redis.pipeline()
            pipe.delete(f"{self._key}:failures")
            pipe.delete(f"{self._key}:state")
            pipe.delete(f"{self._key}:probe")
            await pipe.execute()
            self._cached_state = "closed"
        except Exception as e:
            logger.warning("Redis record_success 失败,降级进程内: %s", e)
            await self._fallback.record_success()
            self._cached_state = self._fallback.state

    async def record_failure(self) -> None:
        """记录失败: INCR 失败计数, 达阈值则熔断"""
        try:
            # 原子递增失败计数
            count = await self._redis.incr(f"{self._key}:failures")
            # 设置失败计数 TTL(避免永久残留)
            if count == 1:
                await self._redis.expire(
                    f"{self._key}:failures", int(self.recovery_timeout * 2)
                )

            if count >= self.failure_threshold:
                # 达阈值: 打开熔断
                state_data = json.dumps({
                    "state": "open",
                    "opened_at": time.time(),
                })
                await self._redis.set(
                    f"{self._key}:state",
                    state_data,
                    ex=int(self.recovery_timeout * 2),
                )
                self._cached_state = "open"
                logger.warning(
                    "分布式熔断器打开: %s (连续失败 %d 次,阈值 %d)",
                    self._key,
                    count,
                    self.failure_threshold,
                )
            else:
                self._cached_state = "closed"
        except Exception as e:
            logger.warning("Redis record_failure 失败,降级进程内: %s", e)
            await self._fallback.record_failure()
            self._cached_state = self._fallback.state

    async def check_and_transition(self) -> None:
        """异步状态检查: 从 Redis 读取最新状态, 处理 OPEN→HALF_OPEN 转换

        应在 is_open() 之前调用(如果需要最新状态)。
        """
        try:
            state_raw = await self._redis.get(f"{self._key}:state")
            if state_raw is None:
                self._cached_state = "closed"
                return

            state_data = json.loads(state_raw)
            opened_at = state_data.get("opened_at", 0)

            if time.time() - opened_at > self.recovery_timeout:
                # 恢复时间到: 转 HALF_OPEN
                self._cached_state = "half_open"
            else:
                self._cached_state = state_data.get("state", "closed")
        except Exception as e:
            logger.debug("Redis 状态检查失败,使用缓存: %s", e)


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

    P0 升级: 支持 Redis 分布式状态共享(多实例部署时所有副本看到同一熔断状态)。
    Redis 不可用时自动降级为进程内状态(单实例行为)。
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        redis_client: Any = None,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._redis = redis_client
        self._circuits: dict[str, CircuitState] = {}
        self._redis_circuits: dict[str, RedisCircuitState] = {}

    def get_or_create(self, key: str) -> CircuitState | RedisCircuitState:
        """按 key 获取或创建熔断器(每个 provider/tier 独立熔断)

        Redis 可用时返回 RedisCircuitState(多实例共享状态),
        否则返回进程内 CircuitState(降级)。
        """
        if self._redis is not None:
            if key not in self._redis_circuits:
                self._redis_circuits[key] = RedisCircuitState(
                    redis_client=self._redis,
                    key=key,
                    failure_threshold=self.failure_threshold,
                    recovery_timeout=self.recovery_timeout,
                )
            return self._redis_circuits[key]

        if key not in self._circuits:
            self._circuits[key] = CircuitState(
                failure_threshold=self.failure_threshold,
                recovery_timeout=self.recovery_timeout,
            )
        return self._circuits[key]

    def all_states(self) -> dict[str, str]:
        """返回所有熔断器的当前状态,供 /admin/model-status 暴露"""
        states = {k: v.state for k, v in self._circuits.items()}
        # Redis 熔断器状态是异步读取的,这里返回缓存的状态
        for k, v in self._redis_circuits.items():
            states[k] = v._cached_state
        return states

    def set_redis_client(self, redis_client: Any) -> None:
        """动态设置 Redis 客户端(在 lifespan 中 Redis 连接成功后调用)"""
        self._redis = redis_client
        logger.info("熔断器注册表已切换到 Redis 分布式模式")


# 全局单例
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
    circuit: CircuitState | RedisCircuitState,
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

    支持 CircuitState(进程内) 和 RedisCircuitState(分布式) 两种实现。
    """
    # Redis 熔断器: 先异步检查最新状态
    if hasattr(circuit, "check_and_transition"):
        await circuit.check_and_transition()

    if circuit.is_open():
        raise fast_fail_exc(
            f"熔断器处于 OPEN 状态,fast-fail (state={circuit.state if hasattr(circuit, 'state') else getattr(circuit, '_cached_state', 'unknown')})"
        )
    state = getattr(circuit, "state", None) or getattr(circuit, "_cached_state", "closed")
    if state == "half_open":
        # 仅放一个探针
        if not await circuit.acquire_probe():
            raise fast_fail_exc("熔断器 HALF_OPEN 状态,已有探针在飞行中")
    try:
        result = await coro_fn()
        await circuit.record_success()
        return result
    except Exception:
        await circuit.record_failure()
        raise
