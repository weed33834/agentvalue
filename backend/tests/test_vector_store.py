"""
memory/vector_store.py 单元测试
- DummyEmbeddingFunction：伪向量生成与接口
- _init_embedding：dummy / EmbeddingClient 两条路径及降级
- ChromaMemoryStore / ChromaCompanyKB：真实 ChromaDB(临时目录) 往返 + Mock 覆盖边界分支
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from core.config import Settings, get_settings
from core.embeddings import EmbeddingClient
from memory.vector_store import (
    ChromaCompanyKB,
    ChromaMemoryStore,
    DummyEmbeddingFunction,
    _init_embedding,
)


# ---------------- DummyEmbeddingFunction ----------------


def test_dummy_embedding_function_interface():
    """DummyEmbeddingFunction 基本接口"""
    fn = DummyEmbeddingFunction(dimensions=8)
    assert fn.name() == "dummy"
    assert fn.is_legacy() is False
    assert fn.supported_spaces() == ["cosine", "l2", "ip"]


def test_dummy_embedding_function_is_deterministic_and_dimensioned():
    """相同文本应生成确定性向量，维度与配置一致"""
    fn = DummyEmbeddingFunction(dimensions=16)
    v1 = fn(["hello", "world"])
    v2 = fn(["hello", "world"])
    assert len(v1) == 2
    assert all(len(vec) == 16 for vec in v1)
    assert v1 == v2  # 确定性
    # 不同文本向量应不同
    assert fn(["hello"]) != fn(["world"])


def test_dummy_embedding_function_default_dimensions():
    """默认维度为 384"""
    fn = DummyEmbeddingFunction()
    vec = fn(["x"])
    assert len(vec[0]) == 384


# ---------------- _init_embedding ----------------


def test_init_embedding_returns_dummy_without_key():
    """未配置任何 API key 时返回 DummyEmbeddingFunction"""
    settings = Settings(embedding_dimensions=10)
    emb = _init_embedding(settings)
    assert isinstance(emb, DummyEmbeddingFunction)
    assert emb.dimensions == 10


def test_init_embedding_returns_embedding_client_when_configured():
    """配置了 embedding_api_key + base_url 时返回 EmbeddingClient"""
    settings = Settings(
        embedding_api_key="k",
        embedding_base_url="https://emb.example.com/v1",
        embedding_dimensions=4,
    )
    emb = _init_embedding(settings)
    assert isinstance(emb, EmbeddingClient)
    assert emb._has_real_key is True


def test_init_embedding_falls_back_to_dummy_on_failure(caplog):
    """EmbeddingClient 初始化失败时应降级到 dummy embedding"""
    settings = Settings(
        embedding_api_key="k",
        embedding_base_url="https://emb.example.com/v1",
        embedding_dimensions=8,
    )
    with patch(
        "memory.vector_store.EmbeddingClient", side_effect=RuntimeError("init boom")
    ):
        emb = _init_embedding(settings)
    assert isinstance(emb, DummyEmbeddingFunction)


def test_init_embedding_uses_cloud_or_openai_key():
    """cloud_api_key / openai_key 也算作有 key，配合 base_url 应返回 EmbeddingClient"""
    s_cloud = Settings(
        cloud_api_key="ck",
        cloud_base_url="https://cloud.example.com/v1",
        embedding_base_url="https://cloud.example.com/v1",
    )
    assert isinstance(_init_embedding(s_cloud), EmbeddingClient)

    s_openai = Settings(
        openai_api_key="ok",
        openai_base_url="https://api.openai.com/v1",
        embedding_base_url="https://api.openai.com/v1",
    )
    assert isinstance(_init_embedding(s_openai), EmbeddingClient)


def test_init_embedding_key_without_base_url_returns_dummy():
    """有 key 但无 embedding_base_url 时仍降级到 dummy"""
    settings = Settings(cloud_api_key="ck")  # 无 embedding_base_url
    assert isinstance(_init_embedding(settings), DummyEmbeddingFunction)


# ---------------- ChromaMemoryStore（真实 ChromaDB 往返） ----------------


@pytest.fixture
def memory_store():
    """无 key 配置 -> 使用 DummyEmbeddingFunction，真实 ChromaDB 临时目录"""
    settings = Settings(vector_store_dir=get_settings().vector_store_dir)
    store = ChromaMemoryStore(settings=settings)
    return store


async def test_memory_add_and_get_history_round_trip(memory_store):
    """写入记忆后应能检索到"""
    await memory_store.add_memory(
        "E1001", {"period": "2026-W01", "summary": "完成重构"}
    )
    history = await memory_store.get_employee_history(
        "E1001", period="2026-W99", limit=5
    )
    assert len(history) == 1
    mem = history[0]
    assert mem["period"] == "2026-W01"
    assert mem["summary"] == "完成重构"
    # 检索分数字段
    assert "_retrieval_score" in mem


async def test_memory_history_excludes_current_period(memory_store):
    """get_employee_history 应排除指定 period 的记录"""
    await memory_store.add_memory("E2001", {"period": "2026-W01", "summary": "第一周"})
    await memory_store.add_memory("E2001", {"period": "2026-W02", "summary": "第二周"})

    # 排除 W02，应只返回 W01（add_memory 用相同 id 会 upsert，两期 doc_id 不同）
    history = await memory_store.get_employee_history(
        "E2001", period="2026-W02", limit=10
    )
    periods = {m["period"] for m in history}
    assert "2026-W02" not in periods
    assert "2026-W01" in periods


async def test_memory_history_unknown_employee_returns_empty(memory_store):
    """查询不存在员工应返回空列表"""
    history = await memory_store.get_employee_history("NOPE", limit=5)
    assert history == []


async def test_memory_add_memory_without_period_defaults_unknown(memory_store):
    """记忆未提供 period 时，doc_id/metadata 周期默认 'unknown'，
    记忆内容原样往返，且可被带 period 过滤的查询检索到（默认周期 != 排除周期）"""
    await memory_store.add_memory("E3001", {"summary": "无周期记忆"})
    # 默认周期为 unknown，排除其他周期时应仍能命中
    history = await memory_store.get_employee_history(
        "E3001", period="2026-W10", limit=5
    )
    assert len(history) == 1
    assert history[0]["summary"] == "无周期记忆"


# ---------------- ChromaMemoryStore（Mock 边界分支） ----------------


def _make_store_with_mock_collection():
    """构造一个使用 Mock collection 的 ChromaMemoryStore，embedding 用 Dummy"""
    settings = Settings(vector_store_dir=get_settings().vector_store_dir)
    store = ChromaMemoryStore(settings=settings)
    store.collection = MagicMock()
    return store


async def test_memory_history_handles_invalid_json_payload():
    """payload 为非法 JSON 时应回退为 {summary: doc, **meta}"""
    store = _make_store_with_mock_collection()
    store.collection.query = MagicMock(
        return_value={
            "metadatas": [[{"employee_id": "E1", "payload": "not-json"}]],
            "documents": [["doc-text"]],
            "distances": [[0.25]],
        }
    )
    history = await store.get_employee_history("E1", limit=5)
    assert len(history) == 1
    mem = history[0]
    assert mem["summary"] == "doc-text"
    assert mem["employee_id"] == "E1"
    assert mem["payload"] == "not-json"
    assert mem["_retrieval_score"] == 0.75  # 1.0 - 0.25


async def test_memory_history_handles_missing_payload_meta():
    """metadata 无 payload 字段时应以 doc 作为 summary"""
    store = _make_store_with_mock_collection()
    store.collection.query = MagicMock(
        return_value={
            "metadatas": [[{"employee_id": "E1", "extra": "v"}]],
            "documents": [["doc-only"]],
            "distances": [[0.0]],
        }
    )
    history = await store.get_employee_history("E1", limit=5)
    assert len(history) == 1
    assert history[0]["summary"] == "doc-only"
    assert history[0]["extra"] == "v"
    assert history[0]["_retrieval_score"] == 1.0


async def test_memory_history_skips_falsy_metadata():
    """metadata 为 None/空 时应跳过"""
    store = _make_store_with_mock_collection()
    store.collection.query = MagicMock(
        return_value={
            "metadatas": [[None, {}]],
            "documents": [["d1", "d2"]],
            "distances": [[0.1, 0.2]],
        }
    )
    history = await store.get_employee_history("E1", limit=5)
    assert history == []


async def test_memory_history_query_exception_returns_empty():
    """collection.query 抛异常时应返回空列表"""
    store = _make_store_with_mock_collection()
    store.collection.query = MagicMock(side_effect=RuntimeError("boom"))
    history = await store.get_employee_history("E1", limit=5)
    assert history == []


async def test_memory_add_memory_embed_failure_raises():
    """当 embedding 具备 embed_query 但调用失败时，add_memory 应向上抛出"""
    store = _make_store_with_mock_collection()

    class FailingEmbedding:
        async def embed_query(self, text):
            raise RuntimeError("embed failed")

    store.embedding = FailingEmbedding()
    with pytest.raises(RuntimeError, match="embed failed"):
        await store.add_memory("E1", {"period": "P1", "summary": "x"})


# ---------------- ChromaCompanyKB（真实 ChromaDB 往返） ----------------


@pytest.fixture
def company_kb():
    """无 key 配置 -> DummyEmbeddingFunction，真实 ChromaDB 临时目录"""
    settings = Settings(vector_store_dir=get_settings().vector_store_dir)
    return ChromaCompanyKB(settings=settings)


async def test_kb_add_and_query_round_trip(company_kb):
    """添加文档后应能检索到"""
    await company_kb.add_document(
        kb_id="kb-001",
        title="代码质量规范",
        content="提交前必须通过单元测试与代码审查",
        metadata={"category": "engineering"},
    )
    results = await company_kb.query("代码质量", top_k=5)
    assert len(results) >= 1
    doc = results[0]
    assert doc["kb_id"] == "kb-001"
    assert doc["title"] == "代码质量规范"
    assert "代码审查" in doc["content"]
    assert "_retrieval_score" in doc


async def test_kb_query_empty_when_no_match(company_kb):
    """查询空知识库或无匹配时应返回空列表"""
    results = await company_kb.query("不存在的主题", top_k=5)
    assert results == []


async def test_kb_query_metadata_round_trips_as_json_string(company_kb):
    """add_document 将 metadata 序列化为 JSON 字符串存入 chromadb（chromadb metadata 仅支持基本类型），
    query 原样返回该字符串，应可解析回原 dict"""
    await company_kb.add_document(
        kb_id="kb-002",
        title="价值观",
        content="客户第一",
        metadata={"category": "culture", "weight": 1},
    )
    await company_kb.add_document(
        kb_id="kb-003", title="价值观手册", content="客户第一优先"
    )
    results = await company_kb.query("价值观", top_k=5)
    assert results
    # metadata 存在时应为可解析的 JSON 字符串
    meta_doc = next(d for d in results if d["kb_id"] == "kb-002")
    assert isinstance(meta_doc["metadata"], str)
    parsed = json.loads(meta_doc["metadata"])
    assert parsed == {"category": "culture", "weight": 1}


# ---------------- ChromaCompanyKB（Mock 边界分支） ----------------


async def test_kb_query_exception_returns_empty():
    """collection.query 抛异常时应返回空列表"""
    settings = Settings(vector_store_dir=get_settings().vector_store_dir)
    kb = ChromaCompanyKB(settings=settings)
    kb.collection = MagicMock()
    kb.collection.query = MagicMock(side_effect=RuntimeError("kb boom"))
    assert await kb.query("anything") == []


async def test_kb_query_skips_falsy_metadata():
    """metadata 为空/None 时应跳过"""
    settings = Settings(vector_store_dir=get_settings().vector_store_dir)
    kb = ChromaCompanyKB(settings=settings)
    kb.collection = MagicMock()
    kb.collection.query = MagicMock(
        return_value={
            "metadatas": [[None, {"kb_id": "kb-1", "title": "t", "content": "c"}]],
            "documents": [["d1", "d2"]],
            "distances": [[0.2, 0.4]],
        }
    )
    results = await kb.query("x", top_k=5)
    assert len(results) == 1
    assert results[0]["kb_id"] == "kb-1"
    assert results[0]["_retrieval_score"] == 0.6  # 1.0 - 0.4


async def test_kb_add_document_embed_failure_raises():
    """embedding 具备 embed_query 但调用失败时，add_document 应向上抛出"""
    settings = Settings(vector_store_dir=get_settings().vector_store_dir)
    kb = ChromaCompanyKB(settings=settings)
    kb.collection = MagicMock()

    class FailingEmbedding:
        async def embed_query(self, text):
            raise RuntimeError("kb embed failed")

    kb.embedding = FailingEmbedding()
    with pytest.raises(RuntimeError, match="kb embed failed"):
        await kb.add_document("kb-1", "t", "c")


# ---------------- query_texts 回退分支（embedding 无 embed_query） ----------------


async def test_memory_history_uses_query_texts_when_no_embed_query():
    """embedding 无 embed_query 时应使用 query_texts 路径检索"""
    store = _make_store_with_mock_collection()
    store.embedding = None  # 触发 else 分支
    store.collection.query = MagicMock(
        return_value={
            "metadatas": [[{"employee_id": "E1", "payload": '{"period": "P1"}'}]],
            "documents": [["doc"]],
            "distances": [[0.5]],
        }
    )
    history = await store.get_employee_history("E1", limit=5)
    assert len(history) == 1
    # 确认走 query_texts 而非 query_embeddings
    call_kwargs = store.collection.query.call_args.kwargs
    assert "query_texts" in call_kwargs
    assert "query_embeddings" not in call_kwargs


async def test_kb_query_uses_query_texts_when_no_embed_query():
    """公司知识库 embedding 无 embed_query 时应使用 query_texts 路径"""
    settings = Settings(vector_store_dir=get_settings().vector_store_dir)
    kb = ChromaCompanyKB(settings=settings)
    kb.collection = MagicMock()
    kb.embedding = None  # 触发 else 分支
    kb.collection.query = MagicMock(
        return_value={
            "metadatas": [[{"kb_id": "kb-1", "title": "t", "content": "c"}]],
            "documents": [["c"]],
            "distances": [[0.3]],
        }
    )
    results = await kb.query("x", top_k=5)
    assert len(results) == 1
    call_kwargs = kb.collection.query.call_args.kwargs
    assert "query_texts" in call_kwargs
    assert "query_embeddings" not in call_kwargs
