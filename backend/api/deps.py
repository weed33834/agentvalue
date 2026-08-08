"""
FastAPI 依赖注入
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.graph import create_evaluation_graph
from auth.rbac import Role
from agent.prompt_loader import PromptLoader
from agent.tools import AgentToolkit, CompanyKB, MemoryStore
from core.config import Settings
from core.database import get_db
from core.feature_flag import FeatureFlagService
from core.model_router import ModelRouter
from core.multimodal import MultimodalCleaner
from core.tenant_context import get_current_tenant
from memory.vector_store import ChromaCompanyKB, ChromaMemoryStore
from models.models import DEFAULT_TENANT_ID
from services.approval_service import ApprovalService
from services.audit_service import AuditService
from services.evaluation_service import EvaluationService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WS-3：API Key Scope 权限词表与依赖
# ---------------------------------------------------------------------------

#: API Key scope 词表。key 为 scope 名，value 为中文描述（供 admin UI / 文档展示）。
#: 约定 ``<资源>:<动作>`` 两段式，动作固定为 read / write / invoke。
#: 通配符：``*`` 代表全部权限；``evaluations:*`` 代表该资源下全部动作。
API_KEY_SCOPES: dict[str, str] = {
    "*": "全部权限（超级 Key，慎用）",
    "evaluations:read": "读取评估记录",
    "evaluations:write": "创建/提交评估",
    "agents:read": "读取 Agent 列表与配置",
    "agents:invoke": "调用 Agent 执行",
    "datasets:read": "读取数据集与数据项",
    "traces:read": "读取调用链路追踪数据",
    "webhooks:read": "读取 Webhook 订阅与投递记录",
    "webhooks:write": "管理 Webhook 订阅",
}

#: 未显式配置 scopes 的历史 Key 的兜底权限（只读，最小权限原则）。
DEFAULT_API_KEY_SCOPES: tuple[str, ...] = (
    "evaluations:read",
    "agents:read",
    "datasets:read",
)


@dataclass
class ApiKeyPrincipal:
    """通过 API Key 鉴权后的调用方身份

    由 :func:`require_api_key` 注入到公共 API 端点，
    携带租户、scope 与限流配额，供端点做数据隔离与配额判断。
    """

    key_id: str
    tenant_id: str
    name: str
    scopes: list[str] = field(default_factory=list)
    rate_limit_per_minute: int = 60

    def has_scope(self, scope: str) -> bool:
        """判断当前 Key 是否覆盖指定 scope（支持 ``*`` 与 ``资源:*`` 通配）"""
        return scope_satisfied(self.scopes, scope)


def parse_api_key_scopes(raw: str | None) -> list[str]:
    """解析 ApiKey.scopes 字段（Text 存 JSON 数组，兼容逗号分隔的历史数据）

    解析失败或为空时返回 :data:`DEFAULT_API_KEY_SCOPES`，保证老 Key 仍可只读访问。
    """
    if not raw:
        return list(DEFAULT_API_KEY_SCOPES)
    text = raw.strip()
    if not text:
        return list(DEFAULT_API_KEY_SCOPES)
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            logger.warning("API Key scopes 解析失败,降级为默认只读 scope")
            return list(DEFAULT_API_KEY_SCOPES)
        if isinstance(parsed, list):
            scopes = [str(s).strip() for s in parsed if str(s).strip()]
            return scopes or list(DEFAULT_API_KEY_SCOPES)
        return list(DEFAULT_API_KEY_SCOPES)
    # 兼容 "a,b,c" / "a b c" 形式
    separator = "," if "," in text else " "
    scopes = [s.strip() for s in text.split(separator) if s.strip()]
    return scopes or list(DEFAULT_API_KEY_SCOPES)


def scope_satisfied(granted: list[str] | tuple[str, ...], required: str) -> bool:
    """判断 granted 集合是否覆盖 required 这一个 scope

    匹配规则（按优先级）：
    1. ``*`` —— 覆盖一切；
    2. 完全相等；
    3. ``资源:*`` —— 覆盖该资源下全部动作。
    """
    if not required:
        return True
    resource = required.split(":", 1)[0]
    for item in granted:
        if item == "*" or item == required:
            return True
        if item.endswith(":*") and item[:-2] == resource:
            return True
    return False


async def _load_api_key_principal(
    request: Request, session: AsyncSession
) -> ApiKeyPrincipal:
    """从 request.state.api_key_id 还原调用方身份并做实时有效性校验

    ``ApiKeyMiddleware`` 已完成 header 提取与 hash 比对，并注入 ``api_key_id``；
    这里再查一次库是为了拿到 scopes，并绕过中间件 30s 进程缓存，
    使吊销 / 过期能即时生效。
    """
    key_id = getattr(request.state, "api_key_id", None)
    if not key_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少有效的 API Key，请在 X-API-Key 请求头中携带",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    from models.models import ApiKey

    result = await session.execute(select(ApiKey).where(ApiKey.key_id == key_id))
    api_key = result.scalar_one_or_none()
    if api_key is None or not api_key.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API Key 无效或已吊销",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    if api_key.expires_at is not None:
        expires_at = api_key.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API Key 已过期",
                headers={"WWW-Authenticate": "ApiKey"},
            )

    return ApiKeyPrincipal(
        key_id=api_key.key_id,
        tenant_id=api_key.tenant_id,
        name=api_key.name,
        scopes=parse_api_key_scopes(api_key.scopes),
        rate_limit_per_minute=api_key.rate_limit or 60,
    )


def require_api_key(*required_scopes: str) -> Callable[..., Any]:
    """构造一个校验 API Key + scope 的 FastAPI 依赖

    用法::

        @router.get("/evaluations", dependencies=[])
        async def list_evaluations(
            principal: ApiKeyPrincipal = Depends(require_api_key("evaluations:read")),
        ): ...

    行为：
    - 未携带 / 无效 / 已吊销 / 已过期 → 401；
    - 携带有效 Key 但 scope 不足 → 403，并在 detail 中列出缺失 scope；
    - 不传 scope 参数时仅校验 Key 有效性。

    注意：本依赖只认 API Key，不接受 JWT，用于 ``/api/public/v1`` 这类对外接口。
    """
    for scope in required_scopes:
        if scope not in API_KEY_SCOPES:
            # 仅告警不阻断，允许业务先行定义新 scope 再补词表
            logger.warning("require_api_key 使用了未登记的 scope: %s", scope)

    async def _dependency(
        request: Request,
        session: AsyncSession = Depends(get_db),
    ) -> ApiKeyPrincipal:
        principal = await _load_api_key_principal(request, session)
        missing = [s for s in required_scopes if not principal.has_scope(s)]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API Key 权限不足，缺少 scope: {', '.join(missing)}",
            )
        return principal

    return _dependency


class AppState:
    """应用级共享状态（无请求级数据库会话）"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._settings_lock = asyncio.Lock()
        self.model_router = ModelRouter(settings)
        self.prompt_loader = PromptLoader()
        # default 租户的向量库实例，单租户/测试场景直接复用
        self.memory_store = ChromaMemoryStore(
            settings=settings, tenant_id=DEFAULT_TENANT_ID
        )
        self.company_kb = ChromaCompanyKB(
            settings=settings, tenant_id=DEFAULT_TENANT_ID
        )
        self.multimodal_cleaner = MultimodalCleaner(
            ocr_api_key=settings.ocr_cloud_api_key,
            ocr_base_url=settings.ocr_cloud_base_url,
            ocr_model=settings.ocr_cloud_model,
            asr_api_key=settings.asr_cloud_api_key,
            asr_base_url=settings.asr_cloud_base_url,
            asr_model=settings.asr_cloud_model,
            # P0-3 修复: 注入 vision_callable 让 CloudOCR 走 ModelRouter 的 Provider 抽象,
            # 复用 LLM 档位降级链路(而非 CloudOCR 内部独立建 client)
            vision_callable=self._build_vision_callable(),
        )
        # AgentToolkit: 封装 memory + kb, 供 ToolRegistry 加载 toolkit 依赖工具
        from agent.tools import AgentToolkit

        self.toolkit = AgentToolkit(memory=self.memory_store, kb=self.company_kb)
        # P2-2: 缓存 rerank provider 实例 (类似 model_router 模式)
        # BGE 依赖缺失或凭证缺失时工厂内部降级 Dummy, 不抛异常
        self.rerank_provider = None
        try:
            from core.providers.rerank_factory import create_rerank_provider

            self.rerank_provider = create_rerank_provider(settings)
        except Exception:
            logger.warning("rerank provider 初始化失败, 降级 Dummy", exc_info=True)
            from core.providers.rerank_provider import DummyRerankProvider

            self.rerank_provider = DummyRerankProvider()
        # 非默认租户的向量库实例缓存，按 tenant_id 懒加载
        self._tenant_memory_stores: dict[str, MemoryStore] = {}
        self._tenant_kb_stores: dict[str, CompanyKB] = {}
        # P4-1: 多 Agent 协作图缓存 (类似 _interrupt_graphs 模式, 按租户惰性创建)
        # 每个 tenant_id 一份独立实例 (含独立 checkpointer), 避免跨租户 thread_id 状态串扰
        self._multi_agent_graphs: dict[str, Any] = {}
        # P3-2: Feature Flag 服务 (应用级功能开关, 60s LRU 缓存)
        # 延迟 import 避免循环依赖 (database 已在模块顶部加载)
        from core.database import AsyncSessionLocal

        self.feature_flag_service = FeatureFlagService(AsyncSessionLocal)
        # 全局 ToolRegistry 实例 (供 admin 工具配置 API 管理超时, 懒加载)
        self.tool_registry = None

    def _build_vision_callable(self):
        """构造 vision_callable,封装 ModelRouter 的 vision_completion 调用。

        闭包形式,延迟到首次调用时拿 provider,避免启动时 ModelRouter 未就绪。
        若云端档位不可用,返回 None(让 CloudOCR 降级走自带 client 或标记复核)。
        """

        async def _vision(prompt: str, image_data: str) -> str:
            try:
                provider, _ = await self.model_router.get_provider_with_fallback()
                return await provider.vision_completion(
                    prompt=prompt, image_data=image_data, is_url=False
                )
            except Exception:
                logger.warning("vision_callable 调用失败,降级返回 None", exc_info=True)
                return None  # 返回 None,CloudOCR 走自带降级

        return _vision

    async def close(self) -> None:
        """关闭应用级资源（向量库客户端、embedding 客户端等）"""
        for store in (self.memory_store, self.company_kb):
            try:
                if hasattr(store, "close"):
                    await store.close()
            except Exception:
                logger.debug("关闭 store 失败: %s", store, exc_info=True)
        for store in list(self._tenant_memory_stores.values()):
            try:
                if hasattr(store, "close"):
                    await store.close()
            except Exception:
                logger.debug("关闭租户 memory store 失败: %s", store, exc_info=True)
        for store in list(self._tenant_kb_stores.values()):
            try:
                if hasattr(store, "close"):
                    await store.close()
            except Exception:
                logger.debug("关闭租户 kb store 失败: %s", store, exc_info=True)

    def get_memory_store(self, tenant_id: str | None = None) -> MemoryStore:
        """按租户获取长期记忆向量库：default 直接复用单例，其他租户懒加载并缓存"""
        if tenant_id is None:
            tenant_id = get_current_tenant()
        if tenant_id == DEFAULT_TENANT_ID:
            return self.memory_store
        if tenant_id not in self._tenant_memory_stores:
            self._tenant_memory_stores[tenant_id] = ChromaMemoryStore(
                settings=self.settings, tenant_id=tenant_id
            )
        return self._tenant_memory_stores[tenant_id]

    def get_kb_store(self, tenant_id: str | None = None) -> CompanyKB:
        """按租户获取公司知识库向量库：default 直接复用单例，其他租户懒加载并缓存"""
        if tenant_id is None:
            tenant_id = get_current_tenant()
        if tenant_id == DEFAULT_TENANT_ID:
            return self.company_kb
        if tenant_id not in self._tenant_kb_stores:
            self._tenant_kb_stores[tenant_id] = ChromaCompanyKB(
                settings=self.settings, tenant_id=tenant_id
            )
        return self._tenant_kb_stores[tenant_id]

    def get_graph(self, eval_service: EvaluationService, tenant_id: str | None = None):
        """创建并返回一个与当前数据库会话绑定的 LangGraph 实例。

        toolkit 按当前租户选择对应向量库 collection，实现记忆/知识库的租户隔离。

        P1 调试增强: 传入 DbPromptLoader (懒加载单例),使 graph 的 build_prompt
        节点优先从 DB 加载 prompt (支持 A/B / 灰度 / 版本管理),并绑定版本到 trace。
        DbPromptLoader 在 DB 不可达时自动回退文件 PromptLoader,故无需额外 try/except。
        """
        if tenant_id is None:
            tenant_id = get_current_tenant()
        toolkit = AgentToolkit(
            memory=self.get_memory_store(tenant_id),
            kb=self.get_kb_store(tenant_id),
        )
        # 懒加载 DbPromptLoader 单例 (避免启动时 DB 未就绪)
        db_prompt_loader = None
        try:
            from agent.db_prompt_loader import get_global_db_prompt_loader

            db_prompt_loader = get_global_db_prompt_loader()
        except Exception:
            logger.debug("DbPromptLoader 不可用,graph 使用文件 PromptLoader")
        return create_evaluation_graph(
            toolkit=toolkit,
            model_router=self.model_router,
            prompt_loader=self.prompt_loader,
            multimodal_cleaner=self.multimodal_cleaner,
            db_prompt_loader=db_prompt_loader,
        )


