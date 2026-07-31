"""
Rerank Provider 单元测试 (P2-2)

覆盖:
- DummyRerankProvider: 保持原顺序, 加 rerank_score, health_check 恒 True
- CohereRerankProvider: httpx Mock 验证 request body / header / endpoint / 响应解析
- JinaRerankProvider: 类似 Cohere, 验证 endpoint 与 model 差异
- BGERerankProvider: 依赖缺失时 raise NotImplementedError
- create_rerank_provider 工厂: dummy 默认 + 凭证缺失/未知名 fallback dummy
"""

import sys
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from core.config import Settings
from core.providers.rerank_factory import create_rerank_provider
from core.providers.rerank_provider import (
    BGERerankProvider,
    CohereRerankProvider,
    DummyRerankProvider,
    JinaRerankProvider,
    RerankProvider,
)

MODULE = "core.providers.rerank_provider"


# ============================================================
# Helpers
# ============================================================


def _make_response(json_data, status_code=200):
    """构造 mock httpx.Response"""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


def _patch_httpx_client(monkeypatch, *, post_response=None, post_side_effect=None):
    """Patch httpx.AsyncClient, 返回 mock client 供断言调用参数

    匹配现有 OllamaProvider / GeminiProvider 测试的 patch 模式。
    """
    mock_client = MagicMock()
    if post_response is not None:
        mock_client.post = AsyncMock(return_value=post_response)
    elif post_side_effect is not None:
        mock_client.post = AsyncMock(side_effect=post_side_effect)
    ctx_manager = MagicMock()
    ctx_manager.__aenter__ = AsyncMock(return_value=mock_client)
    ctx_manager.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr("httpx.AsyncClient", MagicMock(return_value=ctx_manager))
    return mock_client


def _make_docs(*texts):
    """构造 dict 文档列表, 每个含 content 字段"""
    return [{"content": t, "kb_id": f"kb-{i}"} for i, t in enumerate(texts)]


@pytest.fixture
def no_sentence_transformers(monkeypatch):
    """fixture: 让 sentence_transformers import 失败, 测试结束自动恢复

    通过 sys.modules["sentence_transformers"] = None 实现 (Python 标准行为:
    sys.modules 中值为 None 时 import 会抛 ImportError)。
    """
    saved_st = sys.modules.get("sentence_transformers")
    saved_xe = sys.modules.get("transformers")
    sys.modules["sentence_transformers"] = None
    sys.modules["transformers"] = None
    yield
    if saved_st is None:
        sys.modules.pop("sentence_transformers", None)
    else:
        sys.modules["sentence_transformers"] = saved_st
    if saved_xe is None:
        sys.modules.pop("transformers", None)
    else:
        sys.modules["transformers"] = saved_xe


# ============================================================
# RerankProvider 抽象基类
# ============================================================


def test_rerank_provider_is_abstract():
    """RerankProvider 是 ABC, 不能直接实例化"""
    with pytest.raises(TypeError):
        RerankProvider()  # type: ignore[abstract]


# ============================================================
# DummyRerankProvider
# ============================================================


def test_dummy_name():
    assert DummyRerankProvider().name == "dummy"


@pytest.mark.asyncio
async def test_dummy_rerank_preserves_order():
    """Dummy rerank: 保持原顺序, 加 rerank_score (递减)"""
    provider = DummyRerankProvider()
    docs = _make_docs("alpha", "beta", "gamma")
    result = await provider.rerank(query="q", documents=docs, top_k=5)
    assert len(result) == 3
    # 原顺序保留: content 顺序 alpha/beta/gamma
    assert [d["content"] for d in result] == ["alpha", "beta", "gamma"]
    # rerank_score 递减 (3, 2, 1)
    assert result[0]["rerank_score"] == 3.0
    assert result[1]["rerank_score"] == 2.0
    assert result[2]["rerank_score"] == 1.0
    # 原字段保留
    assert result[0]["kb_id"] == "kb-0"


