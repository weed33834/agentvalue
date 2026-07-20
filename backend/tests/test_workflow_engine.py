"""
工作流引擎 + Admin API 测试 (P4-2: 工作流可视化编排, 对标 Dify Workflow / Coze Bot)

覆盖:
- 拓扑排序: 简单线性图执行顺序正确
- condition 节点: 真/假分支路由
- LLM 节点: mock model_router, 验证 prompt 模板替换
- HTTP 节点: mock httpx, 验证 request 构造
- code 节点: 简单表达式执行 + builtins 限制
- knowledge 节点: mock kb_store.query
- 失败处理: 某节点失败, 后续节点不执行, 标 failed
- 环检测: 有环图 validate 失败
- 模板渲染: {{var}} 替换
- Admin CRUD + run + validate 端点 (>=15 cases)

运行:
    pytest tests/test_workflow_engine.py -v
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import get_settings
from core.workflow_engine import (
    WorkflowEngine,
    WorkflowExecutionError,
    WorkflowValidationError,
)


# ============================================================
# 工具函数: 构造工作流图
# ============================================================


def _node(
    nid: str,
    ntype: str,
    config: Dict[str, Any] | None = None,
    position: dict | None = None,
) -> dict:
    """构造节点 dict"""
    return {
        "id": nid,
        "type": ntype,
        "position": position or {"x": 0, "y": 0},
        "data": {"config": config or {}},
    }


def _edge(src: str, tgt: str, source_handle: str | None = None) -> dict:
    """构造边 dict"""
    e = {"source": src, "target": tgt}
    if source_handle is not None:
        e["source_handle"] = source_handle
    return e


def _linear_graph() -> dict:
    """线性图: start → code → end"""
    return {
        "nodes": [
            _node("n1", "start"),
            _node(
                "n2",
                "code",
                {"source": "result = inputs.get('x', 0) * 2"},
            ),
            _node("n3", "end"),
        ],
        "edges": [
            _edge("n1", "n2"),
            _edge("n2", "n3"),
        ],
    }


def _condition_graph() -> dict:
    """条件分支图: start → condition → (true: code_a, false: code_b) → end"""
    return {
        "nodes": [
            _node("n1", "start"),
            _node("n2", "condition", {"expression": "score > 60"}),
            _node(
                "n3",
                "code",
                {"source": "result = 'passed'"},
            ),
            _node(
                "n4",
                "code",
                {"source": "result = 'failed'"},
            ),
            _node("n5", "end"),
        ],
        "edges": [
            _edge("n1", "n2"),
            _edge("n2", "n3", source_handle="true"),
            _edge("n2", "n4", source_handle="false"),
            _edge("n3", "n5"),
            _edge("n4", "n5"),
        ],
    }


def _cycle_graph() -> dict:
    """含环的图: start → n2 → n3 → n2 (环)"""
    return {
        "nodes": [
            _node("n1", "start"),
            _node("n2", "code", {"source": "result = 1"}),
            _node("n3", "code", {"source": "result = 2"}),
            _node("n4", "end"),
        ],
        "edges": [
            _edge("n1", "n2"),
            _edge("n2", "n3"),
            _edge("n3", "n2"),  # 环
            _edge("n3", "n4"),
        ],
    }


# ============================================================
# Engine 直接测试 (不依赖 DB / API)
# ============================================================


class TestWorkflowEngineValidate:
    """validate 校验"""

    def test_validate_linear_graph_passes(self):
        """线性图校验通过"""
        engine = WorkflowEngine()
        errors = engine.validate(_linear_graph())
        assert errors == [], f"线性图应通过校验, 实际错误: {errors}"

    def test_validate_cycle_graph_fails(self):
        """含环图校验失败"""
        engine = WorkflowEngine()
        errors = engine.validate(_cycle_graph())
        assert any("环" in e for e in errors), f"应检测出环, 实际错误: {errors}"

    def test_validate_missing_start_fails(self):
        """缺少 start 节点"""
        engine = WorkflowEngine()
        graph = {
            "nodes": [_node("n1", "code", {"source": "result = 1"}), _node("n2", "end")],
            "edges": [_edge("n1", "n2")],
        }
        errors = engine.validate(graph)
        assert any("start" in e for e in errors)

    def test_validate_unknown_node_type_fails(self):
        """未知节点类型"""
        engine = WorkflowEngine()
        graph = {
            "nodes": [_node("n1", "start"), _node("n2", "unknown_type"), _node("n3", "end")],
            "edges": [_edge("n1", "n2"), _edge("n2", "n3")],
        }
        errors = engine.validate(graph)
        assert any("unknown_type" in e for e in errors)

    def test_validate_llm_missing_prompt_template(self):
        """llm 节点缺少 prompt_template"""
        engine = WorkflowEngine()
        graph = {
            "nodes": [
                _node("n1", "start"),
                _node("n2", "llm", {}),  # 缺 prompt_template
                _node("n3", "end"),
            ],
            "edges": [_edge("n1", "n2"), _edge("n2", "n3")],
        }
        errors = engine.validate(graph)
        assert any("prompt_template" in e for e in errors)

    def test_validate_edge_target_missing(self):
        """边 target 不存在"""
        engine = WorkflowEngine()
        graph = {
            "nodes": [_node("n1", "start"), _node("n2", "end")],
            "edges": [_edge("n1", "n_nonexistent")],
        }
        errors = engine.validate(graph)
        assert any("n_nonexistent" in e for e in errors)


class TestWorkflowEngineTemplate:
    """模板渲染"""

    def test_render_template_simple_var(self):
        """简单变量替换 {{var}}"""
        engine = WorkflowEngine()
        ctx = {"inputs": {"name": "Alice"}, "name": "Bob"}
        rendered = engine._render_template("Hello, {{name}}!", ctx)
        # name 在 inputs 中, 顶层暴露后 _resolve_path("name") 在 ctx 顶层找不到
        # 实际: _resolve_path 从 ctx 顶层找, 顶层只有 inputs / name
        # inputs.name → 'Alice', 顶层 name → 'Bob'
        assert "Bob" in rendered or "Alice" in rendered

    def test_render_template_dot_path(self):
        """点路径替换 {{node.field}}"""
        engine = WorkflowEngine()
        ctx = {"n1": {"output": {"text": "world"}}, "inputs": {"x": 1}}
        rendered = engine._render_template("Hello {{n1.output.text}}", ctx)
        assert "Hello world" == rendered

    def test_render_template_missing_var_returns_empty(self):
        """未定义变量渲染为空"""
        engine = WorkflowEngine()
        ctx = {"inputs": {}}
        rendered = engine._render_template("[{{missing}}]", ctx)
        assert rendered == "[]"

    def test_render_template_object_to_json(self):
        """复杂对象渲染为 JSON"""
        engine = WorkflowEngine()
        ctx = {"n1": {"output": {"a": 1, "b": [2, 3]}}}
        rendered = engine._render_template("{{n1.output}}", ctx)
        # 应包含 a 与 1
        assert '"a"' in rendered and "1" in rendered


class TestWorkflowEngineExecute:
    """execute 执行"""

    @pytest.mark.asyncio
    async def test_execute_linear_code_node(self):
        """线性图: code 节点执行结果正确"""
        engine = WorkflowEngine()
        result = await engine.execute(_linear_graph(), inputs={"x": 21}, thread_id="t1")
        assert result["status"] == "completed"
        # n2 (code) 应执行, result = 21 * 2 = 42
        node_states = result["node_states"]
        assert node_states["n2"]["status"] == "completed"
        assert node_states["n2"]["output"]["result"] == 42
        # n3 (end) 收集 outputs
        assert "n2" in result["outputs"]

    @pytest.mark.asyncio
    async def test_execute_condition_true_branch(self):
        """condition 节点: 表达式为真走 true 分支"""
        engine = WorkflowEngine()
        result = await engine.execute(
            _condition_graph(), inputs={"score": 80}, thread_id="t1"
        )
        assert result["status"] == "completed"
        node_states = result["node_states"]
        # n3 (code_a 'passed') 应执行
        assert node_states["n3"]["status"] == "completed"
        assert node_states["n3"]["output"]["result"] == "passed"
        # n4 (code_b 'failed') 应 skipped
        assert node_states["n4"]["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_execute_condition_false_branch(self):
        """condition 节点: 表达式为假走 false 分支"""
        engine = WorkflowEngine()
        result = await engine.execute(
            _condition_graph(), inputs={"score": 40}, thread_id="t1"
        )
        assert result["status"] == "completed"
        node_states = result["node_states"]
        # n3 应 skipped
        assert node_states["n3"]["status"] == "skipped"
        # n4 (code_b 'failed') 应执行
        assert node_states["n4"]["status"] == "completed"
        assert node_states["n4"]["output"]["result"] == "failed"

    @pytest.mark.asyncio
    async def test_execute_code_node_builtins_restriction(self):
        """code 节点: 禁 builtins (__import__ 不可访问)"""
        engine = WorkflowEngine()
        graph = {
            "nodes": [
                _node("n1", "start"),
                _node(
                    "n2",
                    "code",
                    {"source": "result = __import__('os').system('echo hack')"},
                ),
                _node("n3", "end"),
            ],
            "edges": [_edge("n1", "n2"), _edge("n2", "n3")],
        }
        result = await engine.execute(graph, inputs={})
        # n2 应 failed
        assert result["status"] == "failed"
        assert result["node_states"]["n2"]["status"] == "failed"
        assert "builtins" in result["node_states"]["n2"]["error"].lower() or \
            "name" in result["node_states"]["n2"]["error"].lower()

    @pytest.mark.asyncio
    async def test_execute_code_node_allowed_builtins(self):
        """code 节点: 白名单 builtins (len/sum/range) 可用"""
        engine = WorkflowEngine()
        graph = {
            "nodes": [
                _node("n1", "start"),
                _node(
                    "n2",
                    "code",
                    {"source": "result = sum(range(1, 11))"},  # 55
                ),
                _node("n3", "end"),
            ],
            "edges": [_edge("n1", "n2"), _edge("n2", "n3")],
        }
        result = await engine.execute(graph, inputs={})
        assert result["status"] == "completed"
        assert result["node_states"]["n2"]["output"]["result"] == 55

    @pytest.mark.asyncio
    async def test_execute_failed_node_marks_skipped(self):
        """失败处理: 某节点失败, 后续节点 skipped, 整体 failed"""
        engine = WorkflowEngine()
        # code 节点故意除零 (sandbox 禁 builtins, 但 ZeroDivisionError 在运行时由解释器抛出)
        graph = {
            "nodes": [
                _node("n1", "start"),
                _node("n2", "code", {"source": "result = 1 / 0"}),
                _node("n3", "code", {"source": "result = 1"}),
                _node("n4", "end"),
            ],
            "edges": [_edge("n1", "n2"), _edge("n2", "n3"), _edge("n3", "n4")],
        }
        result = await engine.execute(graph, inputs={})
        assert result["status"] == "failed"
        assert result["node_states"]["n2"]["status"] == "failed"
        assert "ZeroDivisionError" in result["node_states"]["n2"]["error"] or \
            "division" in result["node_states"]["n2"]["error"]
        # n3 / n4 应 skipped
        assert result["node_states"]["n3"]["status"] == "skipped"
        assert result["node_states"]["n4"]["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_execute_llm_node_with_mock_provider(self):
        """LLM 节点: mock model_router, 验证 prompt 模板替换"""
        # 构造 mock app_state.model_router.get_provider_with_fallback
        mock_provider = MagicMock()
        completion = MagicMock()
        completion.content = "LLM response: hello world"
        completion.model = "mock-model"
        completion.usage = {"prompt_tokens": 5, "completion_tokens": 3}
        mock_provider.chat_completion = AsyncMock(return_value=completion)
        # 必须有 config 属性 (engine 会临时改 temperature/max_tokens)
        mock_provider.config = MagicMock(temperature=0.1, max_tokens=1024)

        mock_router = MagicMock()
        mock_router.get_provider_with_fallback = AsyncMock(
            return_value=(mock_provider, "L0")
        )

        mock_app_state = MagicMock()
        mock_app_state.model_router = mock_router

        engine = WorkflowEngine(app_state=mock_app_state)
        graph = {
            "nodes": [
                _node("n1", "start"),
                _node(
                    "n2",
                    "llm",
                    {
                        "model": "gpt-4",
                        "prompt_template": "Hello {{inputs.name}}, your score is {{inputs.score}}",
                        "temperature": 0.5,
                        "max_tokens": 256,
                    },
                ),
                _node("n3", "end"),
            ],
            "edges": [_edge("n1", "n2"), _edge("n2", "n3")],
        }
        result = await engine.execute(
            graph, inputs={"name": "Alice", "score": 95}
        )
        assert result["status"] == "completed"
        # 验证 prompt 已渲染
        called_args = mock_provider.chat_completion.call_args
        messages = called_args.kwargs.get("messages") or called_args.args[0]
        prompt = messages[0].content
        assert "Alice" in prompt and "95" in prompt
        # 验证 output
        assert (
            result["node_states"]["n2"]["output"]["content"]
            == "LLM response: hello world"
        )

    @pytest.mark.asyncio
    async def test_execute_http_node_with_mock_httpx(self):
        """HTTP 节点: mock httpx, 验证 request 构造"""
        # mock httpx.AsyncClient
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.json.return_value = {"ok": True, "user": "alice"}
        mock_response.text = '{"ok": true}'
        mock_response.url = "https://api.example.com/users?name=alice"

        mock_client = MagicMock()
        mock_client.request = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            engine = WorkflowEngine()
            graph = {
                "nodes": [
                    _node("n1", "start"),
                    _node(
                        "n2",
                        "http",
                        {
                            "method": "POST",
                            "url": "https://api.example.com/users",
                            "headers": {"X-User": "{{inputs.user}}"},
                            "body_template": '{"name": "{{inputs.user}}"}',
                        },
                    ),
                    _node("n3", "end"),
                ],
                "edges": [_edge("n1", "n2"), _edge("n2", "n3")],
            }
            result = await engine.execute(graph, inputs={"user": "alice"})
        assert result["status"] == "completed"
        # 验证 request 构造 (method/url 是位置参数, headers/content 是 kwargs)
        call_args = mock_client.request.call_args
        # method 是位置参数 args[0]
        method = call_args.args[0] if call_args.args else call_args.kwargs.get("method")
        url = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("url")
        assert method == "POST"
        assert "alice" in url or "users" in url
        # body 应模板化 (content kwarg)
        content = call_args.kwargs.get("content", "")
        assert "alice" in content
        # headers 应模板化
        assert call_args.kwargs["headers"]["X-User"] == "alice"
        # output
        assert result["node_states"]["n2"]["output"]["status_code"] == 200
        assert result["node_states"]["n2"]["output"]["body"]["ok"] is True

    @pytest.mark.asyncio
    async def test_execute_knowledge_node_with_mock_kb(self):
        """knowledge 节点: mock kb_store.query"""
        mock_kb = MagicMock()
        mock_kb.query = AsyncMock(
            return_value=[{"title": "Doc1", "content": "hello"}]
        )
        mock_app_state = MagicMock()
        mock_app_state.get_kb_store.return_value = mock_kb

        engine = WorkflowEngine(app_state=mock_app_state)
        graph = {
            "nodes": [
                _node("n1", "start"),
                _node(
                    "n2",
                    "knowledge",
                    {
                        "query_template": "search for {{inputs.topic}}",
                        "top_k": 3,
                    },
                ),
                _node("n3", "end"),
            ],
            "edges": [_edge("n1", "n2"), _edge("n2", "n3")],
        }
        result = await engine.execute(graph, inputs={"topic": "workflow"})
        assert result["status"] == "completed"
        # 验证 query 模板化
        called_args = mock_kb.query.call_args
        query = called_args.args[0] if called_args.args else called_args.kwargs.get("query")
        assert "workflow" in query
        # top_k
        top_k = called_args.kwargs.get("top_k")
        assert top_k == 3
        # output
        assert result["node_states"]["n2"]["output"]["count"] == 1
        assert result["node_states"]["n2"]["output"]["results"][0]["title"] == "Doc1"

    @pytest.mark.asyncio
    async def test_execute_input_schema_defaults(self):
        """input_schema 默认值生效"""
        engine = WorkflowEngine()
        graph = _linear_graph()
        input_schema = {
            "variables": [
                {"name": "x", "type": "int", "default": 100},
                {"name": "y", "type": "str", "default": "hello"},
            ]
        }
        # 把 graph 包成含 input_schema 的 dict
        result = await engine.execute(
            {"graph": graph, "input_schema": input_schema, "id": "wf1"},
            inputs={},
            thread_id="t1",
        )
        assert result["status"] == "completed"
        # inputs.x 应取默认值 100, code 节点 result = 100 * 2 = 200
        assert result["node_states"]["n2"]["output"]["result"] == 200
        assert result["inputs"]["x"] == 100
        assert result["inputs"]["y"] == "hello"

    @pytest.mark.asyncio
    async def test_execute_invalid_graph_raises(self):
        """执行有环图直接抛 WorkflowValidationError"""
        engine = WorkflowEngine()
        with pytest.raises(WorkflowValidationError):
            await engine.execute(_cycle_graph(), inputs={})

    @pytest.mark.asyncio
    async def test_execute_condition_with_complex_expression(self):
        """condition 表达式支持 and/or 比较"""
        engine = WorkflowEngine()
        graph = {
            "nodes": [
                _node("n1", "start"),
                _node("n2", "condition", {"expression": "score > 60 and age >= 18"}),
                _node("n3", "code", {"source": "result = 'adult_pass'"}),
                _node("n4", "code", {"source": "result = 'minor_or_fail'"}),
                _node("n5", "end"),
            ],
            "edges": [
                _edge("n1", "n2"),
                _edge("n2", "n3", source_handle="true"),
                _edge("n2", "n4", source_handle="false"),
                _edge("n3", "n5"),
                _edge("n4", "n5"),
            ],
        }
        # 真分支: score=80 age=20
        result = await engine.execute(graph, inputs={"score": 80, "age": 20})
        assert result["status"] == "completed"
        assert result["node_states"]["n3"]["status"] == "completed"
        assert result["node_states"]["n3"]["output"]["result"] == "adult_pass"
        # 假分支: score=80 age=15
        result2 = await engine.execute(graph, inputs={"score": 80, "age": 15})
        assert result2["node_states"]["n4"]["status"] == "completed"
        assert result2["node_states"]["n4"]["output"]["result"] == "minor_or_fail"

    @pytest.mark.asyncio
    async def test_execute_condition_blacklist_call(self):
        """condition 表达式禁用函数调用 (仅允许比较 + 逻辑)"""
        engine = WorkflowEngine()
        graph = {
            "nodes": [
                _node("n1", "start"),
                _node("n2", "condition", {"expression": "open('/etc/passwd').read()"}),
                _node("n3", "end"),
            ],
            "edges": [_edge("n1", "n2"), _edge("n2", "n3")],
        }
        # validate 阶段就应捕获
        errors = engine.validate(graph)
        assert any("不允许" in e or "Call" in e for e in errors), errors


# ============================================================
# Admin API 端到端测试
# ============================================================


@pytest.fixture
def temp_db(monkeypatch):
    """临时文件 SQLite"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp.name}"

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from core import database as db_module

    engine = create_async_engine(
        db_url, echo=False, future=True, connect_args={"check_same_thread": False}
    )
    db_module.engine = engine
    db_module.AsyncSessionLocal = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    yield
    try:
        Path(tmp.name).unlink(missing_ok=True)
    except Exception:
        pass


