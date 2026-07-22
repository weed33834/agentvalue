"""深度文档解析数据模型

对标 RagFlow DeepDoc:
- DocParsingTask: 解析任务 (文件 → 文本/表格/图片提取)
- DocParsingResult: 解析结果 (按页/按内容类型拆分的结构化结果)

parse_strategy: auto / ocr / structure / hybrid
- auto: 自动选择 (PDF 用 structure, 扫描件用 ocr)
- ocr: 强制 OCR (扫描件/图片型 PDF)
- structure: 结构化解析 (文本型 PDF/DOCX, 保留版面结构)
- hybrid: 混合模式 (结构化 + OCR 补充)

DocParsingResult.content_type: text / table / image / heading / list
- text: 普通文本段落
- table: 表格 (content 为 JSON, 含行列数据)
- image: 图片 (metadata 含图片引用路径)
- heading: 标题 (metadata 含层级)
- list: 列表项
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


def _now_utc() -> datetime:
    """当前 UTC 时间"""
    return datetime.now(timezone.utc)


class DocParsingTask(Base):
    """文档解析任务

    status: pending / processing / completed / failed
    """

    __tablename__ = "doc_parsing_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default="default"
    )
    # 待解析文件路径
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    # 文件类型: pdf / docx / xlsx / pptx / txt / md
    file_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # 解析策略: auto / ocr / structure / hybrid
    parse_strategy: Mapped[str] = mapped_column(
        String(16), nullable=False, default="auto"
    )
    # 状态: pending / processing / completed / failed
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    # 解析结果摘要 JSON (含总页数/表格数/图片数/文本片段数等)
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 页数
    page_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 表格数
    table_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 图片数
    image_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_doc_task_tenant_status", "tenant_id", "status"),
        Index("ix_doc_task_tenant_created", "tenant_id", "created_at"),
    )


class DocParsingResult(Base):
    """文档解析结果 (按页 + 内容类型拆分)

    bounding_box: {x0, y0, x1, y1} (页面坐标, 用于版面分析)
    metadata: 内容附加信息 (如 heading 层级 / table 行列数 / image 路径)
    """

    __tablename__ = "doc_parsing_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default="default"
    )
    task_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("doc_parsing_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 页码 (从 1 开始)
    page_num: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    # 内容类型: text / table / image / heading / list
    content_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="text", index=True
    )
    # 内容 (text/heading/list 为文本, table 为 JSON, image 为图片引用)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 边界框 (页面坐标)
    bounding_box: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 元数据 (heading 层级 / table 行列数 / image 路径等)
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )

    __table_args__ = (
        Index("ix_doc_result_task_page", "task_id", "page_num"),
        Index("ix_doc_result_task_type", "task_id", "content_type"),
    )