@pytest.mark.asyncio
async def test_dummy_rerank_top_k_truncation():
    """Dummy rerank: top_k 截断"""
    provider = DummyRerankProvider()
    docs = _make_docs("a", "b", "c", "d", "e")
    result = await provider.rerank(query="q", documents=docs, top_k=2)
    assert len(result) == 2
    assert result[0]["content"] == "a"
    assert result[1]["content"] == "b"


@pytest.mark.asyncio
async def test_dummy_rerank_does_not_mutate_input():
    """Dummy rerank: 不修改入参 docs"""
    provider = DummyRerankProvider()
    docs = _make_docs("alpha")
    original = [dict(d) for d in docs]
    await provider.rerank(query="q", documents=docs, top_k=5)
    assert docs == original
    assert "rerank_score" not in docs[0]


@pytest.mark.asyncio
async def test_dummy_health_check_always_true():
    """Dummy health_check 恒 True"""
    assert await DummyRerankProvider().health_check() is True


@pytest.mark.asyncio
async def test_dummy_rerank_empty_documents():
    """Dummy rerank: 空文档列表返回空"""
    provider = DummyRerankProvider()
    result = await provider.rerank(query="q", documents=[], top_k=5)
    assert result == []


# ============================================================
# CohereRerankProvider
# ============================================================


def test_cohere_init_default_endpoint_and_model():
    """Cohere 默认 endpoint 与 model"""
    provider = CohereRerankProvider(api_key="k-xxx")
    assert provider._endpoint == "https://api.cohere.ai/v1/rerank"
    assert provider._model == "rerank-multilingual-v3.0"
    assert provider.name == "cohere"


def test_cohere_init_custom_base_url_and_model():
    """自定义 base_url 与 model"""
    provider = CohereRerankProvider(
        api_key="k-xxx",
        base_url="https://custom.cohere.example.com/",
        model="rerank-english-v3.0",
    )
    # 末尾斜杠 strip
    assert provider._endpoint == "https://custom.cohere.example.com/v1/rerank"
    assert provider._model == "rerank-english-v3.0"


def test_cohere_init_no_api_key_raises():
    """缺 api_key 抛 ValueError"""
    with pytest.raises(ValueError, match="api_key"):
        CohereRerankProvider(api_key="")


@pytest.mark.asyncio
async def test_cohere_rerank_request_body_format(monkeypatch):
    """验证 Cohere rerank request: URL / header / body 格式正确"""
    data = {
        "results": [
            {"index": 2, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.5},
            {"index": 1, "relevance_score": 0.1},
        ]
    }
    mock_client = _patch_httpx_client(monkeypatch, post_response=_make_response(data))
    provider = CohereRerankProvider(api_key="k-secret")
    docs = _make_docs("doc0", "doc1", "doc2")
    result = await provider.rerank(query="hello", documents=docs, top_k=3)

    # 验证请求
    args, kwargs = mock_client.post.call_args
    assert args[0] == "https://api.cohere.ai/v1/rerank"
    headers = kwargs["headers"]
    assert headers["Authorization"] == "Bearer k-secret"
    assert headers["Content-Type"] == "application/json"
    body = kwargs["json"]
    assert body["model"] == "rerank-multilingual-v3.0"
    assert body["query"] == "hello"
    assert body["documents"] == ["doc0", "doc1", "doc2"]
    assert body["top_n"] == 3

    # 验证响应解析: 按 relevance_score 降序
    assert len(result) == 3
    assert result[0]["content"] == "doc2"
    assert result[0]["rerank_score"] == 0.9
    assert result[1]["content"] == "doc0"
    assert result[2]["content"] == "doc1"
    # 原字段保留
    assert result[0]["kb_id"] == "kb-2"


@pytest.mark.asyncio
async def test_cohere_rerank_top_k_truncates_top_n(monkeypatch):
    """Cohere: top_k 截断 top_n 与返回结果数"""
    data = {
        "results": [
            {"index": 1, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.5},
        ]
    }
    mock_client = _patch_httpx_client(monkeypatch, post_response=_make_response(data))
    provider = CohereRerankProvider(api_key="k-xxx")
    docs = _make_docs("doc0", "doc1")
    result = await provider.rerank(query="q", documents=docs, top_k=1)
    assert len(result) == 1
    assert result[0]["content"] == "doc1"
    # top_n 也应截断到 1
    _, kwargs = mock_client.post.call_args
    assert kwargs["json"]["top_n"] == 1


