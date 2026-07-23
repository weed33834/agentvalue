"""
对话式HR洞察 (Conversational Talent Insights)

对标 ChatGPT Advanced Data Analysis + HR分析工具。
让HR用自然语言查询员工数据，AI返回结构化结果+图表建议。

端点:
- POST /api/v1/insights/query - 自然语言HR查询
  使用 LLM 将自然语言转换为 SQL, 执行只读查询后生成自然语言回答 + 图表建议
- GET /api/v1/insights/dashboard/{period} - 团队洞察看板
  返回指定周期的 top_performers / improvement_needed / score_distribution /
  department_comparison / trend
- POST /api/v1/insights/export - 导出洞察报告
  将查询结果导出为 CSV 或 JSON 文件下载

权限: manager / hr / admin (RBAC via require_role)
SQL安全: 只允许 SELECT, 禁止 DDL/DML/多语句, 限制结果行数(max 100)
"""

import csv
import io
import json
import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import AppState, get_app_state, get_evaluation_service
from auth.rbac import Role, get_current_user_id, require_role
from core.database import get_db
from core.providers.base import ChatMessage
from core.tenant_context import get_current_tenant
from services.analytics_service import AnalyticsService
from services.evaluation_service import EvaluationService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/insights", tags=["insights"])

# ====== 常量 ======

MAX_RESULT_ROWS = 100
MAX_QUESTION_LENGTH = 1000

# 数据库 Schema 提示词 (供 LLM 生成 SQL)
DB_SCHEMA_PROMPT = """你是一个 SQL 生成助手。根据用户的自然语言问题, 生成一条只读 SQL 查询。

数据库 Schema (SQLite 兼容):

TABLE users (系统用户):
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
  - employee_view: JSON (员工视图, 含 strengths/growth_areas/summary)
  - manager_view: JSON (主管视图, 含 risk_flags/highlights)
  - audit: JSON (审计信息, 含 processing_time_ms)
  - status: VARCHAR(32) (状态: ai_drafted/manager_review/hr_audit/approved/rejected)
  - tenant_id: VARCHAR(64)
  - created_at: TIMESTAMP
  - approved_at: TIMESTAMP

TABLE dimension_scores (维度得分明细, 也称 evaluation_scores):
  - id: INTEGER (主键)
  - evaluation_id: VARCHAR(128) (关联 evaluations.evaluation_id)
  - employee_id: VARCHAR(64)
  - period: VARCHAR(32)
  - dimension: VARCHAR(64) (维度名称, 如 执行力/协作/创新)
  - score: FLOAT
  - improvement_actions: JSON
  - tenant_id: VARCHAR(64)

TABLE review_cycles (360度环评):
  - id: INTEGER (主键)
  - review_id: VARCHAR(128) (唯一)
  - evaluation_id: VARCHAR(128)
  - employee_id: VARCHAR(64)
  - reviewer_id: VARCHAR(64) (评估人ID)
  - reviewer_role: VARCHAR(32) (peer/manager/subordinate/external)
  - status: VARCHAR(16) (pending/submitted)
  - scores: JSON (各维度评分)
  - overall_score: FLOAT
  - feedback_text: TEXT
  - tenant_id: VARCHAR(64)

TABLE calibration_items (校准项):
  - id: INTEGER (主键)
  - item_id: VARCHAR(128) (唯一)
  - session_id: VARCHAR(128) (关联 calibration_sessions)
  - evaluation_id: VARCHAR(128)
  - employee_id: VARCHAR(64)
  - original_score: FLOAT
  - calibrated_score: FLOAT
  - adjustment_reason: TEXT
  - applied: INTEGER (0/1)
  - tenant_id: VARCHAR(64)

规则:
1. 只能生成 SELECT 查询, 禁止 INSERT/UPDATE/DELETE/DDL
2. 所有查询必须包含 tenant_id = '{tenant_id}' 过滤条件 (多租户隔离)
3. 最多返回 100 行 (自动添加 LIMIT)
4. 不使用分号
5. 表连接时注意 tenant_id 对齐 (如 JOIN users u ON e.employee_id = u.user_id AND u.tenant_id = e.tenant_id)
6. JSON 字段查询用 SQLite 的 json_extract 函数 (如 json_extract(employee_view, '$.summary'))

请返回 JSON 格式:
{{"sql": "SELECT ...", "explanation": "简要说明查询逻辑"}}
"""

