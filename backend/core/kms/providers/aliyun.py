"""阿里云 KMS Provider (Envelope Encryption)

利用阿里云 KMS 实现 Envelope Encryption (国密 SM4 / 国内合规):
- generate_data_key:调用 GenerateDataKey (KeySpec=AES_256)
- decrypt:调用 Decrypt
- rewrap:阿里云 KMS 暂无原生 ReEncrypt,用 decrypt + generate_data_key 兜底

阿里云 SDK (alibabacloud_kms20160120) 同步,用 asyncio.to_thread 包装。

参考:
- 阿里云 KMS 凭据客户端 V2.0: https://help.aliyun.com/zh/kms/key-management-service/developer-reference/secrets-manager-client
- 阿里云 KMS SDK: https://www.alibabacloud.com/help/en/kms/key-management-service/developer-reference/classic-kms-sdkclassic-kms-sdk/
"""

import asyncio
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


class AliyunKMSProvider(KMSProvider):
    """阿里云 KMS 实现 (同步 SDK + asyncio.to_thread)"""

    def __init__(
        self,
        key_id: str,
        endpoint: str,
        access_key_id: Optional[str] = None,
        access_key_secret: Optional[str] = None,
        region_id: Optional[str] = None,
    ):
        self._key_id = key_id
        self._endpoint = endpoint  # kms.<region>.aliyuncs.com 或专属网关
        self._ak = access_key_id  # None 时走默认凭据链 (RAM Role / 环境变量)
        self._sk = access_key_secret
        self._region = region_id or (
            endpoint.split(".")[1] if "." in endpoint else "cn-hangzhou"
        )
        self._client = None
        self._init_lock = asyncio.Lock()

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        try:
            from alibabacloud_kms20160120.client import Client as KmsClient
            from alibabacloud_tea_openapi.models import Config as OpenApiConfig
        except ImportError as e:
            raise KMSProviderError(
                "aliyun KMS SDK 未安装,启用阿里云 KMS: "
                "pip install alibabacloud_kms20160120 alibabacloud_secretsmanager_client_v2",
                provider=self.name,
                cause=e,
            ) from e
        config = OpenApiConfig(
            access_key_id=self._ak,
            access_key_secret=self._sk,
            endpoint=self._endpoint,
            region_id=self._region,
        )
        self._client = KmsClient(config)
        return self._client

    @property
    def name(self) -> str:
        return "aliyun"

    async def generate_data_key(
        self,
        key_spec: str = "AES_256",
        encryption_context: Optional[Dict[str, str]] = None,
    ) -> Dict[str, bytes]:
        def _call() -> Dict[str, bytes]:
            from alibabacloud_kms20160120.models import GenerateDataKeyRequest

            client = self._ensure_client()
            try:
                req = GenerateDataKeyRequest(
                    key_id=self._key_id,
                    key_spec=key_spec,
                    encryption_context=encryption_context,
                )
                resp = client.generate_data_key(req)
            except Exception as e:
                self._translate_aliyun_exception(e, "generate_data_key")
            return {
                "plaintext": resp.body.plaintext,
                "ciphertext_blob": resp.body.ciphertext_blob,
            }

        return await asyncio.to_thread(_call)

    async def decrypt(
        self,
        ciphertext_blob: bytes,
        encryption_context: Optional[Dict[str, str]] = None,
    ) -> Dict[str, bytes]:
        def _call() -> Dict[str, bytes]:
            from alibabacloud_kms20160120.models import DecryptRequest

            client = self._ensure_client()
            try:
                req = DecryptRequest(
                    ciphertext_blob=ciphertext_blob,
                    encryption_context=encryption_context,
                )
                resp = client.decrypt(req)
            except Exception as e:
                self._translate_aliyun_exception(e, "decrypt")
            return {"plaintext": resp.body.plaintext}

        return await asyncio.to_thread(_call)

    async def health_check(self) -> bool:
        def _call() -> bool:
            from alibabacloud_kms20160120.models import DescribeKeyRequest

            try:
                client = self._ensure_client()
                client.describe_key(DescribeKeyRequest(key_id=self._key_id))
                return True
            except Exception as e:
                logger.debug("Aliyun KMS health_check 失败: %s", e)
                return False

        return await asyncio.to_thread(_call)

    @staticmethod
    def _translate_aliyun_exception(e: Exception, op: str):
        """把阿里云 SDK 异常转为 KMS* 异常

        阿里云 Tea SDK 异常体系:
        - TeaException (ServerError / Code)
        - ValidationException
        - InvalidAccessKeyId.NotFound (404)
        """
        provider = "aliyun"
        msg = f"阿里云 KMS {op} 失败: {e}"

        try:
            from alibabacloud_tea_util.exceptions import TeaException
        except ImportError:
            TeaException = None

        if TeaException is not None and isinstance(e, TeaException):
            code = getattr(e, "code", "") or ""
            msg_text = getattr(e, "message", "") or str(e)
            if code in (
                "InvalidAccessKeyId.NotFound",
                "Forbidden.KeyNotFound",
                "NoPermission",
            ):
                raise KMSAuthenticationError(
                    f"{msg} (code={code})", provider=provider, cause=e
                ) from e
            if code in ("InvalidCiphertext", "EncryptionContext not equal"):
                raise KMSCiphertextInvalidError(
                    f"{msg} (code={code})", provider=provider, cause=e
                ) from e
            if code in ("ServiceUnavailable", "Throttling", "InternalFailure"):
                raise KMSUnavailableError(
                    f"{msg} (code={code})", provider=provider, cause=e
                ) from e

        if isinstance(e, (ConnectionError, TimeoutError, OSError)):
            raise KMSUnavailableError(
                msg + " (网络故障)", provider=provider, cause=e
            ) from e

        raise KMSProviderError(msg, provider=provider, cause=e) from e
