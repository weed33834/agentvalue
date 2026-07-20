"""DEK 内存缓存 (Envelope Encryption 性能关键)

避免每次加解密都打 KMS (网络延迟 + 计费),参考 AWS Encryption SDK
LocalCryptoMaterialsCache 模式。

安全阈值参考 AWS Encryption SDK:
- max_age: 必填,5-15 分钟,作为 key rotation proxy
- max_messages: 单 DEK 最多加密 100 条消息 (限制密文爆破面)
- max_bytes: 单 DEK 最多加密 64MB

权衡:明文 DEK 留内存,需设严格 TTL + 容量阈值。
不建议用 Redis 共享 (多进程泄露面增大),建议仅内存进程隔离。
"""

import logging
import threading
import time
from collections import OrderedDict
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)


class DEKCacheEntry:
    """单个 DEK 缓存条目"""

    __slots__ = ("plaintext_dek", "encrypted_dek", "ts", "msgs", "bytes")

    def __init__(self, plaintext_dek: bytes, encrypted_dek: bytes):
        self.plaintext_dek = plaintext_dek
        self.encrypted_dek = encrypted_dek
        self.ts: float = time.monotonic()
        self.msgs: int = 0
        self.bytes: int = 0

    def is_expired(self, ttl_seconds: int) -> bool:
        return (time.monotonic() - self.ts) > ttl_seconds

    def is_exhausted(self, max_messages: int, max_bytes: int) -> bool:
        return self.msgs >= max_messages or self.bytes >= max_bytes


class DEKCache:
    """LRU + TTL 的 DEK 缓存,线程安全

    缓存 key 由 EnvelopeCipher 按 (tenant, field_type) 维度生成,
    相同维度的加密复用同一 DEK,避免每次 KMS 调用。
    """

    def __init__(
        self,
        capacity: int = 1000,
        ttl_seconds: int = 300,
        max_messages_per_key: int = 100,
        max_bytes_per_key: int = 64 * 1024 * 1024,
    ):
        self._capacity = max(1, capacity)
        self._ttl = max(1, ttl_seconds)
        self._max_msgs = max(1, max_messages_per_key)
        self._max_bytes = max(1, max_bytes_per_key)
        self._cache: "OrderedDict[str, DEKCacheEntry]" = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, cache_key: str) -> Optional[DEKCacheEntry]:
        """取 DEK,失效或耗尽返回 None (调用方需重新生成)"""
        with self._lock:
            entry = self._cache.get(cache_key)
            if entry is None:
                self._misses += 1
                return None
            if entry.is_expired(self._ttl) or entry.is_exhausted(self._max_msgs, self._max_bytes):
                # 过期或耗尽,主动淘汰
                self._cache.pop(cache_key, None)
                self._misses += 1
                return None
            # 命中,移到末尾 (LRU)
            self._cache.move_to_end(cache_key)
            self._hits += 1
            return entry

    def put(self, cache_key: str, plaintext_dek: bytes, encrypted_dek: bytes) -> None:
        """存 DEK (覆盖已有)"""
        with self._lock:
            entry = DEKCacheEntry(plaintext_dek, encrypted_dek)
            if cache_key in self._cache:
                self._cache.move_to_end(cache_key)
            self._cache[cache_key] = entry
            # LRU 淘汰
            while len(self._cache) > self._capacity:
                evicted_key, _ = self._cache.popitem(last=False)
                logger.debug("DEK cache LRU evict: %s", evicted_key)

    def record_usage(self, cache_key: str, bytes_encrypted: int) -> None:
        """记录 DEK 使用量 (消息数 + 字节数)"""
        with self._lock:
            entry = self._cache.get(cache_key)
            if entry is not None:
                entry.msgs += 1
                entry.bytes += bytes_encrypted

    def invalidate(self, cache_key: Optional[str] = None) -> None:
        """失效缓存 (key rotation / 配置变更时调用)

        cache_key=None 时清空全部缓存
        """
        with self._lock:
            if cache_key is None:
                self._cache.clear()
            else:
                self._cache.pop(cache_key, None)

    @property
    def stats(self) -> Dict[str, int]:
        """缓存命中率统计 (供 Prometheus 指标使用)"""
        with self._lock:
            return {
                "size": len(self._cache),
                "capacity": self._capacity,
                "hits": self._hits,
                "misses": self._misses,
            }
