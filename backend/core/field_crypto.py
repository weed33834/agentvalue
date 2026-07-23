"""
字段级加密，用于 DB 中敏感字段（manager_view/audit）。

设计要点：
- 加密在 service 层调用，不在 DB 层（SQLAlchemy event 较复杂）
- 密钥从 Settings.field_encryption_key 注入，未配置时降级不加密（开发模式）
- 密文格式：base64(nonce(12B) + ciphertext + tag(16B))
- AES-GCM 提供机密性 + 完整性，无需单独 MAC
- decrypt 容错：解密失败（如旧明文数据）时原样返回，保证向后兼容

H5 (v1.5.0) 升级：支持 KMS / Vault Envelope Encryption
- field_encryption_backend=env (默认,向后兼容):使用 field_encryption_key 本地 AES-GCM
- field_encryption_backend=vault/aws/aliyun:走 EnvelopeCipher,KMS 生成 DEK
- 旧密文 (无 \x01 前缀) 仍走旧 decrypt 路径,新密文 (有 \x01 前缀) 走 envelope
- FieldCipher 接口保持同步不变,内部 envelope 调用用 asyncio.run 桥接
"""

import asyncio
import base64
import json
import logging
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.kms.envelope import EnvelopeCipher

logger = logging.getLogger(__name__)

# AES-GCM 推荐 nonce 长度
_NONCE_BYTES = 12
# AES-256 密钥长度
_KEY_BYTES = 32