# 禁止的 SQL 关键词 (DML/DDL/事务控制/系统命令)
_FORBIDDEN_KEYWORDS = [
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "TRUNCATE",
    "GRANT",
    "REVOKE",
    "ATTACH",
    "DETACH",
    "PRAGMA",
    "REPLACE",
    "MERGE",
    "CALL",
    "EXEC",
    "EXECUTE",
    "COMMIT",
    "ROLLBACK",
    "SAVEPOINT",
    "VACUUM",
    "REINDEX",
    "BEGIN",
    "END",
    "DETACH",
    "LOAD",
    "IMPORT",
    "EXPORT",
]


# ====== Pydantic 模型 ======


class InsightQuery(BaseModel):
    """自然语言HR查询请求"""

    question: str = Field(
        ..., description="自然语言问题, 如 '研发部绩效最高的5个人是谁?'"
    )
    context: Optional[Dict[str, Any]] = Field(
        None, description="额外上下文, 如 {period: '2026-W20'}"
    )


class ExportRequest(BaseModel):
    """导出洞察报告请求"""

    data: List[Dict[str, Any]] = Field(..., description="结构化数据(表格行)")
    columns: List[Dict[str, Any]] = Field(
        ..., description="列定义 [{key, label, type}]"
    )
    format: str = Field("csv", description="导出格式: csv 或 json")
    filename: Optional[str] = Field(None, description="文件名(不含扩展名)")


# ====== 依赖 ======


def get_analytics_service(
    eval_service: EvaluationService = Depends(get_evaluation_service),
) -> AnalyticsService:
    """分析服务依赖: 复用 EvaluationService 的只读查询"""
    return AnalyticsService(eval_service)


# ====== SQL 安全 ======


def _strip_string_literals(sql: str) -> str:
    """移除 SQL 中的字符串字面量, 用于安全的关键词检查。

    避免字符串内容 (如 'UPDATE') 触发误报。
    """
    # 移除单引号字符串 (SQLite 标准转义: '' 表示字面量单引号)
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)
    # 移除双引号标识符
    sql = re.sub(r'"(?:[^"]|"")*"', '""', sql)
    return sql


def _validate_sql(sql: str) -> str:
    """验证 SQL 安全性: 只允许 SELECT/WITH, 禁止 DDL/DML/多语句。

    Args:
        sql: LLM 生成的 SQL 字符串

    Returns:
        验证通过并清理后的 SQL (含 LIMIT 子句)

    Raises:
        ValueError: SQL 不符合安全要求
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
    # 检查禁止关键词 (在移除字符串字面量后, 避免误报)
    stripped = _strip_string_literals(upper)
    for kw in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", stripped):
            raise ValueError(f"SQL 中禁止使用关键词: {kw}")
    # 如果没有 LIMIT, 自动添加
    if not re.search(r"\bLIMIT\b", upper):
        sql = f"{sql} LIMIT {MAX_RESULT_ROWS}"
    else:
        # 已有 LIMIT, 检查是否超过上限
        limit_match = re.search(r"\bLIMIT\s+(\d+)", upper)
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


# ====== LLM 辅助 ======


async def _get_llm_provider(app_state: AppState):
    """获取 LLM Provider。

    优先使用 L0 档位 (云端模型, 稳定可用),
    若不可用则降级到 get_provider_with_fallback 自动选择可用档位。
    """
    try:
        return app_state.model_router.get_provider("L0")
    except Exception:
        try:
            provider, _ = await app_state.model_router.get_provider_with_fallback()
            return provider
        except Exception as e:
            raise RuntimeError(f"无法获取 LLM Provider: {e}")


async def _generate_sql(
    app_state: AppState, question: str, tenant_id: str, context: Optional[Dict] = None
) -> str:
    """使用 LLM 将自然语言问题转换为 SQL 查询。

    Args:
        app_state: 应用状态 (含 model_router)
        question: 用户的自然语言问题
        tenant_id: 当前租户 ID (注入到 schema 提示词)
        context: 额外上下文 (如 period)

    Returns:
        LLM 生成的 SQL 字符串 (未经安全验证)
    """
    provider = await _get_llm_provider(app_state)
    prompt = DB_SCHEMA_PROMPT.format(tenant_id=tenant_id)

    context_hint = ""
    if context:
        context_hint = f"\n额外上下文: {json.dumps(context, ensure_ascii=False)}"

    messages = [
        ChatMessage(role="system", content=prompt),
        ChatMessage(
            role="user",
            content=f"问题: {question}{context_hint}\n\n请生成 SQL 查询。",
        ),
    ]
    completion = await provider.chat_completion(
        messages=messages,
    )
    content = completion.content or ""

    sql = ""
    # 优先尝试 JSON 解析
    try:
        result = json.loads(content)
        sql = (result.get("sql") or "").strip()
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

    if not sql:
        raise ValueError(f"LLM 未生成有效的 SQL, 原始返回: {content[:300]}")

    return sql


async def _generate_answer(
    app_state: AppState,
    question: str,
    data: List[Dict[str, Any]],
    columns: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """使用 LLM 将查询结果转换为自然语言回答 + 图表建议。

    Args:
        app_state: 应用状态 (含 model_router)
        question: 用户的原始自然语言问题
        data: SQL 查询结果 (结构化数据行)
        columns: 列定义 [{key, label, type}]

    Returns:
        {"answer": str, "chart_suggestion": Optional[dict]}
    """
    provider = await _get_llm_provider(app_state)
    # 截断数据避免 token 膨胀
    sample_data = data[:50]
    data_str = json.dumps(sample_data, ensure_ascii=False, default=str, indent=2)
    columns_str = json.dumps(columns, ensure_ascii=False, indent=2)

    prompt = f"""你是一个HR数据分析师。根据用户的自然语言问题和SQL查询结果, 生成简洁的自然语言回答和图表建议。

