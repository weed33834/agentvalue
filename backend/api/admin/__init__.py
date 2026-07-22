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
- search_routes: 混合检索管理 (向量 + BM25 全文检索 + 增量更新 + 检索配置)
- quota_routes: 租户配额管理 (日请求/token 配额 + 用量统计 + 重置)
- budget_routes: 成本预算告警 (月度/日度预算 + 阈值告警通知)
- billing_routes: API 计费账单 (汇总 + 按用户/端点聚合 + CSV/JSON 导出)

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
from api.admin.search_routes import router as search_router
from api.admin.quota_routes import router as quota_router
from api.admin.budget_routes import router as budget_router
from api.admin.billing_routes import router as billing_router
# Agent 版本管理 + 多渠道发布 + 工具配置 + 敏感词 + 告警
from api.admin.agent_version_routes import router as agent_version_router
from api.admin.publish_routes import router as publish_router
from api.admin.tool_config_routes import router as tool_config_router
from api.admin.sensitive_word_routes import router as sensitive_word_router
from api.admin.alert_routes import router as alert_router
# 模型 Fallback 策略 (对标阿里百炼 AI 网关秒级容灾)
from api.admin.model_fallback_routes import router as model_fallback_router
# 会话分析看板 (对标 Langfuse Token 分析 / Dashboard)
from api.admin.analytics_v2_routes import router as analytics_v2_router
# API 健康监控 (对标 Langfuse 延迟监控 / 告警系统)
from api.admin.api_health_routes import router as api_health_router
# 数据集管理 (对标 Langfuse 数据集管理 + 阿里百炼训练集/评测集)
from api.admin.dataset_routes import router as dataset_router
# LLM-as-a-Judge 自动评测 (对标 Langfuse LLM-as-a-Judge + Dify 日志回放)
from api.admin.llm_judge_routes import router as llm_judge_router
# RAG 质量评测 (对标 RagFlow 检索测试 + 压力测试)
from api.admin.rag_eval_routes import router as rag_eval_router
# 人工标注工具 (对标 Langfuse Human-in-the-loop)
from api.admin.annotation_routes import router as annotation_router
# SSO 单点登录 (对标 Dify SSO / Bisheng SSO, OAuth2/SAML/LDAP)
from api.admin.sso_routes import router as sso_router
# Agent 模板市场 (对标 Coze 插件市场 / LobeChat 助手市场)
from api.admin.agent_template_routes import router as agent_template_router
# NL2SQL 自然语言转 SQL (对标 RagFlow NL2SQL)
from api.admin.nl2sql_routes import router as nl2sql_router
# 深度文档解析 (对标 RagFlow DeepDoc, 表格提取 + 版面分析)
from api.admin.doc_parsing_routes import router as doc_parsing_router

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
    "search_router",
    "quota_router",
    "budget_router",
    "billing_router",
    "agent_version_router",
    "publish_router",
    "tool_config_router",
    "sensitive_word_router",
    "alert_router",
    "model_fallback_router",
    "analytics_v2_router",
    "api_health_router",
    "dataset_router",
    "llm_judge_router",
    "rag_eval_router",
    "annotation_router",
    "sso_router",
    "agent_template_router",
    "nl2sql_router",
    "doc_parsing_router",
]
