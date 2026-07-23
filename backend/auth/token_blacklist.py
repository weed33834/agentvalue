"""
JWT Token 吊销黑名单

支持主动吊销已签发的 JWT（如用户登出、密码泄露应急），解决 JWT 签发后无法主动失效的安全风险。

存储后端：
- RedisTokenBlacklist: 多实例部署用,基于 redis.asyncio,jti 为 key,
  TTL = token 剩余有效期,token 自然过期后黑名单条目自动清除。
  Redis 故障时回退进程内 LRU 镜像（最近 1000 条），命中即拒绝，
  未命中才降级放行并记告警指标,避免「Redis 一挂全部放行」的安全空洞。
- InMemoryTokenBlacklist: 单实例/测试用,纯内存 dict + 过期时间戳。

工厂 create_token_blacklist 镜像 core/job_queue.py 的优雅降级模式：
REDIS_URL 配置且可达时用 Redis,否则降级内存态,Redis 故障不阻塞业务。
"""

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Protocol, runtime_checkable

from core.config import Settings, get_settings

logger = logging.getLogger(__name__)

KEY_PREFIX = "agentvalue:jwt_blacklist:"
# Redis 故障时本地 LRU 镜像的最大条目数
LOCAL_MIRROR_MAX_SIZE = 1000


@runtime_checkable
class TokenBlacklist(Protocol):
    """Token 黑名单抽象接口"""

    async def is_revoked(self, jti: str) -> bool:
        """查询 jti 是否已被吊销"""
        ...

    async def revoke(self, jti: str, ttl_seconds: int) -> None:
        """吊销 jti,TTL 为 token 剩余有效期(秒)"""
        ...

    async def close(self) -> None:
        """释放底层连接(内存实现为空操作)"""
        ...


class _LocalMirror:
    """进程内 LRU 镜像,Redis 故障时的最后防线。

    仅缓存最近 N 条吊销记录（默认 1000），超过 LRU 淘汰最旧。
    条目带过期时间戳,过期自动清理,避免镜像无限膨胀。
    """

    def __init__(self, max_size: int = LOCAL_MIRROR_MAX_SIZE) -> None:
        self._store: "OrderedDict[str, float]" = OrderedDict()
        self._max_size = max_size
        self._lock = asyncio.Lock()

    async def add(self, jti: str, expiry_ts: float) -> None:
        """添加/刷新一条吊销记录,LRU 淘汰最旧"""
        if not jti:
            return
        async with self._lock:
            self._store[jti] = expiry_ts
            self._store.move_to_end(jti)
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)

    async def contains(self, jti: str) -> bool:
        """查询是否命中,过期条目惰性清理"""
        if not jti:
            return False
        async with self._lock:
            expiry = self._store.get(jti)
            if expiry is None:
                return False
            if expiry <= time.time():
                self._store.pop(jti, None)
                return False
            return True

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()


class InMemoryTokenBlacklist:
    """单实例内存黑名单,store {jti: expiry_ts}。
    异步接口与 Redis 实现对齐,便于无缝切换。"""

    def __init__(self) -> None:
        self._store: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def is_revoked(self, jti: str) -> bool:
        if not jti:
            return False
        async with self._lock:
            expiry = self._store.get(jti)
            if expiry is None:
                return False
            if expiry <= time.time():
                # 条目已过期,清理并视为未吊销
                self._store.pop(jti, None)
                return False
            return True

    async def revoke(self, jti: str, ttl_seconds: int) -> None:
        if not jti or ttl_seconds <= 0:
            return
        async with self._lock:
            self._store[jti] = time.time() + ttl_seconds

    async def clear(self) -> None:
        """清空黑名单(测试间状态清理用)"""
        async with self._lock:
            self._store.clear()

    async def close(self) -> None:
        await self.clear()


