"""
Prompt 优化建议数据模型

对标 Langfuse LLM Playground 交互测试：
- PromptOptimizationTask: Prompt 优化任务，记录原始/优化后 prompt、优化建议与质量评分

多租户隔离: 所有模型包含 tenant_id 字段，未显式指定时落 DEFAULT_TENANT_ID。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import JSON, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base
from models.models import DEFAULT_TENANT_ID, now_utc


class PromptOptimizationTask(Base):
    """Prompt 优化任务

    记录一次 LLM 驱动的 Prompt 优化过程，包括原始 prompt、优化后 prompt、
    优化建议（按维度分类）与质量评分（clarity/specificity/completeness/effectiveness）。

    task_type:
    - improve:    分析提示词并给出优化建议
    - simplify:   简化提示词，使其更简洁
    - translate:  将提示词翻译为英文
    - specialize: 为 HR 评估场景专门优化
    """

    __tablename__ = "prompt_optimization_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # 原始 Prompt
    original_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    # 优化后 Prompt（优化完成后填充）
    optimized_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 任务类型: improve | simplify | translate | specialize
    task_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="improve"
    )
    # 使用的模型档位（L0/L1/L2/L3 或模型名）
    model_used: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # 优化建议列表: [{"type": "clarity", "comment": "..."}, ...]
    suggestions: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(
        JSON, nullable=True
    )
    # 质量评分: {"clarity": 8, "specificity": 7, "completeness": 9, "effectiveness": 8}
    quality_scores: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )
    # 综合评分（0-10）
    overall_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # 任务状态: pending | processing | completed | failed
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # 按租户 + 状态检索，便于查询待处理/已完成的优化任务
        Index("ix_prompt_opt_tenant_status", "tenant_id", "status"),
    )
