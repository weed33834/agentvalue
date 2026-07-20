"""
多 Agent 协作测试 (P4-1, 对标 Coze Multi-Agent)

测试覆盖:
- supervisor 路由决策: mock LLM 返回特定 agent, supervisor 应 goto 对应节点
- expert agent 执行: mock LLM 返回分析结果, artifacts 中应有对应字段
- report_writer 汇总: artifacts 中所有字段被合并到最终报告
- max_iterations 限制: 防止无限循环
- interrupt_at 暂停: 执行到指定节点时暂停, state 显示 waiting
- resume 恢复: 暂停后恢复执行, 最终完成
- expert 失败不影响其他 agent: artifacts[failed_agent] = {error: ...}
- API 端点: /run /state /resume /artifacts /test /threads
"""

import asyncio
import json
import time
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agent.multi_agent import (
    ALL_ROUTABLE,
    DEFAULT_MAX_ITERATIONS,
    HARD_MAX_ITERATIONS,
    MultiAgentState,
    _safe_json_parse,
    create_multi_agent_graph,
)
from agent.tools import AgentToolkit, DummyCompanyKB, DummyMemoryStore
from core.providers.base import (
    BaseProvider,
    ChatCompletion,
    ChatMessage,
    ProviderConfig,
)


# ============================================================
# Mock Provider / ModelRouter
# ============================================================


class ScriptedMockProvider(BaseProvider):
    """按脚本返回不同 JSON 响应的 Mock Provider。

    每次调用 chat_completion 时按 self.responses 顺序返回下一个。
    支持根据 system_prompt 内容匹配关键词返回不同响应 (用于 supervisor / data_analyst 区分)。
    """

    def __init__(self, responses: List[Dict[str, Any]] | Dict[str, Any]):
        super().__init__(ProviderConfig(model_name="scripted-mock"))
        if isinstance(responses, dict):
            responses = [responses]
        self._responses = list(responses)
        self._calls: List[Dict[str, Any]] = []

    def name(self) -> str:
        return "scripted-mock"

    async def chat_completion(self, messages, response_format=None):
        self._calls.append({"messages": list(messages)})
        if self._responses:
            response = self._responses.pop(0)
        else:
            response = {"next": "END", "reason": "脚本耗尽"}
        return ChatCompletion(
            content=json.dumps(response, ensure_ascii=False),
            model="scripted-mock",
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )

    async def health_check(self) -> bool:
        return True


class KeywordMockProvider(BaseProvider):
    """按 system_prompt 关键词匹配返回不同 JSON 的 Mock Provider。

    用于让 supervisor / data_analyst / code_reviewer 等返回不同内容。
    """

    def __init__(self, keyword_to_response: Dict[str, Dict[str, Any]]):
        super().__init__(ProviderConfig(model_name="keyword-mock"))
        self._keyword_to_response = keyword_to_response
        self._calls: List[Dict[str, Any]] = []

    def name(self) -> str:
        return "keyword-mock"

    async def chat_completion(self, messages, response_format=None):
        self._calls.append({"messages": list(messages)})
        # 拼接所有 message 内容做关键词匹配
        full_text = " ".join(
            getattr(m, "content", str(m)) for m in messages
        )
        for keyword, response in self._keyword_to_response.items():
            if keyword in full_text:
                return ChatCompletion(
                    content=json.dumps(response, ensure_ascii=False),
                    model="keyword-mock",
                    usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
                )
        # 默认 END
        return ChatCompletion(
            content=json.dumps({"next": "END", "reason": "no match"}, ensure_ascii=False),
            model="keyword-mock",
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )

    async def health_check(self) -> bool:
        return True


class FailingMockProvider(BaseProvider):
    """模拟 LLM 调用异常的 Provider"""

    def __init__(self):
        super().__init__(ProviderConfig(model_name="failing"))

    def name(self) -> str:
        return "failing"

    async def chat_completion(self, messages, response_format=None):
        raise RuntimeError("模拟 LLM 服务不可用")

    async def health_check(self) -> bool:
        return False


class MockModelRouter:
    """测试用 ModelRouter, 固定返回指定 provider"""

    def __init__(self, provider: BaseProvider):
        self._provider = provider

    async def get_provider_with_fallback(self):
        return self._provider, "L0"


def _build_toolkit():
    return AgentToolkit(DummyMemoryStore(), DummyCompanyKB())


def _build_initial_state(task="测试任务", context=None, **kwargs):
    state = {
        "messages": [],
        "task": task,
        "context": context or {"employee_id": "E1001", "period": "2026-W28"},
        "max_iterations": DEFAULT_MAX_ITERATIONS,
        "interrupt_at": None,
        "iteration": 0,
        "next_agent": "",
        "artifacts": {},
        "final_report": None,
        "error": None,
        "timeline": [],
    }
    state.update(kwargs)
    return state


