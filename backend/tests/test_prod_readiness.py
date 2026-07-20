"""
生产就绪检查与 AUTH_DEMO_MODE 生产守护测试

覆盖：
- core.config.Settings 的生产环境守护校验器
- scripts.check_prod_readiness.check_readiness 各检查项与返回结构
"""

import pytest

from core.config import Settings
from scripts.check_prod_readiness import check_readiness


# 一组合法的「非默认」配置，用于构造可通过各项检查的 Settings
SAFE_JWT = "prod-strong-random-jwt-secret-0x9f8e7d6c5b4a"
SAFE_DB = "postgresql+asyncpg://user:pass@db-host:5432/agentvalue"


class TestConfigValidator:
    """core.config.Settings 的生产守护 model_validator。"""

    def test_production_demo_mode_raises(self, monkeypatch):
        """生产环境（AGENTVALUE_ENV=production）+ demo_mode=True -> 抛 ValueError。"""
        monkeypatch.setenv("AGENTVALUE_ENV", "production")
        monkeypatch.setenv("AUTH_DEMO_MODE", "true")
        with pytest.raises(ValueError, match="生产环境禁止开启 AUTH_DEMO_MODE"):
            Settings()

    def test_non_production_demo_mode_no_raise(self, monkeypatch):
        """非生产环境（默认）+ demo_mode=True -> 不抛（现有测试环境兼容）。"""
        # 确保未处于生产环境
        monkeypatch.delenv("AGENTVALUE_ENV", raising=False)
        monkeypatch.delenv("AUTH_DEMO_MODE", raising=False)
        settings = Settings(auth_demo_mode=True)
        assert settings.auth_demo_mode is True
        assert settings.agentvalue_env is None

    def test_production_demo_mode_off_no_raise(self, monkeypatch):
        """生产环境 + demo_mode=False -> 不抛。"""
        monkeypatch.setenv("AGENTVALUE_ENV", "production")
        monkeypatch.setenv("AUTH_DEMO_MODE", "false")
        settings = Settings()
        assert settings.agentvalue_env == "production"
        assert settings.auth_demo_mode is False

    def test_non_production_env_value_no_raise(self, monkeypatch):
        """AGENTVALUE_ENV 为非 production 值（如 staging）+ demo_mode=True -> 不抛。"""
        monkeypatch.setenv("AGENTVALUE_ENV", "staging")
        monkeypatch.setenv("AUTH_DEMO_MODE", "true")
        settings = Settings()
        assert settings.agentvalue_env == "staging"
        assert settings.auth_demo_mode is True


