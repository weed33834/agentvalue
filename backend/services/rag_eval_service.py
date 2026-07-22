"""RAG 质量评测服务

对标 RagFlow 检索测试 + 压力测试:
- 创建评测任务 (关联 collection, 配置测试查询集)
- 异步执行评测: 对每个 query 执行检索, 计算 precision/recall/MRR/NDCG
- 分页查询结果
- 汇总统计 (平均 precision/recall/MRR/NDCG + 延迟统计)

检索指标计算:
- Precision@K: 前 K 个结果中相关文档的比例
- Recall@K: 前 K 个结果中检索到的相关文档占所有相关文档的比例
- MRR (Mean Reciprocal Rank): 第一个相关文档位置的倒数
- NDCG@K (Normalized Discounted Cumulative Gain): 归一化折损累积增益

后台任务使用 asyncio.create_task() 异步执行, 不阻塞 API 响应。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import AsyncSessionLocal
from core.tenant_context import tenant_scope
from models.rag_eval_models import RagEvalResult, RagEvalTask

logger = logging.getLogger(__name__)

# 默认 top_k (检索返回的文档数)
DEFAULT_TOP_K = 5

# 默认 K 值 (用于 Precision@K / NDCG@K 计算)
DEFAULT_K = 5


class RagEvalService:
    """RAG 质量评测服务"""

    def __init__(self, session: AsyncSession):
        self.session = session

    # ===================== 任务 CRUD =====================

    async def create_task(
        self,
        name: str,
        collection_name: str,
        test_queries: List[Dict[str, Any]],
        *,
        tenant_id: str = "default",
    ) -> RagEvalTask:
        """创建 RAG 评测任务

        Args:
            name: 任务名称。
            collection_name: 被评测的 ChromaDB collection 名称。
            test_queries: 测试查询列表, 每项含 query + relevant_doc_ids。
            tenant_id: 租户 ID。

        Returns:
            创建的 RagEvalTask 对象。
        """
        if not name or not name.strip():
            raise ValueError("任务名称不能为空")
        if not collection_name or not collection_name.strip():
            raise ValueError("collection 名称不能为空")
        if not test_queries:
            raise ValueError("测试查询列表不能为空")

        # 规范化查询数据
        normalized_queries = []
        for q in test_queries:
            query_text = q.get("query", "")
            relevant_ids = q.get("relevant_doc_ids", [])
            if not query_text:
                continue
            normalized_queries.append(
                {"query": str(query_text), "relevant_doc_ids": relevant_ids}
            )

        if not normalized_queries:
            raise ValueError("无有效的测试查询 (缺少 query 字段)")

        task = RagEvalTask(
            tenant_id=tenant_id,
            name=name.strip(),
            collection_name=collection_name.strip(),
            test_queries=normalized_queries,
            status="pending",
            total_queries=len(normalized_queries),
            completed_queries=0,
        )
        self.session.add(task)
        await self.session.flush()
        logger.info(
            "创建 RAG 评测任务: %s (collection: %s, 查询数: %d, 租户: %s)",
            name,
            collection_name,
            len(normalized_queries),
            tenant_id,
        )
        return task

    async def get_task(
        self, task_id: int, *, tenant_id: str = "default"
    ) -> Optional[RagEvalTask]:
        """获取评测任务详情"""
        return (
            await self.session.execute(
                select(RagEvalTask).where(
                    RagEvalTask.id == task_id,
                    RagEvalTask.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()

    async def list_tasks(
        self,
        *,
        tenant_id: str = "default",
        status: Optional[str] = None,
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """分页查询评测任务列表"""
        base = (
            select(RagEvalTask)
            .where(RagEvalTask.tenant_id == tenant_id)
            .order_by(RagEvalTask.created_at.desc())
        )
        if status:
            base = base.where(RagEvalTask.status == status)

        total = (
            await self.session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar() or 0

        offset = (page - 1) * size
        rows = (
            await self.session.execute(base.offset(offset).limit(size))
        ).scalars().all()

        return {
            "items": [self._task_to_dict(t) for t in rows],
            "total": total,
            "page": page,
            "size": size,
        }

    async def delete_task(
        self, task_id: int, *, tenant_id: str = "default"
    ) -> bool:
        """删除评测任务 (同时删除所有结果)"""
        task = await self.get_task(task_id, tenant_id=tenant_id)
        if task is None:
            return False

        results = (
            await self.session.execute(
                select(RagEvalResult).where(
                    RagEvalResult.task_id == task_id,
                    RagEvalResult.tenant_id == tenant_id,
                )
            )
        ).scalars().all()
        for r in results:
            await self.session.delete(r)

        await self.session.delete(task)
        await self.session.flush()
        logger.info("删除 RAG 评测任务 id=%s (含 %d 结果)", task_id, len(results))
        return True

    # ===================== 任务执行 =====================

    def run_task_background(
        self,
        task_id: int,
        search_service: Any,
        *,
        tenant_id: str = "default",
        top_k: int = DEFAULT_TOP_K,
    ) -> asyncio.Task:
        """启动后台评测任务 (不阻塞 API 响应)

        Args:
            task_id: 评测任务 ID。
            search_service: HybridSearchService 实例 (用于检索)。
            tenant_id: 租户 ID。
            top_k: 检索返回的文档数。

        Returns:
            asyncio.Task 对象。
        """
        return asyncio.create_task(
            self._run_task_async(
                task_id, search_service, tenant_id=tenant_id, top_k=top_k
            )
        )

    async def _run_task_async(
        self,
        task_id: int,
        search_service: Any,
        *,
        tenant_id: str = "default",
        top_k: int = DEFAULT_TOP_K,
    ) -> None:
        """后台异步执行 RAG 评测任务"""
        with tenant_scope(tenant_id):
            async with AsyncSessionLocal() as session:
                try:
                    task = (
                        await session.execute(
                            select(RagEvalTask).where(
                                RagEvalTask.id == task_id,
                                RagEvalTask.tenant_id == tenant_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if task is None:
                        logger.error("RAG 评测任务 %s 不存在", task_id)
                        return

                    task.status = "running"
                    await session.commit()

                    completed = 0
                    for query_data in task.test_queries or []:
                        try:
                            result = await self._evaluate_single_query(
                                session=session,
                                task=task,
                                query_data=query_data,
                                search_service=search_service,
                                top_k=top_k,
                                tenant_id=tenant_id,
                            )
                            if result:
                                session.add(result)
                                await session.commit()
                        except Exception as e:
                            logger.warning(
                                "评测查询失败 '%s': %s",
                                query_data.get("query", ""),
                                e,
                                exc_info=True,
                            )

                        completed += 1
                        task.completed_queries = completed
                        await session.commit()

                    # 计算汇总
                    summary = await self._compute_summary(
                        session, task_id, tenant_id=tenant_id
                    )
                    task.results_summary = summary
                    task.status = "completed"
                    task.completed_at = datetime.now(timezone.utc)
                    await session.commit()

                    logger.info(
                        "RAG 评测任务 %s 完成: %d/%d 查询, 平均 precision=%.2f",
                        task_id,
                        completed,
                        task.total_queries,
                        summary.get("avg_precision", 0),
                    )

                except Exception as e:
                    logger.error(
                        "RAG 评测任务 %s 执行失败: %s", task_id, e, exc_info=True
                    )
                    try:
                        async with AsyncSessionLocal() as err_session:
                            err_task = (
                                await err_session.execute(
                                    select(RagEvalTask).where(
                                        RagEvalTask.id == task_id,
                                        RagEvalTask.tenant_id == tenant_id,
                                    )
                                )
                            ).scalar_one_or_none()
                            if err_task is not None:
                                err_task.status = "failed"
                                err_task.completed_at = datetime.now(timezone.utc)
                                await err_session.commit()
                    except Exception:
                        logger.error("标记任务失败状态时出错", exc_info=True)

    async def _evaluate_single_query(
        self,
        *,
        session: AsyncSession,
        task: RagEvalTask,
        query_data: Dict[str, Any],
        search_service: Any,
        top_k: int = DEFAULT_TOP_K,
        tenant_id: str = "default",
    ) -> Optional[RagEvalResult]:
        """对单条查询执行检索并评分

        Args:
            session: 数据库会话。
            task: 评测任务。
            query_data: 查询数据 {query, relevant_doc_ids}。
            search_service: HybridSearchService 实例。
            top_k: 检索返回的文档数。
            tenant_id: 租户 ID。

        Returns:
            RagEvalResult 对象。
        """
        query_text = query_data.get("query", "")
        relevant_doc_ids = query_data.get("relevant_doc_ids", [])

        start_time = time.monotonic()

        # 执行混合检索
        try:
            retrieved = await search_service.search(
                query=query_text,
                collection_name=task.collection_name,
                top_k=top_k,
            )
        except Exception as e:
            logger.warning("检索失败 (query: %s): %s", query_text, e)
            retrieved = []

        latency_ms = int((time.monotonic() - start_time) * 1000)

        # 提取检索到的文档 ID/内容
        retrieved_docs = []
        retrieved_ids = []
        for doc in retrieved:
            if isinstance(doc, dict):
                content = doc.get("content", "")
                score = doc.get("score", 0.0)
                metadata = doc.get("metadata", {})
                doc_id = metadata.get("kb_id") or metadata.get("id") or content[:50]
                retrieved_docs.append(
                    {
                        "content": content,
                        "score": float(score) if score else 0.0,
                        "metadata": metadata,
                        "doc_id": doc_id,
                    }
                )
                retrieved_ids.append(doc_id)

        # 计算相关性 (检索到的文档是否在 relevant_doc_ids 中)
        relevance_scores = [
            1 if str(rid) in [str(r) for r in relevant_doc_ids] else 0
            for rid in retrieved_ids
        ]

        # 计算各指标
        k = min(DEFAULT_K, len(retrieved_ids)) if retrieved_ids else DEFAULT_K
        precision = self._calculate_precision_at_k(retrieved_ids, relevant_doc_ids, k)
        recall = self._calculate_recall_at_k(retrieved_ids, relevant_doc_ids)
        mrr = self._calculate_mrr(retrieved_ids, relevant_doc_ids)
        ndcg = self._calculate_ndcg(retrieved_ids, relevant_doc_ids, k)

        # 答案溯源信息
        answer_traceback = {
            "query": query_text,
            "relevant_doc_ids": relevant_doc_ids,
            "retrieved_doc_ids": retrieved_ids,
            "relevance_scores": relevance_scores,
            "top_doc": retrieved_docs[0] if retrieved_docs else None,
        }

        return RagEvalResult(
            tenant_id=tenant_id,
            task_id=task.id,
            query=query_text,
            retrieved_docs=retrieved_docs,
            relevance_scores=relevance_scores,
            precision_score=round(precision, 4),
            recall_score=round(recall, 4),
            mrr_score=round(mrr, 4),
            ndcg_score=round(ndcg, 4),
            answer_traceback=answer_traceback,
            latency_ms=latency_ms,
        )

    # ===================== 指标计算 =====================

    @staticmethod
    def _calculate_precision_at_k(
        retrieved: List[str], relevant: List[str], k: int = DEFAULT_K
    ) -> float:
        """计算 Precision@K

        前 K 个结果中相关文档的比例。

        Args:
            retrieved: 检索到的文档 ID 列表 (按相关性排序)。
            relevant: 实际相关的文档 ID 列表。
            k: 截断位置。

        Returns:
            Precision@K (0.0 - 1.0)
        """
        if k <= 0:
            return 0.0
        relevant_set = {str(r) for r in relevant}
        top_k = retrieved[:k]
        if not top_k:
            return 0.0
        relevant_in_top_k = sum(1 for doc_id in top_k if str(doc_id) in relevant_set)
        return relevant_in_top_k / len(top_k)

    @staticmethod
    def _calculate_recall_at_k(
        retrieved: List[str], relevant: List[str], k: int = DEFAULT_K
    ) -> float:
        """计算 Recall@K

        前 K 个结果中检索到的相关文档占所有相关文档的比例。

        Args:
            retrieved: 检索到的文档 ID 列表 (按相关性排序)。
            relevant: 实际相关的文档 ID 列表。
            k: 截断位置。

        Returns:
            Recall@K (0.0 - 1.0)
        """
        if not relevant:
            return 0.0
        relevant_set = {str(r) for r in relevant}
        top_k = retrieved[:k]
        relevant_in_top_k = sum(1 for doc_id in top_k if str(doc_id) in relevant_set)
        return relevant_in_top_k / len(relevant_set)

    @staticmethod
    def _calculate_mrr(
        retrieved: List[str], relevant: List[str]
    ) -> float:
        """计算 MRR (Mean Reciprocal Rank)

        第一个相关文档在检索结果中位置的倒数。

        Args:
            retrieved: 检索到的文档 ID 列表 (按相关性排序)。
            relevant: 实际相关的文档 ID 列表。

        Returns:
            MRR (0.0 - 1.0)
        """
        relevant_set = {str(r) for r in relevant}
        for i, doc_id in enumerate(retrieved, start=1):
            if str(doc_id) in relevant_set:
                return 1.0 / i
        return 0.0

    @staticmethod
    def _calculate_ndcg(
        retrieved: List[str], relevant: List[str], k: int = DEFAULT_K
    ) -> float:
        """计算 NDCG@K (Normalized Discounted Cumulative Gain)

        归一化折损累积增益, 衡量排序质量。

        公式:
        - DCG@K = Σ_{i=1}^{K} rel_i / log2(i + 1)
        - IDCG@K = DCG@K 的理想排序 (相关文档排在最前)
        - NDCG@K = DCG@K / IDCG@K

        Args:
            retrieved: 检索到的文档 ID 列表 (按相关性排序)。
            relevant: 实际相关的文档 ID 列表。
            k: 截断位置。

        Returns:
            NDCG@K (0.0 - 1.0)
        """
        if k <= 0:
            return 0.0
        relevant_set = {str(r) for r in relevant}
        top_k = retrieved[:k]

        # 计算相关性等级: 相关=1, 不相关=0
        rels = [1 if str(doc_id) in relevant_set else 0 for doc_id in top_k]

        # DCG@K
        dcg = sum(rel / math.log2(i + 2) for i, rel in enumerate(rels))

        # IDCG@K (理想排序: 所有相关文档排在最前)
        ideal_rels = sorted(rels, reverse=True)
        # 补齐到 k 个 (理想情况下前 min(|relevant|, k) 个为 1)
        num_relevant = min(len(relevant_set), k)
        ideal_rels = [1] * num_relevant + [0] * (k - num_relevant)
        idcg = sum(rel / math.log2(i + 2) for i, rel in enumerate(ideal_rels[:k]))

        if idcg == 0:
            return 0.0
        return dcg / idcg

    # ===================== 结果查询 =====================

    async def get_task_results(
        self,
        task_id: int,
        *,
        tenant_id: str = "default",
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """分页查询评测结果"""
        base = (
            select(RagEvalResult)
            .where(
                RagEvalResult.task_id == task_id,
                RagEvalResult.tenant_id == tenant_id,
            )
            .order_by(RagEvalResult.created_at.desc())
        )

        total = (
            await self.session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar() or 0

        offset = (page - 1) * size
        rows = (
            await self.session.execute(base.offset(offset).limit(size))
        ).scalars().all()

        return {
            "items": [self._result_to_dict(r) for r in rows],
            "total": total,
            "page": page,
            "size": size,
        }

    async def get_task_summary(
        self, task_id: int, *, tenant_id: str = "default"
    ) -> Dict[str, Any]:
        """获取评测任务汇总统计"""
        task = await self.get_task(task_id, tenant_id=tenant_id)
        if task is None:
            return {"error": "任务不存在"}

        if task.results_summary:
            return task.results_summary

        return await self._compute_summary(
            self.session, task_id, tenant_id=tenant_id
        )

    async def _compute_summary(
        self,
        session: AsyncSession,
        task_id: int,
        *,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """计算评测汇总统计"""
        results = (
            await session.execute(
                select(RagEvalResult).where(
                    RagEvalResult.task_id == task_id,
                    RagEvalResult.tenant_id == tenant_id,
                )
            )
        ).scalars().all()

        if not results:
            return {
                "total": 0,
                "avg_precision": 0.0,
                "avg_recall": 0.0,
                "avg_mrr": 0.0,
                "avg_ndcg": 0.0,
                "avg_latency_ms": 0.0,
                "min_latency_ms": 0,
                "max_latency_ms": 0,
            }

        total = len(results)
        avg_precision = sum(r.precision_score for r in results) / total
        avg_recall = sum(r.recall_score for r in results) / total
        avg_mrr = sum(r.mrr_score for r in results) / total
        avg_ndcg = sum(r.ndcg_score for r in results) / total
        latencies = [r.latency_ms for r in results if r.latency_ms]
        avg_latency = sum(latencies) / len(latencies) if latencies else 0.0

        return {
            "total": total,
            "avg_precision": round(avg_precision, 4),
            "avg_recall": round(avg_recall, 4),
            "avg_mrr": round(avg_mrr, 4),
            "avg_ndcg": round(avg_ndcg, 4),
            "avg_latency_ms": round(avg_latency, 2),
            "min_latency_ms": min(latencies) if latencies else 0,
            "max_latency_ms": max(latencies) if latencies else 0,
        }

    # ===================== 序列化辅助 =====================

    @staticmethod
    def _task_to_dict(t: RagEvalTask) -> Dict[str, Any]:
        """RagEvalTask -> dict"""
        return {
            "id": t.id,
            "tenant_id": t.tenant_id,
            "name": t.name,
            "collection_name": t.collection_name,
            "test_queries": t.test_queries,
            "status": t.status,
            "total_queries": t.total_queries,
            "completed_queries": t.completed_queries,
            "results_summary": t.results_summary,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        }

    @staticmethod
    def _result_to_dict(r: RagEvalResult) -> Dict[str, Any]:
        """RagEvalResult -> dict"""
        return {
            "id": r.id,
            "tenant_id": r.tenant_id,
            "task_id": r.task_id,
            "query": r.query,
            "retrieved_docs": r.retrieved_docs,
            "relevance_scores": r.relevance_scores,
            "precision_score": r.precision_score,
            "recall_score": r.recall_score,
            "mrr_score": r.mrr_score,
            "ndcg_score": r.ndcg_score,
            "answer_traceback": r.answer_traceback,
            "latency_ms": r.latency_ms,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
