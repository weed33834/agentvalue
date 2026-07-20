"""FieldCipher KMS 集成测试 + Factory 测试 + JWT Vault fallback 测试

覆盖:
- FieldCipher 与 EnvelopeCipher 集成 (sync API 调 async envelope)
- get_field_cipher() 工厂按 settings 选择 backend
- get_field_cipher() 缓存按 (key, backend) 重建
- 生产环境 KMS 初始化失败硬失败
- 非生产环境 KMS 初始化失败降级到本地
- JWT secret 从 Vault KV fallback 到 env
- reset_field_cipher_cache / reset_kms_provider_cache

不依赖真实 Vault/AWS,全部用 monkeypatch + LocalKMSProvider。
"""

import asyncio
import base64
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.field_crypto import FieldCipher, get_field_cipher, reset_field_cipher_cache


# ============================================================
# Fixture
# ============================================================


@pytest.fixture(autouse=True)
def reset_caches():
    """每个测试前后清空所有缓存,避免相互影响"""
    reset_field_cipher_cache()
    yield
    reset_field_cipher_cache()


def _make_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


# ============================================================
# FieldCipher KMS 集成测试
# ============================================================


class TestFieldCipherKMSIntegration:
    """FieldCipher 接入 EnvelopeCipher 后的 sync API 行为"""

    def test_encrypt_with_envelope_backend(self):
        """启用 envelope backend 时,encrypt 走 EnvelopeCipher 路径"""
        from core.kms.envelope import EnvelopeCipher
        from core.kms.providers.local import LocalKMSProvider

        kms = LocalKMSProvider(key="dummy")
        envelope = EnvelopeCipher(kms)
        cipher = FieldCipher(key=None, envelope_cipher=envelope)

        plaintext = "test-data"
        ct = cipher.encrypt(plaintext)
        # 应是 envelope 格式 (\x01 前缀)
        from core.kms.envelope import EnvelopeCipher as EC
        assert EC.is_envelope_ciphertext(ct)

    def test_decrypt_envelope_ciphertext(self):
        """decrypt 检测到 \x01 前缀,走 EnvelopeCipher.decrypt 路径"""
        from core.kms.envelope import EnvelopeCipher
        from core.kms.providers.local import LocalKMSProvider

        kms = LocalKMSProvider(key="dummy")
        envelope = EnvelopeCipher(kms)
        cipher = FieldCipher(key=None, envelope_cipher=envelope)

        # 用 async 加密得到 envelope 密文
        ct = asyncio.run(envelope.encrypt("secret-data"))
        # 用 sync decrypt
        pt = cipher.decrypt(ct)
        assert pt == "secret-data"

    def test_decrypt_legacy_ciphertext_uses_old_path(self):
        """旧 FieldCipher 密文 (无 \x01) 应走旧 AES-GCM decrypt 路径"""
        # 先用本地 AES-GCM 加密
        key = _make_key()
        legacy_cipher = FieldCipher(key=key)
        legacy_ct = legacy_cipher.encrypt("legacy-data")

        # 启用 envelope backend 但 field_encryption_key 仍配置 (用于旧密文兼容)
        from core.kms.envelope import EnvelopeCipher
        from core.kms.providers.local import LocalKMSProvider
        kms = LocalKMSProvider(key="dummy")
        envelope = EnvelopeCipher(kms)
        cipher_with_envelope = FieldCipher(key=key, envelope_cipher=envelope)

        # decrypt 旧密文 (无 \x01 前缀) 应走旧路径
        pt = cipher_with_envelope.decrypt(legacy_ct)
        assert pt == "legacy-data"

    def test_decrypt_non_base64_passthrough(self):
        """非 base64 字符串视为明文原样返回"""
        from core.kms.envelope import EnvelopeCipher
        from core.kms.providers.local import LocalKMSProvider
        kms = LocalKMSProvider(key="dummy")
        envelope = EnvelopeCipher(kms)
        cipher = FieldCipher(key=None, envelope_cipher=envelope)

        assert cipher.decrypt("plain text") == "plain text"

    def test_encrypt_json_with_envelope(self):
        """encrypt_json 也走 envelope 路径"""
        from core.kms.envelope import EnvelopeCipher
        from core.kms.providers.local import LocalKMSProvider
        kms = LocalKMSProvider(key="dummy")
        envelope = EnvelopeCipher(kms)
        cipher = FieldCipher(key=None, envelope_cipher=envelope)

        obj = {"a": 1, "b": "中文"}
        ct = cipher.encrypt_json(obj)
        # 应能解回
        pt = cipher.decrypt_json(ct)
        assert pt == obj

    def test_encrypt_json_decrypt_legacy_compatibility(self):
        """启用 envelope 后,旧的 JSON 字段 (无加密) 仍能 decrypt_json"""
        from core.kms.envelope import EnvelopeCipher
        from core.kms.providers.local import LocalKMSProvider
        kms = LocalKMSProvider(key="dummy")
        envelope = EnvelopeCipher(kms)
        cipher = FieldCipher(key=None, envelope_cipher=envelope)

        # 旧明文 JSON 数据
        old_json = '{"a": 1, "b": "中文"}'
        # decrypt_json 应能处理 (既非 envelope 也非旧 AES-GCM,可能是 JSON 字符串)
        result = cipher.decrypt_json(old_json)
        assert result == {"a": 1, "b": "中文"}


