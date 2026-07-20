"""Local KMS Provider (开发/测试用)

等价现有 FieldCipher 的本地静态密钥方案,无网络调用。
DEK 直接用 os.urandom 生成,"加密"用对称 dict 模拟。

适用场景:
- 本地开发:无 Vault/AWS 环境,等价 FieldCipher 行为
- 单元测试:测试 EnvelopeCipher 逻辑不依赖外部服务
- 降级 fallback:其他 KMS 不可用时临时使用 (生产严禁)

生产环境 agentvalue_env == "production" 时 factory 会拒绝 fallback 到 local,
除非显式 field_encryption_backend=local (开发自检场景)。
"""

import logging
import os
import threading
from typing import Dict, Optional

from core.kms.base import KMSProvider, KMSProviderError

logger = logging.getLogger(__name__)


class LocalKMSProvider(KMSProvider):
    """本地 KMS (开发/测试用)

    - generate_data_key:os.urandom 生成 DEK,"加密"用 dict 存
    - decrypt:从 dict 取出明文 DEK
    - health_check:始终返回 True
    - name:"local"
    """

    def __init__(self, key: Optional[str] = None):
        # key 仅用于 enabled 判断,实际不用 (兼容现有 FieldCipher 透传模式)
        self._enabled = bool(key)
        self._store: Dict[bytes, bytes] = {}
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "local"

    async def generate_data_key(
        self,
        key_spec: str = "AES_256",
        encryption_context: Optional[Dict[str, str]] = None,
    ) -> Dict[str, bytes]:
        if not self._enabled:
            # 未启用:返回占位 (EnvelopeCipher 应降级透传)
            return {"plaintext": b"", "ciphertext_blob": b""}
        size = 32 if key_spec == "AES_256" else (16 if key_spec == "AES_128" else 32)
        pt = os.urandom(size)
        # 模拟"加密":加前缀方便识别
        ct = b"local-dek-" + pt
        with self._lock:
            self._store[ct] = pt
        return {"plaintext": pt, "ciphertext_blob": ct}

    async def decrypt(
        self,
        ciphertext_blob: bytes,
        encryption_context: Optional[Dict[str, str]] = None,
    ) -> Dict[str, bytes]:
        with self._lock:
            pt = self._store.get(ciphertext_blob)
        if pt is None:
            raise KMSProviderError(
                f"local KMS 密文未找到 (ciphertext 前缀: {ciphertext_blob[:20]!r})",
                provider=self.name,
            )
        return {"plaintext": pt}

    async def health_check(self) -> bool:
        return True  # 本地始终可用

    @property
    def enabled(self) -> bool:
        """是否启用 KMS (False 时 EnvelopeCipher 应降级透传)"""
        return self._enabled