def get_app_state(request: Request) -> AppState:
    return request.app.state.app_state


def get_approval_service(session: AsyncSession = Depends(get_db)) -> ApprovalService:
    return ApprovalService(session)


def get_audit_service(session: AsyncSession = Depends(get_db)) -> AuditService:
    return AuditService(session)


def get_evaluation_service(
    session: AsyncSession = Depends(get_db),
) -> EvaluationService:
    return EvaluationService(session)


async def assert_manager_team_access(
    eval_service: EvaluationService,
    role: Role,
    employee_id: str,
    current_user_id: str,
    detail: str = "无权操作非直属下属的评估",
) -> None:
    """H7：主管越权校验。manager 仅能操作/查看自己名下直属下属,HR/ADMIN 不受限。

    下属未配置 manager_id 时放行(兼容历史数据);员工不存在时不阻断(由上游处理)。
    由 api/routes.py 与 api/analytics_routes.py 共享,避免两处重复实现。
    detail 为越权时抛出的错误描述:routes 默认对应评估操作语境,
    analytics 路由传入"无权查看非直属下属的成长路径"以匹配其语义。

    Args:
        eval_service: 评估服务,用于查 user。
        role: 当前用户角色,非 MANAGER 直接放行。
        employee_id: 被访问员工 ID。
        current_user_id: 当前登录用户 ID。
        detail: 越权时 HTTP 403 的 detail 文案。
    """
    if role != Role.MANAGER:
        return
    employee = await eval_service.get_user(employee_id)
    if employee is None:
        # 员工不存在由上游处理，这里不阻断
        return
    if employee.manager_id and employee.manager_id != current_user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=detail,
        )