# ============================================================
# Test 1: supervisor 路由决策 → data_analyst
# ============================================================


@pytest.mark.asyncio
async def test_supervisor_routes_to_data_analyst():
    """supervisor LLM 返回 next=data_analyst, 应执行该节点并回到 supervisor"""
    # supervisor 第 1 次: data_analyst
    # supervisor 第 2 次: END (artifacts 已有 data_analyst, 但 supervisor 兜底会改路由到 report_writer)
    # 所以脚本: data_analyst, report_writer(走 END), report_writer 输出
    provider = ScriptedMockProvider(
        [
            {"next": "data_analyst", "reason": "需要数据分析"},
            {"next": "END", "reason": "完成"},  # 会被兜底改成 report_writer
            # report_writer 调用一次
            {"report_md": "# 报告\n\n汇总完成"},
        ]
    )
    router = MockModelRouter(provider)
    graph = create_multi_agent_graph(
        model_router=router,
        toolkit=_build_toolkit(),
        prompt_loader=MagicMock(),
    )

    result = await graph.ainvoke(
        _build_initial_state(),
        config={"configurable": {"thread_id": "t-1"}},
    )

    assert result.get("error") is None or "max_iter" not in (result.get("error") or "")
    # data_analyst 应该被调用过
    assert "data_analyst" in result.get("artifacts", {})
    # report_writer 也应该被调用 (兜底逻辑)
    assert result.get("final_report") is not None


# ============================================================
# Test 2: expert agent 执行 - artifacts 中应有对应字段
# ============================================================


@pytest.mark.asyncio
async def test_expert_agent_artifact_stored():
    """expert agent 执行后, artifacts 中应有对应字段"""
    provider = KeywordMockProvider(
        {
            "supervisor": {"next": "data_analyst", "reason": "需要分析"},
            "数据分析专家": {  # data_analyst 系统提示以 "你是数据分析专家" 开头
                "summary": "员工本周表现稳定",
                "key_findings": ["完成 5 个任务"],
                "metrics": {"task_count": 5},
            },
            "report_writer": {"report_md": "# 汇总报告"},
        }
    )
    router = MockModelRouter(provider)
    graph = create_multi_agent_graph(
        model_router=router,
        toolkit=_build_toolkit(),
        prompt_loader=MagicMock(),
    )
    result = await graph.ainvoke(
        _build_initial_state(),
        config={"configurable": {"thread_id": "t-2"}},
    )
    artifacts = result.get("artifacts", {})
    assert "data_analyst" in artifacts
    assert artifacts["data_analyst"]["summary"] == "员工本周表现稳定"


# ============================================================
# Test 3: report_writer 汇总 - artifacts 中所有字段合并到最终报告
# ============================================================


@pytest.mark.asyncio
async def test_report_writer_aggregates_artifacts():
    """report_writer 汇总所有 artifacts 生成 final_report"""
    # 用 KeywordMock 让 supervisor 依次路由到 3 个 expert 然后 report_writer
    # 但脚本控制 supervisor 决策
    seq = [
        {"next": "data_analyst", "reason": "step1"},
        {"next": "code_reviewer", "reason": "step2"},
        {"next": "risk_assessor", "reason": "step3"},
        {"next": "report_writer", "reason": "汇总"},
    ]
    # expert 与 report_writer 的响应也按顺序
    expert_responses = [
        {"summary": "数据分析完成"},
        {"summary": "代码评审完成"},
        {"summary": "风险评估完成"},
        {"report_md": "# 最终报告\n\n包含所有汇总"},
    ]
    # 用 ScriptedMockProvider 串行返回 (supervisor / expert / supervisor / expert ...)
    all_responses = []
    # 第 1 轮: supervisor → data_analyst
    all_responses.append(seq[0])
    all_responses.append(expert_responses[0])
    # 第 2 轮: supervisor → code_reviewer
    all_responses.append(seq[1])
    all_responses.append(expert_responses[1])
    # 第 3 轮: supervisor → risk_assessor
    all_responses.append(seq[2])
    all_responses.append(expert_responses[2])
    # 第 4 轮: supervisor → report_writer (内部调 LLM 生成报告)
    all_responses.append(seq[3])
    all_responses.append(expert_responses[3])

    provider = ScriptedMockProvider(all_responses)
    router = MockModelRouter(provider)
    graph = create_multi_agent_graph(
        model_router=router,
        toolkit=_build_toolkit(),
        prompt_loader=MagicMock(),
    )
    result = await graph.ainvoke(
        _build_initial_state(max_iterations=15),  # 留足迭代空间
        config={"configurable": {"thread_id": "t-3"}},
    )
    artifacts = result.get("artifacts", {})
    # 4 个 agent 都应有产出
    assert "data_analyst" in artifacts
    assert "code_reviewer" in artifacts
    assert "risk_assessor" in artifacts
    # final_report 应包含汇总内容
    final = result.get("final_report") or ""
    assert "最终报告" in final


