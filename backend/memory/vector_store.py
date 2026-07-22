"""
基于 ChromaDB + Embedding 的长期记忆与公司知识库真实实现。
所有 ChromaDB 同步操作通过 asyncio.to_thread 包装，避免阻塞事件循环。
"""

import asyncio
import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

import chromadb

from agent.tools import CompanyKB, MemoryStore
from core.config import Settings, get_settings
from core.embeddings import EmbeddingClient

logger = logging.getLogger(__name__)


class DummyEmbeddingFunction:
    """本地测试/演示用 dummy embedding，避免首次启动时下载 ONNX 模型。"""

    def __init__(self, dimensions: int = 384):
        self.dimensions = dimensions

    def name(self) -> str:
        return "dummy"

    def is_legacy(self) -> bool:
        return False

    def supported_spaces(self) -> List[str]:
        return ["cosine", "l2", "ip"]

    def __call__(self, input: List[str]) -> List[List[float]]:
        """基于文本 hash 生成确定性伪向量"""
        results = []
        for text in input:
            seed = int(
                hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest(), 16
            )
            vec = []
            for i in range(self.dimensions):
                # 简单的伪随机 + 归一化前准备
                seed = (seed * 9301 + 49297) % 233280
                vec.append((seed / 233280.0) * 2 - 1)
            results.append(vec)
        return results

    async def embed_query(self, text: str) -> List[float]:
        """单条查询文本的 embedding，与 __call__ 保持一致

        ChromaDB 1.x 在 query 路径会内部调用 embedding_function.embed_query，
        缺少该方法会导致 DummyEmbeddingFunction 无法用于检索（AttributeError）。
        """
        return self([text])[0]


def _init_embedding(settings: Settings):
    """优先使用配置的真实 embedding API；未配置则使用 dummy embedding，避免下载模型。"""
    has_key = bool(
        settings.embedding_api_key or settings.cloud_api_key or settings.openai_api_key
    )
    if has_key and settings.embedding_base_url:
        try:
            return EmbeddingClient(settings)
        except Exception as e:
            logger.warning(f"EmbeddingClient 初始化失败，降级到 dummy embedding: {e}")
    logger.info("未配置 embedding key，使用 dummy embedding（仅适合测试/演示）")
    return DummyEmbeddingFunction(dimensions=settings.embedding_dimensions or 384)