class TestCheckReadiness:
    """check_readiness 各检查项与返回结构。"""

    def test_demo_mode_fail_when_enabled(self):
        """AUTH_DEMO_MODE 开启时该项 FAIL。"""
        settings = Settings(
            auth_demo_mode=True,
            jwt_secret_key=SAFE_JWT,
            database_url=SAFE_DB,
            model_tier="L1",
        )
        result = check_readiness(settings)
        demo_check = next(c for c in result["checks"] if c["name"] == "auth_demo_mode")
        assert demo_check["status"] == "FAIL"
        assert result["all_passed"] is False

    def test_demo_mode_pass_when_disabled(self):
        """AUTH_DEMO_MODE 关闭时该项 PASS。"""
        settings = Settings(
            auth_demo_mode=False,
            jwt_secret_key=SAFE_JWT,
            database_url=SAFE_DB,
            model_tier="L1",
        )
        result = check_readiness(settings)
        demo_check = next(c for c in result["checks"] if c["name"] == "auth_demo_mode")
        assert demo_check["status"] == "PASS"
        assert result["all_passed"] is True

    def test_jwt_default_value_fails(self):
        """JWT_SECRET_KEY 为默认占位值时 FAIL。"""
        for bad_key in (
            "change-me",
            "your-secret-key",
            "change-this-to-a-strong-random-secret",
            "",
        ):
            settings = Settings(
                auth_demo_mode=False,
                jwt_secret_key=bad_key,
                database_url=SAFE_DB,
                model_tier="L1",
            )
            result = check_readiness(settings)
            jwt_check = next(
                c for c in result["checks"] if c["name"] == "jwt_secret_key"
            )
            assert jwt_check["status"] == "FAIL", f"期望 {bad_key!r} 判为 FAIL"
            assert result["all_passed"] is False

    def test_jwt_none_fails(self):
        """JWT_SECRET_KEY 未设置（None）时 FAIL。"""
        settings = Settings(
            auth_demo_mode=False,
            jwt_secret_key=None,
            database_url=SAFE_DB,
            model_tier="L1",
        )
        result = check_readiness(settings)
        jwt_check = next(c for c in result["checks"] if c["name"] == "jwt_secret_key")
        assert jwt_check["status"] == "FAIL"

    def test_jwt_set_passes(self):
        """JWT_SECRET_KEY 修改为非默认值后 PASS。"""
        settings = Settings(
            auth_demo_mode=False,
            jwt_secret_key=SAFE_JWT,
            database_url=SAFE_DB,
            model_tier="L1",
        )
        result = check_readiness(settings)
        jwt_check = next(c for c in result["checks"] if c["name"] == "jwt_secret_key")
        assert jwt_check["status"] == "PASS"

    def test_database_sqlite_warns_not_fail(self):
        """DATABASE_URL 为 sqlite 时给 WARN，不导致 all_passed=False。"""
        settings = Settings(
            auth_demo_mode=False,
            jwt_secret_key=SAFE_JWT,
            database_url="sqlite+aiosqlite:///./test.db",
            model_tier="L1",
        )
        result = check_readiness(settings)
        db_check = next(c for c in result["checks"] if c["name"] == "database_url")
        assert db_check["status"] == "WARN"
        # WARN 不算 FAIL，故 all_passed 仍为 True
        assert result["all_passed"] is True

    def test_model_tier_auto_warns(self):
        """MODEL_TIER 为 auto 时给 WARN。"""
        settings = Settings(
            auth_demo_mode=False,
            jwt_secret_key=SAFE_JWT,
            database_url=SAFE_DB,
            model_tier="auto",
        )
        result = check_readiness(settings)
        tier_check = next(c for c in result["checks"] if c["name"] == "model_tier")
        assert tier_check["status"] == "WARN"

    def test_structure_and_all_passed_true(self):
        """返回结构正确，关键项全 PASS（仅允许 WARN）时 all_passed=True。

        检查项数量随版本演进（当前为 9：demo_mode / jwt_secret / database_url /
        model_tier / field_encryption_key / cloud_credentials / cors_origins /
        jwt_algorithm / kms_configured (H5)），用名字集合做断言更稳健。
        """
        settings = Settings(
            auth_demo_mode=False,
            jwt_secret_key=SAFE_JWT,
            database_url=SAFE_DB,
            model_tier="L2",
        )
        result = check_readiness(settings)
        assert "checks" in result
        assert "all_passed" in result
        assert isinstance(result["checks"], list)
        # 9 项：4 项基础 + field_encryption_key + cloud_credentials
        #       + cors_origins(P1) + jwt_algorithm(P1-6) + kms_configured(H5)
        assert len(result["checks"]) == 9
        names = {c["name"] for c in result["checks"]}
        assert names == {
            "auth_demo_mode",
            "jwt_secret_key",
            "database_url",
            "model_tier",
            "field_encryption_key",
            "cloud_credentials",
            "cors_origins",
            "jwt_algorithm",
            "kms_configured",
        }
        for check in result["checks"]:
            assert {"name", "status", "message"}.issubset(check.keys())
            assert check["status"] in {"PASS", "FAIL", "WARN"}
        # field_encryption_key 未配置 + 非生产环境 -> WARN；cloud_credentials 未配置 -> WARN
        # kms_configured (env/local 非生产) -> WARN；均不计入 FAIL，故 all_passed 仍为 True
        assert result["all_passed"] is True

    # ---------------- field_encryption_key 检查项 ----------------

    def test_field_encryption_key_unconfigured_warns_in_non_prod(self):
        """非生产环境 + 未配置 FIELD_ENCRYPTION_KEY -> WARN（不 FAIL）。"""
        settings = Settings(
            auth_demo_mode=False,
            jwt_secret_key=SAFE_JWT,
            database_url=SAFE_DB,
            model_tier="L1",
            # agentvalue_env 不设默认 None（非生产）
            field_encryption_key=None,
        )
        result = check_readiness(settings)
        check = next(c for c in result["checks"] if c["name"] == "field_encryption_key")
        assert check["status"] == "WARN"
        assert result["all_passed"] is True  # WARN 不算 FAIL

    def test_field_encryption_key_unconfigured_fails_in_production(self):
        """生产环境 + 未配置 FIELD_ENCRYPTION_KEY -> FAIL。"""
        settings = Settings(
            auth_demo_mode=False,
            jwt_secret_key=SAFE_JWT,
            database_url=SAFE_DB,
            model_tier="L1",
            agentvalue_env="production",
            field_encryption_key=None,
        )
        result = check_readiness(settings)
        check = next(c for c in result["checks"] if c["name"] == "field_encryption_key")
        assert check["status"] == "FAIL"
        assert result["all_passed"] is False

    def test_field_encryption_key_placeholder_fails_in_production(self):
        """生产环境 + 占位值 -> FAIL。"""
        for placeholder in (
            "",
            "change-me",
            "your-field-encryption-key",
            "placeholder",
        ):
            settings = Settings(
                auth_demo_mode=False,
                jwt_secret_key=SAFE_JWT,
                database_url=SAFE_DB,
                model_tier="L1",
                agentvalue_env="production",
                field_encryption_key=placeholder,
            )
            result = check_readiness(settings)
            check = next(
                c for c in result["checks"] if c["name"] == "field_encryption_key"
            )
            assert check["status"] == "FAIL", f"占位值 {placeholder!r} 应判 FAIL"

    def test_field_encryption_key_configured_passes(self):
        """生产环境 + 已配置真实密钥 -> PASS。"""
        import base64
        import os

        real_key = base64.b64encode(os.urandom(32)).decode()
        settings = Settings(
            auth_demo_mode=False,
            jwt_secret_key=SAFE_JWT,
            database_url=SAFE_DB,
            model_tier="L1",
            agentvalue_env="production",
            field_encryption_key=real_key,
        )
        result = check_readiness(settings)
        check = next(c for c in result["checks"] if c["name"] == "field_encryption_key")
        assert check["status"] == "PASS"

    # ---------------- cloud_credentials 检查项 ----------------

    def test_cloud_credentials_unconfigured_warns(self):
        """OCR/ASR 云端凭据均未配置 -> WARN（生产也仅 WARN，不阻断）。"""
        settings = Settings(
            auth_demo_mode=False,
            jwt_secret_key=SAFE_JWT,
            database_url=SAFE_DB,
            model_tier="L1",
            agentvalue_env="production",
            ocr_cloud_api_key=None,
            asr_cloud_api_key=None,
        )
        result = check_readiness(settings)
        check = next(c for c in result["checks"] if c["name"] == "cloud_credentials")
        assert check["status"] == "WARN"

    def test_cloud_credentials_partial_configured_warns(self):
        """仅配置 OCR 未配置 ASR -> 仍 WARN（任一缺失即 WARN）。"""
        settings = Settings(
            auth_demo_mode=False,
            jwt_secret_key=SAFE_JWT,
            database_url=SAFE_DB,
            model_tier="L1",
            agentvalue_env="production",
            ocr_cloud_api_key="some-real-ocr-key",
            asr_cloud_api_key=None,
        )
        result = check_readiness(settings)
        check = next(c for c in result["checks"] if c["name"] == "cloud_credentials")
        assert check["status"] == "WARN"
        assert "ASR_CLOUD_API_KEY" in check["message"]

    def test_cloud_credentials_both_configured_passes(self):
        """OCR 与 ASR 云端凭据均配置 -> PASS。"""
        settings = Settings(
            auth_demo_mode=False,
            jwt_secret_key=SAFE_JWT,
            database_url=SAFE_DB,
            model_tier="L1",
            agentvalue_env="production",
            ocr_cloud_api_key="real-ocr-key",
            asr_cloud_api_key="real-asr-key",
        )
        result = check_readiness(settings)
        check = next(c for c in result["checks"] if c["name"] == "cloud_credentials")
        assert check["status"] == "PASS"

    def test_all_passed_false_when_any_fail(self):
        """存在 FAIL 项时 all_passed=False。"""
        settings = Settings(
            auth_demo_mode=True,  # FAIL
            jwt_secret_key=SAFE_JWT,
            database_url=SAFE_DB,
            model_tier="L1",
        )
        result = check_readiness(settings)
        assert result["all_passed"] is False
