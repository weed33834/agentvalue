"""Agent预设模型

对标 ChatGPT GPTs / Coze Bot / LobeChat 助手市场。
PromptTemplate 已合并到 models.models.PromptTemplate (共享同一张表)。
"""

from __future__ import annotations
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, JSON
from core.database import Base


class AgentPreset(Base):
    """Agent预设 - 对标 ChatGPT GPTs / Coze Bot / LobeChat 助手市场"""

    __tablename__ = "agent_presets"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, comment="预设名称")
    description = Column(Text, nullable=True, comment="预设描述")
    avatar = Column(String(512), nullable=True, comment="头像URL或emoji")
    system_prompt = Column(Text, nullable=False, comment="系统提示词")
    category = Column(String(64), nullable=False, default="general", comment="分类")
    tags = Column(JSON, default=list, comment="标签列表")
    model_tier = Column(
        String(10), default="L0", comment="推荐模型层级 L0=云端 L1-L3=本地"
    )
    enabled_tools = Column(JSON, default=list, comment="启用的工具列表")
    temperature = Column(Integer, default=70, comment="温度 0-100")
    is_builtin = Column(Boolean, default=False, comment="是否内置")
    is_public = Column(Boolean, default=True, comment="是否公开")
    use_count = Column(Integer, default=0, comment="使用次数")
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
