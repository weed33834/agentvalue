"""多向量检索服务单元测试（P1-21: 多粒度分块 + 多路召回 + RRF 融合）

覆盖：
- 多粒度分块（段落 / 句子 / 关键词）
- 多路召回 + RRF 融合（数学正确性 / 去重 / 排序 / 粒度归并）
- 文档索引 / 删除 / 重新索引
- 正常文本检索质量（真实 ChromaDB + DummyEmbeddingFunction 往返）

使用真实 ChromaDB（临时目录）+ DummyEmbeddingFunction，与 test_vector_store.py 一致。
"""

import pytest

from core.config import Settings, get_settings
from memory.vector_store import ChromaCompanyKB
from services.multi_vector_service import MultiVectorSearchService, extract_keywords


# ============================================================
# 测试 fixtures
# ============================================================


@pytest.fixture
def kb_store():
    """无 key 配置 -> DummyEmbeddingFunction，真实 ChromaDB 临时目录

    conftest.test_settings 已将 vector_store_dir 设为临时目录并清空 API key，
    故 ChromaCompanyKB 内部走 DummyEmbeddingFunction（确定性 hash 向量）。
    """
    settings = Settings(vector_store_dir=get_settings().vector_store_dir)
    return ChromaCompanyKB(settings=settings)


@pytest.fixture
def mv_service(kb_store):
    """多向量检索服务实例（使用独立 collection 前缀避免与其他测试冲突）"""
    return MultiVectorSearchService(
        kb_store=kb_store, collection_prefix="kb_mv_test"
    )


# 测试用文档（含多段落、多句子、可提取关键词）
SAMPLE_CONTENT = (
    "绩效管理制度\n\n"
    "第一条：绩效考核是企业管理的核心环节。每位员工每季度接受一次评估。\n\n"
    "第二条：代码质量是技术团队的重要考核维度。提交前必须通过单元测试。"
)


# ============================================================
# 多粒度分块：段落级
# ============================================================


def test_split_paragraphs_basic(mv_service):
    """按 \\n\\n 分段，应得到 3 个段落（去除空白段）"""
    paragraphs = mv_service.split_paragraphs(SAMPLE_CONTENT)
    assert len(paragraphs) == 3
    assert paragraphs[0] == "绩效管理制度"
    assert "第一条" in paragraphs[1]
    assert "第二条" in paragraphs[2]


def test_split_paragraphs_caps_at_max_chars(mv_service):
    """单段超过 500 字符应按上限切分，每块不超过 500"""
    long_paragraph = "甲" * 1200
    paragraphs = mv_service.split_paragraphs(long_paragraph)
    assert len(paragraphs) == 3  # 1200 / 500 = 3 块（500, 500, 200）
    for p in paragraphs:
        assert len(p) <= mv_service.PARAGRAPH_MAX_CHARS


def test_split_paragraphs_single_line_without_blank_line(mv_service):
    """无空行但含单换行时，按单换行进一步切分"""
    paragraphs = mv_service.split_paragraphs("第一行\n第二行\n第三行")
    assert paragraphs == ["第一行", "第二行", "第三行"]


def test_split_paragraphs_empty(mv_service):
    """空内容应返回空列表"""
    assert mv_service.split_paragraphs("") == []
    assert mv_service.split_paragraphs("   \n\n  \n") == []


# ============================================================
# 多粒度分块：句子级
# ============================================================


def test_split_sentences_basic(mv_service):
    """按 。！？!? 切分，应得到多个句子"""
    sentences = mv_service.split_sentences(SAMPLE_CONTENT)
    assert len(sentences) == 5
    # 不应携带换行符（尊重段落/行边界）
    for s in sentences:
        assert "\n" not in s
    # 句末标点保留
    assert any(s.endswith("。") for s in sentences)


def test_split_sentences_respects_newlines(mv_service):
    """句子不应跨段落（换行作为分隔，避免携带 \\n）"""
    content = "第一句。第二句。\n\n第三句！"
    sentences = mv_service.split_sentences(content)
    assert sentences == ["第一句。", "第二句。", "第三句！"]


def test_split_sentences_caps_at_max_chars(mv_service):
    """单句超过 200 字符应按上限切分，每块不超过 200"""
    long_sentence = "甲" * 450 + "。"
    sentences = mv_service.split_sentences(long_sentence)
    # 450 字 + 1 标点 = 451，按 200 切分 -> 3 块（200, 200, 51）
    assert len(sentences) == 3
    for s in sentences:
        assert len(s) <= mv_service.SENTENCE_MAX_CHARS


