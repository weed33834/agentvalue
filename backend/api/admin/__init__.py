"""Admin API 路由聚合

P1 管理功能拆分到独立模块,避免 routes.py 过度膨胀:
- prompts: Prompt 模板/版本/Label 管理 (Langfuse 风格)
- tools: 工具管理 (LangChain @tool + ToolNode + MCP)
- debug: 调试与可观测性 (prompt 版本追溯 / trace 查询 / 系统健康)
- kb: 知识库管理 (文档 CRUD + 重建索引 + 检索测试台 + 分块配置)
- rerank: Rerank Provider 测试台 (P2-2, 对标 Dify Rerank)
- custom_tools: 自定义工具上传 (P3-1, OpenAPI Schema 导入, 对标 Dify Custom Tool)
- feature_flags: 功能开关 (P3-2, 对标 Langfuse Feature Flag)
- api_keys: API Key 管理 (外部调用方鉴权, CRUD + 轮换 + 用量统计)
- users: 用户管理 CRUD (列表/详情/更新/禁用/删除/批量导入)
- scheduler: 定时任务调度管理 (APScheduler, 增删改查 + 手动触发 + 执行历史)

后续 models / datasets 等可继续在此扩展。
"""

from api.admin.prompts import router as prompts_router
from api.admin.tools import router as tools_router
from api.admin.debug import router as debug_router
from api.admin.kb import router as kb_router
from api.admin.rerank import router as rerank_router
from api.admin.custom_tools import router as custom_tools_router
from api.admin.feature_flags import router as feature_flags_router
from api.admin.api_keys import router as api_keys_router
from api.admin.users import router as users_router
from api.admin.scheduler import router as scheduler_router

__all__ = [
    "prompts_router",
    "tools_router",
    "debug_router",
    "kb_router",
    "rerank_router",
    "custom_tools_router",
    "feature_flags_router",
    "api_keys_router",
    "users_router",
    "scheduler_router",
]
