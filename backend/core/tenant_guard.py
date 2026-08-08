"""WS-4 租户查询守卫：SQLAlchemy 全局安全网

背景
----
`TenantMiddleware` 会把 tenant_id 写入 contextvar，service 层负责在查询里加
``WHERE tenant_id = :tid``。但 80+ 张表都带 tenant_id，靠人工 review 保证「每条
SELECT 都带租户条件」是不现实的——审计已经发现三个大服务完全漏了过滤。

本模块挂一个 ``do_orm_execute`` 事件监听器，对**租户维度表**上的 SELECT 做
静态检查：若整条语句（含 CTE / 子查询 / JOIN）中找不到任何针对 ``tenant_id``
列的谓词，就判定为「疑似跨租户查询」。

两种模式（配置项 ``tenant_guard_mode``）
--------------------------------------
- ``"warn"``（**默认**）：只打 WARNING 日志（含 SQL 摘要 + 业务侧调用栈）并给
  Prometheus 计数器 ``agentvalue_tenant_guard_violations_total`` +1。**不改变
  任何行为**，用于线上跑一轮收集误报清单。
- ``"enforce"``：直接抛 ``CrossTenantQueryError``，请求失败。

切换方式::

    # .env
    TENANT_GUARD_MODE=enforce

强烈建议先在 warn 模式跑满一个业务周期，把
``agentvalue_tenant_guard_violations_total`` 打到 0 之后再切 enforce。

误报与逃生舱
------------
以下场景是**合法**的跨租户查询：平台级管理统计、跨租户运维巡检、数据迁移脚本、
按主键直取（PK 已全局唯一）。这类调用请显式声明::

    from core.tenant_guard import allow_cross_tenant

    with allow_cross_tenant("平台级 admin 统计"):
        rows = await session.execute(select(ConversationMetrics))

守卫只识别「语句里有没有出现 tenant_id 谓词」，无法判断谓词的值是否正确
（例如 ``WHERE tenant_id = 'other-tenant'`` 会被放行）。它是**安全网**不是
**访问控制**，真正的隔离仍然在 service 层。

性能
----
- 租户维度表名集合在 import 期从 ``Base.metadata`` 预计算一次（O(表数)）。
- 每条语句的检查是一次 ``__visit_name__`` 遍历，无 SQL 编译、无 IO。
- 非 SELECT、非 ORM、命中逃生舱、未启用（``tenant_guard_enabled=False``）时
  几乎零开销（一次 set 查表 + 一次 contextvar 读）。
"""

from __future__ import annotations

import contextvars
import logging
import traceback
from contextlib import contextmanager
from typing import Iterator, Optional, Set

from sqlalchemy import event
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnClause

logger = logging.getLogger(__name__)

# 守卫模式常量
MODE_WARN = "warn"
MODE_ENFORCE = "enforce"
MODE_OFF = "off"

# 租户维度列名（本仓库统一约定）
TENANT_COLUMN = "tenant_id"


class CrossTenantQueryError(RuntimeError):
    """enforce 模式下，检测到缺失租户谓词的查询时抛出。"""


# ─────────────────────────────────────────────────────────────────────────────
# 逃生舱：显式声明「这条查询就是要跨租户」
# ─────────────────────────────────────────────────────────────────────────────
_allow_cross_tenant: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_allow_cross_tenant", default=False
)


@contextmanager
def allow_cross_tenant(reason: str = "") -> Iterator[None]:
    """临时放行跨租户查询（平台级统计 / 运维脚本 / 数据迁移）。

    Args:
        reason: 放行原因，仅用于 debug 日志与代码可读性，不影响行为。

    用法::

        with allow_cross_tenant("平台级 admin 概览"):
            await session.execute(select(func.count(TraceRecord.id)))

    注意：这是 contextvar，作用域覆盖同一 task 内的所有嵌套调用；
    请把范围收到最小，不要包住整个 handler。
    """
    token = _allow_cross_tenant.set(True)
    if reason:
        logger.debug("跨租户查询已显式放行: %s", reason)
    try:
        yield
    finally:
        _allow_cross_tenant.reset(token)