def test_split_sentences_handles_mixed_punctuation(mv_service):
    """中英文句末标点均能切分"""
    sentences = mv_service.split_sentences("你好。世界！test? ok!")
    # 按换行切分为一行 "你好。世界！test? ok!"，再按标点切分
    assert "你好。" in sentences
    assert "世界！" in sentences
    assert "test?" in sentences
    assert "ok!" in sentences


def test_split_sentences_empty(mv_service):
    """空内容应返回空列表"""
    assert mv_service.split_sentences("") == []
    assert mv_service.split_sentences("   ") == []


# ============================================================
# 多粒度分块：关键词级
# ============================================================


def test_extract_keywords_returns_substrings():
    """关键词应为原文子串"""
    content = "绩效管理是企业管理的重要组成部分。代码质量需要持续改进。"
    keywords = extract_keywords(content, top_n=10)
    assert len(keywords) > 0
    for kw in keywords:
        assert kw in content


def test_extract_keywords_top_n_limit():
    """返回关键词数不超过 top_n"""
    content = "绩效管理 代码质量 企业管理 团队协作 项目交付 持续集成 单元测试 代码审查 技术债务 敏捷开发 重构优化"
    keywords = extract_keywords(content, top_n=5)
    assert len(keywords) <= 5


def test_extract_keywords_repeated_term_ranks_first():
    """高频重复词应排在关键词首位（score = 词频 × 词长）"""
    # "绩效管理" 出现 2 次（4-gram，score=2*4=8），高于其他 n-gram
    keywords = extract_keywords("绩效管理，绩效管理，代码质量", top_n=5)
    assert keywords[0] == "绩效管理"


def test_extract_keywords_filters_stopwords():
    """停用词不应出现在关键词中"""
    content = "我们 因为 通过 这个 那个 进行 需要"
    keywords = extract_keywords(content, top_n=10)
    for kw in keywords:
        assert kw not in {"我们", "因为", "通过", "这个", "那个", "进行", "需要"}


def test_extract_keywords_empty():
    """空内容应返回空列表"""
    assert extract_keywords("", top_n=10) == []
    assert extract_keywords("   ", top_n=10) == []


def test_extract_keywords_english_words():
    """英文词应能被提取"""
    content = "machine learning deep learning neural network"
    keywords = extract_keywords(content, top_n=5)
    # learning 出现 2 次，应靠前
    assert "learning" in keywords
    assert "machine" in keywords


# ============================================================
# 多路召回 + RRF 融合
# ============================================================


def _make_item(content, granularity="paragraph", doc_id="d1"):
    """构造标准化检索结果项"""
    return {
        "content": content,
        "score": 1.0,
        "metadata": {},
        "doc_id": doc_id,
        "granularity": granularity,
    }


def test_rrf_fusion_basic_math(mv_service):
    """RRF 分数计算正确：score = Σ 1/(k + rank)"""
    # a: 段落 rank1 + 句子 rank1 = 1/61 + 1/61 = 2/61
    # b: 段落 rank2 + 关键词 rank1 = 1/62 + 1/61
    # c: 句子 rank2 = 1/62
    # d: 关键词 rank2 = 1/62
    lists = [
        [_make_item("a", "paragraph"), _make_item("b", "paragraph")],
        [_make_item("a", "sentence"), _make_item("c", "sentence")],
        [_make_item("b", "keyword"), _make_item("d", "keyword")],
    ]
    results = mv_service.rrf_fusion(lists, top_k=4, rrf_k=60)

    assert [r["content"] for r in results] == ["a", "b", "c", "d"]
    # a 命中两路，分数最高
    assert results[0]["content"] == "a"
    assert results[0]["score"] == pytest.approx(2 / 61, rel=1e-6)
    # b 命中两路但其中一路 rank2，分数次高
    assert results[1]["score"] == pytest.approx(1 / 62 + 1 / 61, rel=1e-6)
    # c, d 各命中一路 rank2，分数最低且相等
    assert results[2]["score"] == pytest.approx(1 / 62, rel=1e-6)
    assert results[3]["score"] == results[2]["score"]


def test_rrf_fusion_dedup_sums_scores(mv_service):
    """同一 content 在多路命中时分数累加（去重）"""
    lists = [
        [_make_item("shared", "paragraph"), _make_item("only_p", "paragraph")],
        [_make_item("shared", "sentence"), _make_item("only_s", "sentence")],
        [_make_item("shared", "keyword")],
    ]
    results = mv_service.rrf_fusion(lists, top_k=5, rrf_k=60)

    # shared 命中三路 rank1，分数 = 3/61，应排第一
    assert results[0]["content"] == "shared"
    assert results[0]["score"] == pytest.approx(3 / 61, rel=1e-6)
    # 去重后只有 3 个不同 content
    contents = [r["content"] for r in results]
    assert len(contents) == len(set(contents))  # 无重复
    assert set(contents) == {"shared", "only_p", "only_s"}