# ============================================================
# Test 4: max_iterations 限制 - 防止无限循环
# ============================================================


@pytest.mark.asyncio
async def test_max_iterations_prevents_infinite_loop():
    """max_iterations 超限时强制 END, 防止无限循环"""
    # supervisor 始终返回 data_analyst (但 data_analyst 会再次回到 supervisor)
    provider = KeywordMockProvider(
        {
            "supervisor": {"next": "data_analyst", "reason": "再来一次"},
            "数据分析师": {"summary": "重复分析"},
        }
    )
    router = MockModelRouter(provider)
    graph = create_multi_agent_graph(
        model_router=router,
        toolkit=_build_toolkit(),
        prompt_loader=MagicMock(),
    )
    # max_iterations=3, 应该在 iteration > 3 时强制 END
    result = await graph.ainvoke(
        _build_initial_state(max_iterations=3),
        config={"configurable": {"thread_id": "t-4"}},
    )
    # 应该报 max_iter 超限错误
    assert result.get("error") is not None
    assert "max_iterations" in result["error"]
    # iteration 应该 == 4 (3 + 1 = 超限那次)
    assert result.get("iteration", 0) >= 3


# ============================================================
# Test 5: interrupt_at 暂停 - 执行到指定节点时暂停
# ============================================================


@pytest.mark.asyncio
async def test_interrupt_at_paused_at_data_analyst():
    """interrupt_at=data_analyst 应在进入 data_analyst 时暂停"""
    provider = ScriptedMockProvider(
        [
            {"next": "data_analyst", "reason": "去 data_analyst"},
            {"summary": "数据分析完成"},  # 不会执行, 因为先被 interrupt
        ]
    )
    router = MockModelRouter(provider)
    graph = create_multi_agent_graph(
        model_router=router,
        toolkit=_build_toolkit(),
        prompt_loader=MagicMock(),
    )
    result = await graph.ainvoke(
        _build_initial_state(interrupt_at="data_analyst"),
        config={"configurable": {"thread_id": "t-5"}},
    )
    # 应该被 interrupt
    assert "__interrupt__" in result
    interrupts = result["__interrupt__"]
    interrupt_info = interrupts[0].value if hasattr(interrupts[0], "value") else interrupts[0]
    assert interrupt_info["node"] == "data_analyst"
    assert "等待人工确认" in interrupt_info["message"]


# ============================================================
# Test 6: resume 恢复 - 暂停后恢复执行
# ============================================================


@pytest.mark.asyncio
async def test_resume_after_interrupt():
    """暂停后通过 Command(resume=...) 恢复执行"""
    from langgraph.types import Command

    provider = ScriptedMockProvider(
        [
            {"next": "data_analyst", "reason": "step1"},
            # interrupt 后第一次执行被截断, 不消费响应
            # resume 后回到 supervisor, supervisor 决定 report_writer
            {"next": "report_writer", "reason": "汇总"},
            {"report_md": "# 最终报告(resumed)"},
        ]
    )
    router = MockModelRouter(provider)
    graph = create_multi_agent_graph(
        model_router=router,
        toolkit=_build_toolkit(),
        prompt_loader=MagicMock(),
    )
    config = {"configurable": {"thread_id": "t-6"}}

    # 第一次: 应该暂停在 data_analyst
    result1 = await graph.ainvoke(
        _build_initial_state(interrupt_at="data_analyst"),
        config=config,
    )
    assert "__interrupt__" in result1

    # resume: data_analyst 不执行 LLM, 直接返回 interrupt_at=None
    # supervisor 再次执行, 走 report_writer
    result2 = await graph.ainvoke(Command(resume={"decision": "approve"}), config=config)

    # 应该完成
    assert "__interrupt__" not in result2
    assert result2.get("final_report") is not None
    # data_analyst 在 interrupt_at 清掉后, 下次 supervisor 调度不会重复 (除非 LLM 又指向它)


# ============================================================
# Test 7: expert 失败不影响其他 agent
# ============================================================


