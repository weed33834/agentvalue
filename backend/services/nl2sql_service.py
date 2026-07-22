"""NL2SQL 自然语言转 SQL 服务

对标 RagFlow NL2SQL:
- generate_sql: 用 LLM 将自然语言转为 SQL (构建含 schema 信息的 prompt)
- execute_sql: 执行 SQL (只读查询, 自动注入 tenant_id 过滤)
- 查询历史 CRUD
- Schema 定义 CRUD
- _validate_sql: SQL 安全验证 (只允许 SELECT, 正则检查)
- _build_prompt: 构建 LLM prompt (含 schema + 示例查询)

安全:
- 只允许 SELECT / WITH 开头
- 禁止 INSERT/UPDATE/DELETE/DROP/ALTER 等关键词 (正则 \b 检查)
- 禁止多语句 (分号)
- 自动注入 tenant_id 过滤条件
- 结果行数限制 (max 100)

事务边界由路由层控制 (service 层不 commit)。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.nl2sql_models import NL2SQLQuery, NL2SQLSchema

logger = logging.getLogger(__name__)

# 最大结果行数
MAX_RESULT_ROWS = 100

# 查询状态
QUERY_STATUS_PENDING = "pending"
QUERY_STATUS_SUCCESS = "success"
QUERY_STATUS_FAILED = "failed"
QUERY_STATUS_EXECUTED = "executed"

# 禁止的 SQL 关键词 (DML/DDL/事务控制/系统命令)
_FORBIDDEN_KEYWORDS = [
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
    "TRUNCATE", "GRANT", "REVOKE", "ATTACH", "DETACH", "PRAGMA",
    "REPLACE", "MERGE", "CALL", "EXEC", "EXECUTE", "COMMIT",
    "ROLLBACK", "SAVEPOINT", "VACUUM", "REINDEX", "BEGIN",
    "END", "LOAD", "IMPORT", "EXPORT",
]

# 允许查询的表白名单 (多租户隔离, 所有表均含 tenant_id 列)
_ALLOWED_TABLES = {
    "evaluations", "dimension_scores", "raw_inputs", "feedback",
    "users", "notifications", "audit_logs",
    "chat_sessions", "chat_messages",
}

# 禁止查询的系统表/元数据表
_FORBIDDEN_TABLES = {
    "sqlite_master", "sqlite_sequence", "sqlite_dbpage",
    "pragma", "information_schema", "pg_catalog", "sys",
    "mysql", "performance_schema", "syscatalog", "pg_tables",
    "pg_views", "pg_class", "pg_namespace",
}

# 默认 schema (HR 核心表, 当租户未配置 schema 时使用)
_DEFAULT_SCHEMA_PROMPT = """TABLE users (系统用户):
  - id: INTEGER (主键)
  - user_id: VARCHAR(64) (员工业务ID, 全局关联键)
  - name: VARCHAR(128) (姓名)
  - email: VARCHAR(256)
  - role: VARCHAR(32) (角色: employee/manager/hr/admin)
  - department: VARCHAR(128) (部门)
  - manager_id: VARCHAR(64) (直属主管ID)
  - tenant_id: VARCHAR(64)
  - created_at: TIMESTAMP

TABLE evaluations (评估主表):
  - id: INTEGER (主键)
  - evaluation_id: VARCHAR(128) (唯一)
  - employee_id: VARCHAR(64) (关联 users.user_id)
  - period: VARCHAR(32) (评估周期, 如 2026-W20)
  - overall_score: FLOAT (综合得分 0-100)
  - status: VARCHAR(32) (状态: ai_drafted/manager_review/hr_audit/approved/rejected)
  - tenant_id: VARCHAR(64)
  - created_at: TIMESTAMP
  - approved_at: TIMESTAMP

TABLE dimension_scores (维度得分明细):
  - id: INTEGER (主键)
  - evaluation_id: VARCHAR(128) (关联 evaluations.evaluation_id)
  - employee_id: VARCHAR(64)
  - period: VARCHAR(32)
  - dimension: VARCHAR(64) (维度名称, 如 执行力/协作/创新)
  - score: FLOAT
  - tenant_id: VARCHAR(64)
