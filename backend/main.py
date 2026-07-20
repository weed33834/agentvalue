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
# P4-1: 多 Agent 协作(supervisor 模式,对标 Coze Multi-Agent)
from api.admin.multi_agent import router as admin_multi_agent_router  # noqa: E402
# P4-2: 工作流可视化编排(对标 Dify Workflow / Coze Bot 编排)
from api.admin.workflows import router as admin_workflows_router  # noqa: E402
# P2-1: Token/成本趋势看板(Prometheus 时序聚合 + DB 评估统计)
from api.admin import analytics as admin_analytics  # noqa: E402
from api.deps import AppState  # noqa: E402
from api.auth_routes import router as auth_router  # noqa: E402
from api.analytics_routes import router as analytics_router  # noqa: E402
from api.middleware import TenantMiddleware  # noqa: E402
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
    # review 修复: 让 graph 节点复用 app_state 的 rerank_provider / feature_flag_service,
    # 避免每次 retrieve_context 都 new 新实例(导致 60s LRU 缓存失效 + 双实例并存)
    try:
        from agent.graph import set_app_state_for_graph
        set_app_state_for_graph(app.state.app_state)
    except Exception:
        pass
    try:
        yield
    finally:
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
