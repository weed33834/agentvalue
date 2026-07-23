"""RAG 质量评测数据模型

对标 RagFlow 检索测试 + 压力测试:
- RagEvalTask: RAG 评测任务 (关联 collection, 测试查询集, 进度, 汇总)
- RagEvalResult: 单条查询评测结果 (检索文档, 相关性评分, precision/recall/MRR/NDCG, 答案溯源)

多租户隔离: 所有模型包含 tenant_id 字段, 查询时按 tenant_id 过滤。
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base
from models.models import DEFAULT_TENANT_ID


def _now_utc() -> datetime:
    """当前 UTC 时间"""
    return datetime.now(timezone.utc)


class RagEvalTask(Base):
    """RAG 质量评测任务

    对标 RagFlow 检索测试:
    - collection_name: 被评测的 ChromaDB collection 名称
    - test_queries: 测试查询列表 JSON, 每项含 query + relevant_doc_ids
    - status: pending / running / completed / failed
    - results_summary: 评测汇总 (平均 precision/recall/MRR/NDCG + 延迟统计)
    """

    __tablename__ = "rag_eval_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 多租户归属
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # 任务名称
    name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    # 被评测的 ChromaDB collection 名称
    collection_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 测试查询列表 [{"query": "...", "relevant_doc_ids": ["id1", "id2"]}]
    test_queries: Mapped[List[Dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    # 任务状态: pending / running / completed / failed
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    # 总查询数
    total_queries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 已完成查询数
    completed_queries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 评测汇总结果
    results_summary: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (Index("ix_rag_eval_task_tenant_status", "tenant_id", "status"),)


class RagEvalResult(Base):
    """单条 RAG 查询评测结果

    对标 RagFlow 检索质量度量:
    - retrieved_docs: 检索返回的文档列表 JSON [{"content": "...", "score": 0.9, "metadata": {...}}]
    - relevance_scores: 各检索文档的相关性评分 [1, 0, 1, ...]
    - precision_score: Precision@K
    - recall_score: Recall@K
    - mrr_score: MRR (Mean Reciprocal Rank)
    - ndcg_score: NDCG@K (Normalized Discounted Cumulative Gain)
    - answer_traceback: 答案溯源信息 JSON
    - latency_ms: 检索耗时 (毫秒)
    """

    __tablename__ = "rag_eval_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 多租户归属
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # 所属评测任务 ID
    task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("rag_eval_tasks.id"), nullable=False, index=True
    )
    # 查询文本
    query: Mapped[str] = mapped_column(Text, nullable=False)
    # 检索返回的文档列表
    retrieved_docs: Mapped[List[Dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    # 各检索文档的相关性评分
    relevance_scores: Mapped[List[int]] = mapped_column(
        JSON, nullable=False, default=list
    )
    # Precision@K
    precision_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # Recall@K
    recall_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # MRR (Mean Reciprocal Rank)
    mrr_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # NDCG@K
    ndcg_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # 答案溯源信息
    answer_traceback: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )
    # 检索耗时 (毫秒)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )

    __table_args__ = (Index("ix_rag_eval_result_tenant_task", "tenant_id", "task_id"),)