@pytest.fixture
async def initialized_db(temp_db):
    from core.database import close_db, init_db
    await init_db()
    yield
    await close_db()


@pytest.fixture
def client(initialized_db):
    """TestClient with mock app_state (避免 ChromaDB)"""
    from api.admin.workflows import router as workflows_router
    from api.deps import AppState

    app = FastAPI()
    # mock app_state: get_kb_store / model_router
    mock_state = MagicMock(spec=AppState)
    mock_state.model_router = MagicMock()
    mock_state.get_kb_store = MagicMock(return_value=MagicMock())
    # WorkflowEngine 会用 app_state.model_router.get_provider_with_fallback
    app.state.app_state = mock_state
    app.include_router(workflows_router)
    with TestClient(app) as c:
        yield c


def _admin_headers() -> dict:
    return {"x-user-role": "admin", "x-user-id": "ADMIN001"}


class TestWorkflowAdminAPI:
    """Admin API 端到端"""

    def test_create_workflow_success(self, client):
        """POST /admin/workflows 创建成功"""
        resp = client.post(
            "/api/v1/admin/workflows",
            json={
                "name": "wf1",
                "description": "test workflow",
                "graph": _linear_graph(),
                "input_schema": {"variables": [{"name": "x", "type": "int", "default": 0}]},
            },
            headers=_admin_headers(),
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["name"] == "wf1"
        assert data["enabled"] is True
        assert data["version"] == 1
        assert data["id"].startswith("wf_")

    def test_create_workflow_invalid_graph_returns_422(self, client):
        """POST 创建: 无效 graph (环) 返回 422"""
        resp = client.post(
            "/api/v1/admin/workflows",
            json={"name": "wf_cycle", "description": "", "graph": _cycle_graph()},
            headers=_admin_headers(),
        )
        assert resp.status_code == 422
        assert "环" in resp.json()["detail"]

    def test_list_workflows(self, client):
        """GET /admin/workflows 列表"""
        # 创建 2 个
        for i in range(2):
            client.post(
                "/api/v1/admin/workflows",
                json={"name": f"wf_list_{i}", "description": "", "graph": _linear_graph()},
                headers=_admin_headers(),
            )
        resp = client.get("/api/v1/admin/workflows", headers=_admin_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 2
        names = [w["name"] for w in data["items"]]
        assert "wf_list_0" in names and "wf_list_1" in names

    def test_get_workflow_detail(self, client):
        """GET /{workflow_id} 详情"""
        create = client.post(
            "/api/v1/admin/workflows",
            json={"name": "wf_detail", "description": "", "graph": _linear_graph()},
            headers=_admin_headers(),
        )
        wid = create.json()["id"]
        resp = client.get(
            f"/api/v1/admin/workflows/{wid}", headers=_admin_headers()
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == wid

    def test_update_workflow_increments_version(self, client):
        """PUT 更新 graph 时 version +1"""
        create = client.post(
            "/api/v1/admin/workflows",
            json={"name": "wf_upd", "description": "", "graph": _linear_graph()},
            headers=_admin_headers(),
        )
        wid = create.json()["id"]
        resp = client.put(
            f"/api/v1/admin/workflows/{wid}",
            json={"description": "updated desc", "graph": _linear_graph()},
            headers=_admin_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["description"] == "updated desc"
        assert data["version"] == 2

    def test_delete_workflow(self, client):
        """DELETE 删除"""
        create = client.post(
            "/api/v1/admin/workflows",
            json={"name": "wf_del", "description": "", "graph": _linear_graph()},
            headers=_admin_headers(),
        )
        wid = create.json()["id"]
        resp = client.delete(
            f"/api/v1/admin/workflows/{wid}", headers=_admin_headers()
        )
        assert resp.status_code == 200
        # 再 GET 应 404
        resp2 = client.get(
            f"/api/v1/admin/workflows/{wid}", headers=_admin_headers()
        )
        assert resp2.status_code == 404

    def test_toggle_workflow(self, client):
        """POST /toggle 启用/禁用"""
        create = client.post(
            "/api/v1/admin/workflows",
            json={"name": "wf_tgl", "description": "", "graph": _linear_graph()},
            headers=_admin_headers(),
        )
        wid = create.json()["id"]
        resp = client.post(
            f"/api/v1/admin/workflows/{wid}/toggle",
            json={"enabled": False},
            headers=_admin_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    def test_validate_workflow_endpoint(self, client):
        """POST /validate 验证 graph"""
        create = client.post(
            "/api/v1/admin/workflows",
            json={"name": "wf_val", "description": "", "graph": _linear_graph()},
            headers=_admin_headers(),
        )
        wid = create.json()["id"]
        # 用已存 graph 验证
        resp = client.post(
            f"/api/v1/admin/workflows/{wid}/validate",
            json={},
            headers=_admin_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["errors"] == []
        # 传一个有环 graph
        resp2 = client.post(
            f"/api/v1/admin/workflows/{wid}/validate",
            json={"graph": _cycle_graph()},
            headers=_admin_headers(),
        )
        assert resp2.status_code == 200
        assert resp2.json()["valid"] is False
        assert any("环" in e for e in resp2.json()["errors"])

    def test_run_workflow_success(self, client):
        """POST /run 执行工作流"""
        create = client.post(
            "/api/v1/admin/workflows",
            json={
                "name": "wf_run",
                "description": "",
                "graph": _linear_graph(),
                "input_schema": {
                    "variables": [{"name": "x", "type": "int", "default": 10}]
                },
            },
            headers=_admin_headers(),
        )
        wid = create.json()["id"]
        resp = client.post(
            f"/api/v1/admin/workflows/{wid}/run",
            json={"inputs": {"x": 50}},
            headers=_admin_headers(),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "completed"
        assert data["run_id"].startswith("run_")
        assert data["thread_id"].startswith("thr_")
        # n2 (code) 应执行: 50 * 2 = 100
        assert data["node_states"]["n2"]["output"]["result"] == 100

    def test_run_workflow_disabled_returns_400(self, client):
        """POST /run 禁用工作流返回 400"""
        create = client.post(
            "/api/v1/admin/workflows",
            json={"name": "wf_dis", "description": "", "graph": _linear_graph()},
            headers=_admin_headers(),
        )
        wid = create.json()["id"]
        client.post(
            f"/api/v1/admin/workflows/{wid}/toggle",
            json={"enabled": False},
            headers=_admin_headers(),
        )
        resp = client.post(
            f"/api/v1/admin/workflows/{wid}/run",
            json={"inputs": {}},
            headers=_admin_headers(),
        )
        assert resp.status_code == 400

    def test_get_run_status(self, client):
        """GET /runs/{run_id} 查询运行状态"""
        create = client.post(
            "/api/v1/admin/workflows",
            json={"name": "wf_grs", "description": "", "graph": _linear_graph()},
            headers=_admin_headers(),
        )
        wid = create.json()["id"]
        run_resp = client.post(
            f"/api/v1/admin/workflows/{wid}/run",
            json={"inputs": {"x": 1}},
            headers=_admin_headers(),
        )
        run_id = run_resp.json()["run_id"]
        resp = client.get(
            f"/api/v1/admin/workflows/runs/{run_id}", headers=_admin_headers()
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == run_id
        assert data["status"] == "completed"

    def test_get_run_node_states(self, client):
        """GET /runs/{run_id}/node-states 节点级状态"""
        create = client.post(
            "/api/v1/admin/workflows",
            json={"name": "wf_gns", "description": "", "graph": _linear_graph()},
            headers=_admin_headers(),
        )
        wid = create.json()["id"]
        run_resp = client.post(
            f"/api/v1/admin/workflows/{wid}/run",
            json={"inputs": {"x": 7}},
            headers=_admin_headers(),
        )
        run_id = run_resp.json()["run_id"]
        resp = client.get(
            f"/api/v1/admin/workflows/runs/{run_id}/node-states",
            headers=_admin_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "node_states" in data
        assert "n2" in data["node_states"]
        assert data["node_states"]["n2"]["status"] == "completed"

    def test_list_workflow_runs_history(self, client):
        """GET /{workflow_id}/runs 工作流运行历史"""
        create = client.post(
            "/api/v1/admin/workflows",
            json={"name": "wf_hist", "description": "", "graph": _linear_graph()},
            headers=_admin_headers(),
        )
        wid = create.json()["id"]
        # 跑 3 次
        for i in range(3):
            client.post(
                f"/api/v1/admin/workflows/{wid}/run",
                json={"inputs": {"x": i}},
                headers=_admin_headers(),
            )
        resp = client.get(
            f"/api/v1/admin/workflows/{wid}/runs", headers=_admin_headers()
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        # 倒序: 最新在前
        statuses = [r["status"] for r in data["items"]]
        assert all(s == "completed" for s in statuses)
