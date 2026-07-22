"""
速率限制模块：封装 slowapi Limiter 实例与装饰器。

独立模块以避免 main.py <-> auth_routes.py 循环导入。
slowapi 未安装时降级为 no-op 装饰器,不阻塞启动。

提供两个装饰器:
- rate_limit:      基于 IP 的限流（与 slowapi 默认行为一致）
- rate_limit_user: 基于用户 ID 的限流（从 x-user-id header 取 key,回退到 IP）
"""

import sys
from typing import Callable, Optional

try:
    from slowapi import Limiter, _rate_limit_exceeded_handler  # type: ignore
    from slowapi.errors import RateLimitExceeded  # type: ignore
    from slowapi.middleware import SlowAPIMiddleware  # type: ignore
    from slowapi.util import get_remote_address  # type: ignore

    limiter = Limiter(key_func=get_remote_address, default_limits=[])
    SLOWAPI_AVAILABLE = True
except ImportError:
    limiter = None  # type: ignore[assignment]
    SLOWAPI_AVAILABLE = False
    _rate_limit_exceeded_handler = None  # type: ignore[assignment]
    SlowAPIMiddleware = None  # type: ignore[assignment]
    RateLimitExceeded = None  # type: ignore[assignment]

# 测试环境下禁用速率限制(避免 TestClient 同 IP 批量请求触发限流)
# 通过检测 pytest 是否已加载来判断,比 env var 更可靠(import 时已就绪)
_rate_limit_enabled = "pytest" not in sys.modules


def rate_limit(limit_string: str):
    """
    速率限制装饰器。slowapi 未安装或测试环境下为 no-op,不阻断功能。

    用法:
        @router.post("/login")
        @rate_limit("10/minute")
        async def login(request: Request, ...):
    """
    if limiter is None or not _rate_limit_enabled:
        # slowapi 未安装或测试环境:返回 no-op 装饰器
        def _noop_decorator(func):
            return func

        return _noop_decorator
    return limiter.limit(limit_string)


def _get_user_or_ip_key(request) -> str:
    """按用户限流的 key 函数: 优先从 x-user-id header 取用户 ID,回退到客户端 IP。

    用于 rate_limit_user 装饰器,实现按用户而非按 IP 的限流粒度。
    演示模式下 x-user-id header 可能由前端直接传入(JWT 解析在 RBAC 层完成),
    此处仅做 header 提取,不依赖 JWT 解析逻辑。

    Args:
        request: FastAPI / Starlette Request 对象

    Returns:
        限流 key 字符串 (user_id 或 IP 地址)
    """
    # 优先从 x-user-id header 提取用户标识
    user_id = request.headers.get("x-user-id")
    if user_id:
        return f"user:{user_id}"
    # 回退到 IP 地址（与 slowapi get_remote_address 行为一致）
    if SLOWAPI_AVAILABLE:
        return get_remote_address(request)
    # slowapi 未安装时的兜底
    client = getattr(request, "client", None)
    if client:
        return client.host or "unknown"
    return "unknown"


def rate_limit_user(
    key_func: Optional[Callable] = None,
    limit: str = "60/minute",
):
    """
    按用户限流装饰器。

    使用 x-user-id header 作为限流 key(不存在时回退到 IP),
    与 rate_limit 保持相同的接口风格(slowapi 未安装或测试环境下为 no-op)。

    用法:
        @router.post("/evaluate")
        @rate_limit_user(limit="30/minute")
        async def evaluate(request: Request, ...):

    Args:
        key_func: 自定义 key 提取函数,接收 Request 返回 str。
                  不传则使用默认的 _get_user_or_ip_key(x-user-id 回退 IP)。
        limit: 限流字符串,格式为 "数量/时间窗口",如 "60/minute"、"100/hour"。
    """
    if limiter is None or not _rate_limit_enabled:
        # slowapi 未安装或测试环境:返回 no-op 装饰器
        def _noop_decorator(func):
            return func

        return _noop_decorator

    # 使用自定义 key_func 或默认的用户/IP key
    _key_func = key_func if key_func is not None else _get_user_or_ip_key
    return limiter.limit(limit, key_func=_key_func)