def test_rrf_fusion_top_k_limit(mv_service):
    """top_k 限制返回数量"""
    lists = [
        [_make_item(f"p{i}", "paragraph") for i in range(10)],
        [_make_item(f"s{i}", "sentence") for i in range(10)],
        [_make_item(f"k{i}", "keyword") for i in range(10)],
    ]
    results = mv_service.rrf_fusion(lists, top_k=5, rrf_k=60)
    assert len(results) == 5
    # 结果按分数降序
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_rrf_fusion_matched_granularities(mv_service):
    """matched_granularities 记录 content 命中的所有粒度"""
    lists = [
        [_make_item("shared", "paragraph")],
        [_make_item("shared", "sentence")],
        [_make_item("shared", "keyword")],
    ]
    results = mv_service.rrf_fusion(lists, top_k=1, rrf_k=60)
    assert len(results) == 1
    assert results[0]["matched_granularities"] == ["keyword", "paragraph", "sentence"]
    assert results[0]["source"] == "multi_vector"


def test_rrf_fusion_empty_lists(mv_service):
    """空输入应返回空列表"""
    assert mv_service.rrf_fusion([], top_k=5) == []
    assert mv_service.rrf_fusion([[], [], []], top_k=5) == []


# ============================================================
# 文档索引 / 删除 / 重新索引
# ============================================================


async def test_index_document_creates_vectors(mv_service):
    """索引文档后应返回各粒度向量数，stats 应一致"""
    result = await mv_service.index_document(
        "doc-1", SAMPLE_CONTENT, metadata={"category": "hr"}
    )
    assert result["doc_id"] == "doc-1"
    assert result["paragraphs"] == 3
    assert result["sentences"] == 5
    assert result["keywords"] > 0
    assert result["total_vectors"] == result["paragraphs"] + result["sentences"] + result["keywords"]

    stats = await mv_service.get_stats()
    assert stats["collections"]["paragraph"] == result["paragraphs"]
    assert stats["collections"]["sentence"] == result["sentences"]
    assert stats["collections"]["keyword"] == result["keywords"]
    assert stats["total_vectors"] == result["total_vectors"]
    # collection 名称符合 {prefix}_{granularity} 约定
    assert stats["collection_names"]["paragraph"] == "kb_mv_test_paragraph"
    assert stats["collection_names"]["sentence"] == "kb_mv_test_sentence"
    assert stats["collection_names"]["keyword"] == "kb_mv_test_keyword"


async def test_index_document_reindex_replaces_old(mv_service):
    """重复索引同一文档应替换旧向量（先删后建，不产生重复）"""
    await mv_service.index_document("doc-1", SAMPLE_CONTENT)
    stats_before = await mv_service.get_stats()

    # 用更短的内容重新索引
    new_content = "全新的内容。这是一个短文档。"
    result = await mv_service.index_document("doc-1", new_content)
    stats_after = await mv_service.get_stats()

    # 重新索引后总向量数应等于新内容的向量数（旧向量已被替换，非叠加）
    assert result["total_vectors"] == stats_after["total_vectors"]
    # 旧向量未叠加：新总量 < 旧总量 + 新增量
    assert stats_after["total_vectors"] < stats_before["total_vectors"] + result["total_vectors"]


async def test_delete_document_removes_vectors(mv_service):
    """删除文档后该文档所有粒度向量应被清除"""
    await mv_service.index_document("doc-1", SAMPLE_CONTENT)
    await mv_service.index_document("doc-2", "另一个文档的内容。包含不同句子。")

    # 删除 doc-1
    result = await mv_service.delete_document("doc-1")
    assert result["doc_id"] == "doc-1"
    assert result["total_deleted"] > 0
    assert sum(result["deleted"].values()) == result["total_deleted"]

    # doc-1 的向量应全部删除
    stats = await mv_service.get_stats()
    # doc-2 仍存在
    assert stats["total_vectors"] > 0

    # 删除不存在的文档应返回 0
    result2 = await mv_service.delete_document("not-exist")
    assert result2["total_deleted"] == 0


async def test_delete_document_all(mv_service):
    """删除所有文档后统计应归零"""
    await mv_service.index_document("doc-1", SAMPLE_CONTENT)
    await mv_service.delete_document("doc-1")
    stats = await mv_service.get_stats()
    assert stats["total_vectors"] == 0
    assert stats["collections"] == {"paragraph": 0, "sentence": 0, "keyword": 0}


async def test_index_document_empty_content_raises(mv_service):
    """空内容应抛出 ValueError"""
    with pytest.raises(ValueError, match="content"):
        await mv_service.index_document("doc-1", "")
    with pytest.raises(ValueError, match="content"):
        await mv_service.index_document("doc-1", "   \n\n  ")


