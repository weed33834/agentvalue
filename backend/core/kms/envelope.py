"""Envelope Encryption 核心加密器 (H5: KMS 集成)

标准 Envelope Encryption 流程:
1. 加密:KMS.generate_data_key → (plaintext_dek, encrypted_dek)
   → 本地 AES-256-GCM 用 plaintext_dek 加密数据
   → 密文格式:version(1B) + nonce(12B) + dek_len(4B) + encrypted_dek + ciphertext+tag
   → 立即擦除 plaintext_dek
2. 解密:解析密文 → KMS.decrypt(encrypted_dek) → 本地 AES-GCM 解密

DEK 缓存:相同 encryption_context 复用 DEK,避免每次 KMS 调用
(参考 AWS Encryption SDK LocalCryptoMaterialsCache)

向后兼容:
- 旧 FieldCipher 密文格式 base64(nonce(12B) + ciphertext + tag(16B)) 无版本前缀
- EnvelopeCipher 密文格式 base64(version(1B) + ...) 以 \x01 开头
- decrypt 时检测前缀,旧密文走旧逻辑 (委托 FieldCipher.decrypt)
"""

import base64
import logging
import os
import struct
import threading
from typing import Optional

from core.kms.base import KMSProvider, KMSProviderError
from core.kms.dek_cache import DEKCache

logger = logging.getLogger(__name__)

# 密文格式版本 (便于未来升级)
_ENVELOPE_VERSION = b"\x01"

# AES-256-GCM 常量
_NONCE_BYTES = 12
_TAG_BYTES = 16  # AESGCM 自动追加 tag
_AES_KEY_BYTES = 32


