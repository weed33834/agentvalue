"""多向量检索服务（多粒度分块 + 多路召回 + RRF 融合）

同一文档按段落级、句子级、关键词级三种粒度分块，各自生成嵌入并存储到独立的
ChromaDB collection 中。检索时对三路结果做多路召回，再通过 RRF
(Reciprocal Rank Fusion) 算法融合排序，返回去重后的结果。

设计要点：
- 复用现有 ChromaCompanyKB 的 ChromaDB 客户端与 embedding 函数，不引入新依赖
- 三种粒度使用独立 collection（默认 {prefix}_paragraph / {prefix}_sentence / {prefix}_keyword）
- 关键词提取采用纯 Python 实现（简单 TF + 停用词过滤），不依赖 jieba
- RRF: score = 1/(k + rank)，k 默认 60，三路等权融合
- 所有 ChromaDB 同步操作通过 asyncio.to_thread 包装，避免阻塞事件循环
- 向量预计算后通过 embeddings=/query_embeddings= 传入 ChromaDB，
  避免 EmbeddingClient.__call__ 在事件循环内同步调用导致 RuntimeError

WS-4 多租户隔离（两层防御）
--------------------------
第一层（既有）：路由层按租户拆 collection 前缀（agentvalue_mv_{tenant_id}）。
第二层（本次新增）：每个 chunk 的 metadata 打上 tenant_id，检索/删除/统计时
按当前租户过滤。这样即便调用方误传了共享 collection 前缀，也不会串数据。

租户来源优先级：显式入参 > 构造函数 > `get_current_tenant()` 上下文。
需要平台级跨租户视角（运维巡检 / 全局统计）时，必须显式传
`allow_cross_tenant=True`，不存在隐式放行。

存量兼容：本次改动前写入的 chunk 没有 tenant_id 元数据，会被视为「未归属」
并对所有租户可见（与 8 个模型的 tenant_id 列取 nullable 是同一决策）。
彻底收口需要一次数据回填，回填前不要把这层当成硬隔离边界。
"""

import asyncio
import hashlib
import json
import logging
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from core.config import Settings, get_settings
from core.tenant_context import get_current_tenant

logger = logging.getLogger(__name__)

# chunk metadata 中的租户字段名
TENANT_META_KEY = "tenant_id"


def _chunk_tenant_allowed(meta: Optional[Dict[str, Any]], tenant_id: str) -> bool:
    """判断一个 chunk 是否属于指定租户。

    未打 tenant_id 的 chunk 视为存量「未归属」数据，放行（见模块 docstring 的
    存量兼容说明）；打了标签的必须精确匹配。
    """
    if not meta:
        return True
    owner = meta.get(TENANT_META_KEY)
    if owner in (None, ""):
        return True
    return owner == tenant_id


# ============================================================
# 分词与关键词提取工具（纯 Python 实现，优先 jieba，降级 n-gram TF）
# ============================================================

# 候选关键词模式：连续 CJK 字符序列 / 长度 2+ 的英文词
_CJK_RUN_PATTERN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+")
_EN_WORD_PATTERN = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{1,}")

# CJK n-gram 取词长度范围（2~4 字，兼顾区分度与召回）
_CJK_NGRAM_SIZES = (2, 3, 4)