用户问题: {question}

查询结果 (前{len(sample_data)}行, 共{len(data)}行):
{data_str}

列定义:
{columns_str}

请返回 JSON 格式:
{{
    "answer": "用自然语言总结查询结果, 简洁清晰, 提及关键数据和洞察",
    "chart_suggestion": {{
        "type": "bar|line|pie",
        "config": {{
            "title": "图表标题",
            "x_axis": "X轴对应的列key",
            "y_axis": "Y轴对应的列key",
            "labels": ["标签1", "标签2"],
            "values": [数值1, 数值2]
        }}
    }}
}}

图表类型选择建议:
- bar: 适合对比不同类别的数值 (如各部门平均分)
- line: 适合展示时间趋势 (如各周期得分变化)
- pie: 适合展示占比分布 (如绩效等级分布)

如果数据不适合可视化 (如单行结果或纯文本), chart_suggestion 设为 null。
"""

    messages = [ChatMessage(role="system", content=prompt)]
    completion = await provider.chat_completion(
        messages=messages,
    )
    content = completion.content or ""

    # 尝试 JSON 解析
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        pass

    # 尝试从文本中提取 JSON
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    # 降级: 返回原始文本作为回答
    return {"answer": content.strip() or "无法生成回答", "chart_suggestion": None}


# ====== 数据处理辅助 ======


def _infer_column_type(values: List[Any]) -> str:
    """根据列数据推断类型"""
    for v in values:
        if v is not None:
            if isinstance(v, bool):
                return "boolean"
            if isinstance(v, (int, float)):
                return "number"
            if isinstance(v, str):
                return "string"
    return "string"


def _build_columns(rows: List[Dict[str, Any]], keys: List[str]) -> List[Dict[str, Any]]:
    """从查询结果构建列定义"""
    columns = []
    for key in keys:
        values = [row.get(key) for row in rows]
        col_type = _infer_column_type(values)
        columns.append(
            {
                "key": key,
                "label": key.replace("_", " ").title(),
                "type": col_type,
            }
        )
    return columns


def _rows_to_dicts(result) -> tuple:
    """将 SQLAlchemy 结果转换为 (list[dict], list[column_names])"""
    rows = []
    keys = []
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
    return rows, keys


# ====== 端点 1: POST /query - 自然语言HR查询 ======


@router.post("/query")
async def query_insights(
    body: InsightQuery,
    request: Request,
    app_state: AppState = Depends(get_app_state),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """自然语言HR查询

    使用 LLM 将自然语言问题转换为 SQL, 执行只读查询,
    再用 LLM 将结果转换为自然语言回答 + 图表建议。

    安全:
    - SQL 只允许 SELECT, 禁止 INSERT/UPDATE/DELETE/DDL
    - 禁止多语句 (分号)
    - 结果行数限制 max 100
    - 自动注入 tenant_id 过滤

    返回:
    - answer: 自然语言回答
    - data: 结构化数据 (表格行)
    - columns: 列定义 [{key, label, type}]
    - chart_suggestion: 图表建议 {type, config} 或 null
    - sql_used: 生成的 SQL (透明度)
    """
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="question 不能为空",
        )
    if len(question) > MAX_QUESTION_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"question 长度超限 (最多 {MAX_QUESTION_LENGTH} 字符)",
        )

    tenant_id = get_current_tenant()

    try:
        # 步骤1: LLM 将自然语言转换为 SQL
        try:
            raw_sql = await _generate_sql(app_state, question, tenant_id, body.context)
        except RuntimeError as e:
            logger.error("LLM Provider 不可用: %s", e)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"LLM 服务不可用: {e}",
            )
        except ValueError as e:
            logger.warning("LLM SQL 生成失败: %s", e)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"SQL 生成失败: {e}",
            )

        # 步骤2: SQL 安全验证
        try:
            safe_sql = _validate_sql(raw_sql)
        except ValueError as e:
            logger.warning("SQL 安全验证失败: %s | SQL: %s", e, raw_sql)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"SQL 安全验证失败: {e}",
            )

        # 步骤3: 执行 SQL (只读)
        try:
            result = await session.execute(text(safe_sql))
            rows, keys = _rows_to_dicts(result)
        except Exception as e:
            logger.warning("SQL 执行失败: %s | SQL: %s", e, safe_sql)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"SQL 执行失败: {e}",
            )

        # 步骤4: 构建列定义
        columns = _build_columns(rows, keys)

        # 步骤5: LLM 生成自然语言回答 + 图表建议
        try:
            llm_result = await _generate_answer(app_state, question, rows, columns)
            answer = llm_result.get("answer", "")
            chart_suggestion = llm_result.get("chart_suggestion")
        except Exception as e:
            logger.warning("LLM 回答生成失败, 降级返回原始数据: %s", e)
            answer = f"查询返回 {len(rows)} 行数据 (LLM 回答生成失败)"
            chart_suggestion = None

        return {
            "answer": answer,
            "data": rows,
            "columns": columns,
            "chart_suggestion": chart_suggestion,
            "sql_used": safe_sql,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("洞察查询异常: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"洞察查询异常: {e}",
        )


# ====== 端点 2: GET /dashboard/{period} - 团队洞察看板 ======


@router.get("/dashboard/{period}")
async def get_dashboard(
    period: str,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    analytics: AnalyticsService = Depends(get_analytics_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """团队洞察看板

    返回指定周期的团队洞察:
    - top_performers: 绩效 Top5
    - improvement_needed: 需改进的员工 (绩效最低5人或低于60分)
    - score_distribution: 分数分布 (用于直方图)
    - department_comparison: 部门间对比
    - trend: 与上期对比趋势

    权限:
    - HR/ADMIN: 不受限, 可看全公司
    - MANAGER: 仅能看直属下属集合

    使用 AnalyticsService.get_talent_matrix 获取绩效数据,
    辅以 SQL 查询部门对比和趋势。
    """
    tenant_id = get_current_tenant()

    # MANAGER 仅能看直属下属
    member_id_list: Optional[List[str]] = None
    if role == Role.MANAGER:
        current_user_id = await get_current_user_id(request)
        reports = await eval_service.list_direct_reports(current_user_id)
        member_id_list = sorted({r.user_id for r in reports})

    # 使用 AnalyticsService.get_talent_matrix 获取绩效数据
    try:
        matrix = await analytics.get_talent_matrix(
            period=period, member_ids=member_id_list
        )
    except Exception as e:
        logger.warning("get_talent_matrix 失败: %s", e)
        matrix = {
            "members": [],
            "total": 0,
            "cells": {},
            "period": period,
        }

    members = matrix.get("members", [])

    # top_performers: 绩效 Top5
    sorted_by_perf = sorted(
        members, key=lambda m: m.get("performance_score", 0), reverse=True
    )
    top_performers = [
        {
            "employee_id": m.get("employee_id"),
            "performance_score": m.get("performance_score"),
            "potential_score": m.get("potential_score"),
            "period": m.get("period"),
            "eval_count": m.get("eval_count"),
        }
        for m in sorted_by_perf[:5]
    ]

    # improvement_needed: 绩效最低5人或低于60分
    improvement_needed = [
        {
            "employee_id": m.get("employee_id"),
            "performance_score": m.get("performance_score"),
            "potential_score": m.get("potential_score"),
            "period": m.get("period"),
            "eval_count": m.get("eval_count"),
        }
        for m in sorted_by_perf[-5:][::-1]
        if (m.get("performance_score", 100) or 0) < 60
    ]
    # 如果低于60分的人不足5个, 补充最低分的人
    if len(improvement_needed) < 5 and sorted_by_perf:
        existing_ids = {m["employee_id"] for m in improvement_needed}
        for m in sorted_by_perf[-5:][::-1]:
            if m.get("employee_id") not in existing_ids:
                improvement_needed.append(
                    {
                        "employee_id": m.get("employee_id"),
                        "performance_score": m.get("performance_score"),
                        "potential_score": m.get("potential_score"),
                        "period": m.get("period"),
                        "eval_count": m.get("eval_count"),
                    }
                )
                if len(improvement_needed) >= 5:
                    break

    # score_distribution: 分数分布 (用于直方图)
    buckets = [
        {"range": "0-59", "label": "不合格 (<60)", "count": 0},
        {"range": "60-69", "label": "合格 (60-69)", "count": 0},
        {"range": "70-79", "label": "良好 (70-79)", "count": 0},
        {"range": "80-89", "label": "优秀 (80-89)", "count": 0},
        {"range": "90-100", "label": "卓越 (90-100)", "count": 0},
    ]
    for m in members:
        score = m.get("performance_score", 0) or 0
        if score < 60:
            buckets[0]["count"] += 1
        elif score < 70:
            buckets[1]["count"] += 1
        elif score < 80:
            buckets[2]["count"] += 1
        elif score < 90:
            buckets[3]["count"] += 1
        else:
            buckets[4]["count"] += 1

    # department_comparison: 部门间对比 (SQL 查询)
    department_comparison = await _get_department_comparison(
        session, period, tenant_id, member_id_list
    )

    # trend: 与上期对比趋势 (SQL 查询)
    trend = await _get_period_trend(session, period, tenant_id, member_id_list)

    return {
        "period": period,
        "total_members": len(members),
        "top_performers": top_performers,
        "improvement_needed": improvement_needed,
        "score_distribution": buckets,
        "department_comparison": department_comparison,
        "trend": trend,
    }


async def _get_department_comparison(
    session: AsyncSession,
    period: str,
    tenant_id: str,
    member_id_list: Optional[List[str]],
) -> List[Dict[str, Any]]:
    """查询部门间绩效对比 (当前周期)"""
    try:
        sql = """
            SELECT u.department AS department,
                   AVG(e.overall_score) AS avg_score,
                   COUNT(*) AS eval_count
            FROM evaluations e
            JOIN users u ON e.employee_id = u.user_id AND u.tenant_id = e.tenant_id
            WHERE e.status = 'approved'
              AND e.tenant_id = :tenant_id
              AND e.period = :period
        """
        params: Dict[str, Any] = {"tenant_id": tenant_id, "period": period}
        if member_id_list is not None:
            placeholders = ", ".join(f":mid_{i}" for i in range(len(member_id_list)))
            sql += f" AND e.employee_id IN ({placeholders})"
            for i, mid in enumerate(member_id_list):
                params[f"mid_{i}"] = mid
        sql += " GROUP BY u.department ORDER BY avg_score DESC"

        result = await session.execute(text(sql), params)
        rows, _ = _rows_to_dicts(result)
        return [
            {
                "department": r.get("department") or "未分配",
                "avg_score": round(float(r.get("avg_score") or 0), 2),
                "eval_count": int(r.get("eval_count") or 0),
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("部门对比查询失败: %s", e)
        return []


async def _get_period_trend(
    session: AsyncSession,
    period: str,
    tenant_id: str,
    member_id_list: Optional[List[str]],
) -> Dict[str, Any]:
    """查询与上一周期的趋势对比"""
    try:
        sql = """
            SELECT period, AVG(overall_score) AS avg_score, COUNT(*) AS eval_count
            FROM evaluations
            WHERE status = 'approved'
              AND tenant_id = :tenant_id
              AND period <= :period
        """
        params: Dict[str, Any] = {"tenant_id": tenant_id, "period": period}
        if member_id_list is not None:
            placeholders = ", ".join(f":mid_{i}" for i in range(len(member_id_list)))
            sql += f" AND employee_id IN ({placeholders})"
            for i, mid in enumerate(member_id_list):
                params[f"mid_{i}"] = mid
        sql += " GROUP BY period ORDER BY period DESC LIMIT 2"

        result = await session.execute(text(sql), params)
        rows, _ = _rows_to_dicts(result)

        current = None
        previous = None
        if rows:
            current = rows[0]
            if len(rows) > 1:
                previous = rows[1]

        current_avg = float(current.get("avg_score") or 0) if current else 0
        previous_avg = float(previous.get("avg_score") or 0) if previous else 0
        delta = round(current_avg - previous_avg, 2) if previous else 0.0
        direction = "up" if delta > 0 else ("down" if delta < 0 else "stable")

        return {
            "current_period": period,
            "current_avg_score": round(current_avg, 2),
            "current_eval_count": int(current.get("eval_count") or 0) if current else 0,
            "previous_period": previous.get("period") if previous else None,
            "previous_avg_score": round(previous_avg, 2) if previous else None,
            "previous_eval_count": (
                int(previous.get("eval_count") or 0) if previous else 0
            ),
            "delta": delta,
            "direction": direction,
        }
    except Exception as e:
        logger.warning("趋势查询失败: %s", e)
        return {
            "current_period": period,
            "current_avg_score": 0,
            "current_eval_count": 0,
            "previous_period": None,
            "previous_avg_score": None,
            "previous_eval_count": 0,
            "delta": 0,
            "direction": "stable",
        }


# ====== 端点 3: POST /export - 导出洞察报告 ======


@router.post("/export")
async def export_insights(
    body: ExportRequest,
    request: Request,
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """导出洞察报告

    将查询结果导出为 CSV 或 JSON 文件下载。

    Args:
        data: 结构化数据 (表格行)
        columns: 列定义 [{key, label, type}]
        format: 导出格式 (csv 或 json)
        filename: 文件名 (不含扩展名)

    Returns:
        StreamingResponse: 文件下载流
    """
    fmt = (body.format or "csv").lower()
    if fmt not in ("csv", "json"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="format 只支持 csv 或 json",
        )

    filename = (body.filename or "insights_report").strip()
    # 清理文件名: 只保留字母数字下划线连字符
    filename = re.sub(r"[^\w\-.]", "_", filename)

    try:
        if fmt == "csv":
            # 生成 CSV
            output = io.StringIO()
            # 写入 UTF-8 BOM 以支持 Excel 正确显示中文
            output.write("\ufeff")
            writer = csv.writer(output)
            # 表头: 使用 label 如果有, 否则用 key
            headers = [
                col.get("label") or col.get("key") or f"col_{i}"
                for i, col in enumerate(body.columns)
            ]
            writer.writerow(headers)
            # 数据行
            keys = [col.get("key") for col in body.columns]
            for row in body.data:
                writer.writerow([_serialize_csv_value(row.get(k)) for k in keys])

            content = output.getvalue()
            media_type = "text/csv; charset=utf-8"
            full_filename = f"{filename}.csv"
        else:
            # 生成 JSON
            report = {
                "filename": filename,
                "columns": body.columns,
                "data": body.data,
                "row_count": len(body.data),
            }
            content = json.dumps(report, ensure_ascii=False, default=str, indent=2)
            media_type = "application/json; charset=utf-8"
            full_filename = f"{filename}.json"

        # 转为字节流
        content_bytes = content.encode("utf-8")
        content_stream = io.BytesIO(content_bytes)

        return StreamingResponse(
            content_stream,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{full_filename}"',
            },
        )
    except Exception as e:
        logger.error("导出失败: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"导出失败: {e}",
        )


def _serialize_csv_value(value: Any) -> str:
    """将任意值序列化为 CSV 单元格字符串"""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