"""


class NL2SQLService:
    """NL2SQL 自然语言转 SQL 服务"""

    def __init__(self, session: AsyncSession):
        self.session = session

    # ===================== SQL 生成 =====================

    async def generate_sql(
        self,
        natural_query: str,
        table_name: Optional[str] = None,
        *,
        llm_provider: Any = None,
        tenant_id: str = "default",
    ) -> NL2SQLQuery:
        """用 LLM 将自然语言转为 SQL

        1. 获取目标表的 schema (从 NL2SQLSchema 表, 未配置则用默认 schema)
        2. 构建 prompt (schema + 示例查询 + 规则)
        3. 调用 LLM 生成 SQL + 解释
        4. SQL 安全验证 (_validate_sql)

        Args:
            natural_query: 用户的自然语言查询。
            table_name: 目标表名 (用于获取 schema, 可选)。
            llm_provider: LLM Provider 实例 (需有 chat_completion 方法)。
            tenant_id: 租户 ID。

        Returns:
            创建的 NL2SQLQuery 记录 (status: success/failed)。

        Raises:
            ValueError: LLM 不可用或 SQL 验证失败。
        """
        if not natural_query or not natural_query.strip():
            raise ValueError("natural_query 不能为空")

        # 获取 schema
        schema_info = await self._get_schema_info(table_name, tenant_id=tenant_id)

        # 构建 prompt
        prompt = self._build_prompt(natural_query, schema_info, tenant_id=tenant_id)

        # 调用 LLM
        if llm_provider is None:
            raise ValueError("LLM Provider 不可用")

        try:
            # 兼容 core.providers.base.ChatMessage 或普通 dict
            try:
                from core.providers.base import ChatMessage

                messages = [
                    ChatMessage(role="system", content=prompt),
                    ChatMessage(role="user", content=f"问题: {natural_query}\n\n请生成 SQL 查询。"),
                ]
            except ImportError:
                messages = [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": f"问题: {natural_query}\n\n请生成 SQL 查询。"},
                ]

            completion = await llm_provider.chat_completion(messages=messages)
            content = completion.content or ""
        except Exception as e:
            logger.error("NL2SQL LLM 调用失败: %s", e)
            query = NL2SQLQuery(
                tenant_id=tenant_id,
                natural_query=natural_query,
                table_name=table_name,
                database_schema=schema_info,
                status=QUERY_STATUS_FAILED,
                error_message=f"LLM 调用失败: {e}",
            )
            self.session.add(query)
            await self.session.flush()
            return query

        # 解析 LLM 返回 (JSON {sql, explanation} 或纯 SQL)
        sql, explanation = self._parse_llm_response(content)

        if not sql:
            query = NL2SQLQuery(
                tenant_id=tenant_id,
                natural_query=natural_query,
                table_name=table_name,
                database_schema=schema_info,
                status=QUERY_STATUS_FAILED,
                error_message=f"LLM 未生成有效 SQL, 原始返回: {content[:300]}",
            )
            self.session.add(query)
            await self.session.flush()
            return query

        # SQL 安全验证
        try:
            safe_sql = self._validate_sql(sql)
        except ValueError as e:
            query = NL2SQLQuery(
                tenant_id=tenant_id,
                natural_query=natural_query,
                generated_sql=sql,
                sql_explanation=explanation,
                table_name=table_name,
                database_schema=schema_info,
                status=QUERY_STATUS_FAILED,
                error_message=f"SQL 安全验证失败: {e}",
            )
            self.session.add(query)
            await self.session.flush()
            return query

        query = NL2SQLQuery(
            tenant_id=tenant_id,
            natural_query=natural_query,
            generated_sql=safe_sql,
            sql_explanation=explanation,
            table_name=table_name,
            database_schema=schema_info,
            status=QUERY_STATUS_SUCCESS,
        )
        self.session.add(query)
        await self.session.flush()
        logger.info("NL2SQL 生成成功 query_id=%s table=%s", query.id, table_name)
        return query

    async def execute_sql(
        self,
        sql: str,
        table_name: Optional[str] = None,
        *,
        query_id: Optional[int] = None,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """执行 SQL (只读查询)

        1. SQL 安全验证
        2. 执行查询
        3. 若提供 query_id, 更新查询记录状态为 executed

        Args:
            sql: 待执行的 SQL 字符串。
            table_name: 目标表名 (仅记录用)。
            query_id: 关联的查询记录 ID (可选, 用于更新状态)。
            tenant_id: 租户 ID (用于记录)。

        Returns:
            {
                "success": bool,
                "rows": list[dict],
                "columns": list[str],
                "row_count": int,
                "error": str | None,
            }

        Raises:
            ValueError: SQL 安全验证失败。
        """
        safe_sql = self._validate_sql(sql, tenant_id=tenant_id)

        try:
            result = await self.session.execute(text(safe_sql))
            rows = []
            keys: List[str] = []
            for row in result:
                if hasattr(row, "_mapping"):
                    row_dict = dict(row._mapping)
                elif isinstance(row, dict):
                    row_dict = row
                else:
                    row_dict = dict(row)
                if not keys:
                    keys = list(row_dict.keys())
                rows.append(row_dict)
        except Exception as e:
            logger.warning("NL2SQL 执行失败 sql=%s: %s", safe_sql, e)
            if query_id is not None:
                await self._update_query_status(
                    query_id, QUERY_STATUS_FAILED, error_message=str(e), tenant_id=tenant_id
                )
            return {
                "success": False,
                "rows": [],
                "columns": [],
                "row_count": 0,
                "error": str(e),
            }

        # 更新查询记录
        if query_id is not None:
            await self._update_query_status(
                query_id,
                QUERY_STATUS_EXECUTED,
                result_count=len(rows),
                result_data={"rows": rows, "columns": keys},
                tenant_id=tenant_id,
            )

        logger.info("NL2SQL 执行成功 query_id=%s rows=%s", query_id, len(rows))
        return {
            "success": True,
            "rows": rows,
            "columns": keys,
            "row_count": len(rows),
            "error": None,
        }

    # ===================== 查询历史 CRUD =====================

    async def save_query(
        self,
        natural_query: str,
        generated_sql: Optional[str],
        sql_explanation: Optional[str] = None,
        table_name: Optional[str] = None,
        status: str = QUERY_STATUS_PENDING,
        *,
        created_by: Optional[str] = None,
        tenant_id: str = "default",
    ) -> NL2SQLQuery:
        """保存查询记录"""
        query = NL2SQLQuery(
            tenant_id=tenant_id,
            natural_query=natural_query,
            generated_sql=generated_sql,
            sql_explanation=sql_explanation,
            table_name=table_name,
            status=status,
            created_by=created_by,
        )
        self.session.add(query)
        await self.session.flush()
        return query

    async def get_query(
        self, query_id: int, *, tenant_id: str = "default"
    ) -> Optional[NL2SQLQuery]:
        """获取查询记录"""
        return (
            await self.session.execute(
                select(NL2SQLQuery).where(
                    NL2SQLQuery.id == query_id,
                    NL2SQLQuery.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()

    async def list_queries(
        self,
        *,
        status_filter: Optional[str] = None,
        table_name: Optional[str] = None,
        page: int = 1,
        size: int = 20,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """查询历史列表 (分页)"""
        base = (
            select(NL2SQLQuery)
            .where(NL2SQLQuery.tenant_id == tenant_id)
            .order_by(NL2SQLQuery.created_at.desc())
        )
        if status_filter:
            base = base.where(NL2SQLQuery.status == status_filter)
        if table_name:
            base = base.where(NL2SQLQuery.table_name == table_name)

        total = (
            await self.session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar() or 0

        offset = (page - 1) * size
        rows = (
            await self.session.execute(base.offset(offset).limit(size))
        ).scalars().all()

        return {
            "items": [self._query_to_dict(q) for q in rows],
            "total": total,
            "page": page,
            "size": size,
        }

    async def delete_query(
        self, query_id: int, *, tenant_id: str = "default"
    ) -> bool:
        """删除查询记录"""
        query = await self.get_query(query_id, tenant_id=tenant_id)
        if query is None:
            return False
        await self.session.delete(query)
        await self.session.flush()
        return True

    # ===================== Schema CRUD =====================

    async def create_schema(
        self,
        table_name: str,
        schema_definition: Dict[str, Any],
        *,
        description: Optional[str] = None,
        sample_queries: Optional[List[str]] = None,
        enabled: bool = True,
        tenant_id: str = "default",
    ) -> NL2SQLSchema:
        """创建表结构定义"""
        if not table_name or not table_name.strip():
            raise ValueError("table_name 不能为空")

        existing = (
            await self.session.execute(
                select(NL2SQLSchema).where(
                    NL2SQLSchema.tenant_id == tenant_id,
                    NL2SQLSchema.table_name == table_name.strip(),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ValueError(f"表 '{table_name}' 的 schema 已存在")

        schema = NL2SQLSchema(
            tenant_id=tenant_id,
            table_name=table_name.strip(),
            schema_definition=schema_definition,
            description=description,
            sample_queries=sample_queries or [],
            enabled=enabled,
        )
        self.session.add(schema)
        await self.session.flush()
        logger.info("创建 NL2SQL schema table=%s tenant=%s", table_name, tenant_id)
        return schema

    async def get_schema(
        self, schema_id: int, *, tenant_id: str = "default"
    ) -> Optional[NL2SQLSchema]:
        """获取 schema 定义"""
        return (
            await self.session.execute(
                select(NL2SQLSchema).where(
                    NL2SQLSchema.id == schema_id,
                    NL2SQLSchema.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()

    async def get_schema_by_table(
        self, table_name: str, *, tenant_id: str = "default"
    ) -> Optional[NL2SQLSchema]:
        """按表名获取 schema"""
        return (
            await self.session.execute(
                select(NL2SQLSchema).where(
                    NL2SQLSchema.tenant_id == tenant_id,
                    NL2SQLSchema.table_name == table_name,
                    NL2SQLSchema.enabled.is_(True),
                )
            )
        ).scalar_one_or_none()

    async def list_schemas(
        self, *, tenant_id: str = "default"
    ) -> List[NL2SQLSchema]:
        """列出租户所有 schema 定义"""
        result = await self.session.execute(
            select(NL2SQLSchema)
            .where(NL2SQLSchema.tenant_id == tenant_id)
            .order_by(NL2SQLSchema.created_at.desc())
        )
        return list(result.scalars().all())

    async def update_schema(
        self,
        schema_id: int,
        *,
        schema_definition: Optional[Dict[str, Any]] = None,
        description: Optional[str] = None,
        sample_queries: Optional[List[str]] = None,
        enabled: Optional[bool] = None,
        tenant_id: str = "default",
    ) -> NL2SQLSchema:
        """更新 schema 定义"""
        schema = await self.get_schema(schema_id, tenant_id=tenant_id)
        if schema is None:
            raise ValueError(f"schema {schema_id} 不存在")
        if schema_definition is not None:
            schema.schema_definition = schema_definition
        if description is not None:
            schema.description = description
        if sample_queries is not None:
            schema.sample_queries = sample_queries
        if enabled is not None:
            schema.enabled = enabled
        await self.session.flush()
        return schema

    async def delete_schema(
        self, schema_id: int, *, tenant_id: str = "default"
    ) -> bool:
        """删除 schema 定义"""
        schema = await self.get_schema(schema_id, tenant_id=tenant_id)
        if schema is None:
            return False
        await self.session.delete(schema)
        await self.session.flush()
        return True

    # ===================== 内部方法 =====================

    def _validate_sql(self, sql: str, *, tenant_id: Optional[str] = None) -> str:
        """SQL 安全验证: 只允许 SELECT/WITH, 禁止 DDL/DML/多语句

        安全措施:
        1. 只允许 SELECT/WITH 开头
        2. 禁止 DDL/DML/事务控制/系统命令关键词
        3. 禁止多语句 (分号)
        4. 表名白名单校验 (FROM/JOIN 后的表名必须在白名单中)
        5. 禁止查询系统表 (sqlite_master / information_schema 等)
        6. 当提供 tenant_id 时, 自动注入 tenant_id 过滤条件 (多租户隔离)
        7. 结果行数限制 (LIMIT)

        Args:
            sql: LLM 生成的 SQL 字符串。
            tenant_id: 租户 ID。提供时自动注入 tenant_id 过滤条件 (执行阶段)。

        Returns:
            验证通过并清理后的 SQL (含 tenant_id 过滤 + LIMIT 子句)。

        Raises:
            ValueError: SQL 不符合安全要求。
        """
        if not sql or not sql.strip():
            raise ValueError("SQL 为空")

        sql = sql.strip()
        # 移除 SQL 行注释 (-- ...) 和块注释 (/* ... */)
        sql = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
        sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
        sql = sql.strip()
        # 移除尾部分号
        sql = sql.rstrip(";").strip()
        # 禁止分号 (多语句防护)
        if ";" in sql:
            raise ValueError("SQL 中不允许分号 (禁止多语句执行)")
        # 检查首关键词: 只允许 SELECT 或 WITH (CTE)
        upper = sql.upper()
        if not (upper.startswith("SELECT") or upper.startswith("WITH")):
            raise ValueError("SQL 必须以 SELECT 或 WITH 开头")
        # 移除字符串字面量后检查禁止关键词 (避免误报)
        stripped = self._strip_string_literals(upper)
        for kw in _FORBIDDEN_KEYWORDS:
            if re.search(rf"\b{kw}\b", stripped):
                raise ValueError(f"SQL 中禁止使用关键词: {kw}")

        # 表名提取与白名单校验
        cte_names = self._extract_cte_names(sql)
        table_names = self._extract_table_names(sql)
        for tbl in table_names:
            tbl_lower = tbl.lower()
            # CTE 定义的临时表名跳过白名单校验
            if tbl_lower in cte_names:
                continue
            # 禁止查询系统表/元数据表
            if tbl_lower in _FORBIDDEN_TABLES or tbl_lower.startswith("sqlite_"):
                raise ValueError(f"禁止查询系统表: {tbl}")
            # 禁止带 schema 前缀的表名 (防止 information_schema.xxx 等)
            if "." in tbl_lower:
                raise ValueError(f"禁止使用带 schema 前缀的表名: {tbl}")
            # 表名必须在白名单中
            if tbl_lower not in _ALLOWED_TABLES:
                raise ValueError(f"表 '{tbl}' 不在允许查询的白名单中")

        # 自动注入 tenant_id 过滤条件 (仅在提供 tenant_id 时, 即执行阶段)
        if tenant_id is not None:
            sql = self._inject_tenant_filter(sql, tenant_id)

        # 如果没有 LIMIT, 自动添加
        if not re.search(r"\bLIMIT\b", sql.upper()):
            sql = f"{sql} LIMIT {MAX_RESULT_ROWS}"
        else:
            # 已有 LIMIT, 检查是否超过上限
            limit_match = re.search(r"\bLIMIT\s+(\d+)", sql.upper())
            if limit_match:
                limit_val = int(limit_match.group(1))
                if limit_val > MAX_RESULT_ROWS:
                    sql = re.sub(
                        r"\bLIMIT\s+\d+",
                        f"LIMIT {MAX_RESULT_ROWS}",
                        sql,
                        flags=re.IGNORECASE,
                    )
        return sql

    @staticmethod
    def _extract_table_names(sql: str) -> List[str]:
        """从 FROM/JOIN 后提取表名 (用于白名单校验)

        匹配 FROM table / JOIN table 模式, 跳过子查询 (FROM (SELECT ...))。
        """
        tables: List[str] = []
        seen: set = set()
        for m in re.finditer(
            r"(?:\bFROM\b|\bJOIN\b)\s+([a-zA-Z_][\w.]*)",
            sql,
            re.IGNORECASE,
        ):
            name = m.group(1)
            if name.lower() not in seen:
                seen.add(name.lower())
                tables.append(name)
        return tables

    @staticmethod
    def _extract_cte_names(sql: str) -> set:
        """提取 CTE (WITH ... AS) 定义的临时表名

        CTE 名不应参与表白名单校验, 需排除。
        """
        cte_names: set = set()
        # WITH cte1 AS (...), cte2 AS (...) ...
        for m in re.finditer(
            r"\bWITH\b\s+(?:RECURSIVE\s+)?(\w+)\s+AS\s*\(",
            sql,
            re.IGNORECASE,
        ):
            cte_names.add(m.group(1).lower())
        # 后续 CTE: ), cte2 AS (
        for m in re.finditer(r"\)\s*,\s*(\w+)\s+AS\s*\(", sql, re.IGNORECASE):
            cte_names.add(m.group(1).lower())
        return cte_names

    @staticmethod
    def _inject_tenant_filter(sql: str, tenant_id: str) -> str:
        """在 SQL 中注入 tenant_id 过滤条件 (多租户隔离)

        - 无 WHERE 子句: 在 GROUP BY/ORDER BY/HAVING/LIMIT 前插入 WHERE tenant_id = 'xxx'
        - 有 WHERE 子句: 在第一个 WHERE 后追加 AND tenant_id = 'xxx'

        Args:
            sql: 已通过基本安全验证的 SQL。
            tenant_id: 租户 ID (单引号会被转义防注入)。

        Returns:
            注入 tenant_id 过滤条件后的 SQL。
        """
        # 转义 tenant_id 中的单引号 (防 SQL 注入)
        safe_tenant = tenant_id.replace("'", "''")
        tenant_cond = f"tenant_id = '{safe_tenant}'"

        upper = sql.upper()
        if re.search(r"\bWHERE\b", upper):
            # 已有 WHERE, 在第一个 WHERE 后追加 AND tenant_id = 'xxx'
            sql = re.sub(
                r"\bWHERE\b",
                f"WHERE {tenant_cond} AND",
                sql,
                count=1,
                flags=re.IGNORECASE,
            )
        else:
            # 无 WHERE, 在 GROUP BY/ORDER BY/HAVING/LIMIT 前插入
            insert_pos = None
            for pattern in [
                r"\bGROUP\s+BY\b",
                r"\bORDER\s+BY\b",
                r"\bHAVING\b",
                r"\bLIMIT\b",
            ]:
                m = re.search(pattern, sql, re.IGNORECASE)
                if m:
                    insert_pos = m.start()
                    break
            if insert_pos is not None:
                sql = sql[:insert_pos] + f" WHERE {tenant_cond}" + sql[insert_pos:]
            else:
                sql = sql + f" WHERE {tenant_cond}"
        return sql

    @staticmethod
    def _strip_string_literals(sql: str) -> str:
        """移除 SQL 中的字符串字面量, 用于安全的关键词检查"""
        sql = re.sub(r"'(?:[^']|'')*'", "''", sql)
        sql = re.sub(r'"(?:[^"]|"")*"', '""', sql)
        return sql

    def _build_prompt(
        self,
        natural_query: str,
        schema_info: Dict[str, Any],
        *,
        tenant_id: str = "default",
    ) -> str:
        """构建 LLM prompt (含 schema + 示例查询 + 规则)

        Args:
            natural_query: 用户的自然语言查询。
            schema_info: schema 信息 (来自 _get_schema_info)。
            tenant_id: 租户 ID (注入到规则中)。

        Returns:
            构建好的 prompt 字符串。
        """
        schema_text = schema_info.get("schema_text", _DEFAULT_SCHEMA_PROMPT)
        samples = schema_info.get("sample_queries", [])
        description = schema_info.get("description", "")

        sample_hint = ""
        if samples:
            sample_hint = "\n示例查询:\n" + "\n".join(f"- {s}" for s in samples)

        desc_hint = ""
        if description:
            desc_hint = f"\n表描述: {description}\n"

        return f"""你是一个 SQL 生成助手。根据用户的自然语言问题, 生成一条只读 SQL 查询。

