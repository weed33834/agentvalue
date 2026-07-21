"""
Chat 会话数据模型

移植自 opencode (TypeScript/Effect) 的 Session/Message/Part 三层数据模型：
- packages/opencode/src/session/session.ts (Session.Info)
- packages/opencode/src/session/message-v2.ts (MessageV2 + Part)

三层结构：
- ChatSession: 一个会话（含 title / model / agent 配置）
- ChatMessage: 一条消息（user / assistant），属于某 session
- ChatPart: 消息内的一个 part（text / reasoning / tool / step-start / step-finish / file）

对齐 opencode 语义但适配现有 models.py 风格（Mapped / mapped_column / tenant_id / now_utc）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

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
from models.models import DEFAULT_TENANT_ID, now_utc


def _new_id() -> str:
    """生成 32 位 hex ID（对齐 opencode 的 MessageID.ascending 语义，用 uuid4 替代 ulid）"""
    import uuid

    return uuid.uuid4().hex


class ChatSession(Base):
    """聊天会话（对齐 opencode Session.Info）

    一个用户可有多个会话，会话内多轮对话。
    """

    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.user_id"), index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(
        String(256), nullable=False, default="新对话"
    )
    model_name: Mapped[str] = mapped_column(
        String(128), nullable=False, default="glm-4.7"
    )
    provider: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    agent_name: Mapped[str] = mapped_column(
        String(64), nullable=False, default="assistant"
    )
    # 通用元数据（share_id / fork 来源等扩展信息），列名映射为 "metadata"
    metadata_: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        "metadata", JSON, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        Index("ix_chat_session_tenant_user", "tenant_id", "user_id"),
    )


class ChatMessage(Base):
    """聊天消息（对齐 opencode MessageV2）

    role: user / assistant
    一条 assistant 消息可含多个 part（text + tool calls + reasoning）
    """

    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user / assistant
    parent_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    model_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    provider_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tokens: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    finish_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    error: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    # 通用元数据（feedback 等扩展信息），列名映射为 "metadata"
    metadata_: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        "metadata", JSON, nullable=True
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_chat_msg_session_created", "session_id", "created_at"),
    )


class ChatPart(Base):
    """消息 part（对齐 opencode Part）

    type 取值：
    - text: 文本内容（text 字段）
    - reasoning: 推理过程（text 字段）
    - tool: 工具调用（tool_name + tool_call_id + tool_state）
    - step-start / step-finish: 一步 LLM 调用的边界（step_index）
    - file: 文件附件（metadata）
    """

    __tablename__ = "chat_parts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    message_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("chat_messages.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    # text / reasoning 用
    text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # tool 用
    tool_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    tool_call_id: Mapped[Optional[str]] = mapped_column(
        String(64), index=True, nullable=True
    )
    tool_state: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )  # {status, input, output, metadata, time, error}
    # step-start / step-finish 用
    step_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 通用元数据
    metadata_: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        "metadata", JSON, nullable=True
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        Index("ix_chat_part_msg_seq", "message_id", "sequence"),
    )
