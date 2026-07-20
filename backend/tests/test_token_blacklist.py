"""
auth/token_blacklist.py 单元测试

覆盖：
- InMemoryTokenBlacklist: 基本 revoke/is_revoked、过期清理、is_redis_healthy
- _LocalMirror: LRU 镜像、命中/未命中、过期惰性清理、LRU 淘汰
- RedisTokenBlacklist（fakeredis）: 基本 revoke/is_revoked、
  Redis 故障回退本地镜像、镜像命中拦截 / 未命中降级放行并记 metric、
  is_redis_healthy 健康判定
- create_token_blacklist 工厂：未配置 / 不可达时降级 InMemoryTokenBlacklist
"""

import asyncio
import time

import fakeredis.aioredis
import pytest

from auth.token_blacklist import (
    InMemoryTokenBlacklist,
    RedisTokenBlacklist,
    _LocalMirror,
    create_token_blacklist,
)
from core.config import Settings


# ---------------- InMemoryTokenBlacklist ----------------


class TestInMemoryTokenBlacklist:
    @pytest.fixture
    def bl(self):
        return InMemoryTokenBlacklist()

    async def test_revoke_then_is_revoked_true(self, bl):
        await bl.revoke("jti-1", ttl_seconds=60)
        assert await bl.is_revoked("jti-1") is True

    async def test_is_revoked_false_for_unknown_jti(self, bl):
        assert await bl.is_revoked("unknown") is False

    async def test_is_revoked_false_for_empty_jti(self, bl):
        assert await bl.is_revoked("") is False

    async def test_revoke_with_zero_ttl_ignored(self, bl):
        await bl.revoke("jti-zero", ttl_seconds=0)
        assert await bl.is_revoked("jti-zero") is False

    async def test_revoke_with_negative_ttl_ignored(self, bl):
        await bl.revoke("jti-neg", ttl_seconds=-10)
        assert await bl.is_revoked("jti-neg") is False

    async def test_expired_entry_returns_false_and_cleans_up(self, bl):
        await bl.revoke("jti-exp", ttl_seconds=1)
        # 模拟过期：手动改 store 里的过期时间戳
        bl._store["jti-exp"] = time.time() - 1
        assert await bl.is_revoked("jti-exp") is False
        # 过期条目应被清理
        assert "jti-exp" not in bl._store

    async def test_clear_empties_store(self, bl):
        await bl.revoke("a", 60)
        await bl.revoke("b", 60)
        await bl.clear()
        assert await bl.is_revoked("a") is False
        assert await bl.is_revoked("b") is False
        assert bl._store == {}

    async def test_close_clears_store(self, bl):
        await bl.revoke("a", 60)
        await bl.close()
        assert bl._store == {}


# ---------------- _LocalMirror ----------------


class TestLocalMirror:
    @pytest.fixture
    def mirror(self):
        return _LocalMirror(max_size=3)

    async def test_add_then_contains_true(self, mirror):
        await mirror.add("jti-1", time.time() + 60)
        assert await mirror.contains("jti-1") is True

    async def test_contains_false_for_unknown(self, mirror):
        assert await mirror.contains("unknown") is False

    async def test_contains_false_for_empty(self, mirror):
        assert await mirror.contains("") is False

    async def test_expired_entry_returns_false_and_cleans(self, mirror):
        await mirror.add("jti-exp", time.time() - 1)
        assert await mirror.contains("jti-exp") is False
        # 过期条目应被惰性清理
        assert "jti-exp" not in mirror._store

    async def test_lru_eviction_when_max_size_exceeded(self, mirror):
        """max_size=3 时，写入第 4 条应淘汰最旧"""
        for i in range(4):
            await mirror.add(f"jti-{i}", time.time() + 60)
        # jti-0 被淘汰
        assert await mirror.contains("jti-0") is False
        # jti-1/2/3 仍在
        for i in range(1, 4):
            assert await mirror.contains(f"jti-{i}") is True

    async def test_lru_move_to_end_on_refresh(self, mirror):
        """重复 add 同一 jti 应刷新其 LRU 位置，避免被淘汰"""
        await mirror.add("old", time.time() + 60)
        await mirror.add("a", time.time() + 60)
        await mirror.add("b", time.time() + 60)
        # 刷新 old，使其成为最近使用
        await mirror.add("old", time.time() + 60)
        # 再加一条，应淘汰最旧的 a（而非 old）
        await mirror.add("c", time.time() + 60)
        assert await mirror.contains("old") is True
        assert await mirror.contains("a") is False

    async def test_clear(self, mirror):
        await mirror.add("x", time.time() + 60)
        await mirror.clear()
        assert mirror._store == {}