class ChromaMemoryStore(MemoryStore):
    """基于 ChromaDB + 真实 embedding 的员工长期记忆存储。

    collection 名按租户隔离：agentvalue_memory_{tenant_id}，默认租户为 agentvalue_memory_default。
    显式传入 collection_name 时仍以传入值为准，保持向后兼容。
    """

    def __init__(
        self,
        collection_name: Optional[str] = None,
        persist_dir: Optional[str] = None,
        settings: Optional[Settings] = None,
        tenant_id: str = "default",
    ):
        self.settings = settings or get_settings()
        self.tenant_id = tenant_id or "default"
        self.persist_dir = persist_dir or self.settings.vector_store_dir
        self.embedding = _init_embedding(self.settings)
        if collection_name is None:
            collection_name = f"agentvalue_memory_{self.tenant_id}"

        self.client = chromadb.PersistentClient(path=self.persist_dir)
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
            embedding_function=self.embedding,
        )

    async def get_employee_history(
        self,
        employee_id: str,
        period: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """向量检索员工历史记忆（排除当前周期）"""
        query = f"员工 {employee_id} 历史评估记忆"
        query_kwargs: Dict[str, Any] = {
            "n_results": limit,
            "include": ["metadatas", "documents", "distances"],
        }

        where: Dict[str, Any] = {"employee_id": employee_id}
        if period:
            # ChromaDB 1.x 要求 where 顶层仅含一个操作符，多条件需用 $and 组合
            where = {
                "$and": [
                    {"employee_id": employee_id},
                    {"period": {"$ne": period}},
                ]
            }
        query_kwargs["where"] = where

        if self.embedding and hasattr(self.embedding, "embed_query"):
            query_kwargs["query_embeddings"] = [await self.embedding.embed_query(query)]
        else:
            query_kwargs["query_texts"] = [query]

        try:
            results = await asyncio.to_thread(self.collection.query, **query_kwargs)
        except Exception as e:
            logger.warning(f"Chroma 记忆查询失败: {e}")
            return []

        memories = []
        metadatas = results.get("metadatas", [[]])[0]
        documents = results.get("documents", [[]])[0]
        distances = results.get("distances", [[]])[0]

        for meta, doc, distance in zip(metadatas, documents, distances):
            if not meta:
                continue
            payload = meta.get("payload")
            if payload:
                try:
                    memory = json.loads(payload)
                except json.JSONDecodeError:
                    memory = {"summary": doc, **meta}
            else:
                memory = {"summary": doc, **meta}
            memory["_retrieval_score"] = 1.0 - float(distance or 0.0)
            memories.append(memory)
        return memories

    async def close(self) -> None:
        """释放 ChromaDB 客户端与 embedding 客户端资源"""
        try:
            self.client.close()
        except Exception:
            logger.warning("ChromaMemoryStore client 关闭失败", exc_info=True)
        if self.embedding and hasattr(self.embedding, "client"):
            try:
                await self.embedding.client.close()
            except Exception:
                logger.warning(
                    "ChromaMemoryStore embedding client 关闭失败", exc_info=True
                )

    async def add_memory(self, employee_id: str, memory: Dict[str, Any]) -> None:
        """添加/更新一条员工记忆，并写入真实向量"""
        period = memory.get("period", "unknown")
        doc_id = f"{employee_id}-{period}"
        document = json.dumps(memory, ensure_ascii=False)

        upsert_kwargs: Dict[str, Any] = {
            "ids": [doc_id],
            "documents": [document],
            "metadatas": [
                {
                    "employee_id": employee_id,
                    "period": period,
                    "payload": document,
                }
            ],
        }

        if self.embedding and hasattr(self.embedding, "embed_query"):
            try:
                upsert_kwargs["embeddings"] = [
                    await self.embedding.embed_query(document)
                ]
            except Exception as e:
                logger.error(f"记忆 embedding 失败，跳过写入: {e}")
                raise

        await asyncio.to_thread(self.collection.upsert, **upsert_kwargs)


class ChromaCompanyKB(CompanyKB):
    """基于 ChromaDB + 真实 embedding 的公司知识库 RAG。

    collection 名按租户隔离：agentvalue_kb_{tenant_id}，默认租户为 agentvalue_kb_default。
    显式传入 collection_name 时仍以传入值为准，保持向后兼容。
    """

    def __init__(
        self,
        collection_name: Optional[str] = None,
        persist_dir: Optional[str] = None,
        settings: Optional[Settings] = None,
        tenant_id: str = "default",
    ):
        self.settings = settings or get_settings()
        self.tenant_id = tenant_id or "default"
        self.persist_dir = persist_dir or self.settings.vector_store_dir
        self.embedding = _init_embedding(self.settings)
        if collection_name is None:
            collection_name = f"agentvalue_kb_{self.tenant_id}"

        self.client = chromadb.PersistentClient(path=self.persist_dir)
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
            embedding_function=self.embedding,
        )

    async def query(
        self,
        query: str,
        top_k: int = 5,
        where: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """向量检索公司知识库

        Args:
            query: 查询文本
            top_k: 返回结果数
            where: ChromaDB metadata 过滤条件（如 {"source": "hr_manual"}），
                   多条件需用 {"$and": [...]} 组合，与 ChromaDB 1.x where 语法一致。
        """
        query_kwargs: Dict[str, Any] = {
            "n_results": top_k,
            "include": ["metadatas", "documents", "distances"],
        }
        # 元数据过滤：通过 ChromaDB where 参数实现向量检索结果过滤
        if where:
            query_kwargs["where"] = where
        if self.embedding and hasattr(self.embedding, "embed_query"):
            query_kwargs["query_embeddings"] = [await self.embedding.embed_query(query)]
        else:
            query_kwargs["query_texts"] = [query]
        try:
            results = await asyncio.to_thread(self.collection.query, **query_kwargs)
        except Exception as e:
            logger.warning(f"Chroma KB 查询失败: {e}")
            return []

        docs = []
        metadatas = results.get("metadatas", [[]])[0]
        documents = results.get("documents", [[]])[0]
        distances = results.get("distances", [[]])[0]

        for meta, doc, distance in zip(metadatas, documents, distances):
            if not meta:
                continue
            docs.append(
                {
                    "kb_id": meta.get("kb_id", ""),
                    "title": meta.get("title", ""),
                    "content": doc or meta.get("content", ""),
                    "metadata": meta.get("metadata", {}),
                    "_retrieval_score": 1.0 - float(distance or 0.0),
                }
            )
        return docs

    async def close(self) -> None:
        """释放 ChromaDB 客户端与 embedding 客户端资源"""
        try:
            self.client.close()
        except Exception:
            logger.warning("ChromaCompanyKB client 关闭失败", exc_info=True)
        if self.embedding and hasattr(self.embedding, "client"):
            try:
                await self.embedding.client.close()
            except Exception:
                logger.warning(
                    "ChromaCompanyKB embedding client 关闭失败", exc_info=True
                )

    async def add_document(
        self,
        kb_id: str,
        title: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """向知识库添加文档并生成 embedding"""
        document = f"{title}\n{content}"
        upsert_kwargs: Dict[str, Any] = {
            "ids": [kb_id],
            "documents": [document],
            "metadatas": [
                {
                    "kb_id": kb_id,
                    "title": title,
                    "content": content,
                    "metadata": json.dumps(metadata or {}, ensure_ascii=False),
                }
            ],
        }
        if self.embedding and hasattr(self.embedding, "embed_query"):
            try:
                upsert_kwargs["embeddings"] = [
                    await self.embedding.embed_query(document)
                ]
            except Exception as e:
                logger.error(f"KB embedding 失败，跳过写入: {e}")
                raise
        await asyncio.to_thread(self.collection.upsert, **upsert_kwargs)
