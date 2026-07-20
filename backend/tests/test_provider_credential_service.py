"""
ProviderCredentialService 单元测试 (P4 测试补全)

覆盖 core/providers/credential_service.py:
- 凭证加密/解密 (FieldCipher 集成)
- mask_secret / mask_credentials 脱敏
- 多凭证 CRUD + 激活切换
- 健康检查 + 冷却熔断
- 默认模型 CRUD

用 sqlite in-memory session (StaticPool 保证 in-memory DB 跨 session 共享)。
"""

import base64
import json
import os

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from core.config import get_settings
from core.providers.credential_service import ProviderCredentialService
from core.providers.seed import OPENAI_PROVIDER_TEMPLATE

TENANT_ID = "tenant-test"
PROVIDER = "openai"


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
async def db_session(monkeypatch):
    """in-memory sqlite session,启用字段加密。"""
    # 注入合法的 field_encryption_key (32 字节 base64)
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setattr(get_settings(), "field_encryption_key", key)

    from core import database as db_module

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db_module.engine = engine
    db_module.AsyncSessionLocal = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    await db_module.init_db()
    async with db_module.AsyncSessionLocal() as sess:
        yield sess
    await engine.dispose()


# ============================================================
# encrypt / decrypt
# ============================================================


@pytest.mark.asyncio
async def test_encrypt_decrypt_credential_roundtrip(db_session):
    """加密后解密应还原原文"""
    svc = ProviderCredentialService(db_session)
    plain = {"api_key": "sk-test123"}
    cipher = svc.encrypt_credential(plain)
    assert svc.decrypt_credential(cipher) == plain


@pytest.mark.asyncio
async def test_encrypt_credential_produces_ciphertext(db_session):
    """启用加密时,密文不应是明文 JSON"""
    svc = ProviderCredentialService(db_session)
    plain = {"api_key": "sk-test123"}
    cipher = svc.encrypt_credential(plain)
    assert cipher != json.dumps(plain)
    # 密文不是合法 JSON(是 base64)
    with pytest.raises(json.JSONDecodeError):
        json.loads(cipher)


@pytest.mark.asyncio
async def test_encrypt_credential_different_each_time(db_session):
    """AES-GCM 每次加密 nonce 不同,密文应不同"""
    svc = ProviderCredentialService(db_session)
    plain = {"api_key": "sk-test123"}
    c1 = svc.encrypt_credential(plain)
    c2 = svc.encrypt_credential(plain)
    assert c1 != c2
    assert svc.decrypt_credential(c1) == plain
    assert svc.decrypt_credential(c2) == plain


# ============================================================
# mask_secret
# ============================================================


def test_mask_secret_normal():
    """前 2 + 后 4,中间 4 星"""
    assert ProviderCredentialService.mask_secret("sk-test1234567890") == "sk****7890"


def test_mask_secret_short():
    """len <= 6 → '****'"""
    assert ProviderCredentialService.mask_secret("short") == "****"


def test_mask_secret_exact_6():
    """len = 6 → '****'"""
    assert ProviderCredentialService.mask_secret("abcdef") == "****"


def test_mask_secret_len_7():
    """len = 7: value[:2] + '****' + value[-4:]"""
    assert ProviderCredentialService.mask_secret("abcdefg") == "ab****defg"


def test_mask_secret_empty():
    assert ProviderCredentialService.mask_secret("") == ""


def test_mask_secret_none():
    assert ProviderCredentialService.mask_secret(None) == ""


# ============================================================
# mask_credentials (schema-aware)
# ============================================================


@pytest.mark.asyncio
async def test_mask_credentials_schema_aware(db_session):
    """只脱敏 schema 中 type=secret-input 的字段"""
    svc = ProviderCredentialService(db_session)
    schema = OPENAI_PROVIDER_TEMPLATE["provider_credential_schema"]
    creds = {
        "api_key": "sk-test1234567890",
        "api_base": "https://api.openai.com/v1",
    }
    masked = svc.mask_credentials(creds, schema)
    assert masked["api_key"] == "sk****7890"
    assert masked["api_base"] == "https://api.openai.com/v1"


@pytest.mark.asyncio
async def test_mask_credentials_no_schema(db_session):
    """无 schema 时不脱敏"""
    svc = ProviderCredentialService(db_session)
    creds = {"api_key": "sk-test1234567890"}
    masked = svc.mask_credentials(creds, schema=None)
    assert masked["api_key"] == "sk-test1234567890"


@pytest.mark.asyncio
async def test_mask_credentials_empty(db_session):
    svc = ProviderCredentialService(db_session)
    assert svc.mask_credentials({}) == {}


# ============================================================
# create_credential + 激活切换
# ============================================================


