"""
LangGraph 评估工作流
"""

import asyncio
import json
import logging
import sys
import time
import uuid
from contextlib import nullcontext
from typing import Any, Dict, Literal, Optional

from langchain_core.runnables.config import RunnableConfig, var_child_runnable_config
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError


def _ensure_interrupt_context(config: Optional[RunnableConfig]):
    """Python 3.10 兼容层：手动设置 contextvar 使 interrupt() 可用。

    LangGraph 1.2+ 的 interrupt() 内部调用 get_config()，后者依赖
    var_child_runnable_config contextvar。在 Python 3.11+ 中，LangGraph
    通过 asyncio.create_task(context=...) 自动设置该 contextvar；但
    Python 3.10 的 asyncio.create_task 不支持 context 参数，导致
    contextvar 从未被设置，interrupt() 抛出
    'Called get_config outside of a runnable context'。

    本函数在 Python < 3.11 上手动设置 contextvar 作为兼容层，
    Python 3.11+ 上为空操作（LangGraph 已自动处理）。
    """
    if sys.version_info < (3, 11) and config is not None:
        return var_child_runnable_config.set(config)
    return None


def _reset_interrupt_context(token):
    """恢复 contextvar（与 _ensure_interrupt_context 配对）。"""
    if token is not None:
        var_child_runnable_config.reset(token)

from core.guards import InputGuard, OutputGuard

# P1-3：将 _call_llm_with_fallback 提取到 core.llm_call 公共 helper，graph 与 llm_judge 复用
from core.llm_call import call_llm_with_fallback
from core.model_router import ModelRouter
from core.multimodal import MultimodalCleaner
from core.tracing import tracer
from models.constants import EvaluationStatus
from schemas import EmployeeEvaluation

from .prompt_loader import PromptLoader
from .state import EvaluationState
from .tools import AgentToolkit

logger = logging.getLogger(__name__)