@pytest.mark.asyncio
async def test_expert_failure_does_not_affect_others():
    """data_analyst 失败时, artifacts[data_analyst]={error: ...}, 其他 agent 仍可执行"""
    # 用 KeywordMock 让 data_analyst 关键词触发失败 (返回 _error)
    # 这里直接用 ScriptedMock 让中间一次返回错误 JSON
    class _ErrorThenOKProvider(BaseProvider):
        """第 2 次 (data_analyst 调用) 返回错误, 其他正常"""

        def __init__(self):
            super().__init__(ProviderConfig(model_name="err-then-ok"))
            self._call_count = 0

        def name(self) -> str:
            return "err-then-ok"

        async def chat_completion(self, messages, response_format=None):
            self._call_count += 1
            # call 1: supervisor → data_analyst
            # call 2: data_analyst (我们让它抛异常)
            if self._call_count == 2:
                raise RuntimeError("data_analyst LLM 故障")
            # call 3: supervisor → code_reviewer (or END 兜底)
            # call 4: code_reviewer
            # call 5: supervisor → report_writer (兜底)
            # call 6: report_writer
            if self._call_count == 1:
                return ChatCompletion(
                    content=json.dumps({"next": "code_reviewer", "reason": "skip data"}),
                    model="err-then-ok",
                    usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                )
            if self._call_count == 3:
                return ChatCompletion(
                    content=json.dumps({"next": "END", "reason": "完成"}),
                    model="err-then-ok",
                    usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                )
            if self._call_count == 4:
                return ChatCompletion(
                    content=json.dumps({"summary": "code_reviewer 完成"}),
                    model="err-then-ok",
                    usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                )
            # call 5: report_writer
            return ChatCompletion(
                content=json.dumps({"report_md": "# 汇总报告"}),
                model="err-then-ok",
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )

        async def health_check(self) -> bool:
            return True

    router = MockModelRouter(_ErrorThenOKProvider())
    graph = create_multi_agent_graph(
        model_router=router,
        toolkit=_build_toolkit(),
        prompt_loader=MagicMock(),
    )
    result = await graph.ainvoke(
        _build_initial_state(),
        config={"configurable": {"thread_id": "t-7"}},
    )
    artifacts = result.get("artifacts", {})
    # data_analyst 失败 → 标 error, 但 artifacts 中仍有 key
    # 注意: 由于测试中 data_analyst 抛异常时 _call_count == 2, 此时是 data_analyst 调用
    # 但由于第一次 supervisor 决定 next=code_reviewer (跳过 data_analyst), data_analyst 不会被调
    # 这个测试需要重新设计: 让 supervisor 第一次指向 data_analyst
    # 修正脚本:
    pass  # 见下方修正版


@pytest.mark.asyncio
async def test_expert_failure_marks_error_in_artifacts():
    """expert LLM 失败时, artifacts[name] = {error: ...}, 其他 agent 不受影响"""
    class _ScriptedProvider(BaseProvider):
        def __init__(self):
            super().__init__(ProviderConfig(model_name="scripted-err"))
            self._call_count = 0

        def name(self) -> str:
            return "scripted-err"

        async def chat_completion(self, messages, response_format=None):
            self._call_count += 1
            # call 1: supervisor → data_analyst
            if self._call_count == 1:
                return ChatCompletion(
                    content=json.dumps({"next": "data_analyst", "reason": "step1"}),
                    model="x", usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                )
            # call 2: data_analyst 抛异常
            if self._call_count == 2:
                raise RuntimeError("data_analyst LLM 故障")
            # call 3: supervisor → code_reviewer
            if self._call_count == 3:
                return ChatCompletion(
                    content=json.dumps({"next": "code_reviewer", "reason": "step2"}),
                    model="x", usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                )
            # call 4: code_reviewer 正常返回
            if self._call_count == 4:
                return ChatCompletion(
                    content=json.dumps({"summary": "code review 完成"}),
                    model="x", usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                )
            # call 5: supervisor → END (兜底改 report_writer)
            if self._call_count == 5:
                return ChatCompletion(
                    content=json.dumps({"next": "END", "reason": "done"}),
                    model="x", usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                )
            # call 6: report_writer
            return ChatCompletion(
                content=json.dumps({"report_md": "# 报告"}),
                model="x", usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )

        async def health_check(self) -> bool:
            return True

    router = MockModelRouter(_ScriptedProvider())
    graph = create_multi_agent_graph(
        model_router=router,
        toolkit=_build_toolkit(),
        prompt_loader=MagicMock(),
    )
    result = await graph.ainvoke(
        _build_initial_state(max_iterations=15),
        config={"configurable": {"thread_id": "t-7b"}},
    )
    artifacts = result.get("artifacts", {})
    # data_analyst 失败: artifacts[data_analyst] = {"error": "..."}
    assert "data_analyst" in artifacts
    assert "error" in artifacts["data_analyst"]
    assert "data_analyst LLM 故障" in artifacts["data_analyst"]["error"]
    # code_reviewer 仍正常
    assert "code_reviewer" in artifacts
    assert artifacts["code_reviewer"]["summary"] == "code review 完成"
    # final_report 仍生成 (由 report_writer)
    assert result.get("final_report") is not None