# ============================================================
# get_field_cipher 工厂测试
# ============================================================


class TestGetFieldCipherFactory:
    """get_field_cipher() 工厂按 settings 选 backend"""

    def test_default_env_backend_uses_local_aes(self, monkeypatch):
        """默认 backend=env,使用 field_encryption_key 本地 AES-GCM"""
        from core.config import Settings
        key = _make_key()
        monkeypatch.setenv("FIELD_ENCRYPTION_KEY", key)
        monkeypatch.setenv("AGENTVALUE_ENV", "")  # 非生产
        # 用 reset_settings 强制重读
        from core.config import get_settings
        get_settings.cache_clear() if hasattr(get_settings, "cache_clear") else None
        try:
            settings = get_settings()
        except Exception:
            # get_settings 可能用 lru_cache,直接构造
            settings = Settings(field_encryption_key=key, field_encryption_backend="env")
        monkeypatch.setattr("core.config.get_settings", lambda: settings)

        cipher = get_field_cipher()
        assert cipher.enabled
        assert cipher._envelope is None  # 未启用 envelope backend

    def test_vault_backend_initializes_envelope(self, monkeypatch):
        """backend=vault 时,初始化 EnvelopeCipher"""
        from core.config import Settings
        key = _make_key()
        settings = Settings(
            field_encryption_key=key,
            field_encryption_backend="vault",
            vault_addr="http://vault:8200",
            vault_auth_method="token",
            vault_token="dummy-token",
            vault_field_kek_name="agentvalue-test-kek",
        )
        monkeypatch.setattr("core.config.get_settings", lambda: settings)

        # mock create_kms_provider 返回 LocalKMSProvider (避免真实 Vault 连接)
        from core.kms.providers.local import LocalKMSProvider
        from core.kms.envelope import EnvelopeCipher

        mock_kms = LocalKMSProvider(key="dummy")

        # patch create_kms_provider 避免真实 Vault 调用
        with patch("core.kms.factory.create_kms_provider", return_value=mock_kms):
            cipher = get_field_cipher()
        assert cipher._envelope is not None
        assert cipher.enabled

    def test_unknown_backend_returns_local_only(self, monkeypatch):
        """未知 backend (如 'env') 返回 None envelope,只用本地 AES-GCM"""
        from core.config import Settings
        key = _make_key()
        settings = Settings(field_encryption_key=key, field_encryption_backend="env")
        monkeypatch.setattr("core.config.get_settings", lambda: settings)
        cipher = get_field_cipher()
        assert cipher._envelope is None
        assert cipher.enabled

    def test_cache_hit_returns_same_instance(self, monkeypatch):
        """相同 (key, backend) 缓存命中,返回相同实例"""
        from core.config import Settings
        key = _make_key()
        settings = Settings(field_encryption_key=key, field_encryption_backend="env")
        monkeypatch.setattr("core.config.get_settings", lambda: settings)

        c1 = get_field_cipher()
        c2 = get_field_cipher()
        assert c1 is c2

    def test_cache_invalidated_on_key_change(self, monkeypatch):
        """field_encryption_key 变更,实例重建"""
        from core.config import Settings
        key1 = _make_key()
        settings1 = Settings(field_encryption_key=key1, field_encryption_backend="env")
        monkeypatch.setattr("core.config.get_settings", lambda: settings1)
        c1 = get_field_cipher()

        key2 = _make_key()
        settings2 = Settings(field_encryption_key=key2, field_encryption_backend="env")
        monkeypatch.setattr("core.config.get_settings", lambda: settings2)
        c2 = get_field_cipher()

        assert c1 is not c2

    def test_cache_invalidated_on_backend_change(self, monkeypatch):
        """field_encryption_backend 变更,实例重建"""
        from core.config import Settings
        key = _make_key()
        settings1 = Settings(field_encryption_key=key, field_encryption_backend="env")
        monkeypatch.setattr("core.config.get_settings", lambda: settings1)
        c1 = get_field_cipher()

        settings2 = Settings(field_encryption_key=key, field_encryption_backend="local")
        monkeypatch.setattr("core.config.get_settings", lambda: settings2)
        c2 = get_field_cipher()

        assert c1 is not c2

    def test_kms_init_failure_non_production_degrades_gracefully(self, monkeypatch):
        """非生产环境 KMS 初始化失败,降级到本地 AES-GCM"""
        from core.config import Settings
        key = _make_key()
        settings = Settings(
            field_encryption_key=key,
            field_encryption_backend="vault",
            agentvalue_env="",  # 非生产
        )
        monkeypatch.setattr("core.config.get_settings", lambda: settings)

        # patch create_kms_provider 抛异常
        # 注意:patch core.kms.create_kms_provider (field_crypto.py 实际导入路径)
        # 不是 core.kms.factory.create_kms_provider (后者不影响已绑定的引用)
        with patch(
            "core.kms.create_kms_provider",
            side_effect=Exception("vault unavailable"),
        ):
            cipher = get_field_cipher()
        # 降级:envelope=None,但仍用本地 AES-GCM
        assert cipher._envelope is None
        assert cipher.enabled  # 用 field_encryption_key

    def test_kms_init_failure_production_raises(self, monkeypatch):
        """生产环境 KMS 初始化失败,硬 raise (避免明文落库)"""
        from core.config import Settings
        key = _make_key()
        settings = Settings(
            field_encryption_key=key,
            field_encryption_backend="vault",
            agentvalue_env="production",  # 生产
        )
        monkeypatch.setattr("core.config.get_settings", lambda: settings)

        with patch(
            "core.kms.create_kms_provider",
            side_effect=Exception("vault unavailable"),
        ):
            with pytest.raises(Exception) as exc_info:
                get_field_cipher()
        assert "vault unavailable" in str(exc_info.value) or "KMS" in str(exc_info.value)


