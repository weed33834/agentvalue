"""提示词模板与Agent预设模型

对标 LobeChat/Open WebUI 的 Prompt 模板库 + ChatGPT GPTs / Coze Bot 助手市场。

注意: 本模块的 PromptTemplate 与 models/models.py 中的 PromptTemplate(Langfuse 风格
版本管理)是两个不同概念:
- models.models.PromptTemplate: 内部 prompt 版本管理(id String, tenant_id, name, type)
- 本模块 PromptTemplate: 用户面向的模板库(id Integer, category, content, variables, is_builtin)
两者通过 extend_existing=True 共享 prompt_templates 表名, 互不影响各自的 ORM 映射。
导入路径区分:
- from models.models import PromptTemplate    → 版本管理实体
- from models.prompt_template import PromptTemplate → 模板库实体
"""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, JSON
from core.database import Base


class PromptTemplate(Base):
    """提示词模板 - 对标 LobeChat/Open WebUI 的 Prompt 模板库"""
    __tablename__ = "prompt_templates"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, comment="模板名称")
    category = Column(String(64), nullable=False, default="general", comment="分类: general/coding/writing/analysis/hr")
    content = Column(Text, nullable=False, comment="模板内容，支持 {{variable}} 占位符")
    variables = Column(JSON, default=list, comment="变量列表 [{name, description}]")
    is_builtin = Column(Boolean, default=False, comment="是否内置")
    is_public = Column(Boolean, default=True, comment="是否公开")
    created_by = Column(Integer, nullable=True, comment="创建者ID")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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
    model_tier = Column(String(10), default="L1", comment="推荐模型层级")
    enabled_tools = Column(JSON, default=list, comment="启用的工具列表")
    temperature = Column(Integer, default=70, comment="温度 0-100")
    is_builtin = Column(Boolean, default=False, comment="是否内置")
    is_public = Column(Boolean, default=True, comment="是否公开")
    use_count = Column(Integer, default=0, comment="使用次数")
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