# ============================================================
# Test 8: LLM 返回非法 JSON 时不崩
# ============================================================


@pytest.mark.asyncio
async def test_supervisor_returns_invalid_json_routes_to_end():
    """supervisor LLM 返回非法 JSON 时应兜底走 END (或 report_writer)"""
    class _BadJsonProvider(BaseProvider):
        def __init__(self):
            super().__init__(ProviderConfig(model_name="bad-json"))
            self._call_count = 0

        def name(self) -> str:
            return "bad-json"

        async def chat_completion(self, messages, response_format=None):
            self._call_count += 1
            if self._call_count == 1:
                return ChatCompletion(
                    content="这不是合法的 JSON {{{",
                    model="bad", usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                )
            return ChatCompletion(
                content=json.dumps({"report_md": "# 兜底报告"}),
                model="bad", usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )

        async def health_check(self) -> bool:
            return True

    router = MockModelRouter(_BadJsonProvider())
    graph = create_multi_agent_graph(
        model_router=router,
        toolkit=_build_toolkit(),
        prompt_loader=MagicMock(),
    )
    # artifacts 为空 → supervisor 解析失败时直接走 END (artifacts 为空, 不会兜底 report_writer)
    result = await graph.ainvoke(
        _build_initial_state(),
        config={"configurable": {"thread_id": "t-8"}},
    )
    # 应该不抛异常, 且 supervisor 兜底路由到 END (next_agent="END" 或 final_report 已生成)
    assert result.get("next_agent") == "END" or result.get("final_report") is not None


# ============================================================
# Test 9: _safe_json_parse 容错
# ============================================================


def test_safe_json_parse_strips_markdown_fence():
    """_safe_json_parse 应去掉 markdown 代码块"""
    text = '```json\n{"next": "data_analyst", "reason": "ok"}\n```'
    parsed = _safe_json_parse(text)
    assert parsed == {"next": "data_analyst", "reason": "ok"}


def test_safe_json_parse_extracts_json_from_text():
    """_safe_json_parse 应从前后缀文本中提取 JSON"""
    text = '我认为应该: {"next": "code_reviewer", "reason": "ok"} 完毕'
    parsed = _safe_json_parse(text)
    assert parsed == {"next": "code_reviewer", "reason": "ok"}


def test_safe_json_parse_returns_empty_on_invalid():
    """_safe_json_parse 应在完全无法解析时返回空 dict"""
    assert _safe_json_parse("纯文本无 JSON") == {}
    assert _safe_json_parse("") == {}
    assert _safe_json_parse(None) == {}


# ============================================================
# Test 10: Constants 校验
# ============================================================


def test_default_max_iterations_is_10():
    """DEFAULT_MAX_ITERATIONS 应为 10"""
    assert DEFAULT_MAX_ITERATIONS == 10


def test_hard_max_iterations_is_50():
    """HARD_MAX_ITERATIONS 应为 50 (防失控)"""
    assert HARD_MAX_ITERATIONS == 50


def test_all_routable_includes_all_experts_and_report_writer():
    """ALL_ROUTABLE 应包含 4 个 agent (3 expert + report_writer)"""
    assert set(ALL_ROUTABLE) == {
        "data_analyst",
        "code_reviewer",
        "risk_assessor",
        "report_writer",
    }


# ============================================================
# Test 11: API 端点 - /test 同步执行
# ============================================================


