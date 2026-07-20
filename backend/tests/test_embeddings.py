"""
EmbeddingClient 单元测试
覆盖：初始化配置降级链、embed 空输入/无 key/成功/维度不一致、embed_query。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import Settings
from core.embeddings import EmbeddingClient


def _build_response(vectors):
    """构造一个模拟的 openai embeddings 响应对象"""
    data = [SimpleNamespace(embedding=vec) for vec in vectors]
    return SimpleNamespace(data=data)


def test_init_uses_embedding_api_key_and_base_url():
    """配置了 embedding_api_key + base_url 时优先使用它们"""
    settings = Settings(
        embedding_api_key="emb-key",
        embedding_base_url="https://emb.example.com/v1",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=4,
    )
    client = EmbeddingClient(settings)
    assert client._has_real_key is True
    assert client.model == "text-embedding-3-small"
    assert client.dimensions == 4
    # AsyncOpenAI 客户端应使用 embedding_base_url 与 embedding_api_key
    assert client.client.base_url.host == "emb.example.com"


def test_init_falls_back_to_cloud_then_openai_keys():
    """未配置 embedding_api_key 时依次回退到 cloud_api_key / openai_api_key"""
    s1 = Settings(
        cloud_api_key="cloud-key", cloud_base_url="https://cloud.example.com/v1"
    )
    c1 = EmbeddingClient(s1)
    assert c1._has_real_key is True

    s2 = Settings(
        openai_api_key="openai-key", openai_base_url="https://api.openai.com/v1"
    )
    c2 = EmbeddingClient(s2)
    assert c2._has_real_key is True


def test_init_without_any_key_uses_dummy_key():
    """无任何 API key 时仍可初始化，但 _has_real_key 为 False"""
    settings = Settings(embedding_dimensions=8)
    client = EmbeddingClient(settings)
    assert client._has_real_key is False
    # 允许无 key 初始化，使用 dummy-key 占位
    assert client.client is not None


async def test_embed_empty_input_returns_empty_list():
    """传入空文本列表应直接返回空列表，不触发网络调用"""
    settings = Settings(
        embedding_api_key="k", embedding_base_url="https://x.example.com/v1"
    )
    client = EmbeddingClient(settings)
    result = await client.embed([])
    assert result == []


async def test_embed_without_key_returns_zero_vectors():
    """未配置真实 key 时调用 embed 应降级返回零向量（不抛异常阻断主流程）"""
    settings = Settings(embedding_dimensions=8)
    client = EmbeddingClient(settings)
    result = await client.embed(["hello"])
    assert result == [[0.0] * 8]


async def test_embed_success_returns_vectors():
    """正常调用应返回向量列表"""
    settings = Settings(
        embedding_api_key="k",
        embedding_base_url="https://x.example.com/v1",
        embedding_dimensions=4,
    )
    client = EmbeddingClient(settings)
    fake_vecs = [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]
    client.client.embeddings.create = AsyncMock(return_value=_build_response(fake_vecs))

    result = await client.embed(["hello", "world"])
    assert result == fake_vecs
    # 确认调用参数
    client.client.embeddings.create.assert_awaited_once()
    call_kwargs = client.client.embeddings.create.call_args.kwargs
    assert call_kwargs["model"] == settings.embedding_model
    assert call_kwargs["input"] == ["hello", "world"]


async def test_embed_dimension_mismatch_still_returns(caplog):
    """返回向量维度与配置不一致时应记录 warning 但仍返回结果"""
    settings = Settings(
        embedding_api_key="k",
        embedding_base_url="https://x.example.com/v1",
        embedding_dimensions=4,
    )
    client = EmbeddingClient(settings)
    # 返回 3 维，配置期望 4 维
    client.client.embeddings.create = AsyncMock(
        return_value=_build_response([[0.1, 0.2, 0.3]])
    )
    with caplog.at_level("WARNING", logger="core.embeddings"):
        result = await client.embed(["x"])
    assert result == [[0.1, 0.2, 0.3]]
    assert any("维度不一致" in r.message for r in caplog.records)


async def test_embed_api_error_falls_back_to_zero_vectors(caplog):
    """底层 API 抛出异常时应降级返回零向量而非向上传播（不阻断主流程）"""
    settings = Settings(
        embedding_api_key="k",
        embedding_base_url="https://x.example.com/v1",
        embedding_dimensions=4,
    )
    client = EmbeddingClient(settings)
    client.client.embeddings.create = AsyncMock(side_effect=RuntimeError("boom"))
    with caplog.at_level("WARNING", logger="core.embeddings"):
        result = await client.embed(["x"])
    assert result == [[0.0, 0.0, 0.0, 0.0]]
    assert any("降级为零向量" in r.message for r in caplog.records)


async def test_embed_query_returns_first_vector():
    """embed_query 应返回 embed 结果的第一个向量"""
    settings = Settings(
        embedding_api_key="k",
        embedding_base_url="https://x.example.com/v1",
        embedding_dimensions=3,
    )
    client = EmbeddingClient(settings)
    client.client.embeddings.create = AsyncMock(
        return_value=_build_response([[0.1, 0.2, 0.3]])
    )
    vec = await client.embed_query("query text")
    assert vec == [0.1, 0.2, 0.3]