# 简单中英文停用词表（关键词提取时过滤高频无意义词，相当于简化 IDF）
_STOPWORDS = {
    # 中文常见停用词（含高频无意义 2-gram）
    "我们", "你们", "他们", "她们", "它们", "这个", "那个", "这些", "那些",
    "什么", "怎么", "为什么", "如何", "如果", "但是", "不过", "然后", "所以",
    "因为", "由于", "对于", "关于", "通过", "进行", "可以", "应该", "需要",
    "已经", "正在", "将会", "可能", "或者", "并且", "以及", "还有", "没有",
    "一个", "一些", "一种", "这样", "那样", "的话", "其实", "现在", "以后",
    "以前", "之后", "之前", "之间", "其中", "其他", "另外", "不是", "不能",
    "不要", "不用", "这种", "那种", "本次", "目前", "非常", "比较", "还是",
    "各位", "大家", "自己", "比如", "例如", "作为", "是一", "的", "了", "在",
    "是", "和", "与", "或", "及", "等", "中", "为", "以", "对", "由", "向",
    # 英文常见停用词
    "the", "is", "are", "was", "were", "be", "been", "being", "have", "has",
    "had", "do", "does", "did", "will", "would", "should", "could", "may",
    "might", "must", "can", "this", "that", "these", "those", "and", "or",
    "but", "if", "of", "to", "in", "on", "at", "for", "with", "by", "from",
    "as", "an", "it", "its", "they", "them", "their", "you", "your", "he",
    "she", "his", "her", "not", "no", "so", "than", "too", "very", "we",
}


def extract_keywords(content: str, top_n: int = 10) -> List[str]:
    """简单 TF 关键词提取（优先 jieba，降级 n-gram TF，无强依赖）

    提取流程：
    1. 若已安装 jieba，优先使用 jieba.analyse.extract_tags（关键词质量更高）；
    2. 否则降级到纯 Python n-gram TF：
       - CJK 文本提取 2~4 字 n-gram，英文提取 2+ 字符词；
       - 统计词频，过滤停用词；
       - 按 score = 词频 × 词长 排序（词长代理 IDF，长词更稀有更具区分度）。

    Args:
        content: 文档全文
        top_n: 返回关键词数量上限

    Returns:
        关键词列表，如 ["绩效管理", "代码质量", ...]
    """
    if not content or not content.strip():
        return []

    # 优先使用 jieba（可选依赖，已安装时关键词质量更高）
    try:
        import jieba.analyse  # type: ignore

        kws = jieba.analyse.extract_tags(content, topK=top_n)
        if kws:
            return [k for k in kws if k and k.strip()][:top_n]
    except ImportError:
        pass
    except Exception:  # jieba 内部异常时降级，不阻断主流程
        pass

    # 降级：纯 Python n-gram TF
    candidates: List[Tuple[str, int]] = []  # (term, length)
    # 英文/数字词（长度 2+）
    for m in _EN_WORD_PATTERN.finditer(content):
        candidates.append((m.group().lower(), len(m.group())))
    # CJK n-gram（2~4 字）
    for cjk_run in _CJK_RUN_PATTERN.finditer(content):
        run = cjk_run.group()
        for n in _CJK_NGRAM_SIZES:
            if len(run) < n:
                continue
            for i in range(len(run) - n + 1):
                candidates.append((run[i : i + n], n))

    if not candidates:
        return []

    # 词频统计
    freq = Counter(term for term, _ in candidates)
    # 去重候选词，保留词长
    unique: Dict[str, int] = {}
    for term, length in candidates:
        if term not in unique:
            unique[term] = length

    # 过滤停用词 + 计算 score = 词频 × 词长
    scored: List[Tuple[str, float, int]] = []
    for term, length in unique.items():
        if term in _STOPWORDS or len(term) < 2:
            continue
        score = float(freq[term]) * length
        scored.append((term, score, length))

    # 按 score 降序，score 相同按词长降序（长词更具区分度），再按字典序稳定排序
    scored.sort(key=lambda x: (-x[1], -x[2], x[0]))
    return [term for term, _, _ in scored[:top_n]]


# ============================================================
# MultiVectorSearchService
# ============================================================


