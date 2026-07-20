"""KMS Provider 抽象基类

Envelope Encryption 标准模式:
- CMK/KEK (主密钥/密钥加密密钥):KMS 托管,永不出 HSM
- DEK (数据加密密钥):每次加密生成,plaintext 用完即擦,wrapped DEK 随密文存储
- 本地加密:AES-256-GCM (带 AEAD,机密性 + 完整性)

接口约定:
- generate_data_key 返回 {plaintext: bytes(32), ciphertext_blob: bytes}
- decrypt 接收 ciphertext_blob 返回 {plaintext: bytes}
- encryption_context 用于 ABAC,加解密需一致(AWS)或可不一致(Vault)
"""

from abc import ABC, abstractmethod
from typing import Dict, Optional


class KMSProvider(ABC):
    """KMS Provider 抽象基类

    所有 KMS 实现需实现 generate_data_key / decrypt / health_check / name。
    encrypt/decrypt 数据本体由 EnvelopeCipher 在本地用 AES-GCM 完成,
    KMSProvider 仅负责 DEK 的生成与解密(KMS 不接触业务数据)。
    """

    @abstractmethod
    async def generate_data_key(
        self,
        key_spec: str = "AES_256",
        encryption_context: Optional[Dict[str, str]] = None,
    ) -> Dict[str, bytes]:
        """生成数据加密密钥(DEK)

        Args:
            key_spec: "AES_256" (默认,32 字节) 或 "AES_128" (16 字节)
            encryption_context: ABAC 上下文 (如 {"app": "agentvalue", "tenant": "t1"}),
                解密时需传入相同上下文 (AWS 必须一致,Vault 仅校验存在)

        Returns:
            {"plaintext": bytes(32), "ciphertext_blob": bytes}
            plaintext 用完必须立即清零;ciphertext_blob 随密文存储

        Raises:
            KMSProviderError: KMS 服务不可用 / 权限不足 / 参数错误
        """
        raise NotImplementedError

    @abstractmethod
    async def decrypt(
        self,
        ciphertext_blob: bytes,
        encryption_context: Optional[Dict[str, str]] = None,
    ) -> Dict[str, bytes]:
        """解密 DEK (KMS 自动选对 key version,应用无需感知轮换)

        Args:
            ciphertext_blob: generate_data_key 返回的 ciphertext_blob
            encryption_context: 必须与加密时一致 (AWS 强校验)

        Returns:
            {"plaintext": bytes}

        Raises:
            KMSProviderError: KMS 服务不可用 / 权限不足 / 密文无效 / context 不匹配
        """
        raise NotImplementedError

    @abstractmethod
    async def health_check(self) -> bool:
        """探活:检查 KMS 服务可达且凭证有效 (不实际加解密)"""
        raise NotImplementedError

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider 名 (vault / aws / aliyun / local)"""
        raise NotImplementedError

    # 可选:rewrap 用于密钥轮换后批量升级旧密文
    async def rewrap(
        self,
        ciphertext_blob: bytes,
        encryption_context: Optional[Dict[str, str]] = None,
    ) -> bytes:
        """把旧版本密文升级到最新 CMK 版本

        默认实现:decrypt + generate_data_key (子类可覆盖用 KMS 原生
        ReEncrypt / Vault rewrap_data 更高效,无需本地解密)

        Returns:
            新的 ciphertext_blob bytes
        """
        decrypted = await self.decrypt(ciphertext_blob, encryption_context)
        new_dek = await self.generate_data_key(
            key_spec=f"AES_{len(decrypted['plaintext']) * 8}",
            encryption_context=encryption_context,
        )
        # 显式擦除中间明文
        self._zero(decrypted["plaintext"])
        self._zero(new_dek["plaintext"])
        return new_dek["ciphertext_blob"]

    @staticmethod
    def _zero(b: bytes) -> None:
        """best-effort 清零 bytes (对 bytearray 有效,bytes 不可变仅 gc 回收)

        生产场景如需真正清零,建议改用 bytearray 持有 DEK
        """
        # bytes 是不可变对象,这里仅做示意;实际依赖进程内存隔离 + GC
        pass


class KMSProviderError(Exception):
    """KMS Provider 异常基类"""

    def __init__(self, message: str, provider: str = "", cause: Optional[Exception] = None):
        self.provider = provider
        self.cause = cause
        super().__init__(f"[{provider}] {message}" if provider else message)


class KMSNotConfiguredError(KMSProviderError):
    """KMS 配置缺失 (如生产环境未配置 vault_addr)"""


class KMSAuthenticationError(KMSProviderError):
    """KMS 认证失败 (AppRole 凭证错误 / Token 过期 / K8s SA 无权限)"""


class KMSCiphertextInvalidError(KMSProviderError):
    """密文无效 / context 不匹配 / 密文已损坏"""


class KMSUnavailableError(KMSProviderError):
    """KMS 服务不可达 (网络故障 / Vault sealed / KMS throttled)"""
