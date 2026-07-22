"""Agent 模板市场数据模型

对标 Coze 插件市场 / LobeChat 助手市场:
- AgentTemplate: Agent 模板 (HR 场景预置, 支持分享/复用)
- TemplateReview: 模板评价 (评分 + 评论)

template_config 结构:
{
    "system_prompt": str,          # 系统提示词
    "model_config": {              # 模型配置 (tier/temperature/max_tokens)
        "tier": "L0", "temperature": 0.7, "max_tokens": 2000
    },
    "tools": [str],                # 工具名称列表
    "knowledge_base_ids": [str],   # 关联知识库 ID
    "workflow_id": str | null,     # 关联工作流 ID
    "guardrails": {                # 安全护栏 (输入/输出过滤)
        "input_guard": bool, "output_guard": bool, "sensitive_words": bool
    }
}
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


def _now_utc() -> datetime:
    """当前 UTC 时间"""
    return datetime.now(timezone.utc)


class AgentTemplate(Base):
    """Agent 模板 (可分享/复用的 Agent 配置)

    category: hr / recruitment / evaluation / training / general
    is_public: True 表示进入公开模板市场 (所有租户可见)
    is_official: True 表示官方预置模板
    """

    __tablename__ = "agent_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default="default"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 分类: hr / recruitment / evaluation / training / general
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, default="general", index=True
    )
    # 模板配置 JSON (system_prompt / model_config / tools / knowledge_base_ids / workflow_id / guardrails)
    template_config: Mapped[dict] = mapped_column(JSON, default=dict)
    author: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    download_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rating: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # 公开模板市场可见 (所有租户可安装)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 官方预置模板
    is_official: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, onupdate=_now_utc
    )

    __table_args__ = (
        Index("ix_agent_template_tenant_category", "tenant_id", "category"),
        Index("ix_agent_template_public", "is_public"),
        Index("ix_agent_template_official", "is_official"),
    )


class TemplateReview(Base):
    """模板评价 (评分 + 评论)

    同一租户内同一用户对同一模板只能评价一次 (uix_tenant_template_reviewer)。
    """

    __tablename__ = "agent_template_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default="default"
    )
    template_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("agent_templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reviewer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # 评分 1-5
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "template_id", "reviewer_id",
            name="uix_tenant_template_reviewer",
        ),
        Index("ix_template_review_template", "template_id"),
    )