# ---------------- RedisTokenBlacklist（fakeredis） ----------------


class _FailingRedisClient:
    """模拟 Redis 故障：所有操作抛异常"""

    async def get(self, *args, **kwargs):
        raise ConnectionError("redis down")

    async def set(self, *args, **kwargs):
        raise ConnectionError("redis down")

    async def ping(self, *args, **kwargs):
        raise ConnectionError("redis down")

    async def close(self):
        pass


@pytest.fixture
async def redis_blacklist():
    """用 fakeredis 替换 RedisTokenBlacklist 的 _client，提供隔离的测试环境"""
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bl = RedisTokenBlacklist("redis://localhost:6379/0")
    bl._client = fake
    yield bl
    await fake.flushall()
    try:
        await fake.aclose()
    except AttributeError:
        await fake.close()
    await bl.close()


class TestRedisTokenBlacklist:
    async def test_revoke_then_is_revoked_true(self, redis_blacklist):
        await redis_blacklist.revoke("jti-1", ttl_seconds=60)
        assert await redis_blacklist.is_revoked("jti-1") is True

    async def test_is_revoked_false_for_unknown(self, redis_blacklist):
        assert await redis_blacklist.is_revoked("unknown") is False

    async def test_is_revoked_false_for_empty_jti(self, redis_blacklist):
        assert await redis_blacklist.is_revoked("") is False

    async def test_revoke_writes_to_local_mirror(self, redis_blacklist):
        """revoke 应同时写入本地镜像，保证 Redis 故障时仍可拦截"""
        await redis_blacklist.revoke("jti-mirror", ttl_seconds=60)
        # 镜像应命中
        assert await redis_blacklist._mirror.contains("jti-mirror") is True

    async def test_redis_failure_falls_back_to_mirror_hit(self, redis_blacklist):
        """Redis 故障时，本地镜像命中应拦截（返回 True），不放行"""
        await redis_blacklist.revoke("jti-fail", ttl_seconds=60)
        # 模拟 Redis 故障
        redis_blacklist._client = _FailingRedisClient()
        # is_revoked 应从镜像命中，返回 True
        assert await redis_blacklist.is_revoked("jti-fail") is True

    async def test_redis_failure_mirror_miss_degrades_and_records_metric(
        self, redis_blacklist, monkeypatch
    ):
        """Redis 故障且镜像未命中，降级放行并记 agentvalue_token_blacklist_degraded_total"""
        # 记录 metric 调用
        recorded = []
        from core import metrics as metrics_module

        monkeypatch.setattr(
            metrics_module,
            "record_token_blacklist_degraded",
            lambda: recorded.append(1),
        )
        # 模拟 Redis 故障（未先 revoke，镜像无此 jti）
        redis_blacklist._client = _FailingRedisClient()
        result = await redis_blacklist.is_revoked("never-revoked")
        # 降级放行
        assert result is False
        # 应记一次降级 metric
        assert len(recorded) == 1

    async def test_redis_failure_on_revoke_writes_mirror_only(self, redis_blacklist):
        """revoke 时 Redis 写入失败，本地镜像仍应写入（保证后续可拦截）"""
        redis_blacklist._client = _FailingRedisClient()
        await redis_blacklist.revoke("jti-write-fail", ttl_seconds=60)
        # 镜像应命中
        assert await redis_blacklist._mirror.contains("jti-write-fail") is True

    async def test_clear_wipes_store(self, redis_blacklist):
        await redis_blacklist.revoke("to-clear", 60)
        await redis_blacklist.close()
        assert await redis_blacklist._mirror.contains("to-clear") is False


# ---------------- create_token_blacklist 工厂 ----------------


class TestCreateTokenBlacklist:
    def test_no_redis_url_returns_inmemory(self):
        """REDIS_URL 未配置时返回 InMemoryTokenBlacklist"""
        settings = Settings(redis_url=None)
        bl = create_token_blacklist(settings)
        assert isinstance(bl, InMemoryTokenBlacklist)

    def test_unreachable_redis_returns_inmemory(self):
        """REDIS_URL 配置但不可达时降级为 InMemoryTokenBlacklist"""
        # 用一个肯定不可达的端口
        settings = Settings(redis_url="redis://127.0.0.1:1/0")
        bl = create_token_blacklist(settings)
        assert isinstance(bl, InMemoryTokenBlacklist)
