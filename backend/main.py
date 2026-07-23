"""
FastAPI 应用入口
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# P0-4：在创建 FastAPI app 之前初始化全局日志配置（结构化日志 + trace_id 关联）
from core.logging_config import setup_logging

setup_logging()

from api.admin.prompts import router as admin_prompts_router  # noqa: E402
from api.admin.tools import router as admin_tools_router  # noqa: E402
from api.admin.debug import router as admin_debug_router  # noqa: E402
# P2 深水区: Provider CRUD + Playground SSE
from api.admin.providers import router as admin_providers_router  # noqa: E402
from api.admin.playground import router as admin_playground_router  # noqa: E402
# P1-1: 知识库管理 UI 全栈(文档 CRUD + 重建索引 + 检索测试台 + 分块配置)
from api.admin.kb import router as admin_kb_router  # noqa: E402
# P2-2: Rerank Provider 抽象与测试台(对标 Dify Rerank)
from api.admin.rerank import router as admin_rerank_router  # noqa: E402
# P3-1: 自定义工具上传(OpenAPI Schema 导入,对标 Dify Custom Tool)
from api.admin.custom_tools import router as admin_custom_tools_router  # noqa: E402
# P3-2: Feature Flag 系统(应用级功能开关,对标 Langfuse Feature Flag)
from api.admin.feature_flags import router as admin_feature_flags_router  # noqa: E402
# API Key 管理(外部调用方鉴权, CRUD + 轮换 + 用量统计)
from api.admin.api_keys import router as admin_api_keys_router  # noqa: E402
# 用户管理 CRUD(列表/详情/更新/禁用/删除/批量导入)
from api.admin.users import router as admin_users_router  # noqa: E402
# P4-1: 多 Agent 协作(supervisor 模式,对标 Coze Multi-Agent)
from api.admin.multi_agent import router as admin_multi_agent_router  # noqa: E402
# P4-2: 工作流可视化编排(对标 Dify Workflow / Coze Bot 编排)
from api.admin.workflows import router as admin_workflows_router  # noqa: E402
# P2-1: Token/成本趋势看板(Prometheus 时序聚合 + DB 评估统计)
from api.admin import analytics as admin_analytics  # noqa: E402
# 定时任务调度管理 (APScheduler, 增删改查 + 手动触发 + 执行历史)
from api.admin.scheduler import router as admin_scheduler_router  # noqa: E402
# 混合检索管理 (向量 + BM25 全文检索 + RRF 融合 + 增量更新 + 检索配置)
from api.admin.search_routes import router as admin_search_router  # noqa: E402
# 租户配额管理 (日请求/token 配额 + 用量统计 + 重置)
from api.admin.quota_routes import router as admin_quota_router  # noqa: E402
# 成本预算告警 (月度/日度预算 + 阈值告警通知)
from api.admin.budget_routes import router as admin_budget_router  # noqa: E402
# API 计费账单 (汇总 + 按用户/端点聚合 + CSV/JSON 导出)
from api.admin.billing_routes import router as admin_billing_router  # noqa: E402
# Agent 版本管理 (版本 CRUD + 发布 + 回滚 + 对比 + 归档)
from api.admin.agent_version_routes import router as admin_agent_version_router  # noqa: E402
# 多渠道发布 (飞书/微信/钉钉/Web/API)
from api.admin.publish_routes import router as admin_publish_router  # noqa: E402
# 工具配置 (超时管理)
from api.admin.tool_config_routes import router as admin_tool_config_router  # noqa: E402
# 敏感词字典管理 (增删改查 + 文本审核 + 导入导出)
from api.admin.sensitive_word_routes import router as admin_sensitive_word_router  # noqa: E402
# 告警通知通道 (创建/通知/确认/解决/统计)
from api.admin.alert_routes import router as admin_alert_router  # noqa: E402
# 模型 Fallback 策略 (对标阿里百炼 AI 网关秒级容灾)
from api.admin.model_fallback_routes import router as admin_model_fallback_router  # noqa: E402
# 会话分析看板 (对标 Langfuse Token 分析 / Dashboard)
from api.admin.analytics_v2_routes import router as admin_analytics_v2_router  # noqa: E402
# API 健康监控 (对标 Langfuse 延迟监控 / 告警系统)
from api.admin.api_health_routes import router as admin_api_health_router  # noqa: E402
# 数据集管理 (对标 Langfuse 数据集管理 + 阿里百炼训练集/评测集)
from api.admin.dataset_routes import router as admin_dataset_router  # noqa: E402
# LLM-as-a-Judge 自动评测 (对标 Langfuse LLM-as-a-Judge + Dify 日志回放)
from api.admin.llm_judge_routes import router as admin_llm_judge_router  # noqa: E402
# RAG 质量评测 (对标 RagFlow 检索测试 + 压力测试)
from api.admin.rag_eval_routes import router as admin_rag_eval_router  # noqa: E402
# 人工标注工具 (对标 Langfuse Human-in-the-loop)
from api.admin.annotation_routes import router as admin_annotation_router  # noqa: E402
# SSO 单点登录 (对标 Dify SSO / Bisheng SSO, OAuth2/SAML/LDAP)
from api.admin.sso_routes import router as admin_sso_router  # noqa: E402
# Agent 模板市场 (对标 Coze 插件市场 / LobeChat 助手市场)
from api.admin.agent_template_routes import router as admin_agent_template_router  # noqa: E402
# NL2SQL 自然语言转 SQL (对标 RagFlow NL2SQL)
from api.admin.nl2sql_routes import router as admin_nl2sql_router  # noqa: E402
# 深度文档解析 (对标 RagFlow DeepDoc, 表格提取 + 版面分析)
from api.admin.doc_parsing_routes import router as admin_doc_parsing_router  # noqa: E402
# GraphRAG 知识图谱 (对标 RagFlow GraphRAG + RAPTOR, 实体关系抽取 + 图增强检索)
from api.admin.graph_rag_routes import router as admin_graph_rag_router  # noqa: E402
# 灰度发布 / 蓝绿部署 (对标 Bisheng/Langfuse Canary 发布)
from api.admin.gray_release_routes import router as admin_gray_release_router  # noqa: E402
# 多环境管理 (对标 Bisheng/Langfuse 环境隔离, dev/staging/prod 配置隔离)
from api.admin.environment_routes import router as admin_environment_router  # noqa: E402
# 知识库自动同步 (对标 RagFlow 自动同步 / 阿里百炼数据源管理)
from api.admin.kb_sync_routes import router as admin_kb_sync_router  # noqa: E402
# Prompt 优化建议 (对标 Langfuse LLM Playground 交互测试)
from api.admin.prompt_optimization_routes import router as admin_prompt_optimization_router  # noqa: E402
# 模型负载均衡 (对标阿里百炼 AI 网关 GPU 感知负载均衡)
from api.admin.model_load_balancer_routes import router as admin_model_lb_router  # noqa: E402
from api.deps import AppState  # noqa: E402
from api.auth_routes import router as auth_router  # noqa: E402
from api.analytics_routes import router as analytics_router  # noqa: E402
from api.middleware import ApiKeyMiddleware, TenantMiddleware  # noqa: E402
from api.routes import router  # noqa: E402
# HR 评估增强: 360° 环评 + 校准会
from api.review_routes import router as review_router  # noqa: E402
from api.calibration_routes import router as calibration_router  # noqa: E402
# 提示词模板库 + Agent预设 (对标 LobeChat/Open WebUI 模板 + ChatGPT GPTs)
from api.preset_routes import router as preset_router  # noqa: E402
# 语音 TTS / STT API (OpenAI TTS + Whisper, 降级 Web Speech API)
from api.voice_routes import router as voice_router  # noqa: E402
# Artifacts 可视化 (对标 Claude Artifacts / ChatGPT Canvas)
from api.artifact_routes import router as artifact_router  # noqa: E402
# 对话式HR洞察 (自然语言查询 → SQL → 图表建议)
from api.insights_routes import router as insights_router  # noqa: E402
# Skills 系统 (对标 Claude Skills / Trae Skills)
from api.skill_routes import router as skill_router  # noqa: E402
# Chat 流式对话 API（移植 opencode session prompt + SSE 事件推送）
from api.chat import router as chat_router  # noqa: E402
# Evidence 引用 API（暴露 EvidenceRef 查询）
from api.evidence import router as evidence_router  # noqa: E402
# Webhook 接收路由（飞书/GitLab/自定义,无需 JWT,用签名/token 验证）
from api.webhook_routes import router as webhook_router  # noqa: E402
# 站内通知系统（列表/未读数/已读/删除）
from api.notification_routes import router as notification_router  # noqa: E402
# 数据导出 (评估/审计/分析/通知, CSV/Excel/JSON)
from api.export_routes import router as export_router  # noqa: E402
from core.config import get_settings  # noqa: E402
from core.database import close_db, init_db  # noqa: E402
from core.metrics import setup_metrics  # noqa: E402
from core.tracing import tracer  # noqa: E402

# P1-7：进程级 slowapi Limiter 实例（从 core.rate_limit 导入,避免与路由模块循环导入）
from core.rate_limit import (  # noqa: E402
    RateLimitExceeded,
    SlowAPIMiddleware,
    SLOWAPI_AVAILABLE,
    _rate_limit_exceeded_handler,
    limiter,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await init_db()
    app.state.app_state = AppState(settings)
    # 初始化全局 ToolRegistry (供 admin 工具配置 API 管理超时)
    try:
        from agent.tool_registry import ToolRegistry

        app.state.app_state.tool_registry = ToolRegistry(
            toolkit=app.state.app_state.toolkit, settings=settings
        )
    except Exception:
        pass
    # review 修复: 让 graph 节点复用 app_state 的 rerank_provider / feature_flag_service,
    # 避免每次 retrieve_context 都 new 新实例(导致 60s LRU 缓存失效 + 双实例并存)
    try:
        from agent.graph import set_app_state_for_graph
        set_app_state_for_graph(app.state.app_state)
    except Exception:
        pass
    # 启动定时任务调度器（APScheduler, 降级容错：启动失败不影响应用）
    try:
        from core.scheduler import TaskScheduler, set_scheduler

        _task_scheduler = TaskScheduler()
        await _task_scheduler.start()
        set_scheduler(_task_scheduler)
    except Exception:
        pass
    # 注册知识库自动同步定时任务（降级容错：注册失败不影响应用）
    try:
        from services.kb_sync_service import KbSyncService

        _kb_sync_service = KbSyncService()
        await _kb_sync_service._register_scheduler()
    except Exception:
        pass
    try:
        yield
    finally:
        # 停止定时任务调度器
        try:
            from core.scheduler import get_scheduler, set_scheduler

            _sched = get_scheduler()
            if _sched is not None:
                await _sched.stop()
                set_scheduler(None)
        except Exception:
            pass
        await app.state.app_state.close()
        try:
            tracer.close()
        except Exception:
            pass
        await close_db()


class LimitRequestBodyMiddleware(BaseHTTPMiddleware):
    """限制请求体大小，防止超大 JSON/Base64 附件导致内存耗尽。"""

    MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10 MB

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                length = int(content_length)
            except ValueError:
                length = 0
            if length > self.MAX_CONTENT_LENGTH:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "请求体超过 10MB 限制"},
                )
        return await call_next(request)


app = FastAPI(
    title="AgentValue-AI",
    description="AI 驱动员工价值量化与成长 Agent 系统",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(LimitRequestBodyMiddleware)

# P1-7：注册 slowapi 限流中间件与异常处理器（slowapi 可选依赖，未安装时降级跳过）
if SLOWAPI_AVAILABLE:
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        o.strip() for o in get_settings().cors_origins.split(",") if o.strip()
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "X-Trace-Id", "X-Tenant-Id"],
)

# 租户上下文中间件最后注册即最外层，确保请求进入业务层前已写入 contextvar
app.add_middleware(TenantMiddleware)
# API Key 认证中间件：从 X-API-Key header 校验外部调用方身份
app.add_middleware(ApiKeyMiddleware)

app.include_router(auth_router)
app.include_router(router)
app.include_router(analytics_router)
# HR 评估增强: 360° 环评 (POST/GET 评估环评邀请 + 评估人提交评分)
app.include_router(review_router)
# HR 评估增强: 校准会 (创建 / 添加校准项 / 调整分数 / 完成应用)
app.include_router(calibration_router)
# 提示词模板库 + Agent预设
app.include_router(preset_router)
# 语音 TTS / STT
app.include_router(voice_router)
# Artifacts 可视化 (Claude Artifacts / ChatGPT Canvas)
app.include_router(artifact_router)
# 对话式HR洞察 (自然语言 → SQL → 图表建议)
app.include_router(insights_router)
# Skills 系统 (Claude Skills / Trae Skills)
app.include_router(skill_router)
# Chat 流式对话（移植 opencode session/prompt + SSE）
app.include_router(chat_router)
# Evidence 引用查询（暴露 EvidenceRef 表）
app.include_router(evidence_router)
# P1 Prompt 管理: admin 端点 (Langfuse 风格的版本/Label/A_B/灰度/回滚)
app.include_router(admin_prompts_router)
# P1 工具管理: admin 端点 (LangChain @tool / ToolNode / MCP / ReAct Agent)
app.include_router(admin_tools_router)
# P1 调试增强: admin 端点 (prompt 版本追溯 / trace 查询 / 系统健康)
app.include_router(admin_debug_router)
# P2 深水区: Provider CRUD(24 端点,对标 Dify model-providers)
app.include_router(admin_providers_router)
# P2 深水区: Prompt Playground SSE(对标 Langfuse Playground)
app.include_router(admin_playground_router)
# P1-1: 知识库管理(文档 CRUD + 重建索引 + 检索测试台 + 分块配置)
app.include_router(admin_kb_router, prefix="/api/v1/admin/kb", tags=["admin-kb"])
# P2-2: Rerank Provider 测试台(对标 Dify Rerank)
app.include_router(
    admin_rerank_router,
    tags=["admin-rerank"],
)
# P3-1: 自定义工具上传(OpenAPI Schema 导入,对标 Dify Custom Tool)
app.include_router(
    admin_custom_tools_router,
    tags=["admin-custom-tools"],
)
# P3-2: Feature Flag 系统(应用级功能开关,对标 Langfuse Feature Flag)
app.include_router(
    admin_feature_flags_router,
    tags=["admin-feature-flags"],
)
# API Key 管理(外部调用方鉴权, CRUD + 轮换 + 用量统计)
app.include_router(
    admin_api_keys_router,
    tags=["admin-api-keys"],
)
# 用户管理 CRUD(列表/详情/更新/禁用/删除/批量导入)
app.include_router(
    admin_users_router,
    tags=["admin-users"],
)
# P4-1: 多 Agent 协作(supervisor 模式,对标 Coze Multi-Agent)
app.include_router(
    admin_multi_agent_router,
    tags=["admin-multi-agent"],
)
# P4-2: 工作流可视化编排(对标 Dify Workflow / Coze Bot 编排)
app.include_router(
    admin_workflows_router,
    tags=["admin-workflows"],
)
# P2-1: Token/成本趋势看板(Prometheus 时序聚合 + DB 评估统计)
app.include_router(
    admin_analytics.router,
    prefix="/api/v1/admin/analytics",
    tags=["admin-analytics"],
)
# 定时任务调度管理 (APScheduler, 增删改查 + 手动触发 + 执行历史)
app.include_router(
    admin_scheduler_router,
    tags=["admin-scheduler"],
)
# 混合检索管理 (向量 + BM25 全文检索 + RRF 融合 + 增量更新 + 检索配置)
app.include_router(
    admin_search_router,
    tags=["admin-search"],
)
# 租户配额管理 (日请求/token 配额 + 用量统计 + 重置)
app.include_router(
    admin_quota_router,
    tags=["admin-quota"],
)
# 成本预算告警 (月度/日度预算 + 阈值告警通知)
app.include_router(
    admin_budget_router,
    tags=["admin-budgets"],
)
# API 计费账单 (汇总 + 按用户/端点聚合 + CSV/JSON 导出)
app.include_router(
    admin_billing_router,
    tags=["admin-billing"],
)
# Agent 版本管理 (版本 CRUD + 发布 + 回滚 + 对比 + 归档)
app.include_router(
    admin_agent_version_router,
    tags=["admin-agent-version"],
)
# 多渠道发布 (飞书/微信/钉钉/Web/API)
app.include_router(
    admin_publish_router,
    tags=["admin-publish"],
)
# 工具配置 (超时管理)
app.include_router(
    admin_tool_config_router,
    tags=["admin-tool-config"],
)
# 敏感词字典管理 (增删改查 + 文本审核 + 导入导出)
app.include_router(
    admin_sensitive_word_router,
    tags=["admin-sensitive-words"],
)
# 告警通知通道 (创建/通知/确认/解决/统计)
app.include_router(
    admin_alert_router,
    tags=["admin-alerts"],
)
# 模型 Fallback 策略 (对标阿里百炼 AI 网关秒级容灾)
app.include_router(
    admin_model_fallback_router,
    tags=["admin-model-fallback"],
)
# 会话分析看板 (对标 Langfuse Token 分析 / Dashboard)
app.include_router(
    admin_analytics_v2_router,
    tags=["admin-analytics-v2"],
)
# API 健康监控 (对标 Langfuse 延迟监控 / 告警系统)
app.include_router(
    admin_api_health_router,
    tags=["admin-api-health"],
)
# 数据集管理 (对标 Langfuse 数据集管理 + 阿里百炼训练集/评测集)
app.include_router(
    admin_dataset_router,
    tags=["admin-datasets"],
)
# LLM-as-a-Judge 自动评测 (对标 Langfuse LLM-as-a-Judge + Dify 日志回放)
app.include_router(
    admin_llm_judge_router,
    tags=["admin-llm-judge"],
)
# RAG 质量评测 (对标 RagFlow 检索测试 + 压力测试)
app.include_router(
    admin_rag_eval_router,
    tags=["admin-rag-eval"],
)
# 人工标注工具 (对标 Langfuse Human-in-the-loop)
app.include_router(
    admin_annotation_router,
    tags=["admin-annotations"],
)
# SSO 单点登录 (对标 Dify SSO / Bisheng SSO, OAuth2/SAML/LDAP)
app.include_router(
    admin_sso_router,
    tags=["admin-sso"],
)
# Agent 模板市场 (对标 Coze 插件市场 / LobeChat 助手市场)
app.include_router(
    admin_agent_template_router,
    tags=["admin-agent-templates"],
)
# NL2SQL 自然语言转 SQL (对标 RagFlow NL2SQL)
app.include_router(
    admin_nl2sql_router,
    tags=["admin-nl2sql"],
)
# 深度文档解析 (对标 RagFlow DeepDoc, 表格提取 + 版面分析)
app.include_router(
    admin_doc_parsing_router,
    tags=["admin-doc-parsing"],
)
# GraphRAG 知识图谱 (对标 RagFlow GraphRAG + RAPTOR, 实体关系抽取 + 图增强检索)
app.include_router(
    admin_graph_rag_router,
    tags=["admin-graph-rag"],
)
# 灰度发布 / 蓝绿部署 (对标 Bisheng/Langfuse Canary 发布)
app.include_router(
    admin_gray_release_router,
    tags=["admin-gray-release"],
)
# 多环境管理 (对标 Bisheng/Langfuse 环境隔离, dev/staging/prod 配置隔离)
app.include_router(
    admin_environment_router,
    tags=["admin-environments"],
)
# 知识库自动同步 (对标 RagFlow 自动同步 / 阿里百炼数据源管理)
app.include_router(
    admin_kb_sync_router,
    tags=["admin-kb-sync"],
)
# Prompt 优化建议 (对标 Langfuse LLM Playground 交互测试)
app.include_router(
    admin_prompt_optimization_router,
    tags=["admin-prompt-optimization"],
)
# 模型负载均衡 (对标阿里百炼 AI 网关 GPU 感知负载均衡)
app.include_router(
    admin_model_lb_router,
    tags=["admin-model-lb"],
)
# 数据导出 (评估/审计/分析/通知, CSV/Excel/JSON)
app.include_router(
    export_router,
    tags=["export"],
)
# Webhook 接收路由(飞书/GitLab/自定义,无需 JWT,用签名/token 验证)
app.include_router(webhook_router)
# 站内通知系统(列表/未读数/已读/删除)
app.include_router(notification_router)

# 挂载 Prometheus 指标端点（/metrics，无需鉴权，供 Prometheus 抓取）
setup_metrics(app)


@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.get("/livez")
async def livez():
    """Liveness probe: 进程存活即可,不查依赖"""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(request: Request):
    """Readiness probe: 检查 DB / Redis / 至少一个 Provider 可达,任一可达即 200(降级),全 fail 返 503"""
    from sqlalchemy import text

    from core.database import AsyncSessionLocal

    checks = {"db": False, "redis": False, "provider": False}

    # DB SELECT 1
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        checks["db"] = True
    except Exception:
        pass

    # Redis ping(若配置了 redis_url)
    app_state: AppState | None = getattr(request.app.state, "app_state", None)
    if app_state and app_state.settings.redis_url:
        try:
            import redis.asyncio as aioredis

            r = aioredis.from_url(app_state.settings.redis_url)
            await r.ping()
            await r.aclose()
            checks["redis"] = True
        except Exception:
            pass
    else:
        # 未配置 redis 时视作可选,不影响 ready
        checks["redis"] = True

    # 至少一个 Provider 可达
    if app_state:
        try:
            for tier in ("L0", "L1", "L2", "L3"):
                try:
                    # get_provider 为同步方法,不可 await（P0-1 修复：去掉误加的 await）
                    provider = app_state.model_router.get_provider(tier)
                    ok = await provider.health_check()
                    if ok:
                        checks["provider"] = True
                        break
                except Exception:
                    continue
        except Exception:
            pass

    if not any(checks.values()):
        return JSONResponse(
            status_code=503, content={"status": "not ready", "checks": checks}
        )
    # 部分降级时仍返 200 但标注 degraded
    degraded = not all(checks.values())
    return {"status": "degraded" if degraded else "ok", "checks": checks}


@app.get("/healthz")
async def healthz(request: Request):
    """聚合端点(K8s 旧命名兼容),等价 readyz"""
    return await readyz(request)
