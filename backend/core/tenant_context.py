"""
多租户上下文管理

基于 contextvars 提供请求级租户隔离：中间件从 JWT / x-tenant-id header 提取 tenant_id
后写入上下文，service 层查询时读取 current_tenant 做数据级过滤。

兼容性设计：未设置时返回 DEFAULT_TENANT_ID，单租户历史数据与现有测试无需改动。
"""

import contextvars
from contextlib import contextmanager
from typing import Iterator

from models.models import DEFAULT_TENANT_ID

# 请求级租户上下文，default 兜底单租户场景
_current_tenant: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_current_tenant", default=DEFAULT_TENANT_ID
)


def get_current_tenant() -> str:
    """获取当前请求所属租户 ID，未设置时返回 default"""
    return _current_tenant.get()


def set_current_tenant(tenant_id: str) -> contextvars.Token:
    """设置当前租户，返回 token 供 reset 使用"""
    return _current_tenant.set(tenant_id or DEFAULT_TENANT_ID)


def reset_current_tenant(token: contextvars.Token) -> None:
    """恢复之前的租户上下文，避免请求间泄漏"""
    _current_tenant.reset(token)


@contextmanager
def tenant_scope(tenant_id: str) -> Iterator[str]:
    """临时切换租户上下文，退出后自动恢复（后台任务/测试场景使用）"""
    token = set_current_tenant(tenant_id)
    try:
        yield tenant_id
    finally:
        reset_current_tenant(token)
