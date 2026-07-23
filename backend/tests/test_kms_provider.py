"""KMS Provider 测试 - Local + DEKCache + EnvelopeCipher 完整 roundtrip

覆盖:
- LocalKMSProvider generate_data_key / decrypt / health_check
- DEKCache LRU / TTL / 容量上限 / 用量阈值 / 失效
- EnvelopeCipher encrypt/decrypt roundtrip
- EnvelopeCipher 向后兼容 (旧密文不识别为 envelope)
- EnvelopeCipher 多 encryption_context 独立 DEK
- KMSProvider 异常体系

不依赖外部服务 (Vault/AWS),全部用 LocalKMSProvider 内存模拟。
"""

import asyncio
import base64
import os
import time
from unittest.mock import patch

import pytest

from core.kms.base import (
    KMSAuthenticationError,
    KMSCiphertextInvalidError,
    KMSProvider,
    KMSProviderError,
    KMSUnavailableError,
)
from core.kms.dek_cache import DEKCache, DEKCacheEntry
from core.kms.envelope import EnvelopeCipher
from core.kms.providers.local import LocalKMSProvider


# ============================================================
# LocalKMSProvider 测试
# ============================================================


class TestLocalKMSProvider:
    """LocalKMSProvider 单元测试"""

    @pytest.mark.asyncio
    async def test_generate_data_key_aes256_returns_32_bytes(self):
        kms = LocalKMSProvider(key="dummy")
        result = await kms.generate_data_key(key_spec="AES_256")
        assert len(result["plaintext"]) == 32
        assert len(result["ciphertext_blob"]) > 0
        assert result["ciphertext_blob"].startswith(b"local-dek-")

    @pytest.mark.asyncio
    async def test_generate_data_key_aes128_returns_16_bytes(self):
        kms = LocalKMSProvider(key="dummy")
        result = await kms.generate_data_key(key_spec="AES_128")
        assert len(result["plaintext"]) == 16

    @pytest.mark.asyncio
    async def test_generate_data_key_unconfigured_returns_empty(self):
        """未配置 key (开发模式透传) 时返回空,调用方应降级"""
        kms = LocalKMSProvider(key=None)
        result = await kms.generate_data_key()
        assert result["plaintext"] == b""
        assert result["ciphertext_blob"] == b""

    @pytest.mark.asyncio
    async def test_decrypt_roundtrip(self):
        kms = LocalKMSProvider(key="dummy")
        result = await kms.generate_data_key(key_spec="AES_256")
        decrypted = await kms.decrypt(result["ciphertext_blob"])
        assert decrypted["plaintext"] == result["plaintext"]

    @pytest.mark.asyncio
    async def test_decrypt_unknown_ciphertext_raises(self):
        kms = LocalKMSProvider(key="dummy")
        with pytest.raises(KMSProviderError) as exc_info:
            await kms.decrypt(b"unknown-ciphertext")
        assert "local" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_health_check_always_true(self):
        kms = LocalKMSProvider(key=None)
        assert await kms.health_check() is True

    def test_name_property(self):
        assert LocalKMSProvider(key="dummy").name == "local"

    @pytest.mark.asyncio
    async def test_enabled_property(self):
        assert LocalKMSProvider(key="dummy").enabled is True
        assert LocalKMSProvider(key=None).enabled is False


# ============================================================
# DEKCache 测试
# ============================================================


