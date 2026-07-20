"""
LangGraph 多 Agent 协作 (P4-1, 对标 Coze Multi-Agent)

Supervisor 模式多 Agent 协作,使用 StateGraph + Command(goto=...) 显式实现 handoff,
不依赖 langgraph.prebuilt.create_supervisor (版本兼容性更可控)。

Agents:
- supervisor: 路由决策者(分析任务,决定下一步交给哪个 Agent)
- data_analyst: 数据分析专家(分析员工日报/任务进度)
- code_reviewer: 代码贡献评估专家(分析 commit/PR)
- risk_assessor: 风险评估专家(识别离职风险/合规风险)
- report_writer: 报告生成专家(汇总其他 Agent 产出, 完成后 goto END)

工作流:
1. supervisor 接收任务,决定交给哪个 Agent
2. 该 Agent 执行,产出存到 state.artifacts[agent_name]
3. expert agents 完成后回到 supervisor
4. supervisor 决定是否继续交给其他 Agent 或交给 report_writer
5. report_writer 汇总 artifacts 生成 final_report, 然后 goto END

约束:
- max_iterations 默认 10, 硬上限 50 (防失控)
- 各 expert agent 失败时 artifacts[agent_name] = {"error": str}, 不影响其他 agent
- MemorySaver 作为默认 checkpointer (支持 interrupt, 后续可换 PostgresSaver)
- 每个节点用 _node_trace 装饰器埋点 Langfuse (类似 graph.py 的实现)
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import nullcontext
from typing import Annotated, Any, Dict, List, Optional

import operator

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

from agent.prompt_loader import PromptLoader
from agent.tools import AgentToolkit
from core.model_router import ModelRouter
from core.providers.base import ChatMessage
from core.tracing import tracer

logger = logging.getLogger(__name__)

# 默认与硬上限 (防失控)
DEFAULT_MAX_ITERATIONS = 10
HARD_MAX_ITERATIONS = 50

# 可路由的 expert agents (不含 supervisor / END)
EXPERT_AGENTS = ("data_analyst", "code_reviewer", "risk_assessor")
# 全部可路由目标 (含 report_writer)
ALL_ROUTABLE = EXPERT_AGENTS + ("report_writer",)


class MultiAgentState(TypedDict):
    """多 Agent 协作状态"""

    # 任务描述 (输入, supervisor 用于决策)
    task: str
    # 共享上下文 (输入, 由调用方传入, 如 employee_id / period)
    context: dict
    # 最大迭代次数 (输入, 默认 10, 硬上限 50)
    max_iterations: int
    # 暂停节点 (输入, 可选, 如 "report_writer" 表示执行到该节点时暂停等人工)
    interrupt_at: Optional[str]

    # 消息累加 (跨节点共享, 用 operator.add 累加)
    messages: Annotated[list, operator.add]
    # 当前迭代数 (supervisor 每次递增)
    iteration: int
    # supervisor 决定的下一步 agent
    next_agent: str
    # 各 agent 产出 (key 为 agent_name, value 为 dict)
    artifacts: dict
    # 最终报告 (report_writer 生成)
    final_report: Optional[str]
    # 错误信息 (任一节点失败时设置, 但不阻断其他 agent)
    error: Optional[str]
    # 节点执行时间线 (供前端可视化)
    timeline: list


# ============================================================
# Langfuse 节点级 trace (复制 graph.py 的 _node_trace 模式)
# ============================================================


def _node_trace(node_name: str, state: Optional[dict] = None):
    """节点级 Langfuse trace 上下文管理器。

    为每个工作流节点创建一个 Langfuse trace, 便于在 Langfuse UI 中按节点观察
    输入/输出/耗时。Langfuse 未启用或 tracer 不支持 is_enabled 时返回 nullcontext,
    避免无谓开销与测试副作用。
    """
    is_enabled_fn = getattr(tracer, "is_enabled", None)
    if is_enabled_fn is None or not is_enabled_fn():
        return nullcontext()
    kwargs: Dict[str, Any] = {"name": node_name}
    if state is not None:
        ctx = state.get("context") or {}
        kwargs["metadata"] = {
            "node": node_name,
            "iteration": state.get("iteration"),
            "task": (state.get("task") or "")[:200],
            "context_keys": list(ctx.keys()) if isinstance(ctx, dict) else [],
        }
    try:
        return tracer.trace(**kwargs)
    except Exception:
        # tracer 异常不应阻断节点执行, 降级为 no-op
        return nullcontext()


# ============================================================
# 各 Agent 的系统提示
# ============================================================


SUPERVISOR_SYSTEM_PROMPT = """你是多 Agent 协作的 supervisor 路由决策者。