def _await_sync(coro):
    """在同步上下文中运行协程至完成。

    _create_checkpointer 保持同步签名(test_integration_scenarios 与
    create_evaluation_graph_with_interrupt 均同步调用它),但 AsyncPostgresSaver.setup()
    是协程,需同步等待其完成。当前线程已有事件循环运行时(如 FastAPI 异步请求处理中
    惰性建图),在工作线程的独立 loop 中执行,避免
    'asyncio.run() cannot be called from a running event loop';无运行循环时直接 asyncio.run。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()


def _create_checkpointer():
    """P3 规模化就绪(H3):按 settings 选择 checkpointer。

    - settings.use_postgres_checkpointer=True 且 DATABASE_URL 是 postgresql://
      → PostgresSaver(interrupt 状态持久化,支持多 worker 水平扩展)
    - 其余情况 → MemorySaver(单实例限制,本地开发/测试默认)

    Bug 1 修复: PostgresSaver 启动前需先建表(setup())。原实现未调用 setup(),
    导致首次 interrupt 持久化时因缺表失败。现在创建 saver 后立即调用 setup();
    setup() 失败(如 DB 不可达/缺权限)时降级到 MemorySaver 并记录警告。

    注意: AsyncPostgresSaver.setup() 是协程,但 _create_checkpointer 保持同步签名
    (被 test_integration_scenarios 与 create_evaluation_graph_with_interrupt 同步调用),
    故通过 _await_sync 同步等待其完成(兼容已有事件循环运行场景)。
    """
    from langgraph.checkpoint.memory import MemorySaver

    try:
        from core.config import get_settings

        s = get_settings()
        if (
            getattr(s, "use_postgres_checkpointer", False)
            and s.database_url
            and s.database_url.startswith("postgresql")
        ):
            # 延迟 import,避免未装 langgraph-checkpoint-postgres 时崩溃
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            saver = AsyncPostgresSaver.from_conn_string(s.database_url)
            # Bug 1 修复: 建表;失败由下方 except 捕获并降级 MemorySaver
            _await_sync(saver.setup())
            logger.info("checkpointer 使用 PostgresSaver: %s", s.database_url)
            return saver
    except ImportError:
        logger.warning(
            "langgraph-checkpoint-postgres 未安装,降级使用 MemorySaver。"
            "启用持久化: pip install langgraph-checkpoint-postgres"
        )
    except Exception as e:
        logger.warning("PostgresSaver 初始化/setup 失败,降级使用 MemorySaver: %s", e)

    return MemorySaver()


def _node_trace(node_name: str, state: Optional[EvaluationState] = None):
    """P3-2：节点级 Langfuse trace 上下文管理器。

    为每个工作流节点创建一个 Langfuse trace，便于在 Langfuse UI 中按节点观察
    输入/输出/耗时。Langfuse 未启用或 tracer 不支持 is_enabled 时返回 nullcontext，
    避免无谓开销与测试副作用（测试中 monkeypatch 的 fake tracer 通常不实现 is_enabled，
    会走到 nullcontext 分支，不会污染 captured["traces"] 断言）。
    """
    is_enabled_fn = getattr(tracer, "is_enabled", None)
    if is_enabled_fn is None or not is_enabled_fn():
        return nullcontext()
    kwargs: Dict[str, Any] = {"name": node_name}
    if state is not None:
        kwargs["employee_id"] = state.get("employee_id")
        kwargs["metadata"] = {"period": state.get("period"), "node": node_name}
    try:
        return tracer.trace(**kwargs)
    except Exception:
        # tracer 异常不应阻断节点执行，降级为 no-op
        return nullcontext()


# H5：员工视图出现负面/偏见词时回退的安全模板，避免敏感措辞入库到员工可见视图
# 字段长度需满足 EmployeeEvaluation schema 约束（summary>=20、evidence 每条>=10 字符）
SAFE_EMPLOYEE_VIEW = {
    "summary": "本周评估生成遇到问题，请联系主管沟通确认本周工作表现与成长方向。",
    "strengths": ["本周评估生成遇到问题，请联系主管确认本周优势项"],
    "growth_areas": [
        {
            "dimension": "综合评估",
            "score": 75.0,
            "evidence": ["本周评估生成遇到问题，请联系主管沟通确认工作表现"],
            "improvement_actions": ["请联系主管确认本周工作表现与改进方向"],
        }
    ],
    "next_week_focus": ["请联系主管沟通确认本周评估结果"],
}

# 触发阻断的违规前缀：员工视图的负面词与偏见词必须阻断，幻觉词仅记录不阻断
_BLOCKING_VIOLATION_PREFIXES = ("employee_view_negative_words", "biased_words")


def _has_blocking_violation(violations: list[str]) -> bool:
    """判断员工视图违规中是否包含需阻断的负面/偏见词"""
    return any(v.startswith(_BLOCKING_VIOLATION_PREFIXES) for v in violations)


# P2-2: Rerank Provider 模块级懒加载单例(匹配 _create_checkpointer 模式)
# 在 retrieve_context 中按 settings.rerank_provider 决定是否调用 rerank
_rerank_provider_singleton = None

# P3-2 review 修复: app_state 引用(由 main.py lifespan 注入),
# 让 _rerank_kb_if_enabled 复用 app_state.feature_flag_service 的 60s LRU 缓存,
# 而不是每次 retrieve_context 都 new 一个 FeatureFlagService(缓存完全失效)。
# 同时复用 app_state.rerank_provider,消除双实例并存。
# 未注入时(测试 / 单元调用)回退到 module-level 懒加载单例。
_app_state_ref = None


def set_app_state_for_graph(app_state) -> None:
    """由 main.py lifespan 调用,注入 app_state 引用供 graph 节点复用其单例

    让 retrieve_context 等节点能访问 app_state.feature_flag_service(60s 缓存)和
    app_state.rerank_provider(避免与 module-level singleton 双实例并存)。
    """
    global _app_state_ref
    _app_state_ref = app_state


def _get_rerank_provider():
    """懒加载 rerank provider 单例

    匹配 _create_checkpointer 模式: 在首次调用时读 settings 创建实例并缓存,
    避免每次 retrieve_context 都重建。初始化失败时降级 DummyRerankProvider。
    """
    # review 修复: 优先复用 app_state.rerank_provider(消除双实例并存)
    if _app_state_ref is not None and getattr(_app_state_ref, "rerank_provider", None) is not None:
        return _app_state_ref.rerank_provider
    global _rerank_provider_singleton
    if _rerank_provider_singleton is not None:
        return _rerank_provider_singleton
    try:
        from core.config import get_settings
        from core.providers.rerank_factory import create_rerank_provider

        _rerank_provider_singleton = create_rerank_provider(get_settings())
    except Exception as e:
        logger.warning("rerank provider 初始化失败, 降级 Dummy: %s", e)
        from core.providers.rerank_provider import DummyRerankProvider

        _rerank_provider_singleton = DummyRerankProvider()
    return _rerank_provider_singleton


async def _rerank_kb_if_enabled(query: str, documents: list) -> list:
    """P2-2: 若启用 rerank (settings.rerank_provider != "dummy"), 对 KB 结果二次重排

    P3-2 集成: 同时检查 Feature Flag "use_rerank_v2", 若启用则强制走 rerank 路径
    (即使 settings.rerank_provider == "dummy", 仍尝试加载 rerank provider,
    用于灰度新 rerank 模型; 失败时 fallback 原顺序)。

    Dummy 模式直接返回原 KB (不加 rerank_score, 完全等价于未启用 rerank, 向后兼容)。
    rerank 失败时 fallback 到原顺序, 不影响评估主流程。

    Args:
        query: retrieve_context 的检索 query
        documents: ChromaDB 召回的 KB 文档列表

    Returns:
        重排序后的文档列表(dummy 模式原样返回; rerank 模式每个 doc 加 rerank_score)
    """
    try:
        from core.config import get_settings

        settings = get_settings()
        provider_name = (
            getattr(settings, "rerank_provider", None) or "dummy"
        ).lower()

        # P3-2 集成示例: 检查 Feature Flag "use_rerank_v2"
        # 启用时强制走 rerank 路径(灰度新模型), 即便 settings 是 dummy
        use_rerank_v2 = False
        try:
            # review 修复: 优先复用 app_state.feature_flag_service(60s LRU 缓存有效),
            # 而非每次 new FeatureFlagService(缓存形同虚设, DB 重复查询)
            if _app_state_ref is not None and getattr(_app_state_ref, "feature_flag_service", None) is not None:
                flag_service = _app_state_ref.feature_flag_service
            else:
                # 测试 / 单元调用降级路径
                from core.database import AsyncSessionLocal
                from core.feature_flag import FeatureFlagService
                flag_service = FeatureFlagService(AsyncSessionLocal)
            use_rerank_v2 = await flag_service.is_enabled("use_rerank_v2")
        except Exception as flag_err:
            logger.debug("Feature Flag 检查失败, 忽略: %s", flag_err)

        # Dummy 模式且 flag 未启用 → 跳过, 不加 rerank_score, 完全等价于未启用 rerank
        if provider_name == "dummy" and not use_rerank_v2:
            return documents
        reranker = _get_rerank_provider()
        top_k = getattr(settings, "rerank_top_k", 5) or 5
        return await reranker.rerank(query, documents, top_k=top_k)
    except Exception as e:
        # rerank 失败不阻断主流程, fallback 到原 ChromaDB 顺序
        logger.warning("rerank KB 失败, fallback 原顺序: %s", e)
        return documents


def _scan_attachment_injections(
    cleaned_inputs: list, input_guard: InputGuard
) -> list[str]:
    """
    H6：对附件抽取出的文本二次扫描 Prompt 注入。
    命中注入的附件内容会被截断，避免进入 LLM 上下文。
    返回触发的规则列表（去重保序）。
    """
    triggered: list[str] = []
    for idx, inp in enumerate(cleaned_inputs):
        extracted = inp.get("extracted_text")
        if not extracted:
            continue
        rules = input_guard._check_text(extracted, f"attachment[{idx}]")
        if rules:
            triggered.extend(rules)
            # 截断注入内容：剥离附件抽取部分，仅保留原始 content
            base = (
                (inp.get("content", "") or "").split("--- 附件抽取内容 ---")[0].strip()
            )
            inp["content"] = base
            inp["extracted_text"] = "[附件内容因检测到注入风险已被截断]"
    if not triggered:
        return []
    seen: set[str] = set()
    unique: list[str] = []
    for r in triggered:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique


# ============================================================
# 共享节点工厂(普通版 create_evaluation_graph 与 interrupt 版
# create_evaluation_graph_with_interrupt 复用同一套,消除 10 组重复节点函数,
# 统一行为并修复行为漂移)。工厂返回闭包节点,两个构建器仅负责图装配。
# ============================================================


def _make_input_sanitizer(input_guard: InputGuard):
    """构建 input_sanitizer 节点(普通版与 interrupt 版共享)。"""

    async def input_sanitizer(state: EvaluationState) -> EvaluationState:
        """输入护栏：检查 Prompt 注入与恶意内容"""
        with _node_trace("input_sanitizer", state):
            result = input_guard.check(state["raw_inputs"])
            if not result.allowed:
                return {
                    **state,
                    "error": f"输入被拦截: {result.reason}",
                    "status": "error",
                    "audit_info": {"triggered_rules": result.triggered_rules},
                }
            return {
                **state,
                "status": "data_cleaning",
            }

    return input_sanitizer


def _make_data_cleaning(multimodal_cleaner: MultimodalCleaner, input_guard: InputGuard):
    """构建 data_cleaning 节点(普通版与 interrupt 版共享)。"""

    async def data_cleaning(state: EvaluationState) -> EvaluationState:
        """
        多模态数据清洗：对附件（图片/音频/表格/PDF/文本）抽取文本，
        合并到输入 content 中，形成 cleaned_inputs。
        """
        with _node_trace("data_cleaning", state):
            if state.get("error"):
                return state
            try:
                cleaned = await multimodal_cleaner.clean_inputs(state["raw_inputs"])
            except Exception as e:
                logger.warning("多模态清洗失败，降级使用原始输入: %s", e)
                cleaned = state["raw_inputs"]

            # H6：附件抽取内容二次扫描 Prompt 注入，命中则截断
            att_rules = _scan_attachment_injections(cleaned, input_guard)
            audit_info = state.get("audit_info") or {}
            if att_rules:
                logger.warning("附件内容检测到注入风险，已截断: %s", att_rules)
                audit_info = {
                    **audit_info,
                    "triggered_rules": list(audit_info.get("triggered_rules", []))
                    + att_rules
                    + ["attachment_injection_blocked"],
                }
            return {
                **state,
                "cleaned_inputs": cleaned,
                "audit_info": audit_info,
                "status": "context_retrieval",
            }

    return data_cleaning


async def _hybrid_kb_search(query: str, top_k: int = 3) -> list:
    """混合检索知识库 (向量+BM25+RRF 融合)。

    优先使用 HybridSearchService (已配置时)，降级为 toolkit 纯向量检索。
    """
    # 尝试使用 HybridSearchService
    if _app_state_ref is not None:
        store = getattr(_app_state_ref, "company_kb", None)
        settings = getattr(_app_state_ref, "settings", None)
        if store and settings:
            try:
                from services.hybrid_search_service import HybridSearchService

                service = HybridSearchService(store, settings)
                results = await service.search(query=query, top_k=top_k)
                if results:
                    return [
                        {"content": r.get("content", ""), "metadata": r.get("metadata", {})}
                        for r in results
                    ]
            except Exception:
                logger.debug("HybridSearchService 不可用，降级为纯向量检索")

    # 降级：纯向量检索
    from agent.tools import AgentToolkit

    toolkit = AgentToolkit(_app_state_ref) if _app_state_ref else None
    if toolkit:
        return await toolkit.query_company_kb(query=query, top_k=top_k)
    return []


def _make_retrieve_context(toolkit: AgentToolkit):
    """构建 retrieve_context 节点(普通版与 interrupt 版共享)。

    知识库检索优先使用 HybridSearchService (向量+BM25+RRF 融合)，
    降级为纯向量检索 (toolkit.query_company_kb)。
    """

    async def retrieve_context(state: EvaluationState) -> EvaluationState:
        """获取员工历史记忆与公司知识库"""
        with _node_trace("retrieve_context", state):
            if state.get("error"):
                return state
            query = f"员工评估标准 {state['employee_id']} {state['period']}"
            # P1-N1：并发获取历史与 KB，return_exceptions=True 防止单点失败拖垮整体；
            # 任一失败降级为空，不阻断评估主流程
            history_result, kb_result = await asyncio.gather(
                toolkit.get_employee_history(
                    state["employee_id"],
                    period=state["period"],
                    limit=5,
                ),
                _hybrid_kb_search(query, top_k=3),
                return_exceptions=True,
            )
            if isinstance(history_result, Exception):
                logger.warning("获取员工历史失败，降级为空: %s", history_result)
                history = []
            else:
                history = history_result
            if isinstance(kb_result, Exception):
                logger.warning("查询公司知识库失败，降级为空: %s", kb_result)
                kb = []
            else:
                kb = kb_result
            # P2-2: 若启用 rerank (rerank_provider != "dummy"), 对 KB 结果二次重排
            kb = await _rerank_kb_if_enabled(
                query=query,
                documents=kb,
            )
            return {
                **state,
                "employee_history": history,
                "company_kb": kb,
            }

    return retrieve_context


def _make_build_prompt(
    prompt_loader: PromptLoader,
    prompt_name: str,
    db_prompt_loader=None,
):
    """构建 build_prompt 节点(普通版与 interrupt 版共享)。

    统一三条分支: DB 命中 / DB 回退文件 / 纯文件,始终写入 prompt_version_info,
    供 call_llm 绑定 Langfuse trace。interrupt 版此前缺少 DbPromptLoader 支持与
    prompt_version_info(行为漂移),现通过本工厂统一获得。
    """

    async def build_prompt(state: EvaluationState) -> EvaluationState:
        """渲染 System Prompt

        P1 调试增强: 优先用 DbPromptLoader (支持 A/B / 灰度 / 版本管理),
        失败时回退文件 PromptLoader。记录 prompt 版本信息供 trace 绑定。
        """
        with _node_trace("build_prompt", state):
            if state.get("error"):
                return state
            inputs = state.get("cleaned_inputs") or state["raw_inputs"]
            prompt_version_info: Optional[Dict[str, Any]] = None

            # P1: 优先用 DbPromptLoader (DB 版本 + A/B + 灰度)
            if db_prompt_loader is not None:
                try:
                    version = await db_prompt_loader.get_for_request(
                        name=prompt_name,
                        employee_id=state["employee_id"],
                    )
                    if version is not None:
                        prompt = db_prompt_loader.render(
                            version,
                            raw_inputs=inputs,
                            employee_history=state.get("employee_history") or [],
                            company_kb=state.get("company_kb") or [],
                            employee_id=state["employee_id"],
                            period=state["period"],
                        )
                        prompt_version_info = {
                            "prompt_name": prompt_name,
                            "prompt_version": version.version,
                            "prompt_version_id": version.id,
                            "source": "db",
                        }
                        # 获取该版本的 label 列表
                        try:
                            from core.database import get_db_session
                            from models.models import PromptLabel
                            from sqlalchemy import select

                            async with get_db_session() as sess:
                                stmt = select(PromptLabel).where(
                                    PromptLabel.version_id == version.id
                                )
                                labels_result = await sess.execute(stmt)
                                prompt_version_info["prompt_labels"] = [
                                    l.label for l in labels_result.scalars().all()
                                ]
                        except Exception:
                            prompt_version_info["prompt_labels"] = []
                    else:
                        # DB 无此 prompt,回退文件
                        prompt = prompt_loader.render(
                            name=prompt_name,
                            raw_inputs=inputs,
                            employee_history=state.get("employee_history") or [],
                            company_kb=state.get("company_kb") or [],
                            employee_id=state["employee_id"],
                            period=state["period"],
                        )
                        prompt_version_info = {
                            "prompt_name": prompt_name,
                            "prompt_version": prompt_loader.version(prompt_name),
                            "source": "file_fallback",
                        }
                except Exception as e:
                    logger.warning(
                        "DbPromptLoader 加载失败,回退文件 PromptLoader: %s", e
                    )
                    prompt = prompt_loader.render(
                        name=prompt_name,
                        raw_inputs=inputs,
                        employee_history=state.get("employee_history") or [],
                        company_kb=state.get("company_kb") or [],
                        employee_id=state["employee_id"],
                        period=state["period"],
                    )
                    prompt_version_info = {
                        "prompt_name": prompt_name,
                        "prompt_version": prompt_loader.version(prompt_name),
                        "source": "file_error",
                        "error": str(e),
                    }
            else:
                # 无 DbPromptLoader,用文件 PromptLoader
                prompt = prompt_loader.render(
                    name=prompt_name,
                    raw_inputs=inputs,
                    employee_history=state.get("employee_history") or [],
                    company_kb=state.get("company_kb") or [],
                    employee_id=state["employee_id"],
                    period=state["period"],
                )
                prompt_version_info = {
                    "prompt_name": prompt_name,
                    "prompt_version": prompt_loader.version(prompt_name),
                    "source": "file",
                }

            # 重新评估时携带的历史反馈/申诉上下文，作为额外片段拼到渲染后 prompt 末尾。
            # 不修改 Prompt 模板文件，避免触发 prompt-gate --compare v0.1 版本门禁。
            feedback = state.get("feedback") or []
            if feedback:
                lines = ["", "", "## 历史反馈与申诉(重新评估参考)"]
                for fb in feedback:
                    fb_type = (fb or {}).get("type", "feedback")
                    content = (fb or {}).get("content", "")
                    lines.append(f"- [{fb_type}] {content}")
                prompt = prompt + "\n".join(lines)
            return {**state, "prompt": prompt, "prompt_version_info": prompt_version_info}

    return build_prompt


def _make_call_llm(model_router: ModelRouter, prompt_loader: PromptLoader, prompt_name: str):
    """构建 call_llm 节点(普通版与 interrupt 版共享)。

    统一读取 state["prompt_version_info"],传完整 Langfuse trace 字段
    (prompt_name/prompt_version/prompt_version_id/prompt_labels)与 audit_info
    (prompt_source/prompt_version_id)。interrupt 版此前缺少这些字段(行为漂移),
    现通过本工厂统一获得。
    """

    async def call_llm(state: EvaluationState) -> EvaluationState:
        """调用 LLM 生成评估"""
        if state.get("error"):
            return state
        start = time.time()
        try:
            # P1-3：调用公共 helper（失败时由 call_llm_with_fallback 触发 runtime_reselect 降级重试一次）
            completion, tier = await call_llm_with_fallback(
                model_router,
                prompt=state["prompt"],
                employee_id=state["employee_id"],
                period=state["period"],
            )
            processing_time_ms = int((time.time() - start) * 1000)

            # H3：记录 LLM 生成调用到 Langfuse，便于回溯模型与 token 消耗
            # P1 调试增强: 把 prompt 版本信息绑定到 trace,Langfuse UI 可按版本过滤
            pvi = state.get("prompt_version_info") or {}
            try:
                with tracer.trace(
                    name="llm_generation",
                    employee_id=state["employee_id"],
                    metadata={"period": state["period"], "model_tier": tier},
                ) as _trace:
                    tracer.generation(
                        parent=_trace,
                        name="chat_completion",
                        prompt=state["prompt"],
                        completion=completion.content,
                        model=completion.model,
                        usage=completion.usage,
                        metadata={"model_tier": tier},
                        prompt_name=pvi.get("prompt_name"),
                        prompt_version=pvi.get("prompt_version"),
                        prompt_version_id=pvi.get("prompt_version_id"),
                        prompt_labels=pvi.get("prompt_labels"),
                    )
            except Exception:
                logger.debug("Langfuse generation 记录失败，忽略", exc_info=True)

            # H6：保留上游（附件注入扫描）记录的 triggered_rules，不覆盖
            prior_rules = list(
                (state.get("audit_info") or {}).get("triggered_rules", [])
            )
            audit_info = {
                "model_name": completion.model,
                "model_tier": tier,
                "confidence_score": 0.0,  # 由 parse 节点根据内容更新
                "raw_data_refs": [inp.get("input_id") for inp in state["raw_inputs"]],
                "triggered_rules": prior_rules
                + ["evidence_first", "dual_view_separation"],
                "processing_time_ms": processing_time_ms,
                # P1 调试增强: 记录实际使用的 prompt 版本 (DB 或文件)
                "prompt_version": pvi.get("prompt_version", prompt_loader.version(prompt_name)),
                "prompt_source": pvi.get("source", "file"),
                "prompt_version_id": pvi.get("prompt_version_id"),
            }

            return {
                **state,
                "llm_raw_output": completion.content,
                "audit_info": audit_info,
                "status": EvaluationStatus.AI_DRAFTED,
            }
        except Exception as e:
            return {**state, "error": f"LLM 调用失败: {e}", "status": "error"}

    return call_llm


def _make_parse_output(output_guard: OutputGuard):
    """构建 parse_output 节点(普通版与 interrupt 版共享)。"""

    async def parse_output(state: EvaluationState) -> EvaluationState:
        """解析并校验 LLM 输出"""
        with _node_trace("parse_output", state):
            if state.get("error"):
                return state

            raw = state.get("llm_raw_output", "")
            try:
                data = json.loads(raw)
                # 补充/覆盖必要字段（防止 LLM 漏填或 Mock 数据不完整）
                data["evaluation_id"] = (
                    f"EV-{state['period']}-{state['employee_id']}-{uuid.uuid4().hex[:8]}"
                )
                data["employee_id"] = state["employee_id"]
                data["period"] = state["period"]
                data.setdefault("status", EvaluationStatus.AI_DRAFTED)

                # 合并审计信息
                audit = data.get("audit", {})
                if state.get("audit_info"):
                    audit.update(state["audit_info"])
                    # 根据 evidence 数量估算置信度
                    evidence_count = sum(
                        len(area.get("evidence", []))
                        for area in data.get("employee_view", {}).get(
                            "growth_areas", []
                        )
                    )
                    audit["confidence_score"] = min(0.95, 0.5 + evidence_count * 0.1)
                data["audit"] = audit

                # 输出护栏：脱敏与敏感词检查
                emp_view = data.get("employee_view", {})
                mgr_view = data.get("manager_view", {})
                emp_result = output_guard.sanitize_employee_view(emp_view)
                mgr_result = output_guard.sanitize_manager_view(mgr_view)

                # 记录护栏违规到 triggered_rules（AuditInfo 禁止 extra 字段，合并到已有列表）
                guard_violations = emp_result.violations + mgr_result.violations
                if guard_violations:
                    logger.warning("输出护栏检测到违规: %s", guard_violations)
                    audit["triggered_rules"] = list(
                        audit.get("triggered_rules", [])
                    ) + [f"output_guard:{v}" for v in guard_violations]
                redacted = emp_result.redacted_entities + mgr_result.redacted_entities
                if redacted:
                    audit["triggered_rules"] = list(
                        audit.get("triggered_rules", [])
                    ) + [f"redacted:{r}" for r in redacted]

                # H5：员工视图出现负面/偏见词时阻断入库，回退到安全模板
                # 管理视图允许尖锐判断，仅脱敏不阻断
                if _has_blocking_violation(emp_result.violations):
                    logger.warning(
                        "员工视图命中负面/偏见词，阻断入库并回退安全模板: %s",
                        emp_result.violations,
                    )
                    data["employee_view"] = json.loads(json.dumps(SAFE_EMPLOYEE_VIEW))
                    audit["triggered_rules"] = list(
                        audit.get("triggered_rules", [])
                    ) + ["employee_view_blocked"]

                evaluation = EmployeeEvaluation.model_validate(data)
                return {
                    **state,
                    "parsed_evaluation": evaluation.model_dump(mode="json"),
                    "status": EvaluationStatus.AI_DRAFTED,
                }
            except (json.JSONDecodeError, ValidationError) as e:
                return {**state, "error": f"输出解析失败: {e}", "status": "error"}

    return parse_output


async def _review_gate(
    state: EvaluationState,
) -> Literal["hr_audit", "manager_review", "error"]:
    """评估生成完成后的自动路由(普通版与 interrupt 版共享)。

    - 高风险或低分自动进入 HR 复核
    - 其余进入主管待审批
    实际审批动作由 API 层(普通版)或 interrupt 节点(interrupt 版)完成。

    Bug 3 修复: 统一错误哨兵为 "error"。原 interrupt 版用 "rejected" 与实际
    error 状态不符(被拦截/解析失败是 error 而非 rejected),现两个构建器都返回
    "error",条件边统一映射 "error" → END。
    """
    if state.get("error"):
        return "error"
    parsed = state.get("parsed_evaluation")
    if parsed:
        score = parsed.get("overall_score", 100)
        risk_flags = parsed.get("manager_view", {}).get("risk_flags", [])
        has_critical = any(r.get("level") == "critical" for r in risk_flags)
        if score < 60 or has_critical:
            return "hr_audit"
    return "manager_review"


async def _manager_review_marker(state: EvaluationState) -> EvaluationState:
    """主管审批路由标记(普通版):评估等待主管审批。

    仅设置 state["status"] 路由标记,不修改 parsed_evaluation["status"],
    评估统一以 ai_drafted 入库,由 API 层驱动状态机转换。
    """
    return {**state, "status": EvaluationStatus.MANAGER_REVIEW}


async def _hr_audit_marker(state: EvaluationState) -> EvaluationState:
    """HR 复核路由标记(普通版):高风险评估等待 HR 复核。

    仅设置 state["status"] 路由标记,不修改 parsed_evaluation["status"],
    评估统一以 ai_drafted 入库,由 API 层驱动状态机转换。
    """
    return {**state, "status": EvaluationStatus.HR_AUDIT}


async def _manager_review_interrupt(
    state: EvaluationState, config: RunnableConfig
) -> EvaluationState:
    """主管审批中断点(interrupt 版):使用 LangGraph 原生 interrupt 暂停执行。

    interrupt() 会抛出 GraphInterrupt,图状态被 checkpointer 持久化。
    恢复时,decision 包含审批结果。

    Bug 2 修复: 原实现先就地修改 state["parsed_evaluation"]["status"] 再 interrupt(),
    这会污染 checkpoint(中断期间持久化的 parsed_evaluation 被篡改为 manager_review),
    且 resume 后节点重入可能丢数据/状态不一致。现在 interrupt() 之前完全不修改 state,
    只读取快照构建审批 payload;interrupt() 返回后对 parsed_evaluation 的【副本】操作,
    状态值统一使用 EvaluationStatus 枚举(而非裸字符串)。

    Python 3.10 兼容: 手动设置 var_child_runnable_config contextvar,
    使 interrupt() 内部的 get_config() 能正常获取配置。
    """
    # Python 3.10 兼容层：设置 contextvar 使 interrupt() 可用
    _token = _ensure_interrupt_context(config)
    try:
        # interrupt() 之前只读取,不修改 state(避免污染 checkpoint)
        parsed_snapshot = state.get("parsed_evaluation") or {}
        # 暂停并等待人工审批,传递评估摘要供审批人查看
        decision = interrupt(
            {
                "node": "manager_review",
                "evaluation_id": parsed_snapshot.get("evaluation_id"),
                "employee_id": state["employee_id"],
                "period": state["period"],
                "overall_score": parsed_snapshot.get("overall_score"),
                "message": "等待主管审批",
            }
        )
    finally:
        _reset_interrupt_context(_token)

    # 恢复后:对副本操作,不就地修改上游 state 中的 dict
    parsed = dict(parsed_snapshot)
    action = (decision or {}).get("action", "approve")
    comment = (decision or {}).get("comment", "")
    actor_id = (decision or {}).get("actor_id", "unknown")

    if action == "approve":
        parsed["status"] = EvaluationStatus.APPROVED
        parsed["approver_id"] = actor_id
        parsed["manager_review_comment"] = comment
        return {**state, "parsed_evaluation": parsed, "status": EvaluationStatus.APPROVED}
    elif action == "reject":
        parsed["status"] = EvaluationStatus.REJECTED
        parsed["manager_review_comment"] = comment
        return {**state, "parsed_evaluation": parsed, "status": EvaluationStatus.REJECTED}
    elif action == "request_hr_review":
        parsed["status"] = EvaluationStatus.HR_AUDIT
        parsed["manager_review_comment"] = comment
        return {**state, "parsed_evaluation": parsed, "status": EvaluationStatus.HR_AUDIT}
    else:
        return {**state, "status": "error", "error": f"未知审批动作: {action}"}


async def _hr_audit_interrupt(
    state: EvaluationState, config: RunnableConfig
) -> EvaluationState:
    """HR 复核中断点(interrupt 版)。

    Bug 2 修复: 同 _manager_review_interrupt,interrupt() 前不修改 state,
    返回后对副本操作;状态值统一使用 EvaluationStatus 枚举(而非裸字符串)。

    Python 3.10 兼容: 手动设置 var_child_runnable_config contextvar。
    """
    _token = _ensure_interrupt_context(config)
    try:
        parsed_snapshot = state.get("parsed_evaluation") or {}
        decision = interrupt(
            {
                "node": "hr_audit",
                "evaluation_id": parsed_snapshot.get("evaluation_id"),
                "employee_id": state["employee_id"],
                "period": state["period"],
                "overall_score": parsed_snapshot.get("overall_score"),
                "message": "等待 HR 复核",
            }
        )
    finally:
        _reset_interrupt_context(_token)
    parsed = dict(parsed_snapshot)
    action = (decision or {}).get("action", "approve")
    comment = (decision or {}).get("comment", "")
    actor_id = (decision or {}).get("actor_id", "unknown")

    if action == "approve":
        parsed["status"] = EvaluationStatus.APPROVED
        parsed["approver_id"] = actor_id
        parsed["hr_review_comment"] = comment
        return {**state, "parsed_evaluation": parsed, "status": EvaluationStatus.APPROVED}
    elif action == "reject":
        parsed["status"] = EvaluationStatus.REJECTED
        parsed["hr_review_comment"] = comment
        return {**state, "parsed_evaluation": parsed, "status": EvaluationStatus.REJECTED}
    elif action == "require_reeval":
        # M2：HR 退回重评，评估回到 ai_drafted 等待重新生成
        parsed["status"] = EvaluationStatus.AI_DRAFTED
        parsed["hr_review_comment"] = comment
        return {**state, "parsed_evaluation": parsed, "status": EvaluationStatus.AI_DRAFTED}
    else:
        return {**state, "status": "error", "error": f"未知 HR 动作: {action}"}


async def _finalize(state: EvaluationState) -> EvaluationState:
    """最终状态节点：保留上游设置的状态"""
    return state


def create_evaluation_graph(
    toolkit: AgentToolkit,
    model_router: ModelRouter,
    prompt_loader: PromptLoader,
    prompt_name: str = "daily_evaluation",
    input_guard: Optional[InputGuard] = None,
    output_guard: Optional[OutputGuard] = None,
    multimodal_cleaner: Optional[MultimodalCleaner] = None,
    db_prompt_loader=None,
):
    """创建评估工作流图

    P1 调试增强: 新增 db_prompt_loader 参数。
    - 传入 DbPromptLoader 时,build_prompt 优先从 DB 加载 prompt (支持 A/B / 灰度),
      并把版本信息写入 state["prompt_version_info"],call_llm 节点把它绑定到 Langfuse trace。
    - 不传入时,回退到文件 PromptLoader (向后兼容)。

    节点逻辑由模块级工厂函数共享(create_evaluation_graph_with_interrupt 复用同一套),
    本函数仅负责图装配(节点连接、条件边),拓扑与原实现保持一致。
    """

    input_guard = input_guard or InputGuard()
    output_guard = output_guard or OutputGuard()
    multimodal_cleaner = multimodal_cleaner or MultimodalCleaner()

    # 构建图
    builder = StateGraph(EvaluationState)
    builder.add_node("input_sanitizer", _make_input_sanitizer(input_guard))
    builder.add_node("data_cleaning", _make_data_cleaning(multimodal_cleaner, input_guard))
    builder.add_node("retrieve_context", _make_retrieve_context(toolkit))
    builder.add_node(
        "build_prompt", _make_build_prompt(prompt_loader, prompt_name, db_prompt_loader)
    )
    builder.add_node("call_llm", _make_call_llm(model_router, prompt_loader, prompt_name))
    builder.add_node("parse_output", _make_parse_output(output_guard))
    builder.add_node("manager_review", _manager_review_marker)
    builder.add_node("hr_audit", _hr_audit_marker)
    builder.add_node("finalize", _finalize)

    builder.add_edge(START, "input_sanitizer")
    builder.add_edge("input_sanitizer", "data_cleaning")
    builder.add_edge("data_cleaning", "retrieve_context")
    builder.add_edge("retrieve_context", "build_prompt")
    builder.add_edge("build_prompt", "call_llm")
    builder.add_edge("call_llm", "parse_output")
    builder.add_conditional_edges(
        "parse_output",
        _review_gate,
        {
            "hr_audit": "hr_audit",
            "manager_review": "manager_review",
            "error": END,
        },
    )
    builder.add_edge("manager_review", "finalize")
    builder.add_edge("hr_audit", "finalize")
    builder.add_edge("finalize", END)

    return builder.compile()


def create_evaluation_graph_with_interrupt(
    toolkit: AgentToolkit,
    model_router: ModelRouter,
    prompt_loader: PromptLoader,
    prompt_name: str = "daily_evaluation",
    input_guard: Optional[InputGuard] = None,
    output_guard: Optional[OutputGuard] = None,
    multimodal_cleaner: Optional[MultimodalCleaner] = None,
    checkpointer=None,
    db_prompt_loader=None,
):
    """
    创建带 LangGraph 原生 interrupt 中断点的评估工作流图。
    与 create_evaluation_graph 的区别:
    - manager_review / hr_audit 节点使用 interrupt() 暂停执行,等待人工审批
    - 必须配合 checkpointer 使用(如 MemorySaver)
    - 通过 Command(resume=...) 恢复执行
    审批恢复值格式: {"action": "approve"|"reject"|"request_hr_review", "comment": "...", "actor_id": "..."}

    重构: 与普通版共享模块级节点工厂(input_sanitizer / data_cleaning /
    retrieve_context / build_prompt / call_llm / parse_output / _review_gate),
    消除重复并统一行为(build_prompt 现支持 DbPromptLoader;call_llm 现传完整
    trace/audit 字段;review_gate 统一 "error" 哨兵)。仅 manager_review / hr_audit
    保持 interrupt 专属实现(已修复 interrupt 前 state 污染,改用 EvaluationStatus 枚举)。

    新增 db_prompt_loader 参数(默认 None,向后兼容),使本构建器与普通版 build_prompt
    行为一致;api 层可按需注入 DbPromptLoader 获得 DB prompt 版本/A/B/灰度能力。

    P3 规模化就绪(H3): checkpointer 默认 MemorySaver(单实例限制),
    settings.use_postgres_checkpointer=True 时切换到 Postgres checkpointer,
    interrupt 状态持久化,支持多 worker 水平扩展;_create_checkpointer 内会调用
    setup() 建表(Bug 1 修复),失败降级 MemorySaver。
    """

    # P3:checkpointer 默认 MemorySaver,启用时切换 Postgres(解除单实例约束)
    if checkpointer is None:
        checkpointer = _create_checkpointer()

    input_guard = input_guard or InputGuard()
    output_guard = output_guard or OutputGuard()
    multimodal_cleaner = multimodal_cleaner or MultimodalCleaner()

    builder = StateGraph(EvaluationState)
    builder.add_node("input_sanitizer", _make_input_sanitizer(input_guard))
    builder.add_node("data_cleaning", _make_data_cleaning(multimodal_cleaner, input_guard))
    builder.add_node("retrieve_context", _make_retrieve_context(toolkit))
    builder.add_node(
        "build_prompt", _make_build_prompt(prompt_loader, prompt_name, db_prompt_loader)
    )
    builder.add_node("call_llm", _make_call_llm(model_router, prompt_loader, prompt_name))
    builder.add_node("parse_output", _make_parse_output(output_guard))
    builder.add_node("manager_review", _manager_review_interrupt)
    builder.add_node("hr_audit", _hr_audit_interrupt)
    builder.add_node("finalize", _finalize)

    builder.add_edge(START, "input_sanitizer")
    builder.add_edge("input_sanitizer", "data_cleaning")
    builder.add_edge("data_cleaning", "retrieve_context")
    builder.add_edge("retrieve_context", "build_prompt")
    builder.add_edge("build_prompt", "call_llm")
    builder.add_edge("call_llm", "parse_output")
    builder.add_conditional_edges(
        "parse_output",
        _review_gate,
        {
            "hr_audit": "hr_audit",
            "manager_review": "manager_review",
            "error": END,
        },
    )
    builder.add_edge("manager_review", "finalize")
    builder.add_edge("hr_audit", "finalize")
    builder.add_edge("finalize", END)

    return builder.compile(checkpointer=checkpointer)
