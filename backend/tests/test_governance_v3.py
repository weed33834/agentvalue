"""WS-4 企业级治理加固测试集

覆盖三个交付物：
1. Redis 分布式令牌桶限流器（core/redis_rate_limit.py）
   - 令牌桶数学（伪造时钟 + fakeredis 跑真实 Lua 脚本）
   - 多维度「全部通过才放行」
   - Redis 不可用降级（fail-open → slowapi 兜底）
   - 标准限流头 / 重置桶
2. 租户查询守卫（core/tenant_guard.py）
   - warn 模式只告警不拦截 / enforce 模式抛异常
   - allow_cross_tenant 逃生舱
   - 语句级检查 check_statement
3. 沙箱资源限制组装（agent/code_interpreter.py）
   - _build_rlimits 默认值 / 关闭 / 自定义 / preexec_fn 平台守卫

pytest.ini 已配置 ``asyncio_mode=auto``，async 测试无需装饰器。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from core.config import Settings
from core.redis_rate_limit import RedisRateLimiter
from models.models import Evaluation

# ---------------------------------------------------------------------------
# 工具：伪造 settings + fakeredis + 可拨动的假时钟
# ---------------------------------------------------------------------------


def make_fake_settings(**overrides):
    """构造最小限流 settings（WS-4 配置项默认值，可覆盖）。"""
    defaults = dict(
        redis_url="redis://fake:6379",
        rate_limit_tenant_capacity=10,
        rate_limit_tenant_refill=1.0,
        rate_limit_api_key_capacity=10,
        rate_limit_api_key_refill=1.0,
        rate_limit_user_capacity=10,
        rate_limit_user_refill=1.0,
        rate_limit_endpoint_capacity=10,
        rate_limit_endpoint_refill=1.0,
        redis_rate_limit_default_per_minute=120,
        rate_limit_degrade_log_interval=60,
        redis_rate_limit_enabled=True,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class FakeClock:
    """可拨动的假时钟，替代 time.time 注入限流器。"""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fakeredis_client():
    import fakeredis.aioredis

    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def make_limiter(settings, client, clock) -> RedisRateLimiter:
    return RedisRateLimiter(settings=settings, client=client, clock=clock)


# ---------------------------------------------------------------------------
# 1. Redis 令牌桶
# ---------------------------------------------------------------------------


class TestTokenBucket:
    async def test_burst_and_refill(self, fakeredis_client):
        """容量=10 连续打 10 发全过，第 11 发被拒；时间流逝后按 refill 恢复。"""
        clock = FakeClock()
        limiter = make_limiter(make_fake_settings(), fakeredis_client, clock)

        for i in range(10):
            r = await limiter.check(tenant="t1")
            assert r.allowed, f"第 {i+1} 发应放行"
            assert not r.degraded

        blocked = await limiter.check(tenant="t1")
        assert not blocked.allowed
        assert blocked.dimension == "tenant"
        # 拒发时需等 (1 - tokens)/refill ≈ 1s
        assert blocked.retry_after >= 1

        # 拨快 2s → 补 2 个令牌 → 可再放行 2 发
        clock.advance(2.0)
        assert (await limiter.check(tenant="t1")).allowed
        assert (await limiter.check(tenant="t1")).allowed
        assert not (await limiter.check(tenant="t1")).allowed

    async def test_multi_dimension_all_buckets_must_pass(self, fakeredis_client):
        """租户桶打满但 api_key 桶是满的：请求仍被租户维度拒绝。"""
        clock = FakeClock()
        limiter = make_limiter(make_fake_settings(), fakeredis_client, clock)
        for _ in range(10):
            await limiter.check(tenant="t1", api_key="k1")
        blocked = await limiter.check(tenant="t1", api_key="k1")
        assert not blocked.allowed
        assert blocked.dimension == "tenant"

        # 换个租户但同 api_key → api_key 桶已耗尽 → 照样被拒（四维独立）
        blocked2 = await limiter.check(tenant="t2", api_key="k1")
        assert not blocked2.allowed
        assert blocked2.dimension == "api_key"

    async def test_dimensions_are_independent(self, fakeredis_client):
        """不同维度/不同键的桶互不影响。"""
        clock = FakeClock()
        limiter = make_limiter(make_fake_settings(), fakeredis_client, clock)
        for _ in range(10):
            await limiter.check(tenant="t1", endpoint="/api/v1/chat")
        # endpoint 是独立桶, 仍可放行
        assert (await limiter.check(endpoint="/api/v1/other")).allowed

    async def test_headers_and_reset(self, fakeredis_client):
        clock = FakeClock()
        limiter = make_limiter(make_fake_settings(), fakeredis_client, clock)
        r = await limiter.check(tenant="t1")
        headers = r.to_headers()
        assert headers["X-RateLimit-Limit"] == "10"
        assert "X-RateLimit-Remaining" in headers
        assert "X-RateLimit-Reset" in headers
        # 满桶打第一发后 remaining 应为 9（最紧桶约束真实反映）
        assert headers["X-RateLimit-Remaining"] == "9"

        # 重置后满桶
        assert (await limiter.reset_bucket("tenant", "t1")) is True
        assert (await limiter.bucket_state("tenant", "t1")) is None
        fresh = await limiter.check(tenant="t1")
        assert fresh.allowed

    async def test_invalid_dimension_reset_fails(self, fakeredis_client):
        clock = FakeClock()
        limiter = make_limiter(make_fake_settings(), fakeredis_client, clock)
        assert (await limiter.reset_bucket("ip", "1.2.3.4")) is False
        assert (await limiter.list_buckets("ip")) == ([], 0)

    def test_degraded_when_no_redis_url(self):
        """REDIS_URL 未配置: 置降级标志, check fail-open, is_redis_available False。"""
        settings = make_fake_settings(redis_url=None)
        limiter = RedisRateLimiter(settings=settings)
        assert limiter.is_redis_available() is False
        assert limiter.get_status()["mode"] == "degraded"

    async def test_degraded_when_redis_down(self):
        """配置了 REDIS_URL 但连接失败: 探测后降级, check fail-open。"""
        settings = make_fake_settings(redis_url="redis://127.0.0.1:1")  # 必失败端口
        limiter = RedisRateLimiter(settings=settings, clock=FakeClock())
        # 构造时是乐观置位, 探测一次后翻为降级
        assert await limiter.probe_availability() is False
        assert limiter.is_redis_available() is False
        assert limiter.get_status()["active"] is False
        # 降级态 check 直接 fail-open
        r = await limiter.check(tenant="t1")
        assert r.allowed
        assert r.degraded


# ---------------------------------------------------------------------------
# 2. 租户查询守卫
# ---------------------------------------------------------------------------


class TestTenantGuard:
    @staticmethod
    def _make_state(stmt):
        return SimpleNamespace(is_select=True, statement=stmt)

    def test_check_statement_detects_missing_tenant(self):
        from core.tenant_guard import check_statement

        assert check_statement(select(Evaluation)) == "evaluations"
        assert (
            check_statement(
                select(Evaluation).where(Evaluation.tenant_id == "t1")
            )
            is None
        )

    def test_warn_mode_does_not_raise(self, monkeypatch):
        from core.config import get_settings
        from core.tenant_guard import (
            MODE_WARN,
            _current_mode,
            handle_orm_execute,
        )

        monkeypatch.setattr(get_settings(), "tenant_guard_mode", MODE_WARN)
        assert _current_mode() == MODE_WARN
        # warn 模式只打日志, 不抛异常
        handle_orm_execute(self._make_state(select(Evaluation)))

    def test_enforce_mode_raises(self, monkeypatch):
        from core.config import get_settings
        from core.tenant_guard import (
            MODE_ENFORCE,
            CrossTenantQueryError,
            handle_orm_execute,
        )

        monkeypatch.setattr(get_settings(), "tenant_guard_mode", MODE_ENFORCE)
        with pytest.raises(CrossTenantQueryError):
            handle_orm_execute(self._make_state(select(Evaluation)))

    def test_enforce_mode_passes_with_tenant_predicate(self, monkeypatch):
        from core.config import get_settings
        from core.tenant_guard import MODE_ENFORCE, handle_orm_execute

        monkeypatch.setattr(get_settings(), "tenant_guard_mode", MODE_ENFORCE)
        handle_orm_execute(
            self._make_state(
                select(Evaluation).where(Evaluation.tenant_id == "t1")
            )
        )  # 不应抛

    def test_allow_cross_tenant_escape_hatch(self, monkeypatch):
        from core.config import get_settings
        from core.tenant_guard import (
            MODE_ENFORCE,
            allow_cross_tenant,
            handle_orm_execute,
            is_cross_tenant_allowed,
        )

        monkeypatch.setattr(get_settings(), "tenant_guard_mode", MODE_ENFORCE)
        assert is_cross_tenant_allowed() is False
        with allow_cross_tenant("平台级 admin 统计"):
            assert is_cross_tenant_allowed() is True
            handle_orm_execute(self._make_state(select(Evaluation)))  # 不应抛
        assert is_cross_tenant_allowed() is False
        # 逃生舱外重新受 enforce 约束
        with pytest.raises(Exception):
            handle_orm_execute(self._make_state(select(Evaluation)))

    def test_guard_disabled_totally(self, monkeypatch):
        from core.config import get_settings
        from core.tenant_guard import MODE_OFF, _current_mode, handle_orm_execute

        monkeypatch.setattr(get_settings(), "tenant_guard_enabled", False)
        assert _current_mode() == MODE_OFF
        handle_orm_execute(self._make_state(select(Evaluation)))  # 不应抛

    def test_default_mode_is_warn(self):
        from core.tenant_guard import MODE_WARN, _current_mode

        assert _current_mode() == MODE_WARN


# ---------------------------------------------------------------------------
# 3. 沙箱资源限制组装
# ---------------------------------------------------------------------------


class TestSandboxRlimits:
    def test_build_rlimits_defaults(self):
        from agent.code_interpreter import _build_rlimits

        import resource

        limits = _build_rlimits()
        assert len(limits) == 5
        assert limits[resource.RLIMIT_AS] == (512 * 1024 * 1024,) * 2
        assert limits[resource.RLIMIT_CPU] == (10, 10)
        assert limits[resource.RLIMIT_NOFILE] == (64, 64)
        assert limits[resource.RLIMIT_FSIZE] == (16 * 1024 * 1024,) * 2
        assert limits[resource.RLIMIT_NPROC] == (32, 32)

    def test_build_rlimits_disabled(self):
        from agent.code_interpreter import _build_rlimits

        assert _build_rlimits(Settings(sandbox_rlimit_enabled=False)) == {}

    def test_build_rlimits_custom(self):
        from agent.code_interpreter import _build_rlimits

        import resource

        limits = _build_rlimits(
            Settings(
                sandbox_max_memory_mb=128,
                sandbox_max_cpu_seconds=5,
                sandbox_max_open_files=8,
                sandbox_max_file_size_mb=2,
                sandbox_max_processes=4,
            )
        )
        assert limits[resource.RLIMIT_AS] == (128 * 1024 * 1024,) * 2
        assert limits[resource.RLIMIT_CPU] == (5, 5)
        assert limits[resource.RLIMIT_NOFILE] == (8, 8)
        assert limits[resource.RLIMIT_FSIZE] == (2 * 1024 * 1024,) * 2
        assert limits[resource.RLIMIT_NPROC] == (4, 4)

    def test_preexec_fn_platform_guard(self):
        from agent.code_interpreter import _build_preexec_fn

        fn = _build_preexec_fn()
        if sys.platform == "win32":
            assert fn is None
        else:
            assert callable(fn)