当前任务: {task}
当前上下文: {context}
已完成的 Agent (artifacts keys): {completed_agents}
当前迭代: {iteration} / {max_iterations}
剩余迭代: {remaining}

可选的下一个 Agent:
- data_analyst: 分析员工日报/任务进度 (未完成时优先)
- code_reviewer: 分析代码贡献 (commit/PR)
- risk_assessor: 识别离职风险/合规风险
- report_writer: 汇总所有 artifacts 生成最终报告 (所有 expert 完成后或剩余迭代不足时)
- END: 直接结束, 不再调用任何 Agent

决策规则:
- 已完成的 Agent 不要再调用
- 当所有 expert 完成 或 剩余迭代次数 <= 1 时, 选 report_writer
- 不需要更多分析时, 选 END

请返回严格 JSON (不要 markdown 代码块):
{{"next": "data_analyst" | "code_reviewer" | "risk_assessor" | "report_writer" | "END", "reason": "<简短理由>"}}
"""


DATA_ANALYST_PROMPT = """你是数据分析专家。分析员工日报与任务进度数据。

任务: {task}
上下文: {context}
员工历史: {history}
知识库: {kb}

请基于以上信息给出数据分析结论。返回严格 JSON (不要 markdown 代码块):
{{"summary": "<整体分析>", "key_findings": ["<要点1>", "<要点2>"], "metrics": {{"<指标名>": <值>}}}}
"""


CODE_REVIEWER_PROMPT = """你是代码贡献评估专家。基于 commit / PR 数据评估代码质量与贡献。

任务: {task}
上下文: {context}
员工历史: {history}
知识库: {kb}

返回严格 JSON (不要 markdown 代码块):
{{"summary": "<整体分析>", "commits_analyzed": <数量>, "code_quality": "<评价>", "highlights": ["<亮点1>", "<亮点2>"]}}
"""


RISK_ASSESSOR_PROMPT = """你是风险评估专家。识别离职风险 / 合规风险 / 性能风险。

任务: {task}
上下文: {context}
员工历史: {history}
知识库: {kb}

返回严格 JSON (不要 markdown 代码块):
{{"summary": "<风险整体分析>", "risks": [{{"category": "<离职|合规|性能>", "level": "<high|medium|low>", "description": "<描述>"}}], "recommendations": ["<建议1>", "<建议2>"]}}
"""


REPORT_WRITER_PROMPT = """你是报告生成专家。汇总各 Agent 的 artifacts, 生成一份综合报告。

任务: {task}
上下文: {context}
各 Agent 产出 (JSON):
{artifacts_json}

请生成 Markdown 格式的综合报告, 涵盖:
1. 概述
2. 数据分析 (来自 data_analyst)
3. 代码贡献 (来自 code_reviewer)
4. 风险评估 (来自 risk_assessor)
5. 综合建议