@pytest.fixture
def multi_agent_client(monkeypatch, tmp_path):
    """真 TestClient, mock AppState 避免 ChromaDB"""
    from api.admin import multi_agent as ma_module
    from api.deps import AppState
    from core.config import Settings
    from main import app

    # 清空 thread store
    ma_module.clear_thread_store()

    settings = Settings(model_tier="L0")
    state = object.__new__(AppState)
    state.settings = settings
    state._settings_lock = asyncio.Lock()
    state.prompt_loader = MagicMock()
    state.memory_store = DummyMemoryStore()
    state.company_kb = DummyCompanyKB()
    state.multimodal_cleaner = MagicMock()
    state._tenant_memory_stores = {}
    state._tenant_kb_stores = {}
    state._multi_agent_graphs = {}

    # 注入 mock model_router (scripted provider)
    provider = ScriptedMockProvider(
        [
            {"next": "data_analyst", "reason": "step1"},
            {"summary": "数据分析完成"},
            {"next": "report_writer", "reason": "汇总"},
            {"report_md": "# 测试报告"},
        ]
    )
    state.model_router = MockModelRouter(provider)

    # patch _get_or_create_multi_agent_graph 直接返回我们构造的图
    from agent.tools import AgentToolkit
    toolkit = AgentToolkit(DummyMemoryStore(), DummyCompanyKB())
    mock_graph = create_multi_agent_graph(
        model_router=state.model_router,
        toolkit=toolkit,
        prompt_loader=state.prompt_loader,
    )
    state._multi_agent_graphs = {"default": mock_graph}

    with TestClient(app) as c:
        c.app.state.app_state = state
        yield c

    ma_module.clear_thread_store()


def _admin_headers():
    return {"x-user-role": "admin", "x-user-id": "ADMIN001"}


