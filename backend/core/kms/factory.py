"""KMS Provider 工厂 (多级 fallback + 单例缓存)

参考 rerank_factory.py 模式:create_kms_provider(settings) 按 settings 创建
KMSProvider 单例并缓存。

Fallback 链:
1. settings.field_encryption_backend 显式指定 (vault/aws/aliyun/local)
2. 生产环境拒绝 fallback 到 local (除非显式 local)

未启用 KMS (backend=env 或 local 且未配 field_encryption_key) 时返回 None,
由调用方降级为纯本地 FieldCipher (向后兼容)。
"""

import logging
from typing import Optional

from core.kms.base import (
    KMSNotConfiguredError,
    KMSProvider,
)
from core.kms.providers.local import LocalKMSProvider

logger = logging.getLogger(__name__)

_kms_provider_cache: Optional[KMSProvider] = None


def create_kms_provider(settings=None) -> Optional[KMSProvider]:
    """创建 KMSProvider 单例

    Args:
        settings: Settings 实例,None 时从 get_settings() 获取

    Returns:
        KMSProvider 实例,或 None (未启用 KMS,调用方降级本地 FieldCipher)

    Raises:
        KMSNotConfiguredError: 生产环境显式 vault/aws/aliyun 但配置缺失
        KMSProviderError: Provider 初始化失败 (认证失败等)
    """
    global _kms_provider_cache
    if _kms_provider_cache is not None:
        return _kms_provider_cache

    if settings is None:
        from core.config import get_settings
        settings = get_settings()

    backend = (settings.field_encryption_backend or "env").lower()
    is_production = settings.agentvalue_env == "production"

    if backend == "env":
        # 传统模式:不用 KMS,降级到 FieldCipher
        return None

    if backend == "local":
        # 本地 KMS 模拟 (开发/测试),生产环境拒绝 (除非显式)
        if is_production:
            raise KMSNotConfiguredError(
                "生产环境严禁 field_encryption_backend=local,请配置 vault/aws/aliyun",
                provider="local",
            )
        _kms_provider_cache = LocalKMSProvider(key=settings.field_encryption_key)
        logger.info("KMS provider: local (开发模式,等价 FieldCipher)")
        return _kms_provider_cache

    if backend == "vault":
        if not settings.vault_addr:
            raise KMSNotConfiguredError(
                "field_encryption_backend=vault 需配置 vault_addr",
                provider="vault",
            )
        from core.kms.providers.vault import VaultKMSProvider
        _kms_provider_cache = VaultKMSProvider(
            addr=settings.vault_addr,
            auth_method=settings.vault_auth_method,
            token=settings.vault_token,
            role_id=settings.vault_role_id,
            secret_id=settings.vault_secret_id,
            k8s_role=settings.vault_k8s_role,
            namespace=settings.vault_namespace,
            transit_mount=settings.vault_transit_mount,
            kv_mount=settings.vault_kv_mount,
            kek_name=settings.vault_field_kek_name,
            jwt_key_path=settings.vault_jwt_key_path,
            verify_tls=settings.vault_verify_tls,
        )
        logger.info("KMS provider: vault (addr=%s, auth=%s)", settings.vault_addr, settings.vault_auth_method)
        return _kms_provider_cache

    if backend == "aws":
        if not settings.aws_kms_key_id:
            raise KMSNotConfiguredError(
                "field_encryption_backend=aws 需配置 aws_kms_key_id",
                provider="aws",
            )
        from core.kms.providers.aws import AWSKMSProvider
        _kms_provider_cache = AWSKMSProvider(
            key_id=settings.aws_kms_key_id,
            region=settings.aws_kms_region,
        )
        logger.info("KMS provider: aws (key_id=%s, region=%s)", settings.aws_kms_key_id, settings.aws_kms_region or "default")
        return _kms_provider_cache

    if backend == "aliyun":
        if not settings.aliyun_kms_key_id or not settings.aliyun_kms_endpoint:
            raise KMSNotConfiguredError(
                "field_encryption_backend=aliyun 需配置 aliyun_kms_key_id + aliyun_kms_endpoint",
                provider="aliyun",
            )
        from core.kms.providers.aliyun import AliyunKMSProvider
        _kms_provider_cache = AliyunKMSProvider(
            key_id=settings.aliyun_kms_key_id,
            endpoint=settings.aliyun_kms_endpoint,
        )
        logger.info("KMS provider: aliyun (key_id=%s, endpoint=%s)", settings.aliyun_kms_key_id, settings.aliyun_kms_endpoint)
        return _kms_provider_cache

    # 未知 backend
    raise KMSNotConfiguredError(
        f"未知 field_encryption_backend: {backend} (支持 env/vault/aws/aliyun/local)",
        provider="unknown",
    )


def reset_kms_provider_cache() -> None:
    """清空缓存,供测试 monkeypatch 后强制重建"""
    global _kms_provider_cache
    if _kms_provider_cache is not None:
        # 释放资源 (Vault token renewer 线程等)
        stop_fn = getattr(_kms_provider_cache, "stop", None)
        if callable(stop_fn):
            try:
                stop_fn()
            except Exception:
                pass
    _kms_provider_cache = None
