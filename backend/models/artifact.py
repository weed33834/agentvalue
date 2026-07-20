"""Artifact 模型 - 对标 Claude Artifacts / ChatGPT Canvas"""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, JSON, ForeignKey, Index
from core.database import Base

class Artifact(Base):
    """对话中生成的可交互产物"""
    __tablename__ = "chat_artifacts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(64), ForeignKey("chat_sessions.id"), nullable=False, index=True)
    message_id = Column(String(64), ForeignKey("chat_messages.id"), nullable=True, index=True)
    name = Column(String(256), nullable=True, comment="产物名称")
    artifact_type = Column(String(32), nullable=False, comment="类型: html/svg/mermaid/markdown/code/react/json")
    language = Column(String(32), nullable=True, comment="代码语言")
    content = Column(Text, nullable=False, comment="产物内容")
    metadata_ = Column("metadata", JSON, default=dict, comment="元数据")
    version = Column(Integer, default=1, comment="版本号")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