# ============================================================
# JWT secret Vault fallback 测试
# ============================================================


class TestJWTSecretVaultFallback:
    """JWT secret 从 Vault KV v2 加载 + fallback env"""

    @pytest.fixture(autouse=True)
    def reset_jwt_cache(self):
        from auth.jwt_handler import reset_jwt_secret_cache
        reset_jwt_secret_cache()
        yield
        reset_jwt_secret_cache()

    def test_env_backend_uses_jwt_secret_key(self, monkeypatch):
        """backend=env 时,直接用 jwt_secret_key"""
        from core.config import Settings
        settings = Settings(
            jwt_secret_key="env-jwt-secret",
            field_encryption_backend="env",
        )
        monkeypatch.setattr("core.config.get_settings", lambda: settings)

        from auth.jwt_handler import _ensure_secret_key
        assert _ensure_secret_key(settings) == "env-jwt-secret"

    def test_vault_backend_fetches_from_kv(self, monkeypatch):
        """backend=vault 时,从 Vault KV v2 读 jwt secret"""
        from core.config import Settings
        settings = Settings(
            jwt_secret_key="env-fallback",
            field_encryption_backend="vault",
            vault_addr="http://vault:8200",
        )
        monkeypatch.setattr("core.config.get_settings", lambda: settings)

        # mock KMS Provider 返回 read_jwt_secret
        mock_kms = MagicMock()
        mock_kms.read_jwt_secret = AsyncMock(return_value="vault-jwt-secret")

        with patch("core.kms.factory.create_kms_provider", return_value=mock_kms):
            from auth.jwt_handler import _ensure_secret_key, reset_jwt_secret_cache
            reset_jwt_secret_cache()
            result = _ensure_secret_key(settings)
        assert result == "vault-jwt-secret"
        # 再次调用应命中 cache
        assert _ensure_secret_key(settings) == "vault-jwt-secret"

    def test_vault_backend_failure_fallback_to_env_non_production(self, monkeypatch):
        """非生产环境 Vault 失败时,fallback 到 env jwt_secret_key"""
        from core.config import Settings
        settings = Settings(
            jwt_secret_key="env-fallback",
            field_encryption_backend="vault",
            agentvalue_env="",
        )
        monkeypatch.setattr("core.config.get_settings", lambda: settings)

        with patch(
            "core.kms.factory.create_kms_provider",
            side_effect=Exception("vault unavailable"),
        ):
            from auth.jwt_handler import _ensure_secret_key, reset_jwt_secret_cache
            reset_jwt_secret_cache()
            result = _ensure_secret_key(settings)
        assert result == "env-fallback"

    def test_vault_backend_failure_production_raises(self, monkeypatch):
        """生产环境 Vault 失败时,硬 raise"""
        from core.config import Settings
        settings = Settings(
            jwt_secret_key="env-fallback",
            field_encryption_backend="vault",
            agentvalue_env="production",
        )
        monkeypatch.setattr("core.config.get_settings", lambda: settings)

        with patch(
            "core.kms.factory.create_kms_provider",
            side_effect=Exception("vault unavailable"),
        ):
            from auth.jwt_handler import _ensure_secret_key, reset_jwt_secret_cache
            reset_jwt_secret_cache()
            with pytest.raises(RuntimeError) as exc_info:
                _ensure_secret_key(settings)
        assert "vault" in str(exc_info.value).lower()

    def test_jwt_secret_cached_after_first_fetch(self, monkeypatch):
        """JWT secret 首次 fetch 后缓存,后续不再调 Vault"""
        from core.config import Settings
        settings = Settings(
            jwt_secret_key="env-fallback",
            field_encryption_backend="vault",
        )
        monkeypatch.setattr("core.config.get_settings", lambda: settings)

        mock_kms = MagicMock()
        mock_kms.read_jwt_secret = AsyncMock(return_value="vault-secret-v1")

        with patch("core.kms.factory.create_kms_provider", return_value=mock_kms):
            from auth.jwt_handler import _ensure_secret_key, reset_jwt_secret_cache
            reset_jwt_secret_cache()
            _ensure_secret_key(settings)
            _ensure_secret_key(settings)
            _ensure_secret_key(settings)
        # 只调用一次 Vault
        assert mock_kms.read_jwt_secret.call_count == 1


