"""Skill 模型 - 可复用的技能模块

对标 Claude Skills / Trae Skills:
- Skill = 系统提示词 + 工具配置 + 输入/输出schema
- 可被Agent动态加载执行
- 支持版本管理和市场分发
"""

from __future__ import annotations
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, JSON
from core.database import Base


class Skill(Base):
    """技能定义"""

    __tablename__ = "skills"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, comment="技能名称(唯一)")
    display_name = Column(String(256), nullable=True, comment="显示名称")
    description = Column(Text, nullable=True, comment="技能描述")
    category = Column(
        String(64), default="general", comment="分类: coding/analysis/writing/hr/data"
    )
    version = Column(String(32), default="1.0.0", comment="版本号")
    system_prompt = Column(Text, nullable=False, comment="系统提示词")
    input_schema = Column(JSON, default=dict, comment="输入参数schema")
    output_schema = Column(JSON, default=dict, comment="输出格式schema")
    required_tools = Column(JSON, default=list, comment="需要的工具列表")
    model_tier = Column(
        String(10), default="L0", comment="推荐模型层级 L0=云端 L1-L3=本地"
    )
    temperature = Column(Integer, default=70, comment="温度0-100")
    is_builtin = Column(Boolean, default=False, comment="是否内置")
    is_public = Column(Boolean, default=True, comment="是否公开")
    is_active = Column(Boolean, default=True, comment="是否激活")
    use_count = Column(Integer, default=0, comment="使用次数")
    tags = Column(JSON, default=list, comment="标签")
    config = Column(JSON, default=dict, comment="额外配置")
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
