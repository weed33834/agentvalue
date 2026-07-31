"""
自定义工具数据模型 (P3-1: 自定义工具上传 - OpenAPI Schema 导入)

对标 Dify Custom Tool: 用户粘贴 OpenAPI JSON/YAML → 解析 paths → 每个 operation 生成一个 LangChain Tool。

表: custom_tools
- id: 主键 (cuid/uuid)
- name: 工具名 (按租户唯一)
- description: 工具描述
- openapi_schema: 完整 OpenAPI 3.x spec (JSON)
- base_url: API base URL
- auth_type: 鉴权类型 (none/bearer/api_key/basic)
- auth_credentials: 加密后的凭证 (FieldCipher 加密,可选)
- enabled: 启用状态
- tenant_id: 租户 ID (多租户隔离)
- created_at / updated_at: 时间戳
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class CustomTool(Base):
    """自定义工具实体 (一个 CustomTool 对应一份 OpenAPI spec,内含多个 operation)"""

    __tablename__ = "custom_tools"

    # 主键: cuid/uuid 字符串 (业务层生成)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 工具名 (按租户唯一,用于展示与 ReAct Agent 加载)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 工具描述
    description: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    # 完整 OpenAPI spec (JSON)
    openapi_schema: Mapped[dict] = mapped_column(JSON, nullable=False)
    # API base URL (调用 HTTP endpoint 时拼接 path 用)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    # 鉴权类型: none / bearer / api_key / basic
    auth_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="none", server_default="none"
    )
    # 加密后的凭证 (FieldCipher 加密后 base64 字符串,none 时为 None)
    auth_credentials: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # 启用状态 (禁用的工具不会加载到 ReAct Agent)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    # 租户 ID (多租户隔离,默认 default)
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default", server_default="default"
    )
    # 创建时间 (数据库 server_default,避免应用时区不一致)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 更新时间 (onupdate 触发,UPDATE 时自动刷新)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        # name 索引: 列表搜索/排序常用
        Index("ix_custom_tools_name", "name"),
        # tenant_id 索引: 多租户过滤
        Index("ix_custom_tools_tenant", "tenant_id"),
    )