class FieldCipher:
    """字段级加解密器。

    key 为 base64 或 hex 编码的 32 字节密钥；None 时降级为透传（开发模式）。

    H5 (v1.5.0) 扩展:支持 envelope backend (KMS-backed)
    - envelope_cipher 为 None (默认):走旧本地 AES-GCM 路径 (向后兼容)
    - envelope_cipher 不为 None:encrypt 走 EnvelopeCipher,decrypt 检测密文前缀
      路由到 envelope 或旧路径 (向后兼容旧密文)
    """

    def __init__(
        self,
        key: Optional[str] = None,
        *,
        envelope_cipher: Optional["EnvelopeCipher"] = None,
        encryption_context: Optional[dict] = None,
    ) -> None:
        self.enabled = bool(key) or envelope_cipher is not None
        self._aes = None
        self._envelope = envelope_cipher
        self._encryption_context = encryption_context
        if key:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            raw_key = self._decode_key(key)
            self._aes = AESGCM(raw_key)

    @staticmethod
    def _decode_key(key: str) -> bytes:
        """支持 base64 或 hex 编码的 32 字节密钥。"""
        # 优先尝试 base64
        try:
            decoded = base64.b64decode(key, validate=True)
            if len(decoded) == _KEY_BYTES:
                return decoded
        except (ValueError, base64.binascii.Error):
            pass
        # 再尝试 hex
        try:
            decoded = bytes.fromhex(key)
            if len(decoded) == _KEY_BYTES:
                return decoded
        except (ValueError, TypeError):
            pass
        raise ValueError(
            "field_encryption_key 必须是 32 字节的 base64 或 hex 编码密钥；"
            '生成方法: python -c "import base64,os; '
            'print(base64.b64encode(os.urandom(32)).decode())"'
        )

    @staticmethod
    def generate_key() -> str:
        """生成新密钥（base64），供运维生成用。"""
        return base64.b64encode(__import__("os").urandom(_KEY_BYTES)).decode()

    def encrypt(self, plaintext: str) -> str:
        """加密字符串，返回 base64(nonce + ciphertext + tag)。

        未启用加密时透传原文（开发模式）。
        H5: 启用 envelope backend 时走 EnvelopeCipher (KMS-backed)
        """
        if not self.enabled:
            return plaintext
        import os

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401

        # envelope backend 路径 (KMS-backed)
        if self._envelope is not None:
            return self._run_async_safely(
                self._envelope.encrypt(plaintext, self._encryption_context)
            )

        # 旧路径:本地 AES-GCM
        try:
            nonce = os.urandom(_NONCE_BYTES)
            ct = self._aes.encrypt(nonce, plaintext.encode("utf-8"), None)
            return base64.b64encode(nonce + ct).decode("ascii")
        except Exception as e:
            # P1-4：加密失败调 record_field_encryption_failure（不再误调 decrypt 失败埋点）
            try:
                from core.metrics import record_field_encryption_failure

                record_field_encryption_failure()
            except Exception:
                logger.debug("record_field_encryption_failure 埋点失败", exc_info=True)
            logger.warning(
                "字段加密失败: plaintext_len=%d error=%s",
                len(plaintext),
                e,
            )
            raise

    def decrypt(self, ciphertext: str) -> str:
        """解密字符串。

        未启用加密时透传原文；解密失败（如旧明文数据）时原样返回，保证向后兼容。
        H5: envelope 密文 (有 \x01 前缀) 走 EnvelopeCipher.decrypt
        """
        if not self.enabled:
            return ciphertext

        # envelope 密文检测 (有 \x01 前缀)
        # 延迟导入避免模块级循环依赖
        from core.kms.envelope import EnvelopeCipher

        if self._envelope is not None and EnvelopeCipher.is_envelope_ciphertext(
            ciphertext
        ):
            try:
                return self._run_async_safely(
                    self._envelope.decrypt(ciphertext, self._encryption_context)
                )
            except Exception as e:
                try:
                    from core.metrics import record_field_decrypt_failure

                    record_field_decrypt_failure()
                except Exception:
                    logger.debug("record_field_decrypt_failure 埋点失败", exc_info=True)
                logger.warning("envelope 字段解密失败: error=%s", e)
                # 生产环境 fail-closed,非生产降级返回原密文 (兼容旧数据)
                from core.config import get_settings

                if get_settings().agentvalue_env == "production":
                    raise
                return ciphertext

        # 旧路径:本地 AES-GCM (或明文透传)
        try:
            raw = base64.b64decode(ciphertext, validate=True)
        except (ValueError, base64.binascii.Error):
            # 不是合法 base64，视为明文
            return ciphertext
        if len(raw) < _NONCE_BYTES + 16:  # nonce + 最小 tag
            return ciphertext
        nonce = raw[:_NONCE_BYTES]
        ct = raw[_NONCE_BYTES:]
        try:
            pt = self._aes.decrypt(nonce, ct, None)
            return pt.decode("utf-8")
        except Exception as e:
            # P1-6：AES-GCM 解密失败记日志（含 cipher 长度/前 8 字节 hex，不泄明文）
            # + Counter。为不改变业务行为（向后兼容旧明文数据），仍原样返回密文。
            try:
                from core.metrics import record_field_decrypt_failure

                record_field_decrypt_failure()
            except Exception:
                logger.debug("record_field_decrypt_failure 埋点失败", exc_info=True)
            logger.warning(
                "字段解密失败: cipher_len=%d cipher_prefix=%s error=%s",
                len(ciphertext),
                raw[:8].hex(),
                e,
            )
            return ciphertext

    @staticmethod
    def _run_async_safely(coro) -> Any:
        """同步上下文中安全执行 async coroutine

        - 已有 event loop (async 应用):用 asyncio.run_coroutine_threadsafe + 新线程
          避免 "asyncio.run() cannot be called from a running event loop"
        - 无 event loop (脚本/测试):直接 asyncio.run
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            # 在 async 应用内被同步调用 → 新线程跑 loop
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        return asyncio.run(coro)

    # ---- JSON 字段便捷方法 ----

    def encrypt_json(self, obj: Any) -> str:
        """加密 JSON 可序列化对象，返回密文字符串。

        未启用加密时返回 JSON 字符串（与密文同为 str 类型，DB 列类型一致）。

        P1-11：生产环境（agentvalue_env == "production"）下加密失败直接 raise，
        不降级返回明文（避免敏感字段明文落库）；非生产环境保持降级行为（开发友好）。
        """
        from core.metrics import record_field_encryption

        if not self.enabled:
            # 未启用加密：明文透传，记 success_passthrough 便于统计明文落库量
            try:
                record_field_encryption("success_passthrough")
            except Exception:
                logger.debug("record_field_encryption 埋点失败", exc_info=True)
            return json.dumps(obj, ensure_ascii=False)

        try:
            payload = json.dumps(obj, ensure_ascii=False)
            encrypted = self.encrypt(payload)
            record_field_encryption("success_encrypted")
            return encrypted
        except Exception:
            record_field_encryption("failure")
            # P1-11：生产环境 fail-closed，不降级明文；非生产降级返回 JSON 字符串
            from core.config import get_settings

            if get_settings().agentvalue_env == "production":
                logger.error("字段加密失败且处于生产环境，拒绝降级明文落库")
                raise
            logger.exception("字段加密失败，降级返回 JSON 字符串（非生产环境）")
            return json.dumps(obj, ensure_ascii=False)

    def decrypt_json(self, value: Any) -> Any:
        """解密为 JSON 对象。

        兼容三种输入：
        1. 密文字符串（启用加密时写入）→ decrypt + json.loads → 对象
        2. JSON 字符串（未启用加密时写入）→ json.loads → 对象
        3. dict/list（旧明文数据，DB JSON 列直接反序列化）→ 原样返回

        解密失败时尝试 json.loads，再失败则原样返回，保证向后兼容。
        """
        if isinstance(value, (dict, list)):
            # 旧明文数据（DB JSON 列直接反序列化为 dict/list）或透传模式
            return value
        if value is None:
            return value
        if not isinstance(value, str):
            return value

        if self.enabled:
            plaintext = self.decrypt(value)
        else:
            plaintext = value

        try:
            return json.loads(plaintext)
        except (json.JSONDecodeError, TypeError, ValueError):
            # 既非密文也非 JSON 字符串，原样返回
            return plaintext


# ---- 模块级单例（按密钥缓存，配置变更自动重建） ----

# 缓存 key 由 (field_encryption_key, field_encryption_backend) 组成,
# 配置任一变更都重建实例
_cipher_cache: Dict[tuple, "FieldCipher"] = {}

# 模块级 EnvelopeCipher 单例 (由 get_field_cipher 懒加载)
_envelope_cipher_singleton: Optional["EnvelopeCipher"] = None


def get_field_cipher() -> FieldCipher:
    """获取与当前 settings 绑定的 FieldCipher 单例。

    按 (field_encryption_key, field_encryption_backend) 组合缓存:
    - backend=env (默认): 用 field_encryption_key 本地 AES-GCM (向后兼容)
    - backend=vault/aws/aliyun: 用 EnvelopeCipher (KMS-backed)
      field_encryption_key 仍可保留用于旧密文 decrypt 兼容期

    密钥或 backend 变更时自动重建实例,避免缓存陈旧。
    """
    from core.config import get_settings

    settings = get_settings()
    cache_key = (
        settings.field_encryption_key,
        settings.field_encryption_backend or "env",
    )
    cached = _cipher_cache.get(cache_key)
    if cached is not None:
        return cached

    # 尝试初始化 envelope backend (KMS-backed)
    envelope_cipher = None
    backend = (settings.field_encryption_backend or "env").lower()
    if backend not in ("env", "local"):
        # vault / aws / aliyun 走 KMS-backed envelope
        try:
            from core.kms import create_kms_provider
            from core.kms.envelope import EnvelopeCipher
            from core.kms.dek_cache import DEKCache

            global _envelope_cipher_singleton
            if _envelope_cipher_singleton is None:
                kms = create_kms_provider(settings)
                if kms is not None:
                    dek_cache = DEKCache(
                        capacity=settings.kms_dek_cache_max_size,
                        ttl_seconds=settings.kms_dek_cache_ttl_seconds,
                        max_messages_per_key=settings.kms_dek_cache_max_messages,
                        max_bytes_per_key=settings.kms_dek_cache_max_bytes,
                    )
                    _envelope_cipher_singleton = EnvelopeCipher(
                        kms_provider=kms, dek_cache=dek_cache
                    )
                    logger.info(
                        "字段级加密启用 envelope backend: %s (DEK cache ttl=%ds cap=%d)",
                        backend,
                        settings.kms_dek_cache_ttl_seconds,
                        settings.kms_dek_cache_max_size,
                    )
            envelope_cipher = _envelope_cipher_singleton
        except Exception as e:
            logger.error(
                "KMS envelope backend 初始化失败 (backend=%s): %s,降级到本地 AES-GCM",
                backend,
                e,
            )
            if settings.agentvalue_env == "production":
                # 生产环境硬失败:不允许降级 (避免明文落库)
                raise
            # 非生产降级到本地 (envelope_cipher=None)

    # 创建 FieldCipher:
    # - envelope 启用时,envelope_cipher 注入,field_encryption_key 仍用于旧密文兼容
    # - envelope 未启用时,用 field_encryption_key 本地 AES-GCM (向后兼容)
    cached = FieldCipher(
        settings.field_encryption_key,
        envelope_cipher=envelope_cipher,
    )
    _cipher_cache[cache_key] = cached
    if cached.enabled:
        if envelope_cipher is not None:
            logger.info("字段级加密已启用 (envelope backend)")
        else:
            logger.info("字段级加密已启用（本地 AES-GCM）")
    else:
        logger.info("字段级加密未启用（FIELD_ENCRYPTION_KEY 未配置，明文透传）")
    return cached


def reset_field_cipher_cache() -> None:
    """清空 FieldCipher 缓存，供测试在 monkeypatch settings 后强制重建。"""
    _cipher_cache.clear()
    global _envelope_cipher_singleton
    if _envelope_cipher_singleton is not None:
        # 释放 KMS provider 资源 (Vault token renewer 等)
        try:
            from core.kms import reset_kms_provider_cache

            reset_kms_provider_cache()
        except Exception:
            pass
    _envelope_cipher_singleton = None
