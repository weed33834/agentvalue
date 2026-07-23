"""Agent 版本管理数据模型

对标 Langfuse Prompt Versioning + Dify App Publish:
- AgentVersion: Agent 预设的不可变历史版本 (system_prompt / tools_config / model_config 等)
- AgentPublishTarget: 版本发布到各渠道 (飞书/微信/钉钉/Web/API) 的发布记录

版本号自增 (1, 2, 3...), 不可修改已创建的版本内容 (强制新建版本)。
状态机: draft (草稿) → published (已发布) → archived (已归档)
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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


def _now_utc() -> datetime:
    """当前 UTC 时间"""
    return datetime.now(timezone.utc)


class AgentVersion(Base):
    """Agent 版本 (不可变历史, 每次更新新建一行)

    与 AgentPreset (agent_presets 表) 关联: agent_id 指向 AgentPreset.id。
    一个 AgentPreset 下可有多个 AgentVersion, 版本号自增。
    """

    __tablename__ = "agent_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 多租户归属
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default="default"
    )
    # 关联的 Agent 预设 ID (软关联 AgentPreset.id, 不加外键避免循环依赖)
    agent_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("agent_presets.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # 版本号 (同一 agent_id 下自增, 从 1 开始)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # 系统提示词 (该版本的 prompt 快照)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    # 工具配置快照 (启用的工具列表等)
    tools_config: Mapped[dict] = mapped_column(JSON, default=list)
    # 模型配置快照 (model_tier / model_name 等)
    model_config: Mapped[dict] = mapped_column(JSON, default=dict)
    # 温度 (0-100, 与 AgentPreset.temperature 对齐)
    temperature: Mapped[int] = mapped_column(Integer, default=70)
    # 版本状态: draft / published / archived
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    # 变更日志 (本次版本相对上一版的改动说明)
    changelog: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 创建人 (用户 ID)
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )
    # 发布时间 (status → published 时写入)
    published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # 同一 agent_id 下 version_number 唯一
        UniqueConstraint("agent_id", "version_number", name="uix_agent_version_number"),
        Index("ix_agent_version_agent_status", "agent_id", "status"),
    )


class AgentPublishTarget(Base):
    """Agent 版本发布目标 (渠道发布记录)

    记录某版本发布到某渠道的状态与配置。
    渠道: feishu / wechat / dingtalk / web / api
    状态机: pending (待发布) → published (已发布) / failed (发布失败)
    """

    __tablename__ = "agent_publish_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 多租户归属
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default="default"
    )
    # 关联的 Agent 预设 ID
    agent_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("agent_presets.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # 关联的版本 ID
    version_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("agent_versions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # 发布渠道: feishu / wechat / dingtalk / web / api
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    # 渠道配置 (JSON, 存储渠道接入信息如 webhook_url / app_id / api_key 等)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    # 发布状态: pending / published / failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # 发布时间
    published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 发布失败时的错误信息
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # 同一 agent_id + channel 唯一 (一个 Agent 每个渠道只能有一个发布记录)
        UniqueConstraint("agent_id", "channel", name="uix_agent_publish_channel"),
        Index("ix_agent_publish_agent_channel", "agent_id", "channel"),
    )