async def test_index_document_empty_doc_id_raises(mv_service):
    """空 doc_id 应抛出 ValueError"""
    with pytest.raises(ValueError, match="doc_id"):
        await mv_service.index_document("", SAMPLE_CONTENT)


# ============================================================
# 正常文本检索质量
# ============================================================


async def test_search_empty_before_index(mv_service):
    """空索引检索应返回空列表"""
    results = await mv_service.search("任意查询", top_k=5)
    assert results == []


async def test_search_empty_query_returns_empty(mv_service):
    """空查询应返回空列表"""
    await mv_service.index_document("doc-1", SAMPLE_CONTENT)
    assert await mv_service.search("", top_k=5) == []
    assert await mv_service.search("   ", top_k=5) == []


async def test_search_returns_results_after_index(mv_service):
    """索引后检索应返回结果"""
    await mv_service.index_document("doc-1", SAMPLE_CONTENT)
    results = await mv_service.search("绩效管理", top_k=5)
    assert len(results) > 0
    # 每项应包含必要字段
    for r in results:
        assert "content" in r
        assert "score" in r
        assert r["source"] == "multi_vector"
        assert r["doc_id"] == "doc-1"


async def test_search_top_k_limit(mv_service):
    """top_k 限制返回数量"""
    await mv_service.index_document("doc-1", SAMPLE_CONTENT)
    results = await mv_service.search("绩效管理", top_k=2)
    assert len(results) <= 2


async def test_search_exact_match_quality(mv_service):
    """检索质量：用精确匹配某句子作为查询，该句子应出现在结果中且为 sentence 路 rank1

    DummyEmbeddingFunction 下，相同文本生成相同向量（cosine distance=0），
    故精确匹配必为 sentence collection 的 rank1（RRF 贡献恰好 1/61）。
    该句子仅存在于 sentence collection（其段落版本更长，content 不同），故融合分=1/61。
    """
    await mv_service.index_document("doc-1", SAMPLE_CONTENT, metadata={"category": "hr"})

    # 选取一个已索引的句子作为精确匹配查询（该句的段落版本更长，不会与段落 content 重复）
    exact_sentence = "每位员工每季度接受一次评估。"
    results = await mv_service.search(exact_sentence, top_k=5)

    # 精确匹配的句子应在结果中
    matched = [r for r in results if r["content"] == exact_sentence]
    assert len(matched) == 1, f"精确匹配句子未出现在结果中: {[r['content'] for r in results]}"
    # 该结果来自 sentence 粒度（句子级 collection）
    assert matched[0]["granularity"] == "sentence"
    # 精确匹配 → distance=0 → sentence 路 rank1 → RRF 贡献恰好 1/61
    assert matched[0]["score"] == pytest.approx(1 / 61, rel=1e-5)
    # 精确匹配应位于结果前列（top_k 内）
    assert results.index(matched[0]) < 5


async def test_search_dedup_no_duplicate_content(mv_service):
    """检索结果不应有重复 content（RRF 去重）"""
    await mv_service.index_document("doc-1", SAMPLE_CONTENT)
    results = await mv_service.search("绩效管理 代码质量", top_k=10)
    contents = [r["content"] for r in results]
    assert len(contents) == len(set(contents)), "检索结果存在重复 content"


async def test_search_metadata_round_trip(mv_service):
    """索引时传入的 metadata 应在检索结果中原样返回"""
    await mv_service.index_document(
        "doc-1", SAMPLE_CONTENT, metadata={"category": "hr", "source": "manual"}
    )
    results = await mv_service.search("绩效管理", top_k=5)
    assert len(results) > 0
    for r in results:
        assert r["metadata"] == {"category": "hr", "source": "manual"}


async def test_search_multiple_documents(mv_service):
    """多文档索引后检索，结果应能区分来源文档"""
    await mv_service.index_document(
        "doc-1", "绩效管理制度是企业核心。考核每季度一次。", metadata={"topic": "perf"}
    )
    await mv_service.index_document(
        "doc-2", "代码质量规范要求单元测试。提交前必须审查。", metadata={"topic": "code"}
    )
    results = await mv_service.search("代码质量", top_k=10)
    assert len(results) > 0
    # 结果中应包含 doc-2 的内容
    doc_ids = {r["doc_id"] for r in results}
    assert "doc-2" in doc_ids


async def test_search_after_delete_returns_empty(mv_service):
    """删除文档后检索应返回空结果"""
    await mv_service.index_document("doc-1", SAMPLE_CONTENT)
    await mv_service.delete_document("doc-1")
    # 用精确匹配查询，删除后应无结果
    results = await mv_service.search("每位员工每季度接受一次评估。", top_k=5)
    assert results == []