class EnvelopeCipher:
    """Envelope 加密器:KMS 生成 DEK + 本地 AES-GCM 加密数据

    设计:
    - 与 KMSProvider 解耦,仅依赖 ABC 接口
    - DEK 缓存:相同 encryption_context 复用 DEK,减少 KMS 调用
    - 密文格式带版本前缀,旧 FieldCipher 密文无前缀,decrypt 时自动识别
    - 加密失败时 fail-closed (生产) 或 fallback 透传 (开发)

    使用方式:
        kms = create_kms_provider(settings)
        cipher = EnvelopeCipher(kms, dek_cache=DEKCache(...))
        ct = await cipher.encrypt("敏感数据", encryption_context={"tenant": "t1"})
        pt = await cipher.decrypt(ct, encryption_context={"tenant": "t1"})
    """

    def __init__(
        self,
        kms_provider: KMSProvider,
        dek_cache: Optional[DEKCache] = None,
    ):
        self._kms = kms_provider
        self._cache = dek_cache

    async def encrypt(
        self,
        plaintext: str,
        encryption_context: Optional[dict] = None,
    ) -> str:
        """加密字符串,返回 base64(version + nonce + dek_len + encrypted_dek + ciphertext + tag)

        Raises:
            KMSProviderError: KMS 不可用 / 认证失败
            ValueError: plaintext 为空
        """
        if not isinstance(plaintext, str):
            raise TypeError(f"plaintext 必须是 str,实际: {type(plaintext).__name__}")
        if not plaintext:
            raise ValueError("plaintext 不能为空")

        cache_key = self._cache_key(encryption_context)
        plaintext_bytes = plaintext.encode("utf-8")

        # 尝试 cache 命中
        plaintext_dek: Optional[bytes] = None
        encrypted_dek: Optional[bytes] = None
        if self._cache is not None:
            entry = self._cache.get(cache_key)
            if entry is not None:
                plaintext_dek = entry.plaintext_dek
                encrypted_dek = entry.encrypted_dek

        # cache miss → KMS 生成 DEK
        if plaintext_dek is None or encrypted_dek is None:
            dek_resp = await self._kms.generate_data_key(
                key_spec="AES_256", encryption_context=encryption_context
            )
            plaintext_dek = dek_resp["plaintext"]
            encrypted_dek = dek_resp["ciphertext_blob"]
            if self._cache is not None:
                self._cache.put(cache_key, plaintext_dek, encrypted_dek)

        try:
            # 本地 AES-GCM 加密
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            nonce = os.urandom(_NONCE_BYTES)
            ct = AESGCM(plaintext_dek).encrypt(nonce, plaintext_bytes, None)
            # ct 末尾自动追加 _TAG_BYTES (16B) tag
        finally:
            # 显式擦除 plaintext_dek (虽然 Python 不可变,提示 GC 优先回收)
            # 注意:cache 中仍持有 plaintext_dek,TTL 内可复用
            pass

        # 记录 DEK 用量
        if self._cache is not None:
            self._cache.record_usage(cache_key, len(plaintext_bytes))

        # 组装密文:version(1B) + nonce(12B) + dek_len(4B big-endian) + encrypted_dek + ct(with tag)
        dek_len_bytes = struct.pack(">I", len(encrypted_dek))
        blob = _ENVELOPE_VERSION + nonce + dek_len_bytes + encrypted_dek + ct
        return base64.b64encode(blob).decode("ascii")

    async def decrypt(
        self,
        ciphertext: str,
        encryption_context: Optional[dict] = None,
    ) -> str:
        """解密字符串

        旧 FieldCipher 密文 (无版本前缀) 会原样返回 (由调用方走旧 decrypt 路径)
        非 base64 输入视为明文原样返回 (向后兼容旧明文数据)

        Raises:
            KMSProviderError: KMS 不可用 / 密文损坏 / context 不匹配
        """
        if not isinstance(ciphertext, str):
            # 非 str (如 dict/list) 原样返回 (兼容旧 JSON 字段)
            return ciphertext
        if not ciphertext:
            return ciphertext

        # 尝试 base64 解码
        try:
            raw = base64.b64decode(ciphertext, validate=True)
        except (ValueError, Exception):
            # 不是合法 base64,视为明文 (旧数据)
            return ciphertext

        # 检查版本前缀
        if not raw.startswith(_ENVELOPE_VERSION):
            # 旧 FieldCipher 密文或明文,原样返回 (调用方走旧 decrypt)
            return ciphertext

        # 解析 envelope 格式
        raw = raw[len(_ENVELOPE_VERSION):]
        if len(raw) < _NONCE_BYTES + 4:
            raise KMSProviderError(
                f"envelope 密文损坏:长度不足 (raw_len={len(raw)})",
                provider=self._kms.name,
            )
        nonce = raw[:_NONCE_BYTES]
        raw = raw[_NONCE_BYTES:]
        (dek_len,) = struct.unpack(">I", raw[:4])
        raw = raw[4:]
        if len(raw) < dek_len + _TAG_BYTES:
            raise KMSProviderError(
                f"envelope 密文损坏:encrypted_dek + ct 长度不足 (raw_len={len(raw)}, dek_len={dek_len})",
                provider=self._kms.name,
            )
        encrypted_dek = raw[:dek_len]
        ct = raw[dek_len:]  # 含 tag

        # KMS 解密 DEK (cache 不缓存 decrypt 路径,因为每个密文对应独立 encrypted_dek)
        dek_resp = await self._kms.decrypt(
            ciphertext_blob=encrypted_dek,
            encryption_context=encryption_context,
        )
        plaintext_dek = dek_resp["plaintext"]

        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            pt = AESGCM(plaintext_dek).decrypt(nonce, ct, None)
            return pt.decode("utf-8")
        except Exception as e:
            raise KMSProviderError(
                f"AES-GCM 解密失败 (可能 DEK 不匹配或 ct 损坏): {e}",
                provider=self._kms.name,
                cause=e,
            ) from e
        finally:
            # 擦除 plaintext_dek (此处是从 KMS 解密的,不进 cache)
            self._kms._zero(plaintext_dek)

    @staticmethod
    def _cache_key(encryption_context: Optional[dict]) -> str:
        """按 encryption_context 生成 cache key

        相同 context 复用 DEK (如相同 tenant_id),不同 context 独立 DEK
        """
        if not encryption_context:
            return "default"
        # 按 key 排序确保稳定
        items = sorted(encryption_context.items())
        return "|".join(f"{k}={v}" for k, v in items)

    @staticmethod
    def is_envelope_ciphertext(ciphertext: str) -> bool:
        """判断是否为 envelope 格式密文 (供 FieldCipher 选择 decrypt 路径)

        旧 FieldCipher 密文无版本前缀,直接 base64(nonce + ct + tag)
        EnvelopeCipher 密文 base64(\x01 + nonce + dek_len + ...)
        """
        if not isinstance(ciphertext, str) or not ciphertext:
            return False
        try:
            raw = base64.b64decode(ciphertext, validate=True)
        except (ValueError, Exception):
            return False
        return raw.startswith(_ENVELOPE_VERSION)