def is_cross_tenant_allowed() -> bool:
    """当前上下文是否处于跨租户放行状态。"""
    return _allow_cross_tenant.get()


# ─────────────────────────────────────────────────────────────────────────────
# 租户维度表名集合（import 期预计算一次）
# ─────────────────────────────────────────────────────────────────────────────
_TENANT_TABLES: Optional[Set[str]] = None


def get_tenant_scoped_tables() -> Set[str]:
    """返回所有带 ``tenant_id`` 列的表名（惰性计算 + 进程级缓存）。

    依赖 ``Base.metadata``，因此必须在全部模型 import 完成后首次调用。
    ``install_tenant_guard()`` 会在安装时触发一次预热。
    """
    global _TENANT_TABLES
    if _TENANT_TABLES is None:
        from core.database import Base

        _TENANT_TABLES = {
            name
            for name, table in Base.metadata.tables.items()
            if TENANT_COLUMN in table.c
        }
    return _TENANT_TABLES


def reset_tenant_table_cache() -> None:
    """清空表名缓存（模型在运行期动态注册后调用，主要给测试用）。"""
    global _TENANT_TABLES
    _TENANT_TABLES = None


# ─────────────────────────────────────────────────────────────────────────────
# 语句检查
# ─────────────────────────────────────────────────────────────────────────────
def _referenced_tenant_tables(stmt: Select) -> Set[str]:
    """收集语句（含子查询/CTE/JOIN）里出现的租户维度表名。"""
    tenant_tables = get_tenant_scoped_tables()
    hit: Set[str] = set()
    try:
        for froms in stmt.get_final_froms():
            for table in froms._from_objects:
                name = getattr(table, "name", None)
                if name in tenant_tables:
                    hit.add(name)
        # 列引用也算（select(User.tenant_id) 这类没有显式 from 的场景）
        for col in getattr(stmt, "column_descriptions", []) or []:
            entity = col.get("entity")
            table = getattr(entity, "__table__", None)
            name = getattr(table, "name", None)
            if name in tenant_tables:
                hit.add(name)
    except Exception:  # pragma: no cover - 语句形态千奇百怪，检查本身绝不能抛
        logger.debug("tenant_guard 解析 FROM 失败", exc_info=True)
    return hit


def _has_tenant_predicate(stmt: Select) -> bool:
    """语句中是否出现了针对 tenant_id 列的谓词。

    实现方式：遍历整棵 WHERE / HAVING / ON 子句树，只要出现名为 ``tenant_id``
    的列引用就算通过。宽松判定是刻意的——守卫的目标是抓「完全没写过滤」的查询，
    而不是校验过滤值是否正确（那是 service 层的责任）。
    """
    try:
        criteria = []
        whereclause = getattr(stmt, "whereclause", None)
        if whereclause is not None:
            criteria.append(whereclause)
        # SQLAlchemy 2.0 中 having/where 是生成式方法，条件存放在 _*_criteria 元组里
        criteria.extend(getattr(stmt, "_where_criteria", ()) or ())
        criteria.extend(getattr(stmt, "_having_criteria", ()) or ())
        # JOIN ... ON 里的租户条件同样有效
        for froms in stmt.get_final_froms():
            onclause = getattr(froms, "onclause", None)
            if onclause is not None:
                criteria.append(onclause)

        for clause in criteria:
            if _clause_mentions_tenant(clause):
                return True
    except Exception:  # pragma: no cover - 同上，检查失败按「通过」处理
        logger.debug("tenant_guard 解析 WHERE 失败", exc_info=True)
        return True
    return False


def _clause_mentions_tenant(clause: object, depth: int = 0) -> bool:
    """递归判断子句里是否引用了 tenant_id 列（深度上限防病态嵌套）。"""
    if depth > 12:
        return False
    name = getattr(clause, "name", None)
    if name == TENANT_COLUMN and isinstance(clause, ColumnClause):
        return True
    if getattr(clause, "key", None) == TENANT_COLUMN:
        return True
    get_children = getattr(clause, "get_children", None)
    if get_children is None:
        return False
    for child in get_children():
        if _clause_mentions_tenant(child, depth + 1):
            return True
    return False


