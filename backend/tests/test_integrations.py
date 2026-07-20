"""Integrations 适配层单测(P7)

覆盖:
- DummyIMAdapter: send_text / send_card / parse_webhook / verify_webhook_signature
- DummyCodeRepoAdapter: list_commits / list_merge_requests / parse_webhook / verify_webhook_signature
- 工厂: 未配置时返回 Dummy; 配置后真实适配器未实现时降级 Dummy
"""
from datetime import datetime, timezone

import pytest

from integrations import (
    DummyCodeRepoAdapter,
    DummyIMAdapter,
    create_coderepo_adapter,
    create_im_adapter,
)
from integrations.base import CodeRepoAdapter, IMAdapter, IMRecipient
from integrations.settings import get_integrations_settings


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture(autouse=True)
def clear_integrations_settings_cache():
    """每个测试前后清理 integrations settings 缓存,避免 lru_cache 跨测试泄漏配置。"""
    get_integrations_settings.cache_clear()
    yield
    get_integrations_settings.cache_clear()


@pytest.fixture
def im_adapter() -> DummyIMAdapter:
    return DummyIMAdapter()


@pytest.fixture
def coderepo_adapter() -> DummyCodeRepoAdapter:
    return DummyCodeRepoAdapter()


# ============================================================
# DummyIMAdapter
# ============================================================


class TestDummyIMAdapter:
    """DummyIMAdapter 行为:所有 send_* 返回 dummy-msg-id,verify_* 返回 True。"""

    async def test_send_text_returns_dummy_msg_id(self, im_adapter):
        result = await im_adapter.send_text(IMRecipient(chat_id="c1"), "hello")
        assert result == "dummy-msg-id"

    async def test_send_card_returns_dummy_msg_id(self, im_adapter):
        result = await im_adapter.send_card(IMRecipient(chat_id="c1"), {"type": "card"})
        assert result == "dummy-msg-id"

    async def test_parse_webhook_returns_none(self, im_adapter):
        result = await im_adapter.parse_webhook({"event": "message"})
        assert result is None

    async def test_verify_webhook_signature_returns_true(self, im_adapter):
        result = await im_adapter.verify_webhook_signature({"any": "payload"}, "fake-sig")
        assert result is True

    def test_is_im_adapter_subclass(self, im_adapter):
        assert isinstance(im_adapter, IMAdapter)


# ============================================================
# DummyCodeRepoAdapter
# ============================================================


class TestDummyCodeRepoAdapter:
    """DummyCodeRepoAdapter 行为:所有 list_* 返回 [],verify_* 返回 True。"""

    async def test_list_commits_returns_empty_list(self, coderepo_adapter):
        now = datetime.now(timezone.utc)
        result = await coderepo_adapter.list_commits("my/repo", "main", now, now)
        assert result == []

    async def test_list_merge_requests_returns_empty_list(self, coderepo_adapter):
        result = await coderepo_adapter.list_merge_requests("my/repo", "opened")
        assert result == []

    async def test_parse_webhook_returns_none(self, coderepo_adapter):
        result = await coderepo_adapter.parse_webhook({"object_kind": "push"}, "push")
        assert result is None

    async def test_verify_webhook_signature_returns_true(self, coderepo_adapter):
        result = await coderepo_adapter.verify_webhook_signature({"any": "payload"}, "fake-sig")
        assert result is True

    def test_is_coderepo_adapter_subclass(self, coderepo_adapter):
        assert isinstance(coderepo_adapter, CodeRepoAdapter)


# ============================================================
# Factory:未配置时返回 Dummy
# ============================================================


class TestFactoryDefaultsToDummy:
    """工厂未配置时返回 Dummy 实现。"""

    def test_create_im_adapter_returns_dummy_when_not_configured(self, monkeypatch):
        # 清空所有可能的 env 配置,确保未配置状态
        for var in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_WEBHOOK_SECRET"):
            monkeypatch.delenv(var, raising=False)
        get_integrations_settings.cache_clear()
        adapter = create_im_adapter()
        assert isinstance(adapter, DummyIMAdapter)

    def test_create_coderepo_adapter_returns_dummy_when_not_configured(self, monkeypatch):
        for var in ("GITLAB_BASE_URL", "GITLAB_TOKEN", "GITLAB_WEBHOOK_SECRET"):
            monkeypatch.delenv(var, raising=False)
        get_integrations_settings.cache_clear()
        adapter = create_coderepo_adapter()
        assert isinstance(adapter, DummyCodeRepoAdapter)

    def test_create_im_adapter_returns_im_adapter(self):
        assert isinstance(create_im_adapter(), IMAdapter)

    def test_create_coderepo_adapter_returns_coderepo_adapter(self):
        assert isinstance(create_coderepo_adapter(), CodeRepoAdapter)


class TestFactoryFallbackWhenNotImplemented:
    """配置了凭证但真实适配器未实现时,工厂捕获 NotImplementedError 并降级为 Dummy。"""

    def test_create_im_adapter_falls_back_to_dummy_when_feishu_configured(self, monkeypatch):
        # 即使配置了飞书凭证,FeishuIMAdapter.__init__ raise NotImplementedError
        # → 工厂应降级为 DummyIMAdapter
        monkeypatch.setenv("FEISHU_APP_ID", "fake-app-id")
        monkeypatch.setenv("FEISHU_APP_SECRET", "fake-app-secret")
        get_integrations_settings.cache_clear()
        adapter = create_im_adapter()
        assert isinstance(adapter, DummyIMAdapter)

    def test_create_coderepo_adapter_falls_back_to_dummy_when_gitlab_configured(self, monkeypatch):
        monkeypatch.setenv("GITLAB_BASE_URL", "https://gitlab.example.com")
        monkeypatch.setenv("GITLAB_TOKEN", "fake-token")
        get_integrations_settings.cache_clear()
        adapter = create_coderepo_adapter()
        assert isinstance(adapter, DummyCodeRepoAdapter)