@pytest.mark.asyncio
async def test_cohere_rerank_empty_documents_returns_empty(monkeypatch):
    """Cohere: documents 为空时直接返回 [],不发请求"""
    mock_client = _patch_httpx_client(monkeypatch, post_response=_make_response({}))
    provider = CohereRerankProvider(api_key="k-xxx")
    result = await provider.rerank(query="q", documents=[], top_k=5)
    assert result == []
    # 没有发请求
    assert mock_client.post.call_count == 0


@pytest.mark.asyncio
async def test_cohere_rerank_http_error_raises_runtime_error(monkeypatch):
    """Cohere: HTTP 错误抛 RuntimeError"""
    err_resp = MagicMock()
    err_resp.status_code = 401
    err_resp.text = "unauthorized"
    http_err = httpx.HTTPStatusError("401", request=MagicMock(), response=err_resp)
    _patch_httpx_client(monkeypatch, post_side_effect=http_err)
    provider = CohereRerankProvider(api_key="bad")
    with pytest.raises(RuntimeError, match="cohere rerank API 错误"):
        await provider.rerank(query="q", documents=_make_docs("a"), top_k=1)


@pytest.mark.asyncio
async def test_cohere_health_check_ok(monkeypatch):
    """Cohere health_check: 200 返回 True"""
    _patch_httpx_client(monkeypatch, post_response=_make_response({"results": []}))
    provider = CohereRerankProvider(api_key="k-xxx")
    assert await provider.health_check() is True


@pytest.mark.asyncio
async def test_cohere_health_check_fail(monkeypatch):
    """Cohere health_check: 异常时返回 False"""
    _patch_httpx_client(monkeypatch, post_side_effect=httpx.ConnectError("nope"))
    provider = CohereRerankProvider(api_key="k-xxx")
    assert await provider.health_check() is False


# ============================================================
# JinaRerankProvider
# ============================================================


def test_jina_init_default_endpoint_and_model():
    """Jina 默认 endpoint 与 model"""
    provider = JinaRerankProvider(api_key="j-xxx")
    assert provider._endpoint == "https://api.jina.ai/v1/rerank"
    assert provider._model == "jina-reranker-v2-base-multilingual"
    assert provider.name == "jina"


@pytest.mark.asyncio
async def test_jina_rerank_request_body_format(monkeypatch):
    """验证 Jina rerank request: URL / header / body 格式正确"""
    data = {
        "results": [
            {"index": 1, "relevance_score": 0.8},
            {"index": 0, "relevance_score": 0.3},
        ]
    }
    mock_client = _patch_httpx_client(monkeypatch, post_response=_make_response(data))
    provider = JinaRerankProvider(api_key="j-secret", model="custom-jina-model")
    docs = _make_docs("alpha", "beta")
    result = await provider.rerank(query="world", documents=docs, top_k=2)

    args, kwargs = mock_client.post.call_args
    assert args[0] == "https://api.jina.ai/v1/rerank"
    assert kwargs["headers"]["Authorization"] == "Bearer j-secret"
    body = kwargs["json"]
    assert body["model"] == "custom-jina-model"
    assert body["query"] == "world"
    assert body["documents"] == ["alpha", "beta"]
    assert body["top_n"] == 2

    # 响应解析
    assert result[0]["content"] == "beta"
    assert result[0]["rerank_score"] == 0.8


@pytest.mark.asyncio
async def test_jina_rerank_custom_base_url(monkeypatch):
    """Jina: 自定义 base_url 拼接 /v1/rerank"""
    data = {"results": [{"index": 0, "relevance_score": 0.5}]}
    mock_client = _patch_httpx_client(monkeypatch, post_response=_make_response(data))
    provider = JinaRerankProvider(
        api_key="j-xxx", base_url="https://api.jina.ai/custom"
    )
    await provider.rerank(query="q", documents=_make_docs("x"), top_k=1)
    args, _ = mock_client.post.call_args
    assert args[0] == "https://api.jina.ai/custom/v1/rerank"


