"""混合检索服务（向量检索 + BM25 全文检索）

在 memory/vector_store.py 中已有的 ChromaDB 向量检索基础上，增加 BM25 全文检索能力，
通过 RRF (Reciprocal Rank Fusion) 算法融合两路检索结果，实现混合检索。

核心功能：
1. 混合检索（alpha 参数控制向量/BM25 权重，0=纯BM25, 1=纯向量, 0.5=等权混合）
2. 纯向量检索 / 纯 BM25 检索
3. 元数据过滤（向量检索走 ChromaDB where，BM25 走结果后过滤）
4. 文档增量更新（hash 对比 + difflib 段落级差异 + 仅重建变化部分）

BM25 实现：优先使用 rank_bm25 库，未安装时降级为纯 Python 实现。
"""

import asyncio
import hashlib
import json
import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple

from core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# 尝试导入 rank_bm25，未安装时使用纯 Python 实现的 BM25Okapi
try:
    from rank_bm25 import BM25Okapi as _RankBM25Okapi

    _HAS_RANK_BM25 = True
except ImportError:
    _HAS_RANK_BM25 = False
    _RankBM25Okapi = None  # type: ignore[assignment,misc]


# ============================================================
# 分词工具
# ============================================================


# 匹配单个 CJK 字符 或 连续的字母数字下划线（英文单词）
_TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]|[\w]+", re.UNICODE)


def _tokenize(text: str) -> List[str]:
    """简单分词器：CJK 字符按单字切分，非 CJK 按空格/标点切分为单词。

    纯 Python 实现，不依赖 jieba，适用于中英文混合文本的 BM25 检索。
    """
    if not text:
        return []
    return [t.lower() for t in _TOKEN_PATTERN.findall(text)]


# ============================================================
# 纯 Python BM25 实现（rank_bm25 未安装时的降级方案）
# ============================================================