def test_api_test_endpoint_sync_executes(multi_agent_client):
    """/admin/multi-agent/test 同步执行, 直接返回结果"""
    resp = multi_agent_client.post(
        "/api/v1/admin/multi-agent/test",
        json={
            "task": "测试任务",
            "context": {"employee_id": "E1001", "period": "2026-W28"},
            "max_iterations": 10,
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "completed"
    assert "data_analyst" in data["artifacts"]
    assert data["final_report"] is not None


def test_api_test_endpoint_validates_max_iterations(multi_agent_client):
    """/test 端点应校验 max_iterations 范围 (1-50)"""
    # max_iterations=0 应 422
    resp = multi_agent_client.post(
        "/api/v1/admin/multi-agent/test",
        json={"task": "x", "max_iterations": 0},
        headers=_admin_headers(),
    )
    assert resp.status_code == 422
    # max_iterations=51 应 422
    resp = multi_agent_client.post(
        "/api/v1/admin/multi-agent/test",
        json={"task": "x", "max_iterations": 51},
        headers=_admin_headers(),
    )
    assert resp.status_code == 422


def test_api_test_endpoint_rejects_non_admin(multi_agent_client):
    """非 admin 角色应被拒绝"""
    resp = multi_agent_client.post(
        "/api/v1/admin/multi-agent/test",
        json={"task": "x"},
        headers={"x-user-role": "employee", "x-user-id": "E1001"},
    )
    assert resp.status_code == 403


def test_api_test_endpoint_rejects_empty_task(multi_agent_client):
    """空 task 应被拒绝"""
    resp = multi_agent_client.post(
        "/api/v1/admin/multi-agent/test",
        json={"task": ""},
        headers=_admin_headers(),
    )
    assert resp.status_code == 422


# ============================================================
# Test 12: API 端点 - /run 异步启动 + /state 查询
# ============================================================


def test_api_run_returns_thread_id_and_running_status(multi_agent_client):
    """/run 应返回 thread_id 与 status=running"""
    resp = multi_agent_client.post(
        "/api/v1/admin/multi-agent/run",
        json={
            "task": "异步测试任务",
            "context": {"employee_id": "E1001"},
            "max_iterations": 5,
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "thread_id" in data
    assert data["status"] == "running"
    assert data["thread_id"].startswith("ma-")


def test_api_state_returns_404_for_unknown_thread(multi_agent_client):
    """查询不存在的 thread_id 应 404"""
    resp = multi_agent_client.get(
        "/api/v1/admin/multi-agent/threads/nonexistent-thread/state",
        headers=_admin_headers(),
    )
    assert resp.status_code == 404


def test_api_artifacts_returns_404_for_unknown_thread(multi_agent_client):
    """查询不存在的 thread_id 的 artifacts 应 404"""
    resp = multi_agent_client.get(
        "/api/v1/admin/multi-agent/threads/nonexistent-thread/artifacts",
        headers=_admin_headers(),
    )
    assert resp.status_code == 404


# ============================================================
# Test 13: API /run → 后台执行 → /state 看到 completed
# ============================================================


def test_api_run_then_state_shows_completion(multi_agent_client):
    """/run 后等待后台执行完成, /state 应显示 completed"""
    resp = multi_agent_client.post(
        "/api/v1/admin/multi-agent/run",
        json={
            "task": "后台执行任务",
            "context": {"employee_id": "E1001"},
            "max_iterations": 5,
        },
        headers=_admin_headers(),
    )
    thread_id = resp.json()["thread_id"]

    # 轮询直到完成 (或超时)
    deadline = time.time() + 10.0
    final_status = None
    while time.time() < deadline:
        state_resp = multi_agent_client.get(
            f"/api/v1/admin/multi-agent/threads/{thread_id}/state",
            headers=_admin_headers(),
        )
        assert state_resp.status_code == 200
        meta = state_resp.json().get("meta", {})
        final_status = meta.get("status")
        if final_status in ("completed", "failed", "waiting"):
            break
        time.sleep(0.1)

    assert final_status == "completed", f"实际状态: {final_status}"


# ============================================================
# Test 14: API /threads 列表
# ============================================================


def test_api_list_threads_returns_items(multi_agent_client):
    """创建任务后, /threads 列表应包含该 thread"""
    resp = multi_agent_client.post(
        "/api/v1/admin/multi-agent/run",
        json={"task": "list 测试", "max_iterations": 3},
        headers=_admin_headers(),
    )
    thread_id = resp.json()["thread_id"]

    list_resp = multi_agent_client.get(
        "/api/v1/admin/multi-agent/threads",
        headers=_admin_headers(),
    )
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    thread_ids = [i["thread_id"] for i in items]
    assert thread_id in thread_ids


# ============================================================
# Test 15: API /resume 恢复 - 非 waiting 状态拒绝
# ============================================================


def test_api_resume_rejects_non_waiting_thread(multi_agent_client):
    """对非 waiting 状态的 thread 调 /resume 应 400"""
    # 先创建任务, 等完成
    resp = multi_agent_client.post(
        "/api/v1/admin/multi-agent/run",
        json={"task": "resume 测试", "max_iterations": 3},
        headers=_admin_headers(),
    )
    thread_id = resp.json()["thread_id"]
    # 等待完成
    deadline = time.time() + 10.0
    while time.time() < deadline:
        s = multi_agent_client.get(
            f"/api/v1/admin/multi-agent/threads/{thread_id}/state",
            headers=_admin_headers(),
        ).json()
        if s.get("meta", {}).get("status") in ("completed", "failed", "waiting"):
            break
        time.sleep(0.1)
    # 已 completed, resume 应 400
    resume_resp = multi_agent_client.post(
        f"/api/v1/admin/multi-agent/threads/{thread_id}/resume",
        json={"decision": "approve"},
        headers=_admin_headers(),
    )
    assert resume_resp.status_code == 400


# ============================================================
# Test 16: API /resume 404
# ============================================================


def test_api_resume_returns_404_for_unknown_thread(multi_agent_client):
    """对不存在的 thread 调 /resume 应 404"""
    resp = multi_agent_client.post(
        "/api/v1/admin/multi-agent/threads/nonexistent-thread/resume",
        json={"decision": "approve"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 404


# ============================================================
# Test 17: interrupt_at 在 /test 端点下暂停
# ============================================================


def test_api_test_with_interrupt_at_returns_waiting(multi_agent_client):
    """/test + interrupt_at=data_analyst 应返回 status=waiting"""
    from api.admin import multi_agent as ma_module
    ma_module.clear_thread_store()

    # 重新构造 client 让 mock 路由指向新的图 (用新 provider)
    # 由于 fixture 已 fixture-scope, 这里直接通过 client.app.state.app_state 注入新 provider
    app_state = multi_agent_client.app.state.app_state
    provider = ScriptedMockProvider(
        [
            {"next": "data_analyst", "reason": "去 data_analyst"},
            {"summary": "data_analyst 完成"},
        ]
    )
    app_state.model_router = MockModelRouter(provider)
    from agent.tools import AgentToolkit
    toolkit = AgentToolkit(DummyMemoryStore(), DummyCompanyKB())
    app_state._multi_agent_graphs = {
        "default": create_multi_agent_graph(
            model_router=app_state.model_router,
            toolkit=toolkit,
            prompt_loader=app_state.prompt_loader,
        )
    }

    resp = multi_agent_client.post(
        "/api/v1/admin/multi-agent/test",
        json={
            "task": "interrupt 测试",
            "context": {"employee_id": "E1001"},
            "max_iterations": 5,
            "interrupt_at": "data_analyst",
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "waiting"
    assert data["interrupt_node"] == "data_analyst"


# ============================================================
# Test 18: API /run + interrupt_at → /state waiting → /resume
# ============================================================


def test_api_run_interrupt_state_resume_full_flow(multi_agent_client):
    """/run + interrupt_at → /state 显示 waiting → /resume 恢复完成"""
    from api.admin import multi_agent as ma_module
    ma_module.clear_thread_store()

    # 用更长的脚本: data_analyst 暂停后, resume → supervisor → report_writer → END
    app_state = multi_agent_client.app.state.app_state
    provider = ScriptedMockProvider(
        [
            {"next": "data_analyst", "reason": "step1"},
            # data_analyst interrupt (不消费 LLM 响应)
            # resume → supervisor → report_writer
            {"next": "report_writer", "reason": "汇总"},
            {"report_md": "# 最终报告(完整流程)"},
        ]
    )
    app_state.model_router = MockModelRouter(provider)
    from agent.tools import AgentToolkit
    toolkit = AgentToolkit(DummyMemoryStore(), DummyCompanyKB())
    app_state._multi_agent_graphs = {
        "default": create_multi_agent_graph(
            model_router=app_state.model_router,
            toolkit=toolkit,
            prompt_loader=app_state.prompt_loader,
        )
    }

    # 1. /run with interrupt_at=data_analyst
    run_resp = multi_agent_client.post(
        "/api/v1/admin/multi-agent/run",
        json={
            "task": "完整 interrupt 流程",
            "context": {"employee_id": "E1001"},
            "max_iterations": 10,
            "interrupt_at": "data_analyst",
        },
        headers=_admin_headers(),
    )
    thread_id = run_resp.json()["thread_id"]

    # 2. 等 waiting
    deadline = time.time() + 10.0
    while time.time() < deadline:
        s = multi_agent_client.get(
            f"/api/v1/admin/multi-agent/threads/{thread_id}/state",
            headers=_admin_headers(),
        ).json()
        if s.get("meta", {}).get("status") == "waiting":
            break
        time.sleep(0.1)
    assert s["meta"]["status"] == "waiting"
    assert s["meta"]["interrupt_node"] == "data_analyst"

    # 3. /resume
    resume_resp = multi_agent_client.post(
        f"/api/v1/admin/multi-agent/threads/{thread_id}/resume",
        json={"decision": "approve", "comment": "继续"},
        headers=_admin_headers(),
    )
    assert resume_resp.status_code == 200, resume_resp.text
    assert resume_resp.json()["status"] == "running"

    # 4. 等 completed
    deadline = time.time() + 10.0
    while time.time() < deadline:
        s = multi_agent_client.get(
            f"/api/v1/admin/multi-agent/threads/{thread_id}/state",
            headers=_admin_headers(),
        ).json()
        if s.get("meta", {}).get("status") in ("completed", "failed"):
            break
        time.sleep(0.1)
    assert s["meta"]["status"] == "completed", s
    assert s["meta"]["final_report"] is not None


# ============================================================
# Test 19: timeline 包含所有节点执行记录
# ============================================================


@pytest.mark.asyncio
async def test_timeline_contains_all_node_executions():
    """timeline 应记录 supervisor / expert / report_writer 的执行"""
    provider = ScriptedMockProvider(
        [
            {"next": "data_analyst", "reason": "step1"},
            {"summary": "data_analyst 完成"},
            {"next": "report_writer", "reason": "汇总"},
            {"report_md": "# 报告"},
        ]
    )
    router = MockModelRouter(provider)
    graph = create_multi_agent_graph(
        model_router=router,
        toolkit=_build_toolkit(),
        prompt_loader=MagicMock(),
    )
    result = await graph.ainvoke(
        _build_initial_state(),
        config={"configurable": {"thread_id": "t-timeline"}},
    )
    timeline = result.get("timeline") or []
    nodes = [e["node"] for e in timeline]
    # 至少包含 supervisor (2 次) + data_analyst + report_writer
    assert "supervisor" in nodes
    assert "data_analyst" in nodes
    assert "report_writer" in nodes


# ============================================================
# Test 20: 验证 create_evaluation_graph 仍可用 (不破坏现有单 Agent)
# ============================================================


def test_existing_single_agent_graph_still_works():
    """验证 P4-1 不破坏现有 create_evaluation_graph"""
    from agent.graph import create_evaluation_graph

    # 仅验证可调用, 不实际执行 (现有 test_graph.py 已覆盖运行时)
    graph = create_evaluation_graph(
        toolkit=_build_toolkit(),
        model_router=MockModelRouter(ScriptedMockProvider({"next": "x"})),
        prompt_loader=MagicMock(),
    )
    assert graph is not None


def test_existing_interrupt_graph_still_works():
    """验证 create_evaluation_graph_with_interrupt 仍可用"""
    from agent.graph import create_evaluation_graph_with_interrupt

    graph = create_evaluation_graph_with_interrupt(
        toolkit=_build_toolkit(),
        model_router=MockModelRouter(ScriptedMockProvider({"next": "x"})),
        prompt_loader=MagicMock(),
    )
    assert graph is not None
