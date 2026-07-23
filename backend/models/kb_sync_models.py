"""
知识库自动同步数据模型

对标 RagFlow 自动同步 / 阿里百炼数据源管理：
- KbDataSource: 数据源配置（local_dir/s3/url/database/git），支持定时增量同步与变更检测
- KbSyncLog: 同步执行日志，记录每次同步的新增/修改/删除统计与详细处理结果

多租户隔离: 所有模型包含 tenant_id 字段，未显式指定时落 DEFAULT_TENANT_ID。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base
from models.models import DEFAULT_TENANT_ID, now_utc


class KbDataSource(Base):
    """知识库数据源

    描述一个外部数据来源（本地目录/S3/URL/数据库/Git 仓库）及其同步策略，
    关联到向量库的一个 collection。enabled=False 时该数据源不参与定时同步。

    config 格式示例:
    - local_dir: {"path": "/data/docs", "pattern": "*.pdf"}
    - url:       {"url": "https://...", "interval": 300}
    - s3:        {"endpoint": "...", "bucket": "...", "prefix": "..."}
    - database:  {"dsn": "...", "query": "SELECT ..."}
    - git:       {"repo": "...", "branch": "main", "token_ref": "env:GIT_TOKEN"}
    """

    __tablename__ = "kb_data_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # 数据源名称（同租户内建议唯一，便于引用）
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 数据源类型: local_dir | s3 | url | database | git
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # 数据源配置（JSON，按 source_type 不同结构不同）
    config: Mapped[Dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    # 关联的向量库 collection 名称
    collection_name: Mapped[str] = mapped_column(String(256), nullable=False)
    # 同步间隔（分钟），0 表示仅手动同步
    sync_interval_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60
    )
    # 上次同步时间
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 上次同步状态: success | failed | partial | never
    last_sync_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="never"
    )
    # 上次同步统计: {"added": 5, "updated": 2, "deleted": 1, "errors": 0}
    last_sync_stats: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )
    # 是否启用定时同步
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        # 按租户 + 启用状态检索，定时任务扫描启用数据源
        Index("ix_kb_ds_tenant_enabled", "tenant_id", "enabled"),
    )


class KbSyncLog(Base):
    """知识库同步日志

    每次同步（手动或定时）落一行，记录起止时间、同步类型、状态、统计与详细结果。
    """

    __tablename__ = "kb_sync_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # 关联数据源
    data_source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("kb_data_sources.id", ondelete="CASCADE"), nullable=False
    )
    # 同步类型: manual | scheduled
    sync_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # 同步状态: running | success | failed | partial
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now_utc
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 同步统计: {"added": N, "updated": N, "deleted": N, "errors": N}
    stats: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    # 错误信息（同步失败时）
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 每个文件的处理结果详情
    details: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(
        JSON, nullable=True
    )

    __table_args__ = (
        # 按租户 + 数据源检索，查询某数据源的同步历史
        Index("ix_kb_sync_log_tenant_ds", "tenant_id", "data_source_id"),
    )
