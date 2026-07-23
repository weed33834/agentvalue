"""
Provider Admin API 路由测试 (P4 测试补全)

覆盖 api/admin/providers.py 的核心端点:
- Provider 模板列表/详情
- 租户 Provider 绑定(启用/禁用)
- 凭证 CRUD (创建/列表脱敏/激活切换/删除/测试连接)
- 默认模型管理
- 健康检查历史

鉴权: 依赖 conftest.py 的 auth_demo_mode=True,用 x-user-role header 模拟 ADMIN。
DB: 临时文件 SQLite,seed 4 个 provider 模板。
HTTP: monkeypatch _validate_provider_credentials 避免真实 OpenAI 调用。
"""

import base64
import os
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.admin.providers import router as providers_router
from core.config import get_settings


# ============================================================
# Fixtures
# ============================================================


def _admin_headers() -> dict:
    return {"x-user-role": "admin", "x-user-id": "ADMIN001"}


def _body(resp):
    """提取响应体。

    FastAPI 将路由返回的 tuple (dict, status_code) 序列化为 JSON 数组
    [dict, status_code],这里统一还原为 dict。
    """
    data = resp.json()
    if isinstance(data, list) and len(data) >= 1 and isinstance(data[0], dict):
        return data[0]
    return data


@pytest.fixture
def temp_db(monkeypatch):
    """临时文件 SQLite + 启用字段加密。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp.name}"

    # 启用字段加密,以便凭证加密存储 + 脱敏
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setattr(get_settings(), "field_encryption_key", key)

    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from core import database as db_module

    engine = create_async_engine(
        db_url,
        echo=False,
        future=True,
        connect_args={"check_same_thread": False},
    )
    db_module.engine = engine
    db_module.AsyncSessionLocal = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )

    yield

    try:
        Path(tmp.name).unlink(missing_ok=True)
    except Exception:
        pass


@pytest.fixture
async def seeded_db(temp_db):
    """建表 + seed provider 模板。"""
    from core import database as db_module
    from core.providers.seed import seed_provider_templates

    await db_module.init_db()
    async with db_module.AsyncSessionLocal() as sess:
        await seed_provider_templates(sess)
    yield
    await db_module.close_db()


@pytest.fixture
def client(seeded_db):
    """最小 FastAPI app + TestClient。"""
    app = FastAPI()
    app.include_router(providers_router)
    with TestClient(app) as c:
        yield c


# ============================================================
# Provider 模板
# ============================================================


def test_list_provider_templates(client):
    """GET /providers → 200,返回 4 个 provider 模板"""
    resp = client.get(
        "/api/v1/admin/model-providers/providers", headers=_admin_headers()
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 4
    providers = {p["provider"] for p in data}
    assert providers == {"openai", "anthropic", "gemini", "ollama"}


def test_list_provider_templates_filter_by_model_type(client):
    """GET /providers?model_type=llm 过滤"""
    resp = client.get(
        "/api/v1/admin/model-providers/providers",
        params={"model_type": "llm"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    # openai/anthropic/gemini 支持 llm,ollama 也支持 llm
    assert len(data) >= 3


def test_get_provider_template_found(client):
    """GET /providers/openai → 200"""
    resp = client.get(
        "/api/v1/admin/model-providers/providers/openai", headers=_admin_headers()
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "openai"
    assert "models" in data
    assert "provider_credential_schema" in data


def test_get_provider_template_not_found(client):
    """GET /providers/nonexistent → 404"""
    resp = client.get(
        "/api/v1/admin/model-providers/providers/nonexistent",
        headers=_admin_headers(),
    )
    assert resp.status_code == 404


# ============================================================
# 租户 Provider 绑定
# ============================================================


def test_list_tenant_providers(client):
    """GET /workspaces/current/providers → 200"""
    resp = client.get(
        "/api/v1/admin/model-providers/workspaces/current/providers",
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 4


def test_update_preferred_type_enable(client):
    """POST .../preferred-type {enabled:true} → 200"""
    resp = client.post(
        "/api/v1/admin/model-providers/workspaces/current/providers/openai/preferred-type",
        json={"enabled": True},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True


# ============================================================
# 凭证 CRUD
# ============================================================


def _create_credential(
    client, provider="openai", name="test", api_key="sk-test12345678901234567"
):
    """辅助:创建凭证并返回 credential_id"""
    resp = client.post(
        f"/api/v1/admin/model-providers/workspaces/current/providers/{provider}/credentials",
        json={
            "credential_name": name,
            "credentials": {
                "api_key": api_key,
                "api_base": "https://api.openai.com/v1",
            },
        },
        headers=_admin_headers(),
    )
    assert resp.status_code in (200, 201), resp.text
    return _body(resp)["credential_id"]


def test_create_credential(client):
    """POST .../credentials → 200/201"""
    resp = client.post(
        "/api/v1/admin/model-providers/workspaces/current/providers/openai/credentials",
        json={
            "credential_name": "test",
            "credentials": {
                "api_key": "sk-test12345678901234567",
                "api_base": "https://api.openai.com/v1",
            },
        },
        headers=_admin_headers(),
    )
    assert resp.status_code in (200, 201)
    body = _body(resp)
    assert body["result"] == "success"
    assert "credential_id" in body


def test_list_credentials_masked(client):
    """GET .../credentials → 200,返回脱敏值"""
    api_key = "sk-test12345678901234567"
    _create_credential(client, api_key=api_key)

    resp = client.get(
        "/api/v1/admin/model-providers/workspaces/current/providers/openai/credentials",
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 1
    masked_key = data[0]["credentials_masked"]["api_key"]
    # 预期:前 2 + **** + 后 4
    expected = api_key[:2] + "****" + api_key[-4:]
    assert masked_key == expected
    assert "****" in masked_key
    # api_base 不脱敏
    assert data[0]["credentials_masked"]["api_base"] == "https://api.openai.com/v1"


def test_activate_credential(client):
    """POST .../credentials/{id}/activate → 200"""
    cid1 = _create_credential(client, name="primary", api_key="sk-primary1234567890")
    cid2 = _create_credential(client, name="secondary", api_key="sk-secondary123456789")

    resp = client.post(
        f"/api/v1/admin/model-providers/workspaces/current/providers/openai/credentials/{cid2}/activate",
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["result"] == "success"

    # 验证激活切换生效
    list_resp = client.get(
        "/api/v1/admin/model-providers/workspaces/current/providers/openai/credentials",
        headers=_admin_headers(),
    )
    creds = list_resp.json()["data"]
    active = [c for c in creds if c["is_active"]]
    assert len(active) == 1
    assert active[0]["credential_id"] == cid2


def test_validate_credentials_success(client, monkeypatch):
    """POST .../credentials/validate → 200 (mock 成功)"""

    async def mock_validate(provider, credentials, model_name=None):
        return True

    from api.admin import providers as providers_module

    monkeypatch.setattr(
        providers_module, "_validate_provider_credentials", mock_validate
    )

    resp = client.post(
        "/api/v1/admin/model-providers/workspaces/current/providers/openai/credentials/validate",
        json={"credentials": {"api_key": "sk-valid"}},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["result"] == "success"


def test_validate_credentials_failure_returns_200(client, monkeypatch):
    """POST .../credentials/validate 失败也返回 200 (mock 失败)"""

    async def mock_validate(provider, credentials, model_name=None):
        return False

    from api.admin import providers as providers_module

    monkeypatch.setattr(
        providers_module, "_validate_provider_credentials", mock_validate
    )

    resp = client.post(
        "/api/v1/admin/model-providers/workspaces/current/providers/openai/credentials/validate",
        json={"credentials": {"api_key": "sk-invalid"}},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["result"] == "error"


def test_delete_credential(client):
    """DELETE .../credentials/{id} → 200"""
    cid = _create_credential(client, name="to-delete")

    resp = client.delete(
        f"/api/v1/admin/model-providers/workspaces/current/providers/openai/credentials/{cid}",
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["result"] == "success"

    # 删除后列表为空
    list_resp = client.get(
        "/api/v1/admin/model-providers/workspaces/current/providers/openai/credentials",
        headers=_admin_headers(),
    )
    assert len(list_resp.json()["data"]) == 0


def test_delete_credential_not_found(client):
    """DELETE 不存在的凭证 → 404"""
    resp = client.delete(
        "/api/v1/admin/model-providers/workspaces/current/providers/openai/credentials/nonexistent",
        headers=_admin_headers(),
    )
    assert resp.status_code == 404


# ============================================================
# 默认模型
# ============================================================


def test_list_default_models_empty(client):
    """GET .../default-models → 200,初始为空"""
    resp = client.get(
        "/api/v1/admin/model-providers/workspaces/current/default-models",
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == []


def test_set_default_model(client):
    """POST .../default-models → 200"""
    resp = client.post(
        "/api/v1/admin/model-providers/workspaces/current/default-models",
        json={"model_type": "llm", "provider": "openai", "model_name": "gpt-4o"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["result"] == "success"

    # 验证已写入
    list_resp = client.get(
        "/api/v1/admin/model-providers/workspaces/current/default-models",
        headers=_admin_headers(),
    )
    data = list_resp.json()["data"]
    assert len(data) == 1
    assert data[0]["provider"] == "openai"
    assert data[0]["model_name"] == "gpt-4o"


def test_set_default_model_upsert(client):
    """同 model_type 再次设置应覆盖"""
    client.post(
        "/api/v1/admin/model-providers/workspaces/current/default-models",
        json={"model_type": "llm", "provider": "openai", "model_name": "gpt-4o"},
        headers=_admin_headers(),
    )
    client.post(
        "/api/v1/admin/model-providers/workspaces/current/default-models",
        json={"model_type": "llm", "provider": "anthropic", "model_name": "claude-3"},
        headers=_admin_headers(),
    )
    list_resp = client.get(
        "/api/v1/admin/model-providers/workspaces/current/default-models",
        headers=_admin_headers(),
    )
    data = list_resp.json()["data"]
    assert len(data) == 1
    assert data[0]["provider"] == "anthropic"


# ============================================================
# 健康检查
# ============================================================


def test_list_health_checks_empty(client):
    """GET .../providers/openai/health-checks → 200"""
    resp = client.get(
        "/api/v1/admin/model-providers/workspaces/current/providers/openai/health-checks",
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == []


def test_trigger_health_check_no_credentials(client):
    """POST .../providers/openai/health-check 无凭证时返回 error"""
    resp = client.post(
        "/api/v1/admin/model-providers/workspaces/current/providers/openai/health-check",
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["result"] == "error"


# ============================================================
# 模型列表
# ============================================================


def test_list_tenant_models(client):
    """GET .../providers/openai/models → 200"""
    resp = client.get(
        "/api/v1/admin/model-providers/workspaces/current/providers/openai/models",
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    # openai 有 4 个模型模板 (gpt-4o, gpt-4o-mini, text-embedding-3-small, text-embedding-3-large)
    assert len(data) >= 3
    assert any(m["model"] == "gpt-4o" for m in data)