class TestDEKCache:
    """DEK 缓存单元测试"""

    def test_put_and_get(self):
        cache = DEKCache(capacity=10, ttl_seconds=60)
        cache.put("k1", b"plaintext", b"encrypted")
        entry = cache.get("k1")
        assert entry is not None
        assert entry.plaintext_dek == b"plaintext"
        assert entry.encrypted_dek == b"encrypted"

    def test_get_miss(self):
        cache = DEKCache()
        assert cache.get("non-existent") is None

    def test_lru_eviction(self):
        cache = DEKCache(capacity=2)
        cache.put("k1", b"p1", b"e1")
        cache.put("k2", b"p2", b"e2")
        cache.put("k3", b"p3", b"e3")  # k1 应被淘汰
        assert cache.get("k1") is None
        assert cache.get("k2") is not None
        assert cache.get("k3") is not None

    def test_lru_access_promotes_recency(self):
        """访问 k1 后,k1 应比 k2 更新,k2 应被淘汰"""
        cache = DEKCache(capacity=2)
        cache.put("k1", b"p1", b"e1")
        cache.put("k2", b"p2", b"e2")
        _ = cache.get("k1")  # k1 移到末尾
        cache.put("k3", b"p3", b"e3")  # k2 应被淘汰
        assert cache.get("k1") is not None
        assert cache.get("k2") is None

    def test_ttl_expiry(self):
        """过期 entry 不返回"""
        cache = DEKCache(ttl_seconds=1)
        cache.put("k1", b"p1", b"e1")
        # 模拟时间流逝 (patch entry.ts)
        entry = cache.get("k1")
        assert entry is not None
        entry.ts = time.monotonic() - 10  # 倒退 10 秒
        assert cache.get("k1") is None

    def test_max_messages_exhaustion(self):
        """超过单 DEK 消息数上限后失效"""
        cache = DEKCache(max_messages_per_key=2)
        cache.put("k1", b"p1", b"e1")
        cache.record_usage("k1", 100)
        cache.record_usage("k1", 100)
        assert cache.get("k1") is None  # 已用尽

    def test_max_bytes_exhaustion(self):
        cache = DEKCache(max_bytes_per_key=100)
        cache.put("k1", b"p1", b"e1")
        cache.record_usage("k1", 200)  # 超 100
        assert cache.get("k1") is None

    def test_invalidate_single_key(self):
        cache = DEKCache()
        cache.put("k1", b"p1", b"e1")
        cache.put("k2", b"p2", b"e2")
        cache.invalidate("k1")
        assert cache.get("k1") is None
        assert cache.get("k2") is not None

    def test_invalidate_all(self):
        cache = DEKCache()
        cache.put("k1", b"p1", b"e1")
        cache.put("k2", b"p2", b"e2")
        cache.invalidate()
        assert cache.get("k1") is None
        assert cache.get("k2") is None

    def test_stats(self):
        cache = DEKCache(capacity=10)
        cache.put("k1", b"p1", b"e1")
        cache.get("k1")  # hit
        cache.get("miss")  # miss
        stats = cache.stats
        assert stats["size"] == 1
        assert stats["capacity"] == 10
        assert stats["hits"] == 1
        assert stats["misses"] == 1


# ============================================================
# EnvelopeCipher 测试
# ============================================================


