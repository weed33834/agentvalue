"""对外公共 API 包（WS-3）

本包下的路由**只认 API Key**（``X-API-Key`` 请求头 + scope 校验），
不接受浏览器会话的 JWT，用于第三方系统 / SDK 集成。

与 ``api/admin`` 的区别：
- admin 路由走 ``require_role(Role.ADMIN)``，面向自家控制台；
- public 路由走 ``require_api_key(*scopes)``，面向外部调用方，
  租户由 API Key 自身绑定，调用方无法跨租户访问。
"""

from api.public.v1_routes import router as public_v1_router

__all__ = ["public_v1_router"]