数据库 Schema (SQLite 兼容):
{schema_text}
{desc_hint}{sample_hint}

规则:
1. 只能生成 SELECT 查询, 禁止 INSERT/UPDATE/DELETE/DDL
2. 所有查询必须包含 tenant_id = '{tenant_id}' 过滤条件 (多租户隔离)
3. 最多返回 {MAX_RESULT_ROWS} 行 (自动添加 LIMIT)
4. 不使用分号
5. 表连接时注意 tenant_id 对齐 (如 JOIN users u ON e.employee_id = u.user_id AND u.tenant_id = e.tenant_id)
6. JSON 字段查询用 SQLite 的 json_extract 函数

请返回 JSON 格式:
{{"sql": "SELECT ...", "explanation": "简要说明查询逻辑"}}
"""

    async def _get_schema_info(
        self, table_name: Optional[str], *, tenant_id: str = "default"
    ) -> Dict[str, Any]:
        """获取 schema 信息

        若指定 table_name 且存在对应 NL2SQLSchema, 使用其 schema_definition 构建 schema 文本。
        否则使用默认 schema (HR 核心表)。
        """
        if table_name:
            schema = await self.get_schema_by_table(table_name, tenant_id=tenant_id)
            if schema is not None:
                schema_def = schema.schema_definition or {}
                columns = schema_def.get("columns", [])
                col_lines = []
                for col in columns:
                    col_name = col.get("name", "")
                    col_type = col.get("type", "")
                    col_desc = col.get("description", "")
                    col_lines.append(f"  - {col_name}: {col_type} ({col_desc})")
                pk = schema_def.get("primary_key", "")
                if pk:
                    col_lines.append(f"  - 主键: {pk}")
                fks = schema_def.get("foreign_keys", [])
                for fk in fks:
                    col_lines.append(
                        f"  - 外键: {fk.get('column')} → {fk.get('ref_table')}.{fk.get('ref_column')}"
                    )
                schema_text = f"TABLE {schema.table_name}:\n" + "\n".join(col_lines)
                return {
                    "schema_text": schema_text,
                    "description": schema.description,
                    "sample_queries": schema.sample_queries or [],
                }

        return {
            "schema_text": _DEFAULT_SCHEMA_PROMPT,
            "description": None,
            "sample_queries": [],
        }

    def _parse_llm_response(self, content: str) -> tuple:
        """解析 LLM 返回内容, 提取 SQL 和解释

        优先尝试 JSON 解析, 失败则尝试从代码块或文本中提取。

        Returns:
            (sql, explanation) 元组。
        """
        sql = ""
        explanation = ""

        # 优先尝试 JSON 解析
        try:
            result = json.loads(content)
            sql = (result.get("sql") or "").strip()
            explanation = (result.get("explanation") or "").strip()
        except (json.JSONDecodeError, TypeError):
            pass

        # JSON 解析失败, 尝试从代码块或文本中提取
        if not sql:
            # 尝试提取 ```sql ... ``` 代码块
            match = re.search(r"```sql\s*(.*?)\s*```", content, re.DOTALL | re.IGNORECASE)
            if match:
                sql = match.group(1).strip()
            else:
                # 尝试提取 "sql": "..." 格式
                match = re.search(r'"sql"\s*:\s*"((?:[^"\\]|\\.)*)"', content, re.DOTALL)
                if match:
                    sql = (
                        match.group(1)
                        .replace("\\n", "\n")
                        .replace('\\"', '"')
                        .replace("\\t", "\t")
                        .strip()
                    )

        # 仍然没有 SQL, 尝试直接从文本中提取 SELECT 语句
        if not sql:
            match = re.search(r"(SELECT\s+.*?)(?:;|$)", content, re.DOTALL | re.IGNORECASE)
            if match:
                sql = match.group(1).strip()

        return sql, explanation

    async def _update_query_status(
        self,
        query_id: int,
        new_status: str,
        *,
        result_count: Optional[int] = None,
        result_data: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
        tenant_id: str = "default",
    ) -> None:
        """更新查询记录状态"""
        query = await self.get_query(query_id, tenant_id=tenant_id)
        if query is not None:
            query.status = new_status
            if result_count is not None:
                query.result_count = result_count
            if result_data is not None:
                query.result_data = result_data
            if error_message is not None:
                query.error_message = error_message
            await self.session.flush()

    # ===================== 序列化 =====================

    @staticmethod
    def _query_to_dict(q: NL2SQLQuery) -> Dict[str, Any]:
        """NL2SQLQuery → dict"""
        return {
            "id": q.id,
            "tenant_id": q.tenant_id,
            "natural_query": q.natural_query,
            "generated_sql": q.generated_sql,
            "sql_explanation": q.sql_explanation,
            "database_schema": q.database_schema if isinstance(q.database_schema, dict) else None,
            "table_name": q.table_name,
            "status": q.status,
            "result_count": q.result_count,
            "result_data": q.result_data if isinstance(q.result_data, dict) else None,
            "error_message": q.error_message,
            "created_by": q.created_by,
            "created_at": q.created_at.isoformat() if q.created_at else None,
        }

    @staticmethod
    def _schema_to_dict(s: NL2SQLSchema) -> Dict[str, Any]:
        """NL2SQLSchema → dict"""
        return {
            "id": s.id,
            "tenant_id": s.tenant_id,
            "table_name": s.table_name,
            "schema_definition": s.schema_definition if isinstance(s.schema_definition, dict) else {},
            "description": s.description,
            "sample_queries": s.sample_queries if isinstance(s.sample_queries, list) else [],
            "enabled": s.enabled,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