@pytest.mark.asyncio
async def test_create_credential_first_activates(db_session):
    """首次创建凭证自动激活"""
    svc = ProviderCredentialService(db_session)
    row, is_valid = await svc.create_credential(
        tenant_id=TENANT_ID,
        provider_name=PROVIDER,
        credential_name="primary",
        credentials={"api_key": "sk-test123"},
    )
    assert row.id is not None
    assert is_valid is True
    tp = await svc.get_tenant_provider(TENANT_ID, PROVIDER)
    assert tp is not None
    assert tp.active_credential_id == row.id
    assert tp.enabled is True


@pytest.mark.asyncio
async def test_create_credential_second_not_activated(db_session):
    """第二次创建不自动激活,仍指向第一个"""
    svc = ProviderCredentialService(db_session)
    row1, _ = await svc.create_credential(
        TENANT_ID, PROVIDER, "primary", {"api_key": "sk-1"}
    )
    row2, _ = await svc.create_credential(
        TENANT_ID, PROVIDER, "secondary", {"api_key": "sk-2"}
    )
    tp = await svc.get_tenant_provider(TENANT_ID, PROVIDER)
    assert tp.active_credential_id == row1.id


@pytest.mark.asyncio
async def test_activate_credential_switches(db_session):
    """activate_credential 切换激活指针"""
    svc = ProviderCredentialService(db_session)
    row1, _ = await svc.create_credential(
        TENANT_ID, PROVIDER, "primary", {"api_key": "sk-1"}
    )
    row2, _ = await svc.create_credential(
        TENANT_ID, PROVIDER, "secondary", {"api_key": "sk-2"}
    )
    ok = await svc.activate_credential(TENANT_ID, PROVIDER, row2.id)
    assert ok is True
    tp = await svc.get_tenant_provider(TENANT_ID, PROVIDER)
    assert tp.active_credential_id == row2.id


@pytest.mark.asyncio
async def test_activate_credential_not_found(db_session):
    svc = ProviderCredentialService(db_session)
    ok = await svc.activate_credential(TENANT_ID, PROVIDER, "nonexistent-id")
    assert ok is False


@pytest.mark.asyncio
async def test_list_credentials(db_session):
    svc = ProviderCredentialService(db_session)
    await svc.create_credential(TENANT_ID, PROVIDER, "primary", {"api_key": "sk-1"})
    await svc.create_credential(TENANT_ID, PROVIDER, "secondary", {"api_key": "sk-2"})
    creds = await svc.list_credentials(TENANT_ID, PROVIDER)
    assert len(creds) == 2


@pytest.mark.asyncio
async def test_delete_credential_switches_active(db_session):
    """删除激活凭证后自动切换到其他可用凭证"""
    svc = ProviderCredentialService(db_session)
    row1, _ = await svc.create_credential(
        TENANT_ID, PROVIDER, "primary", {"api_key": "sk-1"}
    )
    row2, _ = await svc.create_credential(
        TENANT_ID, PROVIDER, "secondary", {"api_key": "sk-2"}
    )
    await svc.activate_credential(TENANT_ID, PROVIDER, row2.id)
    ok = await svc.delete_credential(TENANT_ID, PROVIDER, row2.id)
    assert ok is True
    tp = await svc.get_tenant_provider(TENANT_ID, PROVIDER)
    assert tp.active_credential_id == row1.id


@pytest.mark.asyncio
async def test_delete_credential_not_found(db_session):
    svc = ProviderCredentialService(db_session)
    ok = await svc.delete_credential(TENANT_ID, PROVIDER, "nonexistent-id")
    assert ok is False


# ============================================================
# get_active_credentials
# ============================================================


@pytest.mark.asyncio
async def test_get_active_credentials_returns_plaintext(db_session):
    """get_active_credentials 返回当前激活凭证的明文"""
    svc = ProviderCredentialService(db_session)
    await svc.create_credential(
        TENANT_ID, PROVIDER, "primary", {"api_key": "sk-test123"}
    )
    creds = await svc.get_active_credentials(TENANT_ID, PROVIDER)
    assert creds == {"api_key": "sk-test123"}


@pytest.mark.asyncio
async def test_get_active_credentials_none_when_no_provider(db_session):
    """无 provider 绑定时返回 None"""
    svc = ProviderCredentialService(db_session)
    creds = await svc.get_active_credentials(TENANT_ID, PROVIDER)
    assert creds is None


@pytest.mark.asyncio
async def test_get_active_credentials_none_when_disabled(db_session):
    """provider 被禁用时返回 None"""
    svc = ProviderCredentialService(db_session)
    await svc.create_credential(
        TENANT_ID, PROVIDER, "primary", {"api_key": "sk-test123"}
    )
    tp = await svc.get_tenant_provider(TENANT_ID, PROVIDER)
    tp.enabled = False
    await db_session.flush()
    creds = await svc.get_active_credentials(TENANT_ID, PROVIDER)
    assert creds is None


# ============================================================
# 健康检查 + 冷却熔断
# ============================================================


