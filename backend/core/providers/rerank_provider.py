"""
Rerank Provider 抽象 (P2-2, 对标 Dify Rerank)

针对 ChromaDB 检索结果质量不理想的问题,引入二次 rerank 步骤。
对已召回的 documents 按与 query 的相关性重排序,返回 top_k 个,
每个 doc 加 `rerank_score` 字段供下游排序参考。

实现:
- RerankProvider: 抽象基类 (类似 BaseProvider)
- CohereRerankProvider: 调用 Cohere Rerank API
- JinaRerankProvider: 调用 Jina Rerank API
- BGERerankProvider: 本地 BGE reranker (sentence-transformers, 依赖缺失时 raise NotImplementedError)
- DummyRerankProvider: 保持原顺序 (开发/测试用, 完全等价于未启用 rerank)

向后兼容: settings.rerank_provider 未配置或为 "dummy" 时, retrieve_context 行为
完全等价于未启用 rerank, 不破坏现有 retrieve_context 主流程。
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import httpx


class RerankProvider(ABC):
    """Rerank Provider 抽象基类

    所有 rerank 实现需实现 rerank / health_check / name 三个接口。
    与 BaseProvider 区别: rerank 接收 (query, documents, top_k),
    返回带 rerank_score 字段的 top_k documents, 不涉及 chat completion。
    """

    @abstractmethod
    async def rerank(
        self,
        query: str,
        documents: List[Dict[str, Any]],
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """对 documents 按 query 相关性重排序, 返回 top_k 个

        每个 doc 加 `rerank_score` 字段 (float, 越大越相关)。
        输入 documents 顺序保持稳定 (相同 rerank_score 时按原顺序返回)。

        Args:
            query: 用户查询
            documents: 待重排序的文档列表, 每个元素是 dict (含 content 等字段)
            top_k: 返回前 top_k 个, 默认 5

        Returns:
            重排序后的 top_k documents, 每个加 rerank_score 字段
        """
        raise NotImplementedError

    @abstractmethod
    async def health_check(self) -> bool:
        """探活: 检查 rerank 服务是否可用"""
        raise NotImplementedError

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider 名 (cohere / jina / bge / dummy)"""
        raise NotImplementedError


def _extract_text(doc: Any) -> str:
    """从 doc 中提取待 rerank 的纯文本

    支持 dict (含 content / text / page_content 字段) 与 str,
    其他类型转 str。便于兼容 ChromaDB 召回结果与 LangChain Document。
    """
    if isinstance(doc, str):
        return doc
    if isinstance(doc, dict):
        for key in ("content", "text", "page_content", "document"):
            val = doc.get(key)
            if isinstance(val, str) and val:
                return val
        # 兜底: 拼接所有 str 值
        return " ".join(str(v) for v in doc.values() if isinstance(v, str))
    return str(doc)


def _truncate_top_k(docs: List[Dict[str, Any]], top_k: int) -> List[Dict[str, Any]]:
    """截断到 top_k (top_k <= 0 时返回全部, 防御性兜底)"""
    if top_k is None or top_k <= 0:
        return docs
    return docs[:top_k]


# ============================================================
# DummyRerankProvider: 保持原顺序 (开发/测试用)
# ============================================================