def _origin_frames(limit: int = 4) -> str:
    """抓业务侧调用栈（跳过 sqlalchemy / 本模块内部帧），便于定位漏过滤的代码。"""
    frames = []
    for frame in reversed(traceback.extract_stack()[:-2]):
        path = frame.filename
        if "/sqlalchemy/" in path or path.endswith("tenant_guard.py"):
            continue
        frames.append(f"{frame.filename}:{frame.lineno} in {frame.name}")
        if len(frames) >= limit:
            break
    return " <- ".join(frames)


def check_statement(stmt: Select) -> Optional[str]:
    """检查单条 SELECT，返回违规描述；合规时返回 None。

    抽成独立函数便于单测直接调用，无需真的执行 SQL。
    """
    tables = _referenced_tenant_tables(stmt)
    if not tables:
        return None
    if _has_tenant_predicate(stmt):
        return None
    return ",".join(sorted(tables))


# ─────────────────────────────────────────────────────────────────────────────
# 事件监听器
# ─────────────────────────────────────────────────────────────────────────────
def _record_violation(tables: str) -> None:
    """打点：Prometheus 计数器 +1（指标模块不可用时静默跳过）。"""
    try:
        from core.metrics import record_tenant_guard_violation

        record_tenant_guard_violation(tables)
    except Exception:  # pragma: no cover
        logger.debug("tenant_guard 打点失败", exc_info=True)


def _current_mode() -> str:
    """读取当前守卫模式（每次读配置，get_settings 有 lru_cache，开销可忽略）。"""
    try:
        from core.config import get_settings

        settings = get_settings()
        if not getattr(settings, "tenant_guard_enabled", True):
            return MODE_OFF
        mode = (getattr(settings, "tenant_guard_mode", MODE_WARN) or MODE_WARN).lower()
        return mode if mode in (MODE_WARN, MODE_ENFORCE, MODE_OFF) else MODE_WARN
    except Exception:  # pragma: no cover
        return MODE_WARN


def handle_orm_execute(orm_execute_state) -> None:
    """``do_orm_execute`` 回调：只看 SELECT，其余放行。"""
    if not orm_execute_state.is_select:
        return
    if is_cross_tenant_allowed():
        return
    mode = _current_mode()
    if mode == MODE_OFF:
        return

    stmt = orm_execute_state.statement
    if not isinstance(stmt, Select):
        return

    violation = check_statement(stmt)
    if violation is None:
        return

    _record_violation(violation)
    message = (
        f"跨租户查询风险：表 [{violation}] 的 SELECT 未包含 tenant_id 谓词。"
        f" origin={_origin_frames()}"
    )
    if mode == MODE_ENFORCE:
        raise CrossTenantQueryError(message)
    logger.warning(
        "%s | 若为平台级查询请用 with allow_cross_tenant(...) 显式声明", message
    )


_INSTALLED = False


def install_tenant_guard(engine=None) -> bool:
    """在 Session 层安装守卫。

    Args:
        engine: 兼容参数，当前实现挂在全局 ``Session`` 类上，忽略该参数。

    Returns:
        True 表示本次安装成功；重复调用返回 False（幂等）。
    """
    global _INSTALLED
    if _INSTALLED:
        return False
    from sqlalchemy.orm import Session

    event.listen(Session, "do_orm_execute", handle_orm_execute)
    # 预热表名集合，避免首个请求承担计算成本
    try:
        get_tenant_scoped_tables()
    except Exception:  # pragma: no cover - 模型尚未 import 完时惰性重算
        reset_tenant_table_cache()
    _INSTALLED = True
    logger.info("租户查询守卫已安装 mode=%s", _current_mode())
    return True


def uninstall_tenant_guard() -> None:
    """卸载守卫（测试清理用）。"""
    global _INSTALLED
    if not _INSTALLED:
        return
    from sqlalchemy.orm import Session

    event.remove(Session, "do_orm_execute", handle_orm_execute)
    _INSTALLED = False