# ============================================================
# Factory 测试
# ============================================================


class TestKMSFactory:
    """create_kms_provider 工厂测试"""

    @pytest.fixture(autouse=True)
    def reset_factory(self):
        from core.kms.factory import reset_kms_provider_cache
        reset_kms_provider_cache()
        yield
        reset_kms_provider_cache()

    def test_env_backend_returns_none(self, monkeypatch):
        """backend=env 不创建 KMS (调用方降级 FieldCipher)"""
        from core.config import Settings
        from core.kms.factory import create_kms_provider
        settings = Settings(field_encryption_backend="env")
        monkeypatch.setattr("core.config.get_settings", lambda: settings)
        assert create_kms_provider() is None

    def test_local_backend_returns_local_provider(self, monkeypatch):
        from core.config import Settings
        from core.kms.factory import create_kms_provider
        settings = Settings(
            field_encryption_backend="local",
            field_encryption_key=_make_key(),
        )
        monkeypatch.setattr("core.config.get_settings", lambda: settings)
        kms = create_kms_provider()
        assert kms is not None
        assert kms.name == "local"

    def test_local_backend_production_rejected(self, monkeypatch):
        """生产环境拒绝 local backend"""
        from core.config import Settings
        from core.kms.factory import create_kms_provider
        from core.kms.base import KMSNotConfiguredError
        settings = Settings(
            field_encryption_backend="local",
            field_encryption_key=_make_key(),
            agentvalue_env="production",
        )
        monkeypatch.setattr("core.config.get_settings", lambda: settings)
        with pytest.raises(KMSNotConfiguredError):
            create_kms_provider()

    def test_vault_backend_missing_addr_raises(self, monkeypatch):
        from core.config import Settings
        from core.kms.factory import create_kms_provider
        from core.kms.base import KMSNotConfiguredError
        settings = Settings(
            field_encryption_backend="vault",
            vault_addr=None,
        )
        monkeypatch.setattr("core.config.get_settings", lambda: settings)
        with pytest.raises(KMSNotConfiguredError):
            create_kms_provider()

    def test_unknown_backend_raises(self, monkeypatch):
        from core.config import Settings
        from core.kms.factory import create_kms_provider
        from core.kms.base import KMSNotConfiguredError
        settings = Settings(field_encryption_backend="unknown-xyz")
        monkeypatch.setattr("core.config.get_settings", lambda: settings)
        with pytest.raises(KMSNotConfiguredError):
            create_kms_provider()

    def test_factory_caches_singleton(self, monkeypatch):
        from core.config import Settings
        from core.kms.factory import create_kms_provider
        settings = Settings(
            field_encryption_backend="local",
            field_encryption_key=_make_key(),
        )
        monkeypatch.setattr("core.config.get_settings", lambda: settings)
        kms1 = create_kms_provider()
        kms2 = create_kms_provider()
        assert kms1 is kms2
