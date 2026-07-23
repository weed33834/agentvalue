"""
全局测试配置
"""

import shutil
import tempfile

import pytest

from core.config import Settings, get_settings


@pytest.fixture(autouse=True)
def test_settings(monkeypatch):
    """测试环境配置：开启演示模式、使用临时向量库目录、设置测试 JWT 密钥。

    额外禁用 Settings 的 .env 加载，确保测试在无外部 .env 干扰下运行
    （开发者本地 .env 可能配置了 cloud_api_key 等，会让"无 key"测试失败）。
    测试需要 key 时应显式传参，如 Settings(cloud_api_key="fake-key")。
    """
    # 阻止 Settings 实例从 .env 文件加载（不影响显式 init 参数）
    # SettingsConfigDict 是 dict 子类，但 .copy() 不接受 keyword 参数，用 unpacking 重建
    from pydantic_settings import SettingsConfigDict

    new_config = SettingsConfigDict(**{**Settings.model_config, "env_file": None})
    monkeypatch.setattr(Settings, "model_config", new_config)
    # 注意：不调用 get_settings.cache_clear()，避免破坏 e2e module-scope fixture
    # 持有的 settings 实例引用（e2e_demo_mode 直接修改该实例的 jwt_secret_key）。
    # 新创建的 Settings(...) 实例会读取被 monkeypatch 的 model_config（env_file=None），
    # 从而不加载 .env；get_settings() 缓存的实例保持 e2e_demo_mode 的修改。
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_demo_mode", True)
    monkeypatch.setattr(
        settings, "jwt_secret_key", "test-only-jwt-secret-do-not-use-in-production"
    )
    # 清空缓存实例从 .env 加载的 API key，确保测试在无外部凭据下运行
    # （不 cache_clear 以保持 e2e module-scope fixture 持有的实例引用）
    for key in (
        "cloud_api_key",
        "openai_api_key",
        "embedding_api_key",
        "ocr_cloud_api_key",
        "ocr_cloud_secret_key",
        "asr_cloud_api_key",
        "local_api_key",
        "langfuse_public_key",
        "langfuse_secret_key",
        "s3_access_key",
        "s3_secret_key",
        "field_encryption_key",
    ):
        monkeypatch.setattr(settings, key, None)
    # 重置多模态/OCR/ASR provider 为默认 none/dummy，避免 .env 配置干扰
    monkeypatch.setattr(settings, "ocr_provider", "none")
    monkeypatch.setattr(settings, "asr_provider", "dummy")
    # P0 metrics 鉴权在测试中关闭:TestClient 的 client IP 是 "testclient" 字符串,
    # _ip_allowed 解析失败返回 False 导致 /metrics 403,无法读取指标断言。
    # 生产部署必须保持 ip/token 模式,这里仅测试环境放开
    monkeypatch.setattr(settings, "metrics_auth_mode", "none")

    tmp_dir = tempfile.mkdtemp(prefix="chroma_test_")
    monkeypatch.setattr(settings, "vector_store_dir", tmp_dir)

    yield tmp_dir

    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def clear_global_state():
    """每个测试前后清理全局 job_store / thread_store / token_blacklist,避免状态泄漏。"""
    from api import routes as routes_module
    from auth.token_blacklist import token_blacklist

    routes_module.job_store.clear()
    if hasattr(routes_module, "thread_store"):
        routes_module.thread_store.clear()
    # token_blacklist 在测试环境为 InMemoryTokenBlacklist,直接清内部 dict
    # (sync fixture 不能 await,直接操作底层 store)
    if hasattr(token_blacklist, "_store"):
        token_blacklist._store.clear()
    yield
    routes_module.job_store.clear()
    if hasattr(routes_module, "thread_store"):
        routes_module.thread_store.clear()
    if hasattr(token_blacklist, "_store"):
        token_blacklist._store.clear()
    # P3-4: 清理请求级 contextvar,避免上一个测试的 actor/tenant 上下文泄漏到下一个测试。
    # tenant_context 的 contextvar 实际名为 _current_tenant(非 _current_tenant_id)。
    # _current_tenant 的 default 是 DEFAULT_TENANT_ID,这里恢复到默认而非 None,
    # 保证下一个测试 get_current_tenant() 仍返回 DEFAULT_TENANT_ID(单租户兼容)。
    from core.tenant_context import _current_tenant
    from models.models import DEFAULT_TENANT_ID
    from services.audit_decorator import _current_actor_id, _current_actor_ip

    _current_actor_id.set(None)
    _current_actor_ip.set(None)
    _current_tenant.set(DEFAULT_TENANT_ID)
