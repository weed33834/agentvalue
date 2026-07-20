"""
工作流可视化编排数据模型 (P4-2: 工作流可视化编排, 对标 Dify Workflow / Coze Bot 编排)

实体:
- Workflow: 工作流定义 (DAG 图 + 输入变量 schema + 启用状态 + 版本)
- WorkflowRun: 工作流运行实例 (状态 + 输入/输出 + 节点级执行状态)

DAG 结构 (graph 字段):
    {
        "nodes": [
            {"id": "n1", "type": "start", "position": {"x": 0, "y": 0}, "data": {"config": {}}},
            {"id": "n2", "type": "llm", "position": {"x": 250, "y": 0}, "data": {"config": {...}}},
            {"id": "n3", "type": "end", "position": {"x": 500, "y": 0}, "data": {"config": {}}}
        ],
        "edges": [
            {"source": "n1", "target": "n2", "source_handle": None},
            {"source": "n2", "target": "n3", "source_handle": None}
        ]
    }

节点类型 (与 core.workflow_engine.WorkflowEngine.NODE_TYPES 对齐):
- start: 起点 (只读 inputs)
- llm: LLM 调用 (config: model / prompt_template / temperature / max_tokens)
- http: HTTP 请求 (config: method / url / headers / body_template)
- condition: 条件分支 (config: expression, source_handle 取 true/false 路由)
- code: 代码执行 (受限 Python sandbox, config: source)
- knowledge: 知识库检索 (config: query_template / top_k)
- end: 终点 (输出 outputs)
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class Workflow(Base):
    """工作流定义 (DAG + 输入 schema + 启用状态)

    对标 Dify Workflow:
    - 用户在 Vue Flow 画布拖拽节点 + 连线, 保存为 graph JSON
    - 后端解释执行引擎按拓扑排序顺序执行各节点
    - 同一 name+tenant 维度下可保留多个工作流 (id 唯一)
    """

    __tablename__ = "workflows"

    # 主键: 业务层生成的 cuid/uuid (32 字符)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 工作流名 (展示用)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 工作流描述
    description: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", server_default=""
    )
    # DAG 定义: {nodes: [...], edges: [...]} (节点 + 连线, 含位置信息供前端画布还原)
    graph: Mapped[dict] = mapped_column(JSON, nullable=False)
    # 输入变量 schema: {"variables": [{"name", "type", "default"}]}
    input_schema: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default=func.json("{}")
    )
    # 启用状态 (禁用的工作流不可运行)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=func.text("1")
    )
    # 版本号 (每次保存递增, 用于乐观并发控制 / 历史回溯)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    # 租户 ID (多租户隔离, 默认 default)
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default", server_default="default"
    )
    # 创建时间 / 更新时间 (DB server_default, 避免应用时区不一致)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        # name 索引: 列表搜索/排序常用
        Index("ix_workflows_name", "name"),
        # tenant_id 索引: 多租户过滤
        Index("ix_workflows_tenant", "tenant_id"),
        # enabled 索引: 列表过滤常用
        Index("ix_workflows_enabled", "enabled"),
    )


class WorkflowRun(Base):
    """工作流运行实例 (一次 execute 调用对应一行)

    状态机:
    - pending: 已创建未开始
    - running: 执行中
    - completed: 全部节点执行完成
    - failed: 某节点失败, 后续节点不执行
    - interrupted: 被外部中断 (条件分支停止等)

    node_states 结构 (节点级执行状态):
        {
            "n1": {"status": "completed", "started_at": "...", "completed_at": "...", "output": {...}},
            "n2": {"status": "failed", "started_at": "...", "error": "..."},
            "n3": {"status": "skipped"}  # 因 n2 失败未执行
        }
    """

    __tablename__ = "workflow_runs"

    # 主键: 业务层生成的 cuid/uuid
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 关联的工作流 ID (软关联, 不加外键, 工作流删除后 run 仍可查)
    workflow_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # 线程 ID (可关联到 LangGraph thread / trace, 同一 thread 可重放)
    thread_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # 运行状态: pending / running / completed / failed / interrupted
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    # 输入变量 (input_schema 描述的变量值)
    inputs: Mapped[dict] = mapped_column(JSON, nullable=False)
    # 输出 (end 节点的 outputs)
    outputs: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # 节点级执行状态: {node_id: {status, started_at, completed_at, output, error?}}
    node_states: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default=func.json("{}")
    )
    # 创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 完成时间 (running 时为 None)
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # workflow_id + created_at: 工作流运行历史按时间倒序
        Index("ix_workflow_run_workflow_created", "workflow_id", "created_at"),
        # status 索引: 列表过滤常用
        Index("ix_workflow_run_status", "status"),
    )