class DummyRerankProvider(RerankProvider):
    """Dummy rerank: 保持原顺序, 给每个 doc 一个递减 rerank_score

    完全等价于未启用 rerank (向后兼容)。开发/测试场景默认使用。
    """

    @property
    def name(self) -> str:
        return "dummy"

    async def rerank(
        self,
        query: str,
        documents: List[Dict[str, Any]],
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """保持原顺序, 给每个 doc 加 rerank_score (递减)"""
        # 浅拷贝避免修改入参; 加 rerank_score 字段
        result: List[Dict[str, Any]] = []
        for i, doc in enumerate(documents):
            if isinstance(doc, dict):
                new_doc = dict(doc)
            else:
                # 非 dict (如 str), 包装成 dict 便于下游统一处理
                new_doc = {"content": _extract_text(doc)}
            new_doc["rerank_score"] = float(len(documents) - i)
            result.append(new_doc)
        return _truncate_top_k(result, top_k)

    async def health_check(self) -> bool:
        """Dummy 始终可用"""
        return True


# ============================================================
# _HTTPRerankProvider: HTTP API rerank 公共基类 (Cohere / Jina 共用)
# ============================================================


class _HTTPRerankProvider(RerankProvider):
    """HTTP API rerank 公共基类

    Cohere / Jina API 协议高度相似 (model / query / documents / top_n),
    抽公共逻辑到此基类, 子类只覆盖 _default_endpoint / _default_model / name。
    """

    _default_endpoint: str = ""
    _default_model: str = ""

    def __init__(
        self,
        api_key: str,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 30.0,
    ):
        if not api_key:
            raise ValueError(f"{self.name} rerank api_key 未配置")
        self._api_key = api_key
        # base_url 末尾斜杠 strip, 拼接 /v1/rerank 路径
        base = (base_url or self._default_endpoint).rstrip("/")
        # 若 base 已含 /v1/rerank 全路径, 直接用; 否则补 /v1/rerank
        if base.endswith("/rerank"):
            self._endpoint = base
        else:
            self._endpoint = f"{base}/v1/rerank"
        self._model = model or self._default_model
        self._timeout = timeout

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _parse_response(
        self,
        data: Dict[str, Any],
        documents: List[Dict[str, Any]],
        original_texts: List[str],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        """解析 Cohere/Jina 通用响应格式

        响应通常为 {"results": [{"index": int, "relevance_score": float}, ...]}
        按 relevance_score 降序排序后, 取出对应 original doc 并加 rerank_score。
        """
        results = data.get("results") or data.get("data") or []
        # 按 relevance_score 降序 (Cohere 字段: relevance_score; Jina 字段: relevance_score / score)
        scored: List[tuple[int, float]] = []
        for r in results:
            idx = r.get("index")
            score = r.get("relevance_score")
            if score is None:
                score = r.get("score", 0.0)
            if idx is None:
                continue
            try:
                idx_int = int(idx)
                score_float = float(score)
            except (TypeError, ValueError):
                continue
            if 0 <= idx_int < len(documents):
                scored.append((idx_int, score_float))
        scored.sort(key=lambda x: x[1], reverse=True)
        # 截断到 top_k 后再组装, 避免无谓拷贝
        scored = scored[:top_k] if top_k and top_k > 0 else scored
        out: List[Dict[str, Any]] = []
        for idx_int, score_float in scored:
            doc = documents[idx_int]
            if isinstance(doc, dict):
                new_doc = dict(doc)
            else:
                new_doc = {"content": original_texts[idx_int]}
            new_doc["rerank_score"] = score_float
            out.append(new_doc)
        return out

    async def rerank(
        self,
        query: str,
        documents: List[Dict[str, Any]],
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """调 HTTP rerank API, 解析响应返回 top_k docs"""
        if not documents:
            return []
        original_texts = [_extract_text(d) for d in documents]
        payload = {
            "model": self._model,
            "query": query,
            "documents": original_texts,
            "top_n": top_k if top_k and top_k > 0 else len(documents),
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    self._endpoint, headers=self._headers(), json=payload
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"{self.name} rerank API 错误: "
                f"{e.response.status_code} {e.response.text}"
            ) from e
        except httpx.HTTPError as e:
            raise RuntimeError(f"{self.name} rerank 请求失败: {e}") from e
        return self._parse_response(data, documents, original_texts, top_k)

    async def health_check(self) -> bool:
        """探活: 发一个最小 rerank 请求, 200 即视为可达

        失败 (含 4xx 凭证错误) 视为不可达。
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    self._endpoint,
                    headers=self._headers(),
                    json={
                        "model": self._model,
                        "query": "ping",
                        "documents": ["ok"],
                        "top_n": 1,
                    },
                )
                return resp.status_code == 200
        except Exception:
            return False


# ============================================================
# CohereRerankProvider
# ============================================================


class CohereRerankProvider(_HTTPRerankProvider):
    """Cohere Rerank Provider

    API: POST https://api.cohere.ai/v1/rerank
    Body: {model, query, documents: [str], top_n}
    Header: Authorization: Bearer {api_key}
    """

    _default_endpoint = "https://api.cohere.ai"
    _default_model = "rerank-multilingual-v3.0"

    @property
    def name(self) -> str:
        return "cohere"


# ============================================================
# JinaRerankProvider
# ============================================================


class JinaRerankProvider(_HTTPRerankProvider):
    """Jina Rerank Provider

    API: POST https://api.jina.ai/v1/rerank
    Body: {model, query, documents: [str], top_n}
    Header: Authorization: Bearer {api_key}
    """

    _default_endpoint = "https://api.jina.ai"
    _default_model = "jina-reranker-v2-base-multilingual"

    @property
    def name(self) -> str:
        return "jina"


# ============================================================
# BGERerankProvider: 本地 BGE reranker (sentence-transformers)
# ============================================================


class BGERerankProvider(RerankProvider):
    """本地 BGE reranker

    使用 sentence-transformers 计算 query 与 docs 的余弦相似度重排。
    若依赖缺失则 raise NotImplementedError, 不强制安装 transformers。

    默认模型: BAAI/bge-reranker-base (可经 model 参数覆盖)
    """

    _default_model = "BAAI/bge-reranker-base"

    def __init__(
        self,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        **_extra: Any,
    ):
        # 延迟 import: 仅在实例化时检查依赖, 避免模块加载即报错
        try:
            # transformers 是 sentence-transformers 的依赖, 后者更直接
            from sentence_transformers import CrossEncoder  # noqa: F401
        except ImportError as e:
            raise NotImplementedError(
                "BGERerankProvider 需要安装 sentence-transformers "
                "(pip install sentence-transformers), 当前环境未安装。"
            ) from e
        self._model_name = model or self._default_model
        # 加载到内存 (首次较慢), 后续 rerank 直接复用
        from sentence_transformers import CrossEncoder

        self._encoder = CrossEncoder(self._model_name)

    @property
    def name(self) -> str:
        return "bge"

    async def rerank(
        self,
        query: str,
        documents: List[Dict[str, Any]],
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """用 CrossEncoder 计算 query-doc 对的相关性分数, 降序返回 top_k"""
        if not documents:
            return []
        import asyncio

        texts = [_extract_text(d) for d in documents]
        pairs = [(query, t) for t in texts]
        # CrossEncoder.predict 是同步 CPU 操作, 放到线程池避免阻塞事件循环
        scores = await asyncio.to_thread(self._encoder.predict, pairs)
        scored: List[tuple[int, float]] = [(i, float(s)) for i, s in enumerate(scores)]
        scored.sort(key=lambda x: x[1], reverse=True)
        scored = scored[:top_k] if top_k and top_k > 0 else scored
        out: List[Dict[str, Any]] = []
        for idx_int, score_float in scored:
            doc = documents[idx_int]
            if isinstance(doc, dict):
                new_doc = dict(doc)
            else:
                new_doc = {"content": texts[idx_int]}
            new_doc["rerank_score"] = score_float
            out.append(new_doc)
        return out

    async def health_check(self) -> bool:
        """探活: encoder 已加载即可用"""
        return self._encoder is not None
