"""NL2SQL 自然语言转 SQL 数据模型

对标 RagFlow NL2SQL:
- NL2SQLQuery: 查询记录 (自然语言 → SQL → 执行结果)
- NL2SQLSchema: 表结构定义 (供 LLM 生成 SQL 的 schema 上下文)

安全约束:
- 只允许 SELECT 查询, 禁止 INSERT/UPDATE/DELETE/DROP 等 DML/DDL
- NL2SQLService._validate_sql 做正则校验
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
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


class NL2SQLQuery(Base):
    """NL2SQL 查询记录

    status: pending / success / failed / executed
    - pending: 已生成 SQL 但未执行
    - success: SQL 生成成功
    - executed: SQL 已执行
    - failed: 生成或执行失败
    """

    __tablename__ = "nl2sql_queries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default="default"
    )
    # 用户输入的自然语言查询
    natural_query: Mapped[str] = mapped_column(Text, nullable=False)
    # LLM 生成的 SQL
    generated_sql: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # SQL 解释 (LLM 生成的查询逻辑说明)
    sql_explanation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 查询时的数据库 schema 快照 (JSON)
    database_schema: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 目标表名
    table_name: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, index=True
    )
    # 状态: pending / success / failed / executed
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    # 执行结果行数
    result_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 执行结果数据 (JSON, 最多 100 行)
    result_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 创建人 (用户 ID)
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )

    __table_args__ = (
        Index("ix_nl2sql_query_tenant_created", "tenant_id", "created_at"),
        Index("ix_nl2sql_query_tenant_status", "tenant_id", "status"),
    )


class NL2SQLSchema(Base):
    """NL2SQL 表结构定义 (供 LLM 生成 SQL 的 schema 上下文)

    管理员预先定义各表的 schema, NL2SQL 生成 SQL 时注入到 prompt 中。
    schema_definition 结构:
    {
        "columns": [
            {"name": "user_id", "type": "VARCHAR(64)", "description": "员工ID"},
            {"name": "name", "type": "VARCHAR(128)", "description": "姓名"}
        ],
        "primary_key": "id",
        "foreign_keys": [{"column": "employee_id", "ref_table": "users", "ref_column": "user_id"}]
    }
    sample_queries: ["查询研发部所有员工", "统计各部门人数"]
    """

    __tablename__ = "nl2sql_schemas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default="default"
    )
    # 表名 (租户内唯一)
    table_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # 表结构定义 JSON
    schema_definition: Mapped[dict] = mapped_column(JSON, default=dict)
    # 表描述 (业务含义说明)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 示例查询列表 (帮助 LLM 理解常见查询模式)
    sample_queries: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "table_name", name="uix_tenant_nl2sql_table"),
    )