class _PurePythonBM25Okapi:
    """BM25Okapi 的纯 Python 实现，接口与 rank_bm25.BM25Okapi 保持一致。

    公式：score(q, d) = Σ_t IDF(t) * (tf(t,d) * (k1+1)) / (tf(t,d) + k1*(1-b+b*|d|/avgdl))
    其中 IDF(t) = log((N - df(t) + 0.5) / (df(t) + 0.5) + 1)
    """

    def __init__(self, corpus: List[List[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.corpus_size = len(corpus)
        self.doc_len = [len(doc) for doc in corpus]
        self.avgdl = (
            sum(self.doc_len) / self.corpus_size if self.corpus_size > 0 else 0.0
        )

        # 统计每个词的文档频率 (df) 和每篇文档的词频 (tf)
        self.df: Dict[str, int] = {}
        self.idf: Dict[str, float] = {}
        self.tf: List[Dict[str, int]] = []

        for doc_tokens in corpus:
            tf: Dict[str, int] = {}
            for token in doc_tokens:
                tf[token] = tf.get(token, 0) + 1
            self.tf.append(tf)
            for token in tf:
                self.df[token] = self.df.get(token, 0) + 1

        # 计算 IDF
        for token, freq in self.df.items():
            self.idf[token] = math.log(
                (self.corpus_size - freq + 0.5) / (freq + 0.5) + 1
            )

    def get_scores(self, query: List[str]) -> List[float]:
        """计算 query 与语料库中每篇文档的 BM25 分数"""
        scores = [0.0] * self.corpus_size
        for i in range(self.corpus_size):
            doc_tf = self.tf[i]
            doc_len = self.doc_len[i]
            for token in query:
                tf = doc_tf.get(token, 0)
                if tf == 0:
                    continue
                idf = self.idf.get(token, 0.0)
                # BM25 分数公式
                if self.avgdl > 0:
                    denom = tf + self.k1 * (
                        1 - self.b + self.b * doc_len / self.avgdl
                    )
                else:
                    denom = tf + self.k1
                scores[i] += idf * tf * (self.k1 + 1) / denom
        return scores


def _create_bm25(corpus: List[List[str]], k1: float = 1.5, b: float = 0.75):
    """创建 BM25 实例：优先使用 rank_bm25 库，降级为纯 Python 实现"""
    if _HAS_RANK_BM25 and _RankBM25Okapi is not None:
        return _RankBM25Okapi(corpus, k1=k1, b=b)
    return _PurePythonBM25Okapi(corpus, k1=k1, b=b)


# ============================================================
# 默认检索配置
# ============================================================

_DEFAULT_CONFIG = {
    "default_alpha": "0.5",
    "bm25_enabled": "true",
    "rrf_k": "60",
    "bm25_k1": "1.5",
    "bm25_b": "0.75",
}

_CONFIG_DESCRIPTIONS = {
    "default_alpha": "混合检索默认权重（0=纯BM25, 1=纯向量, 0.5=等权混合）",
    "bm25_enabled": "是否启用 BM25 全文检索",
    "rrf_k": "RRF (Reciprocal Rank Fusion) 常数 k",
    "bm25_k1": "BM25 参数 k1（词频饱和度）",
    "bm25_b": "BM25 参数 b（文档长度归一化）",
}


# ============================================================
# HybridSearchService
# ============================================================


class HybridSearchService:
    """混合检索服务

    在 ChromaDB 向量检索基础上增加 BM25 全文检索，通过 RRF 算法融合两路结果。
    支持元数据过滤、文档增量更新。

    Args:
        kb_store: ChromaCompanyKB 实例，提供 ChromaDB 客户端、embedding 函数与默认 collection
        settings: 应用配置，未提供时从 kb_store 或全局获取
    """

    def __init__(
        self,
        kb_store,
        settings: Optional[Settings] = None,
    ):
        self.kb_store = kb_store
        self.settings = settings or getattr(kb_store, "settings", None) or get_settings()
        # BM25 索引缓存：{collection_name: (bm25_instance, doc_list, doc_id_list, doc_meta_list)}
        # 每次 incremental_update 或文档变更后清除缓存，下次检索时重建
        self._bm25_cache: Dict[str, Tuple[Any, List[str], List[str], List[dict]]] = {}

    # --------------------------------------------------------
    # 公共方法
    # --------------------------------------------------------

    async def search(
        self,
        query: str,
        collection_name: str,
        top_k: int = 5,
        metadata_filter: Optional[dict] = None,
        alpha: float = 0.5,
    ) -> List[Dict[str, Any]]:
        """混合检索（向量 + BM25）

        Args:
            query: 查询文本
            collection_name: ChromaDB collection 名称
            top_k: 返回结果数
            metadata_filter: 元数据过滤条件，如 {"source": "hr_manual", "department": "tech"}
            alpha: 向量/BM25 权重（0=纯BM25, 1=纯向量, 0.5=等权混合）

        Returns:
            检索结果列表，每项格式：
            {"content": str, "score": float, "metadata": dict, "source": "vector"|"bm25"|"hybrid"}
        """
        # 边界处理：alpha 裁剪到 [0, 1]
        alpha = max(0.0, min(1.0, float(alpha)))

        # alpha=1.0 → 纯向量检索
        if alpha >= 1.0:
            return await self._vector_search(
                query, collection_name, top_k, metadata_filter
            )

        # alpha=0.0 → 纯 BM25 检索
        if alpha <= 0.0:
            return await self._bm25_search(
                query, collection_name, top_k, metadata_filter
            )

        # 混合检索：两路并行检索 + RRF 融合
        # 每路取 top_k * 2 候选，避免融合后 top_k 不足
        candidate_k = max(top_k * 2, top_k + 5)
        vector_results, bm25_results = await asyncio.gather(
            self._vector_search(query, collection_name, candidate_k, metadata_filter),
            self._bm25_search(query, collection_name, candidate_k, metadata_filter),
        )

        return self._rrf_fusion(vector_results, bm25_results, alpha, top_k)

    async def incremental_update(
        self,
        document_id: str,
        content: str,
        metadata: Optional[dict] = None,
    ) -> Dict[str, Any]:
        """文档增量更新

        对比新旧内容的 hash，如果内容未变化则跳过；
        如果内容变化，通过 difflib 对比段落级差异，只重新分块和嵌入变化的部分。

        Args:
            document_id: 文档 ID（对应 ChromaDB 中的 kb_id / parent_kb_id）
            content: 新的文档全文
            metadata: 文档元数据

        Returns:
            更新结果摘要，如 {"updated": bool, "added": int, "deleted": int, "reason": str}
        """
        metadata = metadata or {}
        collection = self._get_collection(self.kb_store.collection.name)
        chunk_size = getattr(self.settings, "chunk_size", 800) or 800
        chunk_overlap = getattr(self.settings, "chunk_overlap", 100) or 0

        # 1. 获取现有文档的所有 chunk
        old_chunks = await self._get_document_chunks(collection, document_id)

        # 2. 计算新内容的 hash
        new_content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        # 3. 从现有 chunk 的 metadata 中获取旧 content_hash
        old_content_hash = None
        if old_chunks:
            # 取第一个 chunk 的 metadata 中的 content_hash
            first_meta = old_chunks[0].get("metadata", {}) or {}
            old_content_hash = first_meta.get("content_hash")

        # 4. hash 对比：内容未变化则跳过
        if old_content_hash and old_content_hash == new_content_hash:
            logger.debug(f"文档 {document_id} 内容未变化，跳过增量更新")
            return {
                "updated": False,
                "added": 0,
                "deleted": 0,
                "reason": "content unchanged (hash match)",
            }

        # 5. 内容变化：通过 difflib 对比段落级差异
        # 重建旧内容（按 paragraph_index + chunk_index 排序拼接）
        old_paragraphs = self._extract_paragraphs_from_chunks(old_chunks)
        new_paragraphs = self._split_paragraphs(content)

        added_count = 0
        deleted_count = 0

        # 如果旧 chunk 没有 paragraph 信息（首次索引或旧格式），全量重建
        if not old_chunks or not old_paragraphs:
            # 删除所有旧 chunk
            if old_chunks:
                old_ids = [c["id"] for c in old_chunks if c.get("id")]
                if old_ids:
                    await asyncio.to_thread(collection.delete, ids=old_ids)
                    deleted_count = len(old_ids)
            # 全量索引新内容
            added_count = await self._index_paragraphs(
                collection,
                document_id,
                new_paragraphs,
                metadata,
                new_content_hash,
                chunk_size,
                chunk_overlap,
            )
        else:
            # 使用 difflib 对比段落级差异
            added_count, deleted_count = await self._diff_and_update(
                collection,
                document_id,
                old_paragraphs,
                new_paragraphs,
                metadata,
                new_content_hash,
                chunk_size,
                chunk_overlap,
            )

        # 6. 清除 BM25 索引缓存，下次检索时重建
        cache_key = self.kb_store.collection.name
        self._bm25_cache.pop(cache_key, None)

        logger.info(
            f"文档 {document_id} 增量更新完成: 新增 {added_count} chunk, 删除 {deleted_count} chunk"
        )
        return {
            "updated": True,
            "added": added_count,
            "deleted": deleted_count,
            "reason": "content changed",
            "content_hash": new_content_hash,
        }

    # --------------------------------------------------------
    # 向量检索
    # --------------------------------------------------------

    async def _vector_search(
        self,
        query: str,
        collection_name: str,
        top_k: int,
        metadata_filter: Optional[dict],
    ) -> List[Dict[str, Any]]:
        """纯向量检索，调用 ChromaCompanyKB 的 query 方法

        元数据过滤通过两层保障：
        1. ChromaDB where 参数：对 ChromaDB 顶层 metadata 字段（如 kb_id, parent_kb_id）高效过滤
        2. 结果后过滤：对嵌套在 metadata JSON 字符串中的用户自定义字段（如 source, department）过滤
        由于用户元数据可能存储为 JSON 字符串（ChromaCompanyKB.add_document 的存储方式），
        ChromaDB where 无法匹配这些字段，因此需要 over-fetch + 后过滤。
        """
        # 构建 ChromaDB where 条件（对顶层字段生效）
        where = self._build_chroma_where(metadata_filter)

        # over-fetch：为后过滤预留余量，避免过滤后结果不足
        fetch_k = top_k * 3 if metadata_filter else top_k

        # 调用现有 ChromaCompanyKB 的 query 方法
        try:
            results = await self.kb_store.query(query, top_k=fetch_k, where=where)
        except Exception as e:
            logger.warning(f"向量检索失败: {e}")
            return []

        # 统一输出格式 + 后过滤
        output: List[Dict[str, Any]] = []
        for r in results or []:
            if not isinstance(r, dict):
                continue
            content = r.get("content", "") or ""
            score = r.get("_retrieval_score")
            if score is None:
                score = r.get("score", 0.0)
            meta = r.get("metadata", {}) or {}
            # metadata 可能是 JSON 字符串（ChromaCompanyKB 存储时序列化）
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            # 后过滤：检查嵌套 metadata 中的用户自定义字段
            if metadata_filter and not self._metadata_matches(meta, metadata_filter):
                continue
            output.append(
                {
                    "content": content,
                    "score": float(score),
                    "metadata": meta,
                    "source": "vector",
                }
            )
            if len(output) >= top_k:
                break
        return output

    # --------------------------------------------------------
    # BM25 检索
    # --------------------------------------------------------

    async def _bm25_search(
        self,
        query: str,
        collection_name: str,
        top_k: int,
        metadata_filter: Optional[dict],
    ) -> List[Dict[str, Any]]:
        """纯 BM25 全文检索

        从 ChromaDB collection 中取出所有文档构建 BM25 索引，
        对查询打分后按 metadata_filter 过滤。
        """
        # 获取或构建 BM25 索引
        bm25_instance, doc_texts, doc_ids, doc_metas = await self._get_or_build_bm25_index(
            collection_name
        )

        if not doc_texts:
            return []

        # 对查询打分
        query_tokens = _tokenize(query)
        try:
            scores = bm25_instance.get_scores(query_tokens)
        except Exception as e:
            logger.warning(f"BM25 打分失败: {e}")
            return []

        # 组装结果并按分数降序排序
        scored: List[Tuple[float, str, str, dict]] = []
        for i, score in enumerate(scores):
            if score <= 0:
                continue
            scored.append((float(score), doc_texts[i], doc_ids[i], doc_metas[i]))
        scored.sort(key=lambda x: x[0], reverse=True)

        # 元数据过滤（BM25 结果后过滤）
        output: List[Dict[str, Any]] = []
        for score, text, doc_id, meta in scored:
            if metadata_filter and not self._metadata_matches(meta, metadata_filter):
                continue
            output.append(
                {
                    "content": text,
                    "score": score,
                    "metadata": meta,
                    "source": "bm25",
                }
            )
            if len(output) >= top_k:
                break
        return output

    async def _get_or_build_bm25_index(
        self, collection_name: str
    ) -> Tuple[Any, List[str], List[str], List[dict]]:
        """获取或构建 BM25 索引（带缓存）

        缓存键为 collection_name，incremental_update 后清除缓存。
        """
        if collection_name in self._bm25_cache:
            return self._bm25_cache[collection_name]

        collection = self._get_collection(collection_name)

        # 从 ChromaDB 取出所有文档
        try:
            result = await asyncio.to_thread(
                collection.get,
                include=["metadatas", "documents"],
            )
        except Exception as e:
            logger.warning(f"BM25 索引构建: 获取 collection 文档失败: {e}")
            return None, [], [], []

        ids = result.get("ids", [])
        documents = result.get("documents", [])
        metadatas = result.get("metadatas", [])

        # 过滤掉空文档
        doc_texts: List[str] = []
        doc_ids: List[str] = []
        doc_metas: List[dict] = []
        for i, doc in enumerate(documents):
            if not doc or not doc.strip():
                continue
            doc_texts.append(doc)
            doc_ids.append(ids[i] if i < len(ids) else "")
            meta = metadatas[i] if i < len(metadatas) and metadatas[i] else {}
            # 解析嵌套的 metadata JSON 字符串（ChromaCompanyKB.add_document 存储方式）
            # ChromaDB metadata 形如: {"kb_id": "x", "title": "y", "metadata": '{"source":"z"}'}
            # 将嵌套的 metadata JSON 解析后合并到顶层，使 _metadata_matches 能直接匹配用户字段
            meta = self._normalize_metadata(meta)
            doc_metas.append(meta)

        # 分词并构建 BM25 索引
        tokenized_corpus = [_tokenize(text) for text in doc_texts]

        # 从配置读取 BM25 参数
        k1, b = self._get_bm25_params()

        if tokenized_corpus:
            bm25_instance = _create_bm25(tokenized_corpus, k1=k1, b=b)
        else:
            bm25_instance = None

        cached = (bm25_instance, doc_texts, doc_ids, doc_metas)
        self._bm25_cache[collection_name] = cached
        return cached

    # --------------------------------------------------------
    # RRF 融合
    # --------------------------------------------------------

    def _rrf_fusion(
        self,
        vector_results: List[Dict[str, Any]],
        bm25_results: List[Dict[str, Any]],
        alpha: float,
        top_k: int,
    ) -> List[Dict[str, Any]]:
        """RRF (Reciprocal Rank Fusion) 融合两路检索结果

        加权 RRF 公式：
            score(d) = alpha * 1/(k + rank_v(d)) + (1-alpha) * 1/(k + rank_b(d))

        其中 rank 从 1 开始，k 为 RRF 常数（默认 60）。

        Args:
            vector_results: 向量检索结果列表
            bm25_results: BM25 检索结果列表
            alpha: 向量权重（0~1）
            top_k: 返回结果数

        Returns:
            融合后的结果列表，source 标记为 "hybrid"
        """
        rrf_k = self._get_rrf_k()
        vector_weight = alpha
        bm25_weight = 1.0 - alpha

        # 以 content 作为文档去重键（同一文档可能在两路检索中都出现）
        fused_scores: Dict[str, float] = {}
        doc_map: Dict[str, Dict[str, Any]] = {}

        # 向量检索结果按 rank 贡献分数
        for rank, item in enumerate(vector_results, start=1):
            content = item["content"]
            rrf_score = vector_weight * (1.0 / (rrf_k + rank))
            fused_scores[content] = fused_scores.get(content, 0.0) + rrf_score
            if content not in doc_map:
                doc_map[content] = item

        # BM25 检索结果按 rank 贡献分数
        for rank, item in enumerate(bm25_results, start=1):
            content = item["content"]
            rrf_score = bm25_weight * (1.0 / (rrf_k + rank))
            fused_scores[content] = fused_scores.get(content, 0.0) + rrf_score
            if content not in doc_map:
                doc_map[content] = item

        # 按融合分数降序排序，取 top_k
        sorted_contents = sorted(
            fused_scores.keys(), key=lambda c: fused_scores[c], reverse=True
        )

        output: List[Dict[str, Any]] = []
        for content in sorted_contents[:top_k]:
            item = doc_map[content]
            output.append(
                {
                    "content": content,
                    "score": fused_scores[content],
                    "metadata": item.get("metadata", {}),
                    "source": "hybrid",
                }
            )
        return output

    # --------------------------------------------------------
    # 增量更新辅助方法
    # --------------------------------------------------------

    async def _get_document_chunks(self, collection, document_id: str) -> List[dict]:
        """获取文档的所有 chunk（包括主文档和 parent_kb_id 子文档）"""
        chunks: List[dict] = []

        # 1. 尝试按 parent_kb_id 获取分块子文档
        try:
            result = await asyncio.to_thread(
                collection.get,
                where={"parent_kb_id": document_id},
                include=["metadatas", "documents"],
            )
            ids = result.get("ids", [])
            documents = result.get("documents", [])
            metadatas = result.get("metadatas", [])
            for i, doc_id in enumerate(ids):
                chunks.append(
                    {
                        "id": doc_id,
                        "content": documents[i] if i < len(documents) else "",
                        "metadata": metadatas[i] if i < len(metadatas) and metadatas[i] else {},
                    }
                )
        except Exception as e:
            logger.debug(f"按 parent_kb_id 获取 chunk 失败: {e}")

        # 2. 如果没有分块子文档，尝试获取主文档（id == document_id）
        if not chunks:
            try:
                result = await asyncio.to_thread(
                    collection.get,
                    ids=[document_id],
                    include=["metadatas", "documents"],
                )
                ids = result.get("ids", [])
                documents = result.get("documents", [])
                metadatas = result.get("metadatas", [])
                for i, doc_id in enumerate(ids):
                    chunks.append(
                        {
                            "id": doc_id,
                            "content": documents[i] if i < len(documents) else "",
                            "metadata": metadatas[i]
                            if i < len(metadatas) and metadatas[i]
                            else {},
                        }
                    )
            except Exception as e:
                logger.debug(f"获取主文档失败: {e}")

        # 按 paragraph_index + chunk_index 排序
        def _sort_key(c: dict) -> Tuple[int, int]:
            meta = c.get("metadata", {}) or {}
            return (
                int(meta.get("paragraph_index", -1)),
                int(meta.get("chunk_index", -1)),
            )

        chunks.sort(key=_sort_key)
        return chunks

    def _extract_paragraphs_from_chunks(
        self, chunks: List[dict]
    ) -> List[Tuple[int, str]]:
        """从现有 chunk 的 metadata 中提取段落信息

        Returns:
            [(paragraph_index, paragraph_text), ...] 按 paragraph_index 排序
        """
        if not chunks:
            return []

        para_map: Dict[int, str] = {}
        for chunk in chunks:
            meta = chunk.get("metadata", {}) or {}
            para_idx = meta.get("paragraph_index")
            if para_idx is None:
                # 旧格式 chunk 没有 paragraph_index，返回空让调用方走全量重建
                return []
            para_idx = int(para_idx)
            if para_idx not in para_map:
                para_map[para_idx] = chunk.get("content", "")
            else:
                # 同一段落的多个 chunk 拼接
                para_map[para_idx] += chunk.get("content", "")

        return sorted(para_map.items(), key=lambda x: x[0])

    def _split_paragraphs(self, content: str) -> List[str]:
        """将内容按段落切分（双换行为段落分隔，兼容单换行）"""
        if not content:
            return []
        # 优先按双换行（空行）分段
        paragraphs = re.split(r"\n\s*\n", content)
        # 如果只有一段但内容含单换行，按单换行进一步切分
        if len(paragraphs) <= 1 and "\n" in content:
            paragraphs = content.split("\n")
        # 过滤空白段落并去首尾空格
        return [p.strip() for p in paragraphs if p.strip()]

    async def _diff_and_update(
        self,
        collection,
        document_id: str,
        old_paragraphs: List[Tuple[int, str]],
        new_paragraphs: List[str],
        metadata: dict,
        content_hash: str,
        chunk_size: int,
        chunk_overlap: int,
    ) -> Tuple[int, int]:
        """使用 difflib 对比段落级差异，只更新变化部分

        Returns:
            (added_count, deleted_count)
        """
        import difflib

        # old_paragraphs: [(index, text), ...], new_paragraphs: [text, ...]
        old_texts = [text for _, text in old_paragraphs]
        old_indices = [idx for idx, _ in old_paragraphs]

        # SequenceMatcher 对比段落列表
        differ = difflib.SequenceMatcher(None, old_texts, new_paragraphs)

        added_count = 0
        deleted_count = 0
        # 记录新的段落索引偏移：旧段落删除/新增后，保留段落的 paragraph_index 需要重映射
        # 简化处理：新段落统一从 0 开始重新编号
        new_para_offset = 0

        for tag, i1, i2, j1, j2 in differ.get_opcodes():
            if tag == "equal":
                # 段落未变化，更新其 paragraph_index 为新编号
                for k in range(i2 - i1):
                    old_idx = old_indices[i1 + k]
                    new_idx = new_para_offset + k
                    if old_idx != new_idx:
                        # 需要更新 chunk 的 paragraph_index
                        await self._update_paragraph_index(
                            collection, document_id, old_idx, new_idx
                        )
                new_para_offset += i2 - i1
                continue

            if tag in ("delete", "replace"):
                # 删除旧段落对应的 chunk
                for k in range(i1, i2):
                    old_idx = old_indices[k]
                    count = await self._delete_paragraph_chunks(
                        collection, document_id, old_idx
                    )
                    deleted_count += count

            if tag in ("insert", "replace"):
                # 新增新段落对应的 chunk
                new_para_texts = new_paragraphs[j1:j2]
                count = await self._index_paragraphs(
                    collection,
                    document_id,
                    new_para_texts,
                    metadata,
                    content_hash,
                    chunk_size,
                    chunk_overlap,
                    paragraph_offset=new_para_offset,
                )
                added_count += count
                new_para_offset += j2 - j1

        return added_count, deleted_count

    async def _delete_paragraph_chunks(
        self, collection, document_id: str, paragraph_index: int
    ) -> int:
        """删除指定段落的所有 chunk"""
        try:
            result = await asyncio.to_thread(
                collection.get,
                where={
                    "$and": [
                        {"parent_kb_id": document_id},
                        {"paragraph_index": paragraph_index},
                    ]
                },
                include=["metadatas"],
            )
            ids = result.get("ids", [])
            if ids:
                await asyncio.to_thread(collection.delete, ids=ids)
            return len(ids)
        except Exception as e:
            logger.warning(f"删除段落 {paragraph_index} chunk 失败: {e}")
            return 0

    async def _update_paragraph_index(
        self,
        collection,
        document_id: str,
        old_index: int,
        new_index: int,
    ) -> None:
        """更新段落索引（删除旧 chunk 并按新索引重新写入）"""
        try:
            result = await asyncio.to_thread(
                collection.get,
                where={
                    "$and": [
                        {"parent_kb_id": document_id},
                        {"paragraph_index": old_index},
                    ]
                },
                include=["metadatas", "documents"],
            )
            ids = result.get("ids", [])
            documents = result.get("documents", [])
            metadatas = result.get("metadatas", [])
            if not ids:
                return

            # 删除旧 chunk
            await asyncio.to_thread(collection.delete, ids=ids)

            # 按新索引重新写入
            for i, old_id in enumerate(ids):
                doc = documents[i] if i < len(documents) else ""
                meta = dict(metadatas[i]) if i < len(metadatas) and metadatas[i] else {}
                meta["paragraph_index"] = new_index
                # 生成新 id
                new_id = f"{document_id}__p{new_index}__c{i}"
                await self._upsert_chunk(collection, new_id, doc, meta)
        except Exception as e:
            logger.warning(f"更新段落索引 {old_index}→{new_index} 失败: {e}")

    async def _index_paragraphs(
        self,
        collection,
        document_id: str,
        paragraphs: List[str],
        metadata: dict,
        content_hash: str,
        chunk_size: int,
        chunk_overlap: int,
        paragraph_offset: int = 0,
    ) -> int:
        """将段落列表分块并写入 ChromaDB

        Args:
            paragraph_offset: 段落起始编号（增量更新时用于对齐新段落索引）

        Returns:
            新增的 chunk 数量
        """
        added = 0
        for p_idx, paragraph in enumerate(paragraphs):
            real_para_idx = paragraph_offset + p_idx
            # 段落 hash，用于增量更新时快速判断段落是否变化
            para_hash = hashlib.md5(
                paragraph.encode("utf-8"), usedforsecurity=False
            ).hexdigest()

            # 按 chunk_size 切分段落
            chunks = self._chunk_text(paragraph, chunk_size, chunk_overlap)
            if not chunks:
                chunks = [paragraph]

            for c_idx, chunk in enumerate(chunks):
                chunk_id = f"{document_id}__p{real_para_idx}__c{c_idx}"
                chunk_meta = dict(metadata)
                chunk_meta["parent_kb_id"] = document_id
                chunk_meta["paragraph_index"] = real_para_idx
                chunk_meta["chunk_index"] = c_idx
                chunk_meta["chunk_total"] = len(chunks)
                chunk_meta["paragraph_hash"] = para_hash
                chunk_meta["content_hash"] = content_hash

                await self._upsert_chunk(collection, chunk_id, chunk, chunk_meta)
                added += 1

        return added

    async def _upsert_chunk(
        self, collection, chunk_id: str, document: str, metadata: dict
    ) -> None:
        """写入单个 chunk 到 ChromaDB（含 embedding）"""
        upsert_kwargs: Dict[str, Any] = {
            "ids": [chunk_id],
            "documents": [document],
            "metadatas": [metadata],
        }
        embedding = getattr(self.kb_store, "embedding", None)
        if embedding and hasattr(embedding, "embed_query"):
            try:
                upsert_kwargs["embeddings"] = [await embedding.embed_query(document)]
            except Exception as e:
                logger.error(f"chunk embedding 失败，跳过写入: {e}")
                raise
        await asyncio.to_thread(collection.upsert, **upsert_kwargs)

    def _chunk_text(self, content: str, chunk_size: int, chunk_overlap: int) -> List[str]:
        """按 chunk_size 切分文本，带 chunk_overlap 重叠"""
        if chunk_size <= 0 or len(content) <= chunk_size:
            return [content] if content.strip() else []
        chunks: List[str] = []
        start = 0
        text_len = len(content)
        while start < text_len:
            end = start + chunk_size
            chunks.append(content[start:end])
            if end >= text_len:
                break
            step = max(1, chunk_size - chunk_overlap)
            start += step
        return chunks

    # --------------------------------------------------------
    # 元数据过滤辅助
    # --------------------------------------------------------

    def _normalize_metadata(self, metadata: Optional[dict]) -> dict:
        """规范化 ChromaDB metadata：解析嵌套的 metadata JSON 字符串并合并到顶层

        ChromaCompanyKB.add_document 将用户元数据存储为 JSON 字符串放在 'metadata' 字段中：
            {"kb_id": "x", "title": "y", "metadata": '{"source":"z","department":"tech"}'}
        本方法将其解析后合并到顶层，使过滤逻辑能直接匹配用户自定义字段。
        """
        if not metadata:
            return {}
        result = dict(metadata)
        nested = result.get("metadata")
        if isinstance(nested, str):
            try:
                parsed = json.loads(nested)
                if isinstance(parsed, dict):
                    # 将解析后的用户元数据合并到顶层（不覆盖已有的顶层字段）
                    for k, v in parsed.items():
                        if k not in result:
                            result[k] = v
            except (json.JSONDecodeError, TypeError):
                pass
        elif isinstance(nested, dict):
            for k, v in nested.items():
                if k not in result:
                    result[k] = v
        return result

    def _build_chroma_where(
        self, metadata_filter: Optional[dict]
    ) -> Optional[Dict[str, Any]]:
        """将 metadata_filter 转换为 ChromaDB where 条件

        单条件: {"source": "hr_manual"} → {"source": "hr_manual"}
        多条件: {"source": "x", "dept": "y"} → {"$and": [{"source": "x"}, {"dept": "y"}]}

        注意：ChromaDB where 只能匹配 ChromaDB 顶层 metadata 字段。
        对于存储在嵌套 JSON 字符串中的用户元数据（如 ChromaCompanyKB.add_document 的存储方式），
        where 无法匹配，需要配合 _vector_search 的后过滤逻辑。
        """
        if not metadata_filter:
            return None
        conditions = [
            {k: v for k, v in metadata_filter.items() if v is not None}
        ]
        conditions = [c for c in conditions if c]
        if not conditions:
            return None
        if len(conditions) == 1 and len(conditions[0]) == 1:
            # 单条件直接返回
            return conditions[0]
        if len(conditions[0]) == 1:
            return conditions[0]
        # 多条件用 $and 组合（ChromaDB 1.x 要求）
        return {"$and": [{k: v} for k, v in conditions[0].items()]}

    def _metadata_matches(
        self, metadata: Optional[dict], metadata_filter: dict
    ) -> bool:
        """检查文档 metadata 是否满足过滤条件（向量检索和 BM25 结果后过滤用）

        支持两种 metadata 格式：
        1. 顶层字段: {"source": "hr_manual"} → 直接匹配
        2. 嵌套字段: {"metadata": '{"source":"hr_manual"}'} → 解析后匹配
        """
        if not metadata_filter:
            return True
        if not metadata:
            return False
        # 先规范化 metadata（解析嵌套 JSON）
        normalized = self._normalize_metadata(metadata)
        for key, value in metadata_filter.items():
            if value is None:
                continue
            if normalized.get(key) != value:
                return False
        return True

    # --------------------------------------------------------
    # Collection 辅助
    # --------------------------------------------------------

    def _get_collection(self, collection_name: str):
        """获取指定名称的 ChromaDB collection

        如果 collection_name 与 kb_store 的默认 collection 名称一致，直接复用；
        否则通过 client.get_or_create_collection 获取。
        """
        current_collection = getattr(self.kb_store, "collection", None)
        if current_collection is not None:
            try:
                if current_collection.name == collection_name:
                    return current_collection
            except Exception:
                pass
        # 获取或创建指定 collection
        client = getattr(self.kb_store, "client", None)
        embedding = getattr(self.kb_store, "embedding", None)
        if client is None:
            return current_collection
        try:
            kwargs: Dict[str, Any] = {
                "name": collection_name,
                "metadata": {"hnsw:space": "cosine"},
            }
            if embedding is not None:
                kwargs["embedding_function"] = embedding
            return client.get_or_create_collection(**kwargs)
        except Exception as e:
            logger.warning(f"获取 collection {collection_name} 失败: {e}")
            return current_collection

    # --------------------------------------------------------
    # 配置读取辅助
    # --------------------------------------------------------

    def _get_rrf_k(self) -> int:
        """获取 RRF 常数 k（默认 60）"""
        return 60

    def _get_bm25_params(self) -> Tuple[float, float]:
        """获取 BM25 参数 (k1, b)（默认 1.5, 0.75）"""
        return 1.5, 0.75