@pytest.mark.asyncio
async def test_record_failure_triggers_cooldown(db_session):
    """record_failure 触发冷却后,get_active_credentials 返回 None"""
    svc = ProviderCredentialService(db_session)
    row, _ = await svc.create_credential(
        TENANT_ID, PROVIDER, "primary", {"api_key": "sk-test123"}
    )
    await svc.record_failure(TENANT_ID, PROVIDER, row.id, "timeout")
    cred = await svc.get_credential(TENANT_ID, PROVIDER, row.id)
    assert cred.failure_count == 1
    assert cred.cooldown_until is not None
    # 冷却中,get_active_credentials 应跳过该凭证
    creds = await svc.get_active_credentials(TENANT_ID, PROVIDER)
    assert creds is None


@pytest.mark.asyncio
async def test_record_failure_threshold_down(db_session):
    """连续失败 3 次后 is_valid=False"""
    svc = ProviderCredentialService(db_session)
    row, _ = await svc.create_credential(
        TENANT_ID, PROVIDER, "primary", {"api_key": "sk-test123"}
    )
    for _ in range(3):
        await svc.record_failure(TENANT_ID, PROVIDER, row.id, "err")
    cred = await svc.get_credential(TENANT_ID, PROVIDER, row.id)
    assert cred.is_valid is False
    assert cred.failure_count == 3


@pytest.mark.asyncio
async def test_record_success_clears_failures(db_session):
    """record_success 清零失败计数"""
    svc = ProviderCredentialService(db_session)
    row, _ = await svc.create_credential(
        TENANT_ID, PROVIDER, "primary", {"api_key": "sk-test123"}
    )
    await svc.record_failure(TENANT_ID, PROVIDER, row.id, "err")
    await svc.record_success(TENANT_ID, PROVIDER, row.id, latency_ms=50)
    cred = await svc.get_credential(TENANT_ID, PROVIDER, row.id)
    assert cred.failure_count == 0
    assert cred.is_valid is True
    assert cred.cooldown_until is None


@pytest.mark.asyncio
async def test_list_health_checks_empty(db_session):
    svc = ProviderCredentialService(db_session)
    checks = await svc.list_health_checks(TENANT_ID, PROVIDER)
    assert checks == []


@pytest.mark.asyncio
async def test_record_health_check(db_session):
    svc = ProviderCredentialService(db_session)
    await svc.record_health_check(
        TENANT_ID, PROVIDER, status="healthy", latency_ms=50
    )
    checks = await svc.list_health_checks(TENANT_ID, PROVIDER)
    assert len(checks) == 1
    assert checks[0].status == "healthy"
    assert checks[0].latency_ms == 50


@pytest.mark.asyncio
async def test_record_failure_writes_health_check(db_session):
    """record_failure 同时写入一条健康检查记录"""
    svc = ProviderCredentialService(db_session)
    row, _ = await svc.create_credential(
        TENANT_ID, PROVIDER, "primary", {"api_key": "sk-test123"}
    )
    await svc.record_failure(TENANT_ID, PROVIDER, row.id, "timeout")
    checks = await svc.list_health_checks(TENANT_ID, PROVIDER)
    assert len(checks) == 1
    assert checks[0].status == "degraded"
    assert checks[0].credential_id == row.id


# ============================================================
# 默认模型 CRUD
# ============================================================


@pytest.mark.asyncio
async def test_list_default_models_empty(db_session):
    svc = ProviderCredentialService(db_session)
    models = await svc.list_default_models(TENANT_ID)
    assert models == []


@pytest.mark.asyncio
async def test_set_default_model_insert(db_session):
    svc = ProviderCredentialService(db_session)
    row = await svc.set_default_model(TENANT_ID, "llm", "openai", "gpt-4o")
    assert row.provider == "openai"
    assert row.model_name == "gpt-4o"
    models = await svc.list_default_models(TENANT_ID)
    assert len(models) == 1


@pytest.mark.asyncio
async def test_set_default_model_upsert(db_session):
    """同 model_type 再次设置应 upsert 而非插入"""
    svc = ProviderCredentialService(db_session)
    await svc.set_default_model(TENANT_ID, "llm", "openai", "gpt-4o")
    row = await svc.set_default_model(TENANT_ID, "llm", "anthropic", "claude-3")
    assert row.provider == "anthropic"
    assert row.model_name == "claude-3"
    models = await svc.list_default_models(TENANT_ID)
    assert len(models) == 1


@pytest.mark.asyncio
async def test_set_default_model_different_types(db_session):
    """不同 model_type 各自独立"""
    svc = ProviderCredentialService(db_session)
    await svc.set_default_model(TENANT_ID, "llm", "openai", "gpt-4o")
    await svc.set_default_model(TENANT_ID, "embedding", "openai", "text-embedding-3-small")
    models = await svc.list_default_models(TENANT_ID)
    assert len(models) == 2