class MultiVectorSearchService:
    """多向量检索服务（多粒度分块 + 多路召回 + RRF 融合）

    对同一文档进行段落级、句子级、关键词级三种粒度分块，各自嵌入后存入独立
    ChromaDB collection；检索时三路召回并通过 RRF 融合排序。

    Args:
        kb_store: ChromaCompanyKB 实例，提供 ChromaDB 客户端与 embedding 函数
        settings: 应用配置，未提供时从 kb_store 或全局获取
        collection_prefix: collection 名称前缀，三种粒度 collection 名分别为
            {prefix}_paragraph / {prefix}_sentence / {prefix}_keyword。
            默认 "kb"；多租户场景应传入租户隔离前缀（如 agentvalue_mv_{tenant_id}）。
        tenant_id: WS-4 租户绑定，缺省取 `get_current_tenant()`。作为
            collection 前缀之外的第二层隔离（chunk metadata 打标 + 检索过滤）。
    """

    # 三种粒度标识
    GRANULARITY_PARAGRAPH = "paragraph"
    GRANULARITY_SENTENCE = "sentence"
    GRANULARITY_KEYWORD = "keyword"

    # 分块参数
    PARAGRAPH_MAX_CHARS = 500
    SENTENCE_MAX_CHARS = 200
    KEYWORD_TOP_N = 10

    # RRF 常数 k（与 hybrid_search_service 对齐）
    RRF_K = 60

    def __init__(
        self,
        kb_store,
        settings: Optional[Settings] = None,
        collection_prefix: str = "kb",
        tenant_id: Optional[str] = None,
    ):
        self.kb_store = kb_store
        self.settings = settings or getattr(kb_store, "settings", None) or get_settings()
        self.collection_prefix = collection_prefix or "kb"
        # WS-4：构造期固定租户；未传时按当时的请求上下文解析
        self.tenant_id = tenant_id or get_current_tenant()
        # 三种粒度对应的 collection 名称
        self.paragraph_collection_name = f"{self.collection_prefix}_paragraph"
        self.sentence_collection_name = f"{self.collection_prefix}_sentence"
        self.keyword_collection_name = f"{self.collection_prefix}_keyword"
        # collection 懒加载缓存：{collection_name: collection}
        self._collections: Dict[str, Any] = {}

    # --------------------------------------------------------
    # 公共方法
    # --------------------------------------------------------

    def _resolve_tenant(
        self, tenant_id: Optional[str], allow_cross_tenant: bool
    ) -> Optional[str]:
        """解析本次操作的租户作用域。

        Returns:
            租户 ID；`allow_cross_tenant=True` 时返回 None，表示不做租户过滤。
        """
        if allow_cross_tenant:
            logger.info(
                "多向量服务执行跨租户操作（allow_cross_tenant=True）prefix=%s",
                self.collection_prefix,
            )
            return None
        return tenant_id or self.tenant_id or get_current_tenant()

    async def index_document(
        self,
        doc_id: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """对文档进行多粒度分块，各自生成嵌入并存储

        若文档已存在索引，先删除旧索引（支持重新索引）。

        Args:
            doc_id: 文档唯一标识
            content: 文档全文
            metadata: 文档元数据（随每个 chunk 一起存储，便于检索时回溯）
            tenant_id: WS-4 归属租户，缺省取构造函数 / 上下文租户

        Returns:
            索引结果摘要，如
            {"doc_id": str, "paragraphs": int, "sentences": int,
             "keywords": int, "total_vectors": int}
        """
        if not doc_id:
            raise ValueError("doc_id 不能为空")
        if not content or not content.strip():
            raise ValueError("content 不能为空")
        metadata = metadata or {}
        effective_tenant = tenant_id or self.tenant_id or get_current_tenant()

        # 先删除旧索引，避免重复写入（支持重新索引 / upsert 语义）
        # 只删本租户的，避免重名 doc_id 抹掉别的租户数据
        await self.delete_document(doc_id, tenant_id=effective_tenant)

        # 多粒度分块
        paragraphs = self.split_paragraphs(content)
        sentences = self.split_sentences(content)
        keywords = extract_keywords(content, top_n=self.KEYWORD_TOP_N)

        # 各粒度分别嵌入并存储
        paragraph_count = await self._index_granularity(
            self.GRANULARITY_PARAGRAPH,
            self.paragraph_collection_name,
            doc_id,
            paragraphs,
            metadata,
            effective_tenant,
        )
        sentence_count = await self._index_granularity(
            self.GRANULARITY_SENTENCE,
            self.sentence_collection_name,
            doc_id,
            sentences,
            metadata,
            effective_tenant,
        )
        keyword_count = await self._index_granularity(
            self.GRANULARITY_KEYWORD,
            self.keyword_collection_name,
            doc_id,
            keywords,
            metadata,
            effective_tenant,
        )

        total = paragraph_count + sentence_count + keyword_count
        logger.info(
            "文档 %s 多向量索引完成: 段落=%d, 句子=%d, 关键词=%d, 总向量=%d",
            doc_id,
            paragraph_count,
            sentence_count,
            keyword_count,
            total,
        )
        return {
            "doc_id": doc_id,
            "paragraphs": paragraph_count,
            "sentences": sentence_count,
            "keywords": keyword_count,
            "total_vectors": total,
        }

    async def search(
        self,
        query: str,
        top_k: int = 5,
        tenant_id: Optional[str] = None,
        allow_cross_tenant: bool = False,
    ) -> List[Dict[str, Any]]:
        """多路召回（段落/句子/关键词）+ RRF 融合排序，返回去重结果

        Args:
            query: 查询文本
            top_k: 返回结果数
            tenant_id: WS-4 检索作用域，缺省取构造函数 / 上下文租户
            allow_cross_tenant: 显式开启平台级跨租户检索（默认 False）。
                仅用于运维巡检/全局质量分析，业务路径不得开启。

        Returns:
            检索结果列表，每项格式：
            {"content": str, "score": float, "metadata": dict,
             "doc_id": str, "granularity": str,
             "matched_granularities": List[str], "source": "multi_vector"}
        """
        if not query or not query.strip():
            return []
        top_k = max(1, int(top_k))
        scope = self._resolve_tenant(tenant_id, allow_cross_tenant)
        # 每路多召回一些候选，保证融合后 top_k 充足
        candidate_k = max(top_k * 2, top_k + 5)
        # 租户过滤是召回后过滤（Chroma where 无法表达「等于 X 或字段缺失」），
        # 因此额外放大候选数，避免过滤后结果不足
        fetch_k = candidate_k * 3 if scope is not None else candidate_k

        # 预计算查询向量（避免 ChromaDB 内部 __call__ 在事件循环内抛错）
        query_embedding = await self._embed_single(query)

        # 三路并行召回
        paragraph_results, sentence_results, keyword_results = await asyncio.gather(
            self._query_collection(
                self.paragraph_collection_name,
                query,
                query_embedding,
                fetch_k,
                scope,
            ),
            self._query_collection(
                self.sentence_collection_name,
                query,
                query_embedding,
                fetch_k,
                scope,
            ),
            self._query_collection(
                self.keyword_collection_name,
                query,
                query_embedding,
                fetch_k,
                scope,
            ),
        )

        # RRF 三路融合 + 去重
        return self.rrf_fusion(
            [paragraph_results, sentence_results, keyword_results], top_k
        )

    async def delete_document(
        self,
        doc_id: str,
        tenant_id: Optional[str] = None,
        allow_cross_tenant: bool = False,
    ) -> Dict[str, Any]:
        """删除文档在所有粒度 collection 中的全部向量

        Args:
            doc_id: 文档唯一标识
            tenant_id: WS-4 删除作用域，缺省取构造函数 / 上下文租户
            allow_cross_tenant: 显式跨租户删除（默认 False）。危险操作，
                仅用于数据治理脚本。

        Returns:
            删除结果摘要，如
            {"doc_id": str, "deleted": {"paragraph": int, "sentence": int, "keyword": int},
             "total_deleted": int}
        """
        if not doc_id:
            return {"doc_id": "", "deleted": {}, "total_deleted": 0}

        scope = self._resolve_tenant(tenant_id, allow_cross_tenant)
        deleted: Dict[str, int] = {}
        for granularity, collection_name in self._granularity_collections():
            deleted[granularity] = await self._delete_from_collection(
                collection_name, doc_id, scope
            )
        total = sum(deleted.values())
        logger.info(
            "文档 %s 多向量索引删除: 段落=%d, 句子=%d, 关键词=%d, 总删除=%d",
            doc_id,
            deleted.get(self.GRANULARITY_PARAGRAPH, 0),
            deleted.get(self.GRANULARITY_SENTENCE, 0),
            deleted.get(self.GRANULARITY_KEYWORD, 0),
            total,
        )
        return {"doc_id": doc_id, "deleted": deleted, "total_deleted": total}

    async def get_stats(
        self,
        tenant_id: Optional[str] = None,
        allow_cross_tenant: bool = False,
    ) -> Dict[str, Any]:
        """获取多向量索引统计信息（各粒度 collection 的向量数）

        Args:
            tenant_id: WS-4 统计作用域，缺省取构造函数 / 上下文租户
            allow_cross_tenant: 显式统计整个 collection（平台级视角，默认 False）。
                开启后走 `collection.count()` 快路径；否则需要拉 metadata 逐条
                判定归属，成本更高但保证不泄漏其他租户的规模信息。

        Returns:
            {"collections": {"paragraph": int, "sentence": int, "keyword": int},
             "total_vectors": int, "tenant_id": str | None,
             "collection_names": {"paragraph": str, "sentence": str, "keyword": str}}
        """
        scope = self._resolve_tenant(tenant_id, allow_cross_tenant)
        stats: Dict[str, int] = {}
        total = 0
        names: Dict[str, str] = {}
        for granularity, collection_name in self._granularity_collections():
            names[granularity] = collection_name
            collection = self._get_collection(collection_name)
            try:
                if scope is None:
                    count = await asyncio.to_thread(collection.count)
                else:
                    result = await asyncio.to_thread(
                        collection.get, include=["metadatas"]
                    )
                    metadatas = result.get("metadatas", []) or []
                    count = sum(
                        1 for m in metadatas if _chunk_tenant_allowed(m, scope)
                    )
            except Exception as e:
                logger.warning("获取 collection %s 计数失败: %s", collection_name, e)
                count = 0
            stats[granularity] = count
            total += count
        return {
            "collections": stats,
            "total_vectors": total,
            "tenant_id": scope,
            "collection_names": names,
        }

    # --------------------------------------------------------
    # 分块策略
    # --------------------------------------------------------

    def split_paragraphs(self, content: str) -> List[str]:
        """段落级分块：按 \\n\\n 分割，每段最大 500 字符（超出按上限切分）

        Args:
            content: 文档全文

        Returns:
            段落列表（已去除空白段）
        """
        if not content or not content.strip():
            return []
        # 优先按双换行（空行）分段，兼容多种空白形式
        raw = re.split(r"\n\s*\n", content)
        # 若无空行分段但含单换行，按单换行进一步切分
        if len(raw) <= 1 and "\n" in content:
            raw = content.split("\n")
        paragraphs: List[str] = []
        for seg in raw:
            seg = seg.strip()
            if not seg:
                continue
            paragraphs.extend(self._cap_segment(seg, self.PARAGRAPH_MAX_CHARS))
        return paragraphs

    def split_sentences(self, content: str) -> List[str]:
        """句子级分块：先按换行切分（尊重段落/行边界），再按 。！？!? 切分，
        每句最大 200 字符（超出按上限切分）

        Args:
            content: 文档全文

        Returns:
            句子列表（已去除空白句，保留句末标点）
        """
        if not content or not content.strip():
            return []
        sentences: List[str] = []
        # 先按换行切分，避免句子跨段落携带换行符
        for line in re.split(r"\n+", content):
            line = line.strip()
            if not line:
                continue
            # 在句末标点之后切分，保留标点附着于前句（lookbehind 零宽断言）
            for seg in re.split(r"(?<=[。！？!?])", line):
                seg = seg.strip()
                if not seg:
                    continue
                sentences.extend(self._cap_segment(seg, self.SENTENCE_MAX_CHARS))
        return sentences

    @staticmethod
    def _cap_segment(text: str, max_chars: int) -> List[str]:
        """将文本按 max_chars 上限切分（无重叠，保证每块不超过上限）

        Args:
            text: 待切分文本（已 strip）
            max_chars: 单块最大字符数

        Returns:
            切分后的块列表（过滤空白块）
        """
        if not text:
            return []
        if max_chars <= 0 or len(text) <= max_chars:
            return [text]
        chunks: List[str] = []
        for i in range(0, len(text), max_chars):
            chunk = text[i : i + max_chars]
            if chunk.strip():
                chunks.append(chunk)
        return chunks

    # --------------------------------------------------------
    # RRF 融合
    # --------------------------------------------------------

    def rrf_fusion(
        self,
        result_lists: List[List[Dict[str, Any]]],
        top_k: int,
        rrf_k: int = RRF_K,
    ) -> List[Dict[str, Any]]:
        """RRF (Reciprocal Rank Fusion) 融合多路检索结果

        加权 RRF 公式（三路等权）：
            score(d) = Σ 1/(k + rank_i(d))

        其中 rank_i 从 1 开始，k 为 RRF 常数（默认 60）。
        以 content 作为去重键，同一内容在多路命中时分数累加。

        Args:
            result_lists: 多路检索结果列表（每路一个结果列表）
            top_k: 返回结果数
            rrf_k: RRF 常数 k

        Returns:
            融合后的去重结果列表，按融合分数降序，取 top_k
        """
        fused_scores: Dict[str, float] = {}
        doc_map: Dict[str, Dict[str, Any]] = {}
        granularities_map: Dict[str, set] = {}

        for result_list in result_lists:
            for rank, item in enumerate(result_list, start=1):
                # 以 content 作为文档去重键
                key = item.get("content", "")
                if not key:
                    continue
                rrf_score = 1.0 / (rrf_k + rank)
                fused_scores[key] = fused_scores.get(key, 0.0) + rrf_score
                if key not in doc_map:
                    doc_map[key] = item
                    granularities_map[key] = set()
                # 记录该内容命中的粒度
                gran = item.get("granularity", "")
                if gran:
                    granularities_map[key].add(gran)

        # 按融合分数降序排序，取 top_k
        sorted_keys = sorted(
            fused_scores.keys(), key=lambda k: fused_scores[k], reverse=True
        )

        output: List[Dict[str, Any]] = []
        for key in sorted_keys[:top_k]:
            item = doc_map[key]
            output.append(
                {
                    "content": key,
                    "score": fused_scores[key],
                    "metadata": item.get("metadata", {}),
                    "doc_id": item.get("doc_id", ""),
                    "granularity": item.get("granularity", ""),
                    "matched_granularities": sorted(granularities_map[key]),
                    "source": "multi_vector",
                }
            )
        return output

    # --------------------------------------------------------
    # 索引写入辅助
    # --------------------------------------------------------

    async def _index_granularity(
        self,
        granularity: str,
        collection_name: str,
        doc_id: str,
        chunks: List[str],
        metadata: Dict[str, Any],
        tenant_id: Optional[str] = None,
    ) -> int:
        """将单粒度的分块列表批量嵌入并写入对应 collection

        Args:
            granularity: 粒度标识
            collection_name: collection 名称
            doc_id: 文档 ID
            chunks: 分块文本列表
            metadata: 文档元数据
            tenant_id: WS-4 归属租户，写入 chunk metadata 供检索过滤

        Returns:
            写入的向量数
        """
        # 过滤空白块
        chunks = [c for c in chunks if c and c.strip()]
        if not chunks:
            return 0

        collection = self._get_collection(collection_name)

        # 批量嵌入（EmbeddingClient 支持批量；DummyEmbeddingFunction 走逐条）
        vectors = await self._embed_batch(chunks)
        if len(vectors) != len(chunks):
            logger.warning(
                "粒度 %s 批量嵌入数量不匹配: %d/%d，跳过该粒度写入",
                granularity,
                len(vectors),
                len(chunks),
            )
            return 0

        ids: List[str] = []
        documents: List[str] = []
        metadatas: List[Dict[str, Any]] = []
        embeddings: List[List[float]] = []

        doc_metadata_json = json.dumps(metadata, ensure_ascii=False)

        for idx, (chunk, vec) in enumerate(zip(chunks, vectors)):
            # chunk_id 含粒度、索引与内容 hash，保证唯一且可重新索引
            content_hash = hashlib.sha256(
                chunk.encode("utf-8")
            ).hexdigest()[:8]
            chunk_id = f"{doc_id}__{granularity}__{idx}__{content_hash}"
            ids.append(chunk_id)
            documents.append(chunk)
            # ChromaDB metadata 仅支持基本类型，用户元数据序列化为 JSON 字符串
            chunk_meta: Dict[str, Any] = {
                "doc_id": doc_id,
                "granularity": granularity,
                "chunk_index": idx,
                "doc_metadata": doc_metadata_json,
            }
            # WS-4：租户打标（collection 前缀之外的第二层隔离）
            if tenant_id:
                chunk_meta[TENANT_META_KEY] = tenant_id
            metadatas.append(chunk_meta)
            embeddings.append(vec)

        await asyncio.to_thread(
            collection.upsert,
            ids=ids,
            documents=documents,
            metadatas=metadatas,
            embeddings=embeddings,
        )
        return len(ids)

    # --------------------------------------------------------
    # 检索辅助
    # --------------------------------------------------------

    async def _query_collection(
        self,
        collection_name: str,
        query: str,
        query_embedding: Optional[List[float]],
        top_k: int,
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """对单个 collection 进行向量检索，返回标准化结果列表

        Args:
            collection_name: collection 名称
            query: 查询文本（query_embedding 不可用时降级使用）
            query_embedding: 预计算的查询向量
            top_k: 返回结果数
            tenant_id: WS-4 租户过滤，None 表示跨租户（调用方已显式授权）

        Returns:
            结果列表，每项 {"content", "score", "metadata", "doc_id", "granularity"}
        """
        collection = self._get_collection(collection_name)
        query_kwargs: Dict[str, Any] = {
            "n_results": top_k,
            "include": ["metadatas", "documents", "distances"],
        }
        # 优先使用预计算向量，避免 ChromaDB 内部 __call__ 在事件循环内抛错
        if query_embedding is not None:
            query_kwargs["query_embeddings"] = [query_embedding]
        else:
            query_kwargs["query_texts"] = [query]

        try:
            results = await asyncio.to_thread(collection.query, **query_kwargs)
        except Exception as e:
            logger.warning("collection %s 查询失败: %s", collection_name, e)
            return []

        output: List[Dict[str, Any]] = []
        metadatas = results.get("metadatas", [[]])[0] or []
        documents = results.get("documents", [[]])[0] or []
        distances = results.get("distances", [[]])[0] or []

        for meta, doc, distance in zip(metadatas, documents, distances):
            if not doc:
                continue
            # WS-4 租户后过滤：Chroma where 无法表达「等于 X 或字段缺失」，
            # 因此在召回结果上判定归属（存量未打标的 chunk 放行）
            if tenant_id is not None and not _chunk_tenant_allowed(meta, tenant_id):
                continue
            # 解析随 chunk 存储的用户元数据（JSON 字符串）
            doc_meta: Dict[str, Any] = {}
            if meta:
                raw = meta.get("doc_metadata")
                if isinstance(raw, str):
                    try:
                        doc_meta = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        doc_meta = {}
            # cosine 空间下 distance ∈ [0, 2]，转换为相似度分数
            score = 1.0 - float(distance or 0.0)
            output.append(
                {
                    "content": doc,
                    "score": score,
                    "metadata": doc_meta,
                    "doc_id": (meta or {}).get("doc_id", ""),
                    "granularity": (meta or {}).get("granularity", ""),
                }
            )
        return output

    async def _delete_from_collection(
        self, collection_name: str, doc_id: str, tenant_id: Optional[str] = None
    ) -> int:
        """从指定 collection 删除文档的所有向量

        Args:
            collection_name: collection 名称
            doc_id: 文档 ID
            tenant_id: WS-4 租户过滤，None 表示跨租户删除（调用方已显式授权）

        Returns:
            删除的向量数
        """
        collection = self._get_collection(collection_name)
        try:
            # 先按 doc_id 查出所有 chunk id（用于计数）
            result = await asyncio.to_thread(
                collection.get, where={"doc_id": doc_id}
            )
            ids = result.get("ids", []) or []
            # WS-4：只删本租户的 chunk，避免不同租户 doc_id 重名时互相抹数据
            if tenant_id is not None and ids:
                metadatas = result.get("metadatas", []) or []
                ids = [
                    cid
                    for i, cid in enumerate(ids)
                    if _chunk_tenant_allowed(
                        metadatas[i] if i < len(metadatas) else None, tenant_id
                    )
                ]
            if ids:
                await asyncio.to_thread(collection.delete, ids=ids)
            return len(ids)
        except Exception as e:
            logger.warning(
                "从 collection %s 删除文档 %s 失败: %s",
                collection_name,
                doc_id,
                e,
            )
            return 0

    # --------------------------------------------------------
    # 嵌入辅助
    # --------------------------------------------------------

    async def _embed_batch(self, texts: List[str]) -> List[List[float]]:
        """批量嵌入文本

        优先使用 EmbeddingClient.embed（批量，高效）；
        DummyEmbeddingFunction 无 embed 方法时逐条 embed_query。

        Args:
            texts: 文本列表

        Returns:
            向量列表（与 texts 等长）
        """
        if not texts:
            return []
        embedding = getattr(self.kb_store, "embedding", None)
        if embedding is None:
            return []
        if hasattr(embedding, "embed"):
            return await embedding.embed(texts)
        # DummyEmbeddingFunction 仅暴露 embed_query，逐条调用
        return [await embedding.embed_query(t) for t in texts]

    async def _embed_single(self, text: str) -> Optional[List[float]]:
        """单条文本嵌入（用于查询向量预计算）"""
        embedding = getattr(self.kb_store, "embedding", None)
        if embedding is None:
            return None
        if hasattr(embedding, "embed_query"):
            return await embedding.embed_query(text)
        return None

    # --------------------------------------------------------
    # Collection 辅助
    # --------------------------------------------------------

    def _granularity_collections(self) -> List[Tuple[str, str]]:
        """返回 (粒度, collection名) 列表"""
        return [
            (self.GRANULARITY_PARAGRAPH, self.paragraph_collection_name),
            (self.GRANULARITY_SENTENCE, self.sentence_collection_name),
            (self.GRANULARITY_KEYWORD, self.keyword_collection_name),
        ]

    def _get_collection(self, collection_name: str):
        """获取或创建指定名称的 ChromaDB collection（带缓存）

        复用 kb_store 的 ChromaDB 客户端与 embedding 函数。
        """
        if collection_name in self._collections:
            return self._collections[collection_name]
        client = getattr(self.kb_store, "client", None)
        embedding = getattr(self.kb_store, "embedding", None)
        if client is None:
            raise RuntimeError("kb_store 缺少 ChromaDB client，无法获取 collection")
        kwargs: Dict[str, Any] = {
            "name": collection_name,
            "metadata": {"hnsw:space": "cosine"},
        }
        if embedding is not None:
            kwargs["embedding_function"] = embedding
        collection = client.get_or_create_collection(**kwargs)
        self._collections[collection_name] = collection
        return collection