返回严格 JSON (不要 markdown 代码块):
{{"report_md": "<markdown 报告正文>"}}
"""


# ============================================================
# 工具函数
# ============================================================


def _safe_json_parse(content: str) -> dict:
    """容错 JSON 解析: LLM 输出可能含 markdown 代码块或前后缀"""
    if not content:
        return {}
    text = content.strip()
    # 去掉 markdown code fence (```json ... ``` 或 ``` ... ```)
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except Exception:
        # 尝试截取第一个 { 到最后一个 } 之间的内容
        first = text.find("{")
        last = text.rfind("}")
        if first >= 0 and last > first:
            try:
                return json.loads(text[first : last + 1])
            except Exception:
                pass
    return {}


async def _call_llm_json(
    model_router: ModelRouter,
    system_prompt: str,
    user_prompt: str = "",
) -> dict:
    """统一 LLM 调用 helper, 返回解析后的 JSON dict。

    失败时不抛异常, 返回 {"_error": "<msg>"} 让调用方根据返回内容判断。
    """
    try:
        provider, tier = await model_router.get_provider_with_fallback()
    except Exception as e:
        logger.warning("model_router 获取 provider 失败: %s", e)
        return {"_error": f"provider unavailable: {e}"}

    messages: List[ChatMessage] = []
    if system_prompt:
        messages.append(ChatMessage(role="system", content=system_prompt))
    if user_prompt:
        messages.append(ChatMessage(role="user", content=user_prompt))

    try:
        completion = await provider.chat_completion(messages=messages)
        parsed = _safe_json_parse(completion.content)
        if not parsed:
            # 解析失败, 把原文回传, 调用方可作为 fallback 文本使用
            return {"_raw": completion.content}
        return parsed
    except Exception as e:
        logger.warning("LLM 调用失败: %s", e)
        return {"_error": f"llm call failed: {e}"}


def _append_timeline(
    state: dict, node: str, status: str = "ok", **extra: Any
) -> list:
    """追加节点执行记录到 timeline, 便于前端时间线可视化"""
    entry: Dict[str, Any] = {
        "node": node,
        "iteration": state.get("iteration", 0),
        "status": status,
        "ts": time.time(),
    }
    if extra:
        entry["extra"] = extra
    timeline = list(state.get("timeline") or [])
    timeline.append(entry)
    return timeline


async def _fetch_toolkit_context(
    toolkit: Optional[AgentToolkit],
    agent_name: str,
    task: str,
    context: dict,
) -> tuple[list, list]:
    """获取员工历史与知识库 (按需, 失败降级空列表)

    各 expert agent 调用 LLM 前可选择性获取 toolkit 数据补充上下文。
    """
    if toolkit is None:
        return [], []
    history: list = []
    kb: list = []
    employee_id = None
    period = None
    if isinstance(context, dict):
        employee_id = context.get("employee_id")
        period = context.get("period")
    if employee_id:
        try:
            history = await toolkit.get_employee_history(
                employee_id, period=period, limit=5
            )
        except Exception as e:
            logger.warning("%s: get_employee_history 失败: %s", agent_name, e)
    try:
        kb = await toolkit.query_company_kb(
            query=f"{agent_name} {(task or '')[:200]}",
            top_k=3,
        )
    except Exception as e:
        logger.warning("%s: query_company_kb 失败: %s", agent_name, e)
    return history, kb


# ============================================================
# 主工厂: create_multi_agent_graph
# ============================================================


def create_multi_agent_graph(
    model_router: ModelRouter,
    toolkit: AgentToolkit,
    prompt_loader: PromptLoader,
    checkpointer=None,
):
    """创建多 Agent 协作图

    Args:
        model_router: ModelRouter 实例 (用于获取 LLM provider)
        toolkit: AgentToolkit (员工历史 / 知识库工具, 可为 None)
        prompt_loader: PromptLoader (保留接口, 当前未直接使用, 留作未来扩展)
        checkpointer: LangGraph checkpointer, 默认 MemorySaver

    Returns:
        编译后的 LangGraph (可 ainvoke), 含 checkpointer 支持 interrupt/resume
    """
    if checkpointer is None:
        checkpointer = MemorySaver()

    # ---------------- supervisor 节点 ----------------

    async def supervisor(state: MultiAgentState):
        """supervisor: 决策下一步交给哪个 Agent。

        返回 Command(goto=<agent_name|END>, update={...})。
        每次进入 supervisor 都递增 iteration 计数, 超过 max_iterations 时强制 END。
        """
        iteration = (state.get("iteration") or 0) + 1
        max_iter = state.get("max_iterations") or DEFAULT_MAX_ITERATIONS

        # 防失控: 超过 max_iter 强制结束 (理论上 iteration == max_iter + 1 时才触发)
        if iteration > max_iter:
            with _node_trace("supervisor", state):
                timeline = _append_timeline(
                    state, "supervisor", "max_iter_exceeded", iteration=iteration
                )
            return Command(
                goto=END,
                update={
                    "iteration": iteration,
                    "error": (
                        f"max_iterations ({max_iter}) 超限, 强制结束"
                    ),
                    "timeline": timeline,
                    "next_agent": "END",
                    "messages": [
                        f"supervisor: max_iterations exceeded ({iteration}/{max_iter})"
                    ],
                },
            )

        ctx = state.get("context") or {}
        artifacts = state.get("artifacts") or {}
        completed = [k for k in artifacts.keys() if not k.startswith("_")]
        remaining = max(0, max_iter - iteration)

        system_prompt = SUPERVISOR_SYSTEM_PROMPT.format(
            task=state.get("task", ""),
            context=json.dumps(ctx, ensure_ascii=False),
            completed_agents=completed,
            iteration=iteration,
            max_iterations=max_iter,
            remaining=remaining,
        )

        with _node_trace("supervisor", state):
            decision = await _call_llm_json(model_router, system_prompt)

        # 处理 LLM 错误: 默认走 END, 避免无限循环
        if "_error" in decision:
            logger.warning(
                "supervisor LLM 调用失败: %s, 强制 END", decision["_error"]
            )
            timeline = _append_timeline(
                state, "supervisor", "llm_error", error=decision["_error"]
            )
            return Command(
                goto=END,
                update={
                    "iteration": iteration,
                    "error": f"supervisor LLM 失败: {decision['_error']}",
                    "timeline": timeline,
                    "next_agent": "END",
                    "messages": [
                        f"supervisor: LLM error, force END: {decision['_error']}"
                    ],
                },
            )

        next_agent = decision.get("next", "END")
        reason = decision.get("reason", "")

        # 防御性: 校验 next_agent 合法性
        if next_agent not in ALL_ROUTABLE and next_agent != "END":
            logger.warning(
                "supervisor 返回非法 next_agent: %s, 默认 END", next_agent
            )
            next_agent = "END"

        # 兜底: 还有 artifacts 未汇总且没有 final_report, 强制走 report_writer
        # (避免 LLM 直接 END 导致最终报告缺失)
        if (
            next_agent == "END"
            and artifacts
            and not state.get("final_report")
            and remaining > 0
        ):
            logger.info(
                "supervisor 决定 END 但 artifacts 非空且无 final_report, "
                "改路由到 report_writer"
            )
            next_agent = "report_writer"

        timeline = _append_timeline(
            state, "supervisor", "ok", next=next_agent, reason=reason
        )

        if next_agent == "END":
            return Command(
                goto=END,
                update={
                    "iteration": iteration,
                    "next_agent": "END",
                    "timeline": timeline,
                    "messages": [f"supervisor: END ({reason})"],
                },
            )

        return Command(
            goto=next_agent,
            update={
                "iteration": iteration,
                "next_agent": next_agent,
                "timeline": timeline,
                "messages": [f"supervisor → {next_agent} ({reason})"],
            },
        )

    # ---------------- 通用 expert agent 执行器 ----------------

    async def _run_expert(
        state: MultiAgentState,
        agent_name: str,
        system_prompt_template: str,
    ):
        """通用 expert agent 执行器。

        - 检查 interrupt_at: 命中则 interrupt(), resume 后清 interrupt_at 返回
          (不执行 LLM 调用, 等待 supervisor 再次调度)
        - 调用 LLM 产出
        - 失败时 artifacts[agent_name] = {"error": str}, 不影响其他 agent
        - 完成后通过 add_edge 回到 supervisor
        """
        # 中断检查 (在 trace 之前, 否则 trace 上下文被中断打断)
        interrupt_at = state.get("interrupt_at")
        if interrupt_at == agent_name:
            with _node_trace(agent_name, state):
                interrupt_info = interrupt(
                    {
                        "node": agent_name,
                        "message": f"等待人工确认 {agent_name} 执行",
                        "iteration": state.get("iteration", 0),
                        "task": state.get("task", ""),
                    }
                )
            # resume 后通过返回值清 interrupt_at, 避免重复中断
            timeline = _append_timeline(
                state, agent_name, "resumed", decision=interrupt_info
            )
            return {
                "interrupt_at": None,
                "timeline": timeline,
                "messages": [f"{agent_name}: resumed with {interrupt_info}"],
            }

        ctx = state.get("context") or {}
        task = state.get("task", "")
        history, kb = await _fetch_toolkit_context(toolkit, agent_name, task, ctx)

        system_prompt = system_prompt_template.format(
            task=task,
            context=json.dumps(ctx, ensure_ascii=False),
            history=json.dumps(history, ensure_ascii=False)[:1000],
            kb=json.dumps(kb, ensure_ascii=False)[:500],
        )

        with _node_trace(agent_name, state):
            result = await _call_llm_json(model_router, system_prompt)

        artifacts = dict(state.get("artifacts") or {})
        if "_error" in result:
            artifacts[agent_name] = {"error": result["_error"]}
            timeline = _append_timeline(
                state, agent_name, "error", error=result["_error"]
            )
        else:
            artifacts[agent_name] = result
            timeline = _append_timeline(state, agent_name, "ok")

        return {
            "artifacts": artifacts,
            "timeline": timeline,
            "messages": [f"{agent_name}: completed"],
        }

    async def data_analyst(state: MultiAgentState):
        """数据分析专家: 分析员工日报/任务进度"""
        return await _run_expert(state, "data_analyst", DATA_ANALYST_PROMPT)

    async def code_reviewer(state: MultiAgentState):
        """代码贡献评估专家: 分析 commit/PR"""
        return await _run_expert(state, "code_reviewer", CODE_REVIEWER_PROMPT)

    async def risk_assessor(state: MultiAgentState):
        """风险评估专家: 识别离职风险/合规风险"""
        return await _run_expert(state, "risk_assessor", RISK_ASSESSOR_PROMPT)

    # ---------------- report_writer 节点 ----------------

    async def report_writer(state: MultiAgentState):
        """报告生成专家: 汇总所有 artifacts 生成 final_report, 然后 goto END"""
        # 中断检查
        interrupt_at = state.get("interrupt_at")
        if interrupt_at == "report_writer":
            with _node_trace("report_writer", state):
                interrupt_info = interrupt(
                    {
                        "node": "report_writer",
                        "message": "等待人工确认生成最终报告",
                        "iteration": state.get("iteration", 0),
                        "artifacts_summary": list(
                            (state.get("artifacts") or {}).keys()
                        ),
                    }
                )
            timeline = _append_timeline(
                state, "report_writer", "resumed", decision=interrupt_info
            )
            # resume 后清 interrupt_at, 但不再调用 LLM (避免重复生成)
            # 直接用当前 artifacts 生成报告
            # 这里不立即生成, 而是清掉 interrupt_at 后回到 supervisor, 让 supervisor
            # 再次调度 report_writer (此时 interrupt_at=None, 正常执行 LLM 生成)
            return {
                "interrupt_at": None,
                "timeline": timeline,
                "messages": [f"report_writer: resumed with {interrupt_info}"],
            }

        artifacts = state.get("artifacts") or {}
        ctx = state.get("context") or {}
        system_prompt = REPORT_WRITER_PROMPT.format(
            task=state.get("task", ""),
            context=json.dumps(ctx, ensure_ascii=False),
            artifacts_json=json.dumps(artifacts, ensure_ascii=False, indent=2),
        )

        with _node_trace("report_writer", state):
            result = await _call_llm_json(model_router, system_prompt)

        timeline = _append_timeline(state, "report_writer", "ok")
        update: Dict[str, Any] = {
            "timeline": timeline,
            "messages": ["report_writer: completed"],
        }
        if "_error" in result:
            artifacts_with_err = {
                **artifacts,
                "report_writer": {"error": result["_error"]},
            }
            update["artifacts"] = artifacts_with_err
            update["error"] = f"report_writer failed: {result['_error']}"
        else:
            report_md = result.get("report_md")
            if not report_md:
                # 兜底: LLM 没返回 report_md, 把整个 result 序列化为报告
                report_md = json.dumps(result, ensure_ascii=False, indent=2)
            update["final_report"] = report_md
            update["artifacts"] = {**artifacts, "report_writer": result}

        # report_writer 完成后直接结束
        return Command(goto=END, update=update)

    # ---------------- 构建图 ----------------

    builder = StateGraph(MultiAgentState)
    builder.add_node("supervisor", supervisor)
    builder.add_node("data_analyst", data_analyst)
    builder.add_node("code_reviewer", code_reviewer)
    builder.add_node("risk_assessor", risk_assessor)
    builder.add_node("report_writer", report_writer)

    builder.add_edge(START, "supervisor")
    # expert agents 执行完毕回到 supervisor (supervisor 决定下一步)
    builder.add_edge("data_analyst", "supervisor")
    builder.add_edge("code_reviewer", "supervisor")
    builder.add_edge("risk_assessor", "supervisor")
    # report_writer 内部 Command(goto=END) 自行结束, 不需要显式 edge

    return builder.compile(checkpointer=checkpointer)
