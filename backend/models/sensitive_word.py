"""敏感词字典管理数据模型

对标 Dify 敏感词审核 + 阿里云内容安全:
- SensitiveWord: 敏感词条目 (词 + 分类 + 严重程度 + 处理动作)
- SensitiveWordCategory: 敏感词分类 (政治/色情/暴力/广告/垃圾/自定义)

处理动作 (action):
- block: 阻断 (返回错误, 不允许内容通过)
- replace: 替换 (用 replacement 字段替换敏感词)
- mask: 掩码 (用 *** 替换敏感词)
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


def _now_utc() -> datetime:
    """当前 UTC 时间"""
    return datetime.now(timezone.utc)


class SensitiveWord(Base):
    """敏感词条目

    每条记录一个敏感词, 支持分类、严重程度、处理动作。
    用于输入/输出内容审核 (check_text / filter_text)。
    """

    __tablename__ = "sensitive_words"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 多租户归属
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default="default"
    )
    # 敏感词文本
    word: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    # 分类: politics / porn / violence / ad / spam / custom
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, default="custom", index=True
    )
    # 严重程度: low / medium / high
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default="medium"
    )
    # 处理动作: block / replace / mask
    action: Mapped[str] = mapped_column(
        String(16), nullable=False, default="mask"
    )
    # 替换文本 (action=replace 时使用)
    replacement: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True, default="***"
    )
    # 是否启用
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    # 创建人 (用户 ID)
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )

    __table_args__ = (
        # 同一分类下 word 唯一 (避免重复添加)
        UniqueConstraint("category", "word", name="uix_sensitive_word_category_word"),
        Index("ix_sensitive_word_active", "is_active"),
    )


class SensitiveWordCategory(Base):
    """敏感词分类

    预置分类: politics / porn / violence / ad / spam / custom
    可扩展自定义分类。
    """

    __tablename__ = "sensitive_word_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 多租户归属
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default="default"
    )
    # 分类名称 (如 politics / porn / violence / ad / spam / custom)
    name: Mapped[str] = mapped_column(
        String(32), nullable=False, unique=True, index=True
    )
    # 分类描述
    description: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    # 是否启用
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    __table_args__ = (
        Index("ix_sensitive_category_active", "is_active"),
    )
