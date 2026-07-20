"""
速率限制模块：封装 slowapi Limiter 实例与装饰器。

独立模块以避免 main.py <-> auth_routes.py 循环导入。
slowapi 未安装时降级为 no-op 装饰器,不阻塞启动。
"""

import sys

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
