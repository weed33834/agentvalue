"""KMS Provider 抽象模块 (H5: 消除密钥明文配置)

对标 RerankProvider 抽象,提供统一的 Envelope Encryption 接口。
所有 KMS 实现(vault/aws/aliyun/local)需实现 generate_data_key /
decrypt / health_check / name 四个接口。

设计要点:
- 接口与具体云厂商解耦,EnvelopeCipher 只依赖此 ABC
- 异步接口(async def),同步 SDK(hvac/aliyun)实现内部用 asyncio.to_thread 包装
- generate_data_key 返回标准化 dict,屏蔽 AWS/Vault/Aliyun 响应差异
- factory create_kms_provider(settings) 多级 fallback:
  1. settings.field_encryption_backend 显式指定
  2. LocalKMSProvider(开发模式,本地 AES-GCM,等价现有 FieldCipher)

参考:
- AWS KMS Envelope Encryption: https://docs.aws.amazon.com/kms/latest/developerguide/concepts.html#enveloping
- Vault Transit Engine: https://developer.hashicorp.com/vault/docs/secrets/transit
- AWS Encryption SDK 安全阈值: https://docs.aws.amazon.com/encryption-sdk/latest/developer-guide/thresholds.html
"""

from core.kms.base import KMSProvider
from core.kms.factory import create_kms_provider, reset_kms_provider_cache

__all__ = [
    "KMSProvider",
    "create_kms_provider",
    "reset_kms_provider_cache",
]