# ============================================================
# BGERerankProvider
# ============================================================


def test_bge_provider_missing_dependency_raises_not_implemented(
    no_sentence_transformers,
):
    """BGE: sentence-transformers 缺失时 raise NotImplementedError

    通过 fixture 将 sys.modules["sentence_transformers"] 置 None 模拟依赖缺失,
    不依赖真实环境是否安装 transformers。
    """
    with pytest.raises(NotImplementedError, match="sentence-transformers"):
        BGERerankProvider()


# ============================================================
# create_rerank_provider 工厂
# ============================================================


def test_factory_default_returns_dummy():
    """工厂: rerank_provider 未配置时返回 DummyRerankProvider"""
    settings = Settings()  # 默认 rerank_provider="dummy"
    provider = create_rerank_provider(settings)
    assert isinstance(provider, DummyRerankProvider)
    assert provider.name == "dummy"


def test_factory_explicit_dummy_returns_dummy():
    """工厂: 显式 rerank_provider="dummy" 返回 DummyRerankProvider"""
    settings = Settings(rerank_provider="dummy")
    provider = create_rerank_provider(settings)
    assert isinstance(provider, DummyRerankProvider)


def test_factory_cohere_no_api_key_falls_back_to_dummy():
    """工厂: cohere 但无 api_key 降级 Dummy"""
    settings = Settings(rerank_provider="cohere", rerank_api_key=None)
    provider = create_rerank_provider(settings)
    assert isinstance(provider, DummyRerankProvider)


def test_factory_cohere_with_api_key_returns_cohere():
    """工厂: cohere + api_key 返回 CohereRerankProvider"""
    settings = Settings(
        rerank_provider="cohere", rerank_api_key="co-key", rerank_model=None
    )
    provider = create_rerank_provider(settings)
    assert isinstance(provider, CohereRerankProvider)
    assert provider.name == "cohere"


def test_factory_jina_with_api_key_returns_jina():
    """工厂: jina + api_key 返回 JinaRerankProvider"""
    settings = Settings(rerank_provider="jina", rerank_api_key="jina-key")
    provider = create_rerank_provider(settings)
    assert isinstance(provider, JinaRerankProvider)
    assert provider.name == "jina"


def test_factory_jina_no_api_key_falls_back_to_dummy():
    """工厂: jina 但无 api_key 降级 Dummy"""
    settings = Settings(rerank_provider="jina", rerank_api_key=None)
    provider = create_rerank_provider(settings)
    assert isinstance(provider, DummyRerankProvider)


def test_factory_unknown_provider_falls_back_to_dummy():
    """工厂: 未知 provider 名降级 Dummy"""
    settings = Settings(rerank_provider="nonexistent", rerank_api_key="k")
    provider = create_rerank_provider(settings)
    assert isinstance(provider, DummyRerankProvider)


def test_factory_bge_falls_back_to_dummy_when_dep_missing(
    no_sentence_transformers,
):
    """工厂: bge 但依赖缺失时降级 Dummy (不抛 NotImplementedError)"""
    settings = Settings(rerank_provider="bge")
    provider = create_rerank_provider(settings)
    # 依赖缺失时工厂应降级 Dummy, 不抛异常
    assert isinstance(provider, DummyRerankProvider)


def test_factory_passes_custom_base_url_and_model_to_cohere():
    """工厂: rerank_base_url / rerank_model 透传给 Cohere"""
    settings = Settings(
        rerank_provider="cohere",
        rerank_api_key="co-key",
        rerank_base_url="https://custom.cohere.example.com",
        rerank_model="rerank-english-v3.0",
    )
    provider = create_rerank_provider(settings)
    assert isinstance(provider, CohereRerankProvider)
    assert provider._endpoint == "https://custom.cohere.example.com/v1/rerank"
    assert provider._model == "rerank-english-v3.0"