class TestEnvelopeCipher:
    """EnvelopeCipher 单元测试"""

    @pytest.fixture
    def kms(self):
        return LocalKMSProvider(key="dummy")

    @pytest.fixture
    def cipher(self, kms):
        return EnvelopeCipher(kms)

    @pytest.mark.asyncio
    async def test_encrypt_decrypt_roundtrip(self, cipher):
        plaintext = "敏感数据 employee_id=E001"
        ct = await cipher.encrypt(plaintext)
        assert ct != plaintext
        assert isinstance(ct, str)
        pt = await cipher.decrypt(ct)
        assert pt == plaintext

    @pytest.mark.asyncio
    async def test_encrypt_returns_envelope_format(self, cipher):
        """envelope 密文以 \x01 版本前缀开头 (base64 解码后)"""
        ct = await cipher.encrypt("test")
        raw = base64.b64decode(ct, validate=True)
        assert raw.startswith(b"\x01"), "envelope 密文必须有 \x01 版本前缀"

    @pytest.mark.asyncio
    async def test_is_envelope_ciphertext_detects_envelope(self, cipher):
        ct = await cipher.encrypt("test")
        assert EnvelopeCipher.is_envelope_ciphertext(ct) is True

    @pytest.mark.asyncio
    async def test_is_envelope_ciphertext_rejects_legacy(self, cipher):
        """旧 FieldCipher 密文格式:base64(nonce(12) + ct + tag(16)) 无 \x01 前缀"""
        # 构造一个旧格式密文
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        legacy_key = os.urandom(32)
        aes = AESGCM(legacy_key)
        nonce = os.urandom(12)
        legacy_ct = base64.b64encode(
            nonce + aes.encrypt(nonce, b"legacy-data", None)
        ).decode()
        assert EnvelopeCipher.is_envelope_ciphertext(legacy_ct) is False

    @pytest.mark.asyncio
    async def test_is_envelope_ciphertext_rejects_plaintext(self, cipher):
        assert EnvelopeCipher.is_envelope_ciphertext("plain text") is False
        assert EnvelopeCipher.is_envelope_ciphertext("") is False
        assert EnvelopeCipher.is_envelope_ciphertext(None) is False

    @pytest.mark.asyncio
    async def test_decrypt_legacy_passthrough(self, cipher):
        """非 envelope 密文 (无 \x01 前缀) 应原样返回,调用方走旧 decrypt"""
        legacy = base64.b64encode(os.urandom(28)).decode()
        result = await cipher.decrypt(legacy)
        assert result == legacy  # 原样返回

    @pytest.mark.asyncio
    async def test_decrypt_non_base64_passthrough(self, cipher):
        """非 base64 字符串视为明文原样返回"""
        result = await cipher.decrypt("plain text")
        assert result == "plain text"

    @pytest.mark.asyncio
    async def test_decrypt_empty_string(self, cipher):
        assert await cipher.decrypt("") == ""

    @pytest.mark.asyncio
    async def test_encrypt_empty_raises(self, cipher):
        with pytest.raises(ValueError):
            await cipher.encrypt("")

    @pytest.mark.asyncio
    async def test_encrypt_non_string_raises(self, cipher):
        with pytest.raises(TypeError):
            await cipher.encrypt(b"bytes not allowed")

    @pytest.mark.asyncio
    async def test_different_context_uses_different_dek(self, kms):
        """不同 encryption_context 应生成独立 DEK"""
        cipher = EnvelopeCipher(kms)
        ct1 = await cipher.encrypt("data", encryption_context={"tenant": "t1"})
        ct2 = await cipher.encrypt("data", encryption_context={"tenant": "t2"})
        # 密文应不同 (不同 DEK)
        assert ct1 != ct2
        # 各自能解密
        assert await cipher.decrypt(ct1, encryption_context={"tenant": "t1"}) == "data"
        assert await cipher.decrypt(ct2, encryption_context={"tenant": "t2"}) == "data"

    @pytest.mark.asyncio
    async def test_cache_key_stable(self):
        """相同 context 顺序不同也应生成相同 cache_key (sorted)"""
        # 通过 stats 验证 cache 命中
        kms = LocalKMSProvider(key="dummy")
        cache = DEKCache(capacity=10, ttl_seconds=60)
        cipher = EnvelopeCipher(kms, dek_cache=cache)
        # 第一次加密生成 DEK (cache miss)
        await cipher.encrypt("data1", encryption_context={"a": "1", "b": "2"})
        assert cache.stats["hits"] == 0
        # 第二次同 context 加密应命中 cache (不调 KMS.generate_data_key)
        await cipher.encrypt(
            "data2", encryption_context={"b": "2", "a": "1"}
        )  # 顺序不同
        assert cache.stats["hits"] == 1

    @pytest.mark.asyncio
    async def test_long_plaintext(self, cipher):
        """长文本 (10KB) 也能正确加解密"""
        plaintext = "x" * 10240
        ct = await cipher.encrypt(plaintext)
        pt = await cipher.decrypt(ct)
        assert pt == plaintext

    @pytest.mark.asyncio
    async def test_unicode_plaintext(self, cipher):
        """unicode 字符 (中文/emoji) 正确处理"""
        plaintext = "员工评价:优秀 👍 中文测试"
        ct = await cipher.encrypt(plaintext)
        pt = await cipher.decrypt(ct)
        assert pt == plaintext

    @pytest.mark.asyncio
    async def test_decrypt_corrupted_ciphertext_raises(self, cipher):
        """损坏的 envelope 密文 (有 \x01 前缀但内容损坏) 应抛 KMSProviderError"""
        ct = await cipher.encrypt("test")
        # 翻转最后一个字节 (破坏 tag)
        raw = bytearray(base64.b64decode(ct))
        raw[-1] ^= 0xFF
        corrupted = base64.b64encode(bytes(raw)).decode()
        with pytest.raises(KMSProviderError):
            await cipher.decrypt(corrupted)


# ============================================================
# KMSProvider 异常体系测试
# ============================================================


class TestKMSExceptions:
    """KMS 异常类层级测试"""

    def test_kms_provider_error_base(self):
        err = KMSProviderError("test error", provider="vault")
        assert "vault" in str(err)
        assert err.provider == "vault"

    def test_kms_provider_error_no_provider(self):
        err = KMSProviderError("test")
        assert str(err) == "test"

    def test_kms_not_configured_error(self):
        from core.kms.base import KMSNotConfiguredError

        err = KMSNotConfiguredError("vault_addr missing", provider="vault")
        assert isinstance(err, KMSProviderError)

    def test_kms_authentication_error(self):
        err = KMSAuthenticationError("approle failed", provider="vault")
        assert isinstance(err, KMSProviderError)
        assert err.provider == "vault"

    def test_kms_ciphertext_invalid_error(self):
        err = KMSCiphertextInvalidError("context mismatch", provider="aws")
        assert isinstance(err, KMSProviderError)

    def test_kms_unavailable_error(self):
        err = KMSUnavailableError("vault sealed", provider="vault")
        assert isinstance(err, KMSProviderError)