class RedisTokenBlacklist:
    """Redis 黑名单,jti 作为 key,TTL = token 剩余有效期。
    token 自然过期后黑名单条目自动清除,Redis 不积压。

    安全加固：
    - revoke 时同时写入进程内 LRU 镜像,保证 Redis 故障期间仍可拦截最近吊销
    - is_revoked Redis 故障时回退本地镜像,命中即拒绝；未命中才降级放行
      并记录 agentvalue_token_blacklist_degraded_total 告警指标
    """

    def __init__(self, redis_url: str) -> None:
        import redis.asyncio as redis_asyncio

        self._client = redis_asyncio.from_url(redis_url, decode_responses=True)
        self._mirror = _LocalMirror()

    async def is_revoked(self, jti: str) -> bool:
        if not jti:
            return False
        try:
            result = bool(await self._client.get(KEY_PREFIX + jti))
            return result
        except Exception as e:
            # Redis 故障:回退本地镜像,命中即拒绝
            mirror_hit = await self._mirror.contains(jti)
            if mirror_hit:
                logger.warning(
                    "黑名单查询 Redis 故障,本地镜像命中,拒绝 jti=%s: %s", jti, e
                )
                return True
            # 本地镜像未命中:降级放行(rbac 仍校验签名与过期),记告警指标
            logger.warning(
                "黑名单查询 Redis 故障,本地镜像未命中,降级放行 jti=%s: %s", jti, e
            )
            try:
                from core.metrics import record_token_blacklist_degraded

                record_token_blacklist_degraded()
            except Exception:
                logger.debug("记录 token_blacklist 降级指标失败", exc_info=True)
            return False

    async def revoke(self, jti: str, ttl_seconds: int) -> None:
        if not jti or ttl_seconds <= 0:
            return
        # 先写本地镜像,保证 Redis 写入失败时仍可拦截该 jti
        await self._mirror.add(jti, time.time() + ttl_seconds)
        try:
            await self._client.set(KEY_PREFIX + jti, "1", ex=ttl_seconds)
        except Exception as e:
            logger.warning("黑名单写入 Redis 失败,已写入本地镜像 jti=%s: %s", jti, e)

    async def close(self) -> None:
        # redis-py 5.0.1+ 弃用异步客户端的 close(),改用 aclose();兼容旧版本回退
        aclose = getattr(self._client, "aclose", None)
        try:
            if aclose is not None:
                await aclose()
            else:
                await self._client.close()
        except Exception:
            logger.debug("关闭 Redis 客户端失败", exc_info=True)
        await self._mirror.clear()


def _can_connect_sync(redis_url: str) -> bool:
    """同步探测 Redis 可达性,与 core/job_queue 保持一致"""
    try:
        import redis as redis_sync

        client = redis_sync.from_url(
            redis_url, socket_timeout=1.0, socket_connect_timeout=1.0
        )
        try:
            client.ping()
            return True
        finally:
            client.close()
    except Exception as e:
        logger.debug("Redis 同步探测失败,将降级到内存黑名单: %s", e)
        return False


def create_token_blacklist(settings: Settings) -> TokenBlacklist:
    """工厂:REDIS_URL 配置且可达时用 Redis,否则降级内存态"""
    redis_url = settings.redis_url
    if redis_url and _can_connect_sync(redis_url):
        logger.info("Token 黑名单使用 Redis 存储")
        return RedisTokenBlacklist(redis_url)
    if redis_url:
        logger.warning("Redis 不可达,Token 黑名单降级为内存态(仅单实例可用)")
    else:
        logger.info("未配置 REDIS_URL,Token 黑名单使用内存态(仅单实例可用)")
    return InMemoryTokenBlacklist()


# 模块级单例,与 api/routes.py 的 job_queue 模式一致
token_blacklist: TokenBlacklist = create_token_blacklist(get_settings())


async def blacklist_all_user_tokens(user_id: str) -> int:
    """吊销指定用户的所有活跃 Token

    密码变更/重置时调用，强制用户重新登录。
    由于 JWT 是无状态的，无法按 user_id 批量查找 jti，
    因此在 Redis 中标记一个 user_id 级别的全局吊销时间戳，
    JWT 验证时检查 token 的签发时间是否早于该时间戳。

    Args:
        user_id: 用户 ID

    Returns:
        总是返回 1（标记成功），Redis 不可用时返回 0
    """
    try:
        import time

        key = f"agentvalue:user_revoke:{user_id}"
        # 尝试 Redis
        if hasattr(token_blacklist, "_redis") and token_blacklist._redis:
            await token_blacklist._redis.setex(
                key, 86400, str(int(time.time()))
            )  # 24h TTL
            logger.info("已吊销用户 %s 的所有 Token (Redis)", user_id)
            return 1
    except Exception as e:
        logger.warning("吊销用户 Token 失败(降级): %s", e)
    return 0
