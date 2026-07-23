"""AWS KMS Provider (Envelope Encryption)

利用 AWS KMS Envelope Encryption:
- generate_data_key:调用 kms.generate_data_key (KeySpec=AES_256),返回 plaintext + CiphertextBlob
- decrypt:调用 kms.decrypt,CiphertextBlob 自动识别 key version (应用无需感知轮换)
- rewrap:调用 kms.re_encrypt,无需本地解密高效升级版本

使用 aioboto3 原生异步 SDK (基于 aiobotocore + aiohttp),无需 to_thread 包装。
EncryptionContext 用于 ABAC,加解密必须一致。

参考:
- aioboto3 文档: https://aioboto3.readthedocs.io/
- AWS KMS Envelope: https://docs.aws.amazon.com/kms/latest/developerguide/concepts.html#enveloping
- KMS GenerateDataKey: https://docs.aws.amazon.com/kms/latest/APIReference/API_GenerateDataKey.html
- moto 5.x mock: https://docs.getmoto.org/en/latest/
"""

import logging
from typing import Dict, Optional

from core.kms.base import (
    KMSAuthenticationError,
    KMSCiphertextInvalidError,
    KMSProvider,
    KMSProviderError,
    KMSUnavailableError,
)

logger = logging.getLogger(__name__)


class AWSKMSProvider(KMSProvider):
    """AWS KMS 实现 (原生 async, 用 aioboto3)"""

    def __init__(
        self,
        key_id: str,
        region: Optional[str] = None,
        max_pool_connections: int = 20,
        max_retries: int = 5,
    ):
        self._key_id = key_id  # alias/agentvalue-field-kek 或 key ARN
        self._region = region  # None 时从环境推断 (~/.aws/config / env)
        self._max_pool = max_pool_connections
        self._max_retries = max_retries
        self._session = None  # 惰性创建 aioboto3.Session

    def _ensure_session(self):
        if self._session is not None:
            return self._session
        try:
            import aioboto3
            from botocore.config import Config
        except ImportError as e:
            raise KMSProviderError(
                "aioboto3 未安装,启用 AWS KMS: pip install aioboto3>=15.5.0",
                provider=self.name,
                cause=e,
            ) from e
        self._config = Config(
            max_pool_connections=self._max_pool,
            retries={"max_attempts": self._max_retries, "mode": "adaptive"},
            connect_timeout=2,
            read_timeout=5,
        )
        self._session = aioboto3.Session()
        return self._session

    def _client_kwargs(self):
        kwargs = {"config": self._config}
        if self._region:
            kwargs["region_name"] = self._region
        return kwargs

    @property
    def name(self) -> str:
        return "aws"

    async def generate_data_key(
        self,
        key_spec: str = "AES_256",
        encryption_context: Optional[Dict[str, str]] = None,
    ) -> Dict[str, bytes]:
        self._ensure_session()
        try:
            async with self._session.client("kms", **self._client_kwargs()) as kms:
                resp = await kms.generate_data_key(
                    KeyId=self._key_id,
                    KeySpec=key_spec,
                    EncryptionContext=encryption_context or {},
                )
                return {
                    "plaintext": resp["Plaintext"],
                    "ciphertext_blob": resp["CiphertextBlob"],
                }
        except Exception as e:
            self._translate_aws_exception(e, "generate_data_key")

    async def decrypt(
        self,
        ciphertext_blob: bytes,
        encryption_context: Optional[Dict[str, str]] = None,
    ) -> Dict[str, bytes]:
        self._ensure_session()
        try:
            async with self._session.client("kms", **self._client_kwargs()) as kms:
                resp = await kms.decrypt(
                    CiphertextBlob=ciphertext_blob,
                    EncryptionContext=encryption_context or {},
                )
                return {"plaintext": resp["Plaintext"]}
        except Exception as e:
            self._translate_aws_exception(e, "decrypt")

    async def rewrap(
        self,
        ciphertext_blob: bytes,
        encryption_context: Optional[Dict[str, str]] = None,
    ) -> bytes:
        """AWS KMS 原生 ReEncrypt,无需本地解密高效升级版本"""
        self._ensure_session()
        try:
            async with self._session.client("kms", **self._client_kwargs()) as kms:
                resp = await kms.re_encrypt(
                    CiphertextBlob=ciphertext_blob,
                    DestinationKeyId=self._key_id,
                    SourceEncryptionContext=encryption_context or {},
                    DestinationEncryptionContext=encryption_context or {},
                )
                return resp["CiphertextBlob"]
        except Exception as e:
            self._translate_aws_exception(e, "rewrap")

    async def health_check(self) -> bool:
        """检查 KMS key 可达且 enabled"""
        self._ensure_session()
        try:
            async with self._session.client("kms", **self._client_kwargs()) as kms:
                resp = await kms.describe_key(KeyId=self._key_id)
                return resp["KeyMetadata"]["KeyState"] == "Enabled"
        except Exception as e:
            logger.debug("AWS KMS health_check 失败: %s", e)
            return False

    @staticmethod
    def _translate_aws_exception(e: Exception, op: str):
        """把 botocore ClientError 转为 KMS* 异常"""
        provider = "aws"
        msg = f"AWS KMS {op} 失败: {e}"

        try:
            from botocore.exceptions import ClientError
        except ImportError:
            ClientError = None

        if ClientError is not None and isinstance(e, ClientError):
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("AccessDeniedException", "UnauthorizedOperation"):
                raise KMSAuthenticationError(
                    msg + f" (code={code})", provider=provider, cause=e
                ) from e
            if code in ("InvalidCiphertextException", "ValidationException"):
                raise KMSCiphertextInvalidError(
                    msg + f" (code={code})", provider=provider, cause=e
                ) from e
            if code in ("KMSInvalidStateException", "NotFoundException"):
                raise KMSCiphertextInvalidError(
                    msg + f" (code={code})", provider=provider, cause=e
                ) from e
            if code in (
                "ThrottlingException",
                "ServiceUnavailableException",
                "RequestLimitExceeded",
            ):
                raise KMSUnavailableError(
                    msg + f" (code={code})", provider=provider, cause=e
                ) from e

        # 网络异常
        if isinstance(e, (ConnectionError, TimeoutError, OSError)):
            raise KMSUnavailableError(
                msg + " (网络故障)", provider=provider, cause=e
            ) from e

        # 兜底
        raise KMSProviderError(msg, provider=provider, cause=e) from e
