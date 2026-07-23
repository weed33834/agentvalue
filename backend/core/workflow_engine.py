"""
工作流 DAG 解释执行引擎 (P4-2: 工作流可视化编排, 对标 Dify Workflow / Coze Bot 编排)

设计:
- 拓扑排序后顺序执行各节点, condition 节点根据表达式结果选择 source_handle 路由
- 失败节点标记 failed, 后续节点标记 skipped, 整体 status = failed
- 节点间数据通过 context (dict) 传递, 每个节点 output 写入 context[node_id]
- 模板渲染: {{var}} 替换 (递归支持 {{node_id.field}})
- 安全约束: code 节点 exec 时禁 builtins, 仅允许少量白名单
- condition 节点 eval 时禁 builtins, 仅允许比较 + 逻辑运算

节点类型 (与前端 NODE_TYPES 对齐):
- start: 起点 (只读 inputs, 写入 context.inputs)
- llm: LLM 调用 (config: model / prompt_template / temperature / max_tokens)
- http: HTTP 请求 (config: method / url / headers / body_template)
- condition: 条件分支 (config: expression, source_handle 取 true/false 路由)
- code: 代码执行 (受限 Python sandbox, config: source)
- knowledge: 知识库检索 (config: query_template / top_k)
- end: 终点 (输出 outputs)
- loop: 循环节点 (config: items / item_var / body / break_when), 对 items 列表逐个执行 body 子节点
- parallel: 并行节点 (config: branches), 使用 asyncio.gather 并行执行所有分支
"""

from __future__ import annotations

import asyncio
import ast
import ipaddress
import logging
import re
import socket
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ============================================================
# 异常类型
# ============================================================


class WorkflowValidationError(ValueError):
    """工作流图校验失败 (环 / 必填字段缺失 / 节点类型未知)"""


class WorkflowExecutionError(RuntimeError):
    """节点执行失败"""


# ============================================================
# 安全的代码 / 条件表达式求值
# ============================================================

# code 节点允许的 builtins 白名单 (其余一律禁用, 防止逃逸)
#
# 安全说明: 已移除 getattr / hasattr / isinstance 这三个内省函数
# - getattr(obj, name): 可通过动态属性名访问对象任意属性/方法, 结合其它对象
#   (如函数对象的 __globals__ / __builtins__) 可间接读取受保护命名空间,
#   从而拿到 __import__ 等危险能力, 导致沙箱逃逸。
# - hasattr(obj, name): 本质是 getattr 的封装, 同样可触发任意属性访问及
#   魔术方法 (如 __class__), 间接获得反射能力逃逸沙箱。
# - isinstance(obj, cls): 可访问对象的 __class__ / __mro__ 等内省属性,
#   配合类型层级链定位到 object 等基类, 进而获取其方法 (如 __subclasses__),
#   最终突破沙箱访问任意模块/函数。
_CODE_ALLOWED_BUILTINS = {
    "abs": abs,
    "min": min,
    "max": max,
    "sum": sum,
    "len": len,
    "round": round,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "set": set,
    "range": range,
    "sorted": sorted,
    "enumerate": enumerate,
    "zip": zip,
    "map": map,
    "filter": filter,
    "any": any,
    "all": all,
    "Print": print,  # 故意大写以保留 print 能力但不让用户直接调 print()
}


def _safe_builtins() -> Dict[str, Any]:
    """构造安全的 builtins 字典 (用于 exec / eval)"""
    return {"__builtins__": _CODE_ALLOWED_BUILTINS}


# ============================================================
# SSRF 防护: 内网地址黑名单
# ============================================================

# 禁止访问的内网 / 本地地址段 (H7: HTTP 节点 SSRF 防护)
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),  # 私有网络 A 类
    ipaddress.ip_network("172.16.0.0/12"),  # 私有网络 B 类
    ipaddress.ip_network("192.168.0.0/16"),  # 私有网络 C 类
    ipaddress.ip_network(
        "169.254.0.0/16"
    ),  # 链路本地地址 (含云元数据服务 169.254.169.254)
    ipaddress.ip_network("127.0.0.0/8"),  # 环回地址
    ipaddress.ip_network("::1/128"),  # IPv6 环回地址
]


def _is_internal_url(url: str) -> bool:
    """检查 URL 是否指向内网 / 本地地址 (SSRF 防护)

    判定规则:
    1. 仅允许 http / https 协议, 其它协议一律视为不安全
    2. 解析出的 host 为 IP 字面量时, 落入黑名单段则视为不安全
    3. host 为域名时, 进一步解析 DNS, 任一解析结果落入黑名单则视为不安全
       (防止通过域名绕过 IP 黑名单, 如 DNS rebinding); DNS 无法解析时不
       视为内部地址 (交由 httpx 在连接阶段自然失败), 避免对临时不可解析
       的合法域名误判。

    Returns:
        True 表示该 URL 不安全 (内部地址), 应阻止访问
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return True
    # 仅允许 http / https 协议
    if parsed.scheme not in ("http", "https"):
        return True
    host = parsed.hostname
    if not host:
        return True
    host = host.strip().lower()
    # localhost 等本地名称直接拦截
    if host in ("localhost",):
        return True
    # IP 字面量直接判定
    try:
        ip = ipaddress.ip_address(host)
        return any(ip in net for net in _BLOCKED_NETWORKS)
    except ValueError:
        # 不是 IP 字面量, 是域名, 继续 DNS 解析
        pass
    # 解析域名得到 IP, 检查是否落在内网段
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # 无法解析的域名不视为内部地址, 交由 httpx 在连接阶段处理,
        # 避免对临时不可解析的合法域名 (如沙箱/离线环境) 误判
        return False
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if any(ip in net for net in _BLOCKED_NETWORKS):
            return True
    return False


# 条件表达式 AST 白名单: 仅允许 BinOp / Compare / BoolOp / 常量 / Name / Load
_CONDITION_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.Compare,
    ast.Gt,
    ast.GtE,
    ast.Lt,
    ast.LtE,
    ast.Eq,
    ast.NotEq,
    ast.Is,
    ast.IsNot,
    ast.In,
    ast.NotIn,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Mod,
    ast.FloorDiv,
    ast.Pow,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.Dict,
)


def _validate_condition_ast(node: ast.AST) -> None:
    """递归校验条件表达式 AST 只包含白名单节点类型"""
    if not isinstance(node, _CONDITION_ALLOWED_NODES):
        raise WorkflowValidationError(
            f"条件表达式中包含不允许的语法: {type(node).__name__}"
        )
    for child in ast.iter_child_nodes(node):
        _validate_condition_ast(child)


# ============================================================
# 模板渲染
# ============================================================

# {{var}} 或 {{node_id.field}} 形式
_TEMPLATE_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


def _resolve_path(context: Dict[str, Any], path: str) -> Any:
    """从 context 解析点路径 (如 'n2.output.text' → context['n2']['output']['text'])

    支持简写:
    - 'inputs.user_input' → context['inputs']['user_input']
    - 'n2' → context['n2']
    - 'n2.output' → context['n2']['output']
    """
    parts = [p.strip() for p in path.split(".") if p.strip()]
    if not parts:
        return None
    cur: Any = context
    for part in parts:
        if isinstance(cur, dict):
            if part in cur:
                cur = cur[part]
            else:
                return None
        elif isinstance(cur, list):
            try:
                idx = int(part)
            except ValueError:
                return None
            if 0 <= idx < len(cur):
                cur = cur[idx]
            else:
                return None
        else:
            return None
    return cur


# ============================================================
# 工作流引擎
# ============================================================


class WorkflowEngine:
    """DAG 解释执行器

    用法:
        engine = WorkflowEngine(app_state)
        run = await engine.execute(workflow, inputs={"user_input": "hello"}, thread_id="t1")
    """

    # 支持的节点类型 (与前端 NODE_TYPES 对齐)
    NODE_TYPES = {
        "start",
        "llm",
        "http",
        "condition",
        "code",
        "knowledge",
        "end",
        "loop",
        "parallel",
    }

    # H8: 循环节点单次最大迭代次数上限, 超出则拒绝执行 (防止资源耗尽 / 死循环)
    MAX_LOOP_ITERATIONS = 1000
    # H8: 整个工作流执行的最大超时时间 (秒), 超出则取消
    EXECUTE_TIMEOUT_SECONDS = 300

    def __init__(self, app_state: Any = None):
        """app_state 可选, 提供 model_router / kb_store (None 时降级为 mock)"""
        self.app_state = app_state

    # ===================== 校验 =====================

    def validate(self, workflow_or_graph: Any) -> List[str]:
        """校验工作流图合法性, 返回错误列表 (空表示通过)

        检查项:
        - graph 必含 nodes / edges
        - 节点 id 唯一
        - 节点 type 在 NODE_TYPES 内
        - 节点 config 必填字段
        - edges source/target 必须指向存在的节点
        - 必须有且仅有一个 start 节点和一个 end 节点
        - 无环 (拓扑排序可执行)
        """
        errors: List[str] = []
        # 兼容 Workflow 对象 / dict
        graph = self._extract_graph(workflow_or_graph)

        if not isinstance(graph, dict):
            errors.append("graph 必须是 dict")
            return errors

        nodes = graph.get("nodes")
        edges = graph.get("edges")
        if not isinstance(nodes, list):
            errors.append("graph.nodes 必须是 list")
            return errors
        if not isinstance(edges, list):
            errors.append("graph.edges 必须是 list")
            return errors

        # 节点 id 唯一性
        node_ids: List[str] = []
        node_map: Dict[str, dict] = {}
        for n in nodes:
            if not isinstance(n, dict):
                errors.append(f"节点必须是 dict, 实际: {type(n).__name__}")
                continue
            nid = n.get("id")
            if not nid:
                errors.append("节点缺少 id 字段")
                continue
            if nid in node_map:
                errors.append(f"节点 id 重复: {nid}")
                continue
            ntype = n.get("type")
            if ntype not in self.NODE_TYPES:
                errors.append(
                    f"节点 {nid} 类型 {ntype!r} 不支持, 允许: {sorted(self.NODE_TYPES)}"
                )
            node_ids.append(nid)
            node_map[nid] = n
            # config 必填校验
            cfg_errors = self._validate_node_config(n)
            errors.extend(cfg_errors)

        # start / end 节点数量
        start_count = sum(1 for n in node_map.values() if n.get("type") == "start")
        end_count = sum(1 for n in node_map.values() if n.get("type") == "end")
        if start_count == 0:
            errors.append("缺少 start 节点")
        elif start_count > 1:
            errors.append(f"只能有一个 start 节点, 实际: {start_count}")
        if end_count == 0:
            errors.append("缺少 end 节点")
        elif end_count > 1:
            errors.append(f"只能有一个 end 节点, 实际: {end_count}")

        # edges 引用合法性
        for e in edges:
            if not isinstance(e, dict):
                errors.append(f"边必须是 dict, 实际: {type(e).__name__}")
                continue
            src = e.get("source")
            tgt = e.get("target")
            if not src:
                errors.append("边缺少 source")
            elif src not in node_map:
                errors.append(f"边 source {src!r} 不存在于节点列表")
            if not tgt:
                errors.append("边缺少 target")
            elif tgt not in node_map:
                errors.append(f"边 target {tgt!r} 不存在于节点列表")

        # 环检测 (拓扑排序)
        if not errors:
            cycle = self._detect_cycle(node_map, edges)
            if cycle:
                errors.append(f"图中存在环: {' → '.join(cycle)}")

        return errors

    def _validate_node_config(self, node: dict) -> List[str]:
        """校验单个节点 config 必填字段"""
        nid = node.get("id", "?")
        ntype = node.get("type")
        cfg = node.get("data", {}).get("config") or {}
        errors: List[str] = []
        if ntype == "llm":
            if not cfg.get("prompt_template"):
                errors.append(f"节点 {nid} (llm) 缺少 config.prompt_template")
        elif ntype == "http":
            if not cfg.get("url"):
                errors.append(f"节点 {nid} (http) 缺少 config.url")
            if not cfg.get("method"):
                errors.append(f"节点 {nid} (http) 缺少 config.method")
        elif ntype == "condition":
            if not cfg.get("expression"):
                errors.append(f"节点 {nid} (condition) 缺少 config.expression")
            # 条件表达式语法预校验
            try:
                self._compile_condition(cfg["expression"])
            except WorkflowValidationError as e:
                errors.append(f"节点 {nid} 条件表达式非法: {e}")
        elif ntype == "code":
            if not cfg.get("source"):
                errors.append(f"节点 {nid} (code) 缺少 config.source")
            # 语法预编译
            try:
                compile(cfg["source"], f"<workflow:{nid}>", "exec")
            except SyntaxError as e:
                errors.append(f"节点 {nid} 代码语法错误: {e}")
        elif ntype == "knowledge":
            if not cfg.get("query_template"):
                errors.append(f"节点 {nid} (knowledge) 缺少 config.query_template")
        elif ntype == "loop":
            # 循环节点: items (列表路径) + body (子节点列表)
            if not cfg.get("items"):
                errors.append(f"节点 {nid} (loop) 缺少 config.items")
            body = cfg.get("body")
            if not isinstance(body, list) or not body:
                errors.append(f"节点 {nid} (loop) 缺少 config.body (子节点列表)")
            else:
                # 递归校验 body 中的子节点
                for i, sub in enumerate(body):
                    if not isinstance(sub, dict):
                        errors.append(f"节点 {nid} (loop) body[{i}] 必须是 dict")
                        continue
                    sub_type = sub.get("type")
                    if sub_type not in self.NODE_TYPES:
                        errors.append(
                            f"节点 {nid} (loop) body[{i}] 类型 {sub_type!r} 不支持"
                        )
                        continue
                    # loop/parallel 不允许嵌套 (避免复杂度过高)
                    if sub_type in ("loop", "parallel"):
                        errors.append(
                            f"节点 {nid} (loop) body[{i}] 不允许嵌套 {sub_type} 节点"
                        )
                        continue
                    errors.extend(self._validate_node_config(sub))
            # break_when 表达式预校验 (可选)
            if cfg.get("break_when"):
                try:
                    self._compile_condition(cfg["break_when"])
                except WorkflowValidationError as e:
                    errors.append(f"节点 {nid} (loop) break_when 表达式非法: {e}")
        elif ntype == "parallel":
            # 并行节点: branches (分支列表, 每个分支含 nodes 子节点列表)
            branches = cfg.get("branches")
            if not isinstance(branches, list) or not branches:
                errors.append(f"节点 {nid} (parallel) 缺少 config.branches (分支列表)")
            else:
                for i, branch in enumerate(branches):
                    if not isinstance(branch, dict):
                        errors.append(
                            f"节点 {nid} (parallel) branches[{i}] 必须是 dict"
                        )
                        continue
                    bnodes = branch.get("nodes")
                    if not isinstance(bnodes, list) or not bnodes:
                        errors.append(
                            f"节点 {nid} (parallel) branches[{i}].nodes 必须为非空 list"
                        )
                        continue
                    for j, sub in enumerate(bnodes):
                        if not isinstance(sub, dict):
                            errors.append(
                                f"节点 {nid} (parallel) branches[{i}].nodes[{j}] 必须是 dict"
                            )
                            continue
                        sub_type = sub.get("type")
                        if sub_type not in self.NODE_TYPES:
                            errors.append(
                                f"节点 {nid} (parallel) branches[{i}].nodes[{j}] 类型 {sub_type!r} 不支持"
                            )
                            continue
                        if sub_type in ("loop", "parallel"):
                            errors.append(
                                f"节点 {nid} (parallel) branches[{i}].nodes[{j}] 不允许嵌套 {sub_type} 节点"
                            )
                            continue
                        errors.extend(self._validate_node_config(sub))
        return errors

    def _detect_cycle(self, node_map: Dict[str, dict], edges: List[dict]) -> List[str]:
        """拓扑排序检测环, 返回环路径 (空表示无环)"""
        # 构造邻接表
        adj: Dict[str, List[str]] = {nid: [] for nid in node_map}
        for e in edges:
            src = e.get("source")
            tgt = e.get("target")
            if src in adj and tgt in node_map:
                adj[src].append(tgt)
        # DFS 检测环 (灰白黑染色)
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {nid: WHITE for nid in node_map}
        path: List[str] = []
        cycle_path: List[str] = []

        def _dfs(u: str) -> bool:
            color[u] = GRAY
            path.append(u)
            for v in adj[u]:
                if color[v] == GRAY:
                    # 找到环: path 中从 v 到 u
                    idx = path.index(v)
                    cycle_path.extend(path[idx:] + [v])
                    return True
                if color[v] == WHITE:
                    if _dfs(v):
                        return True
            path.pop()
            color[u] = BLACK
            return False

        for nid in node_map:
            if color[nid] == WHITE:
                if _dfs(nid):
                    return cycle_path
        return []

    def _topological_order(
        self, node_map: Dict[str, dict], edges: List[dict]
    ) -> List[str]:
        """Kahn 算法拓扑排序, 返回节点 id 顺序 (无环时唯一)"""
        in_degree: Dict[str, int] = {nid: 0 for nid in node_map}
        adj: Dict[str, List[str]] = {nid: [] for nid in node_map}
        for e in edges:
            src = e.get("source")
            tgt = e.get("target")
            if src in adj and tgt in adj:
                adj[src].append(tgt)
                in_degree[tgt] += 1
        # 入度为 0 的节点入队
        queue = [nid for nid, d in in_degree.items() if d == 0]
        order: List[str] = []
        while queue:
            u = queue.pop(0)
            order.append(u)
            for v in adj[u]:
                in_degree[v] -= 1
                if in_degree[v] == 0:
                    queue.append(v)
        if len(order) != len(node_map):
            # 有环 (理论上 validate 已拦截)
            raise WorkflowValidationError("图中存在环, 拓扑排序失败")
        return order

    # ===================== 执行 =====================

    async def execute(
        self,
        workflow: Any,
        inputs: Optional[Dict[str, Any]] = None,
        thread_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """执行工作流

        Args:
            workflow: Workflow 对象 (必须有 .graph / .input_schema 属性) 或 dict {graph, input_schema}
            inputs: 输入变量值 (key 与 input_schema.variables[].name 对齐)
            thread_id: 线程 ID (用于关联 trace, 不传则自动生成)

        Returns:
            模拟的 WorkflowRun dict (含 status / inputs / outputs / node_states / thread_id)
            注: 不写库, 由调用方 (admin route) 落库

        H8: 整体执行受 EXECUTE_TIMEOUT_SECONDS 超时保护, 超时则抛出
        WorkflowExecutionError, 防止恶意/异常工作流长时间占用资源。
        """
        try:
            return await asyncio.wait_for(
                self._execute_impl(workflow, inputs, thread_id),
                timeout=self.EXECUTE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as e:
            raise WorkflowExecutionError(
                f"工作流执行超时 (上限 {self.EXECUTE_TIMEOUT_SECONDS} 秒)"
            ) from e

    async def _execute_impl(
        self,
        workflow: Any,
        inputs: Optional[Dict[str, Any]] = None,
        thread_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """实际工作流执行逻辑 (由 execute 包装超时保护)"""
        # 1. 提取 graph / input_schema
        graph = self._extract_graph(workflow)
        input_schema = self._extract_input_schema(workflow)
        inputs = inputs or {}

        # 2. 校验 graph (执行前再次检查, 防止绕过 validate 直接运行)
        errors = self.validate(graph)
        if errors:
            raise WorkflowValidationError(f"工作流图校验失败: {'; '.join(errors)}")

        # 3. 应用 input_schema 默认值
        normalized_inputs = self._apply_input_schema(input_schema, inputs)

        # 4. 构造执行上下文
        thread_id = thread_id or f"thr_{uuid.uuid4().hex[:16]}"
        run_id = f"run_{uuid.uuid4().hex[:16]}"
        context: Dict[str, Any] = {
            "inputs": dict(normalized_inputs),
            "variables": dict(normalized_inputs),
        }
        node_states: Dict[str, Dict[str, Any]] = {}
        # 节点 id → node dict
        node_map: Dict[str, dict] = {
            n["id"]: n for n in graph["nodes"] if isinstance(n, dict) and "id" in n
        }
        edges: List[dict] = [e for e in graph.get("edges", []) if isinstance(e, dict)]

        # 5. 拓扑排序
        order = self._topological_order(node_map, edges)

        # 6. 顺序执行各节点
        # 用于跳过不可达节点 (condition 分支未走的下游标 skipped)
        # 简化: 默认全部节点都按拓扑顺序执行;
        # condition 节点执行后, 根据表达式结果选择 source_handle 路由,
        # 未被选中的下游节点标 skipped
        executed: set = set()
        skipped: set = set()

        # 记录 condition 节点选择: source_node_id → 被选中的 source_handle
        condition_choice: Dict[str, Optional[str]] = {}

        for nid in order:
            node = node_map[nid]
            ntype = node.get("type")

            # 已被跳过的节点 (上游 condition 未选) 直接 skipped
            if nid in skipped:
                node_states[nid] = {
                    "status": "skipped",
                    "started_at": None,
                    "completed_at": None,
                }
                continue

            # 检查所有上游节点是否都已执行 (跳过条件分支未选的)
            upstream_edges = [e for e in edges if e.get("target") == nid]
            upstream_ok = True
            for e in upstream_edges:
                src = e.get("source")
                # 上游节点必须是 executed 或 skipped 状态
                if src not in executed and src not in skipped:
                    upstream_ok = False
                    break
                # 如果上游是 condition, 检查是否选择了对应 source_handle
                src_node = node_map.get(src, {})
                if src_node.get("type") == "condition":
                    chosen_handle = condition_choice.get(src)
                    edge_handle = e.get("source_handle")
                    # 若上游 condition 选择了一个 handle, 且当前边的 handle 不匹配, 跳过
                    if chosen_handle is not None and edge_handle is not None:
                        if chosen_handle != edge_handle:
                            upstream_ok = False
                            break
            if not upstream_ok:
                node_states[nid] = {
                    "status": "skipped",
                    "started_at": None,
                    "completed_at": None,
                }
                skipped.add(nid)
                continue

            # 执行节点
            started_at = self._now_iso()
            try:
                result = await self._execute_node(node, context)
                completed_at = self._now_iso()
                node_states[nid] = {
                    "status": "completed",
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "output": result.get("output"),
                }
                # 把节点 output 写入 context, 让下游节点可通过 {{node_id.field}} 引用
                context[nid] = result.get("output")
                executed.add(nid)
                # condition 节点: 记录选择
                if ntype == "condition":
                    condition_choice[nid] = result.get("branch")
                # end 节点: 收集 outputs
                if ntype == "end":
                    context.setdefault("outputs", {}).update(result.get("output") or {})
            except Exception as e:
                completed_at = self._now_iso()
                node_states[nid] = {
                    "status": "failed",
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "error": str(e),
                }
                # 后续所有未执行节点标记 skipped
                # (拓扑顺序下, 后续节点都因上游失败而 skipped)
                logger.warning("工作流节点 %s (%s) 执行失败: %s", nid, ntype, e)
                run_status = "failed"
                # 把后续未执行的节点标 skipped
                for later_nid in order:
                    if later_nid == nid:
                        break
                # 找到 nid 在 order 中的位置, 之后全部 skipped
                try:
                    idx = order.index(nid)
                    for later_nid in order[idx + 1 :]:
                        if later_nid not in executed and later_nid not in node_states:
                            node_states[later_nid] = {
                                "status": "skipped",
                                "started_at": None,
                                "completed_at": None,
                            }
                            skipped.add(later_nid)
                except ValueError:
                    pass
                return {
                    "id": run_id,
                    "workflow_id": self._extract_workflow_id(workflow),
                    "thread_id": thread_id,
                    "status": run_status,
                    "inputs": normalized_inputs,
                    "outputs": {},
                    "node_states": node_states,
                    "created_at": started_at,
                    "completed_at": completed_at,
                    "error": str(e),
                }

        return {
            "id": run_id,
            "workflow_id": self._extract_workflow_id(workflow),
            "thread_id": thread_id,
            "status": "completed",
            "inputs": normalized_inputs,
            "outputs": context.get("outputs", {}),
            "node_states": node_states,
            "created_at": self._now_iso(),
            "completed_at": self._now_iso(),
            "error": None,
        }

    # ===================== 节点执行 =====================

    async def _execute_node(
        self, node: dict, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """执行单个节点, 返回 {output, branch?, error?}"""
        ntype = node.get("type")
        cfg = (node.get("data") or {}).get("config") or {}
        handler = getattr(self, f"_node_{ntype}", None)
        if handler is None:
            raise WorkflowExecutionError(f"未实现的节点类型: {ntype}")
        return await handler(node, cfg, context)

    async def _node_start(
        self, node: dict, cfg: dict, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """起点节点: 把 inputs 写入 context.start"""
        return {"output": {"inputs": dict(context.get("inputs", {}))}}

    async def _node_end(
        self, node: dict, cfg: dict, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """终点节点: 把 context.outputs 收集返回"""
        # end 节点的 config.output_template 可指定要收集的变量
        outputs: Dict[str, Any] = {}
        output_template = cfg.get("output_template")
        if isinstance(output_template, dict):
            for key, path in output_template.items():
                outputs[key] = _resolve_path(context, str(path))
        else:
            # 默认收集 inputs + 各节点 output
            # 注: context[nid] 是节点 output 本身 (execute 中存的是 result.get("output")),
            # 不是 {"output": ...} 嵌套结构
            outputs = {"inputs": context.get("inputs", {})}
            for nid, val in context.items():
                if nid in ("inputs", "variables", "outputs"):
                    continue
                outputs[nid] = val
        return {"output": outputs}

    async def _node_llm(
        self, node: dict, cfg: dict, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """LLM 节点: 渲染 prompt_template → 调 model_router.get_provider_with_fallback()"""
        from core.providers.base import ChatMessage

        prompt_template = cfg.get("prompt_template", "")
        rendered = self._render_template(prompt_template, context)
        temperature = float(cfg.get("temperature", 0.1))
        max_tokens = int(cfg.get("max_tokens", 1024))
        model_name = cfg.get("model")

        # 调 LLM (若 app_state 未提供, 抛错由调用方 mock)
        if self.app_state is None or not hasattr(self.app_state, "model_router"):
            raise WorkflowExecutionError(
                "LLM 节点需要 app_state.model_router, 但当前未注入"
            )
        provider, _tier = await self.app_state.model_router.get_provider_with_fallback()
        # 临时覆盖 temperature / max_tokens
        original_temperature = provider.config.temperature
        original_max_tokens = provider.config.max_tokens
        try:
            provider.config.temperature = temperature
            provider.config.max_tokens = max_tokens
            messages = [ChatMessage(role="user", content=rendered)]
            completion = await provider.chat_completion(messages)
            content = completion.content or ""
        finally:
            provider.config.temperature = original_temperature
            provider.config.max_tokens = original_max_tokens
        return {
            "output": {
                "prompt": rendered,
                "content": content,
                "model": model_name or completion.model,
                "usage": completion.usage,
            }
        }

    async def _node_http(
        self, node: dict, cfg: dict, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """HTTP 节点: 模板化 url/body → httpx 调用"""
        import httpx

        method = (cfg.get("method") or "GET").upper()
        url = self._render_template(cfg.get("url", ""), context)
        # H7: SSRF 防护 - 阻止访问内网 / 本地地址
        # DNS 解析在子线程执行, 避免阻塞事件循环
        is_internal = await asyncio.to_thread(_is_internal_url, url)
        if is_internal:
            return {"output": {"error": "不允许访问内部地址"}}
        headers_raw = cfg.get("headers") or {}
        headers = {
            self._render_template(k, context): self._render_template(str(v), context)
            for k, v in headers_raw.items()
        }
        body_template = cfg.get("body_template")
        body = None
        if body_template:
            body = self._render_template(body_template, context)
            # 若 body 是 JSON, 尝试解析为 dict
            if not headers.get("Content-Type"):
                headers["Content-Type"] = "application/json"
        timeout = float(cfg.get("timeout", 30.0))

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, url, headers=headers, content=body)
            # 尝试解析 JSON, 失败则返回 text
            try:
                data = response.json()
            except Exception:
                data = response.text
            return {
                "output": {
                    "status_code": response.status_code,
                    "headers": dict(response.headers),
                    "body": data,
                    "url": str(response.url),
                    "method": method,
                }
            }

    async def _node_condition(
        self, node: dict, cfg: dict, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """条件分支节点: 安全 eval 表达式 → 选择 true/false 路由"""
        expression = cfg.get("expression", "")
        result = self._eval_condition(expression, context)
        # branch 为 "true" / "false", 用于 source_handle 路由匹配
        branch = "true" if result else "false"
        return {
            "output": {"expression": expression, "result": result},
            "branch": branch,
        }

    async def _node_code(
        self, node: dict, cfg: dict, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """代码节点: 受限 Python sandbox 执行

        用户提供 source 中可访问:
        - inputs: 工作流输入变量
        - context: 当前上下文 (只读)
        - result: 必须在 source 中赋值, 作为节点 output
        - loop 节点绑定的 item / index / loop_result 等 (供 loop body 中直接引用)
        - 白名单 builtins: abs / min / max / sum / len / round / int / float / str / ...
        """
        source = cfg.get("source", "")
        # 构造受限 globals / locals
        local_vars: Dict[str, Any] = {
            "inputs": dict(context.get("inputs", {})),
            "context": dict(context),
            "result": None,
        }
        # 暴露 loop / parallel 节点绑定的变量 (如 item / index / loop_result)
        # 直接作为顶层名字, 让 loop body 中的代码可写 item * 2 而非 context["item"] * 2
        for k, v in context.items():
            if k in ("inputs", "variables"):
                continue
            if k in local_vars:
                # 不覆盖 inputs / context / result
                continue
            if isinstance(v, dict) and "output" in v:
                # 暴露节点 output (供下游 code 节点引用 node_id)
                local_vars[k] = v["output"]
            else:
                # 暴露简单值 (如 loop 节点的 item / index / loop_result)
                local_vars[k] = v
        # exec 时禁 builtins (传 {"__builtins__": {}} 阻止访问 __import__ 等)
        safe_globals: Dict[str, Any] = {"__builtins__": _CODE_ALLOWED_BUILTINS}
        try:
            exec(
                compile(source, f"<workflow:{node.get('id', '?')}>", "exec"),
                safe_globals,
                local_vars,
            )  # noqa: S102
        except Exception as e:
            raise WorkflowExecutionError(f"代码节点执行失败: {e}") from e
        result = local_vars.get("result")
        return {"output": {"result": result}}

    async def _node_knowledge(
        self, node: dict, cfg: dict, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """知识库检索节点: 渲染 query_template → app_state.get_kb_store().query()"""
        query_template = cfg.get("query_template", "")
        query = self._render_template(query_template, context)
        top_k = int(cfg.get("top_k", 5))
        if self.app_state is None or not hasattr(self.app_state, "get_kb_store"):
            raise WorkflowExecutionError(
                "knowledge 节点需要 app_state.get_kb_store(), 但当前未注入"
            )
        kb_store = self.app_state.get_kb_store()
        results = await kb_store.query(query, top_k=top_k)
        return {
            "output": {
                "query": query,
                "top_k": top_k,
                "results": results,
                "count": len(results) if hasattr(results, "__len__") else None,
            }
        }

    async def _node_loop(
        self, node: dict, cfg: dict, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """循环节点: 对 items 列表中每个元素执行 body 子节点

        配置:
        - items: JSONPath 风格路径, 如 "$.input.list" 或 "inputs.list" / "n1.output.items"
        - item_var: 每次迭代绑定的变量名 (默认 "item"), 子节点可通过 {{item}} 引用
        - index_var: 索引变量名 (默认 "index"), 子节点可通过 {{index}} 引用
        - body: 子节点列表, 每次迭代按顺序执行
        - break_when: 可选, 条件表达式为 True 时中断循环

        输出: {items_count, results: [每次迭代最后的 output], break_index}
        """
        # 解析 items 列表路径: "$.input.list" → "input.list" → resolve_path
        items_path = cfg.get("items", "")
        # 兼容 "$.xxx" 前缀 (JSONPath 风格)
        if items_path.startswith("$."):
            items_path = items_path[2:]
        items = _resolve_path(context, items_path)
        if items is None:
            items = []
        if not isinstance(items, list):
            raise WorkflowExecutionError(
                f"循环节点 items 路径 '{cfg.get('items')}' 解析结果不是列表: {type(items).__name__}"
            )

        item_var = cfg.get("item_var", "item")
        index_var = cfg.get("index_var", "index")
        body = cfg.get("body") or []
        break_when = cfg.get("break_when")

        # H8: 迭代次数上限检查, 防止超长列表耗尽资源 / 死循环
        if len(items) > self.MAX_LOOP_ITERATIONS:
            raise WorkflowExecutionError(
                f"循环节点 items 数量 {len(items)} 超过上限 "
                f"{self.MAX_LOOP_ITERATIONS}, 已拒绝执行"
            )

        results: List[Any] = []
        break_index: Optional[int] = None

        for idx, item in enumerate(items):
            # 构造子上下文 (继承外层 context + 绑定 item / index)
            sub_context = dict(context)
            sub_context[item_var] = item
            sub_context[index_var] = idx
            sub_context["loop_item"] = item
            sub_context["loop_index"] = idx

            # 执行 body 子节点
            iter_output = await self._execute_sub_nodes(
                body, sub_context, node.get("id", "?")
            )

            results.append(iter_output)

            # 检查 break 条件
            if break_when:
                # 把迭代结果注入子上下文供 break_when 引用
                break_context = dict(sub_context)
                break_context["loop_result"] = iter_output
                if self._eval_condition(break_when, break_context):
                    break_index = idx
                    break

        return {
            "output": {
                "items_count": len(items),
                "results": results,
                "break_index": break_index,
            }
        }

    async def _node_parallel(
        self, node: dict, cfg: dict, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """并行节点: 使用 asyncio.gather 并行执行所有分支

        配置:
        - branches: 分支列表, 每个分支 {"name": "...", "nodes": [子节点列表]}

        各分支共享同一个 context 快照 (互不影响),
        结果合并到 output.branch_results (按分支名/索引),
        同时写入 context["parallel_results"] 供下游引用。
        """
        branches = cfg.get("branches") or []
        if not branches:
            return {"output": {"branch_results": {}, "parallel_results": {}}}

        async def _run_branch(branch_index: int, branch: dict) -> Tuple[int, Any]:
            """执行单个分支的子节点列表"""
            branch_name = branch.get("name", f"branch_{branch_index}")
            branch_nodes = branch.get("nodes") or []
            # 每个分支用独立的 context 副本 (避免并行写冲突)
            sub_context = dict(context)
            output = await self._execute_sub_nodes(
                branch_nodes, sub_context, node.get("id", "?")
            )
            return branch_index, {"name": branch_name, "output": output}

        # 并行执行所有分支
        tasks = [_run_branch(i, branch) for i, branch in enumerate(branches)]
        completed = await asyncio.gather(*tasks, return_exceptions=True)

        branch_results: Dict[str, Any] = {}
        parallel_results: Dict[str, Any] = {}
        errors: List[str] = []
        for item in completed:
            if isinstance(item, Exception):
                errors.append(str(item))
                continue
            branch_index, result = item
            key = result["name"]
            branch_results[str(branch_index)] = result
            parallel_results[key] = result["output"]

        return {
            "output": {
                "branch_results": branch_results,
                "parallel_results": parallel_results,
                "errors": errors if errors else None,
                "branch_count": len(branches),
            }
        }

    async def _execute_sub_nodes(
        self,
        sub_nodes: List[dict],
        context: Dict[str, Any],
        parent_id: str = "?",
    ) -> Any:
        """执行子节点列表 (用于 loop body / parallel branch)

        子节点按顺序执行, 每个子节点的 output 写入子上下文 (以 sub node id 为 key),
        返回最后一个子节点的 output (若无子节点返回 None)。

        子节点 dict 格式兼容两种:
        - {"id": "n1", "type": "llm", "data": {"config": {...}}}
        - {"type": "llm", "config": {...}}
        """
        last_output: Any = None
        for sub in sub_nodes:
            if not isinstance(sub, dict):
                continue
            sub_type = sub.get("type")
            if sub_type is None:
                continue
            # 兼容 data.config 和顶层 config 两种格式
            sub_cfg = (sub.get("data") or {}).get("config") or sub.get("config") or {}
            handler = getattr(self, f"_node_{sub_type}", None)
            if handler is None:
                raise WorkflowExecutionError(
                    f"子节点类型未实现: {sub_type} (父节点: {parent_id})"
                )
            result = await handler(sub, sub_cfg, context)
            output = result.get("output")
            # 用子节点 id 作为 key 写入上下文 (若有 id), 让后续子节点可引用
            sub_id = sub.get("id")
            if sub_id:
                context[sub_id] = output
            last_output = output
        return last_output

    # ===================== 工具方法 =====================

    def _render_template(self, template: str, context: Dict[str, Any]) -> str:
        """简单模板渲染: {{var}} 替换 (支持点路径 {{node_id.field}})"""
        if not template:
            return ""

        def _repl(m: "re.Match[str]") -> str:
            path = m.group(1).strip()
            value = _resolve_path(context, path)
            if value is None:
                return ""
            if isinstance(value, (dict, list)):
                # 复杂对象用 str 表示, 避免渲染出 'dict' 字面量
                try:
                    import json

                    return json.dumps(value, ensure_ascii=False, default=str)
                except Exception:
                    return str(value)
            return str(value)

        return _TEMPLATE_RE.sub(_repl, template)

    def _compile_condition(self, expression: str) -> ast.AST:
        """编译条件表达式并做 AST 白名单校验"""
        try:
            tree = ast.parse(expression.strip(), mode="eval")
        except SyntaxError as e:
            raise WorkflowValidationError(f"条件表达式语法错误: {e}") from e
        _validate_condition_ast(tree)
        return tree

    def _eval_condition(self, expr: str, context: Dict[str, Any]) -> bool:
        """安全 eval 条件表达式 (只允许比较和逻辑运算, 禁用 builtins)

        表达式中可引用 context 中的变量名 (如 inputs.score > 10)
        简化: 只支持 context 顶层 key (不含点路径, 复杂场景让用户用 code 节点)

        暴露的变量:
        - inputs 中的各字段 (如 score)
        - 各节点的 output (node_id → output dict)
        - loop 节点绑定的 item / index / loop_result 等 (供 break_when 引用)
        """
        if not expr or not expr.strip():
            return False
        # 编译 + AST 校验
        self._compile_condition(expr)
        # 构造求值环境: 把 context 顶层 key 暴露为名字
        # 把 inputs 顶层 key 也直接暴露 (便捷: 用户可写 score > 10)
        eval_locals: Dict[str, Any] = {}
        if isinstance(context.get("inputs"), dict):
            for k, v in context["inputs"].items():
                eval_locals[k] = v
        # context 各节点 output 也暴露: 用 node_id 引用
        for k, v in context.items():
            if k in ("inputs", "variables"):
                continue
            if isinstance(v, dict) and "output" in v:
                # 暴露整个 node_id.output dict, 用户写 n1.output.score 不可 (eval 不支持点)
                # 改为暴露 node_id (整个 output dict) 与扁平化各字段
                eval_locals[k] = v["output"]
            else:
                # 暴露简单值 (如 loop 节点的 item / index / loop_result / parallel_results)
                eval_locals[k] = v
        safe_globals: Dict[str, Any] = {"__builtins__": _CODE_ALLOWED_BUILTINS}
        try:
            result = eval(expr, safe_globals, eval_locals)  # noqa: S307
        except Exception as e:
            raise WorkflowExecutionError(f"条件表达式求值失败: {e}") from e
        return bool(result)

    # ===================== 辅助 =====================

    @staticmethod
    def _extract_graph(workflow: Any) -> dict:
        """从 Workflow 对象 / dict 提取 graph

        支持三种入参:
        - Workflow ORM 对象: 取 .graph 属性
        - dict {graph, input_schema, id, ...}: 取 graph 字段
        - 直接是 graph dict {nodes, edges}: 整体返回
        """
        # 1. ORM 对象: 取 .graph 属性
        if hasattr(workflow, "graph") and not isinstance(workflow, dict):
            return workflow.graph or {}
        # 2. dict: 优先看是否有 "graph" 字段 (workflow-like dict), 否则整体视为 graph
        if isinstance(workflow, dict):
            # 直接是 graph dict (含 nodes/edges): 整体返回
            if "nodes" in workflow or "edges" in workflow:
                return workflow
            # workflow-like dict: 取 graph 字段
            if "graph" in workflow:
                return workflow.get("graph") or {}
        return {}

    @staticmethod
    def _extract_input_schema(workflow: Any) -> dict:
        """从 Workflow 对象 / dict 提取 input_schema"""
        if hasattr(workflow, "input_schema") and not isinstance(workflow, dict):
            return workflow.input_schema or {}
        if isinstance(workflow, dict):
            # workflow-like dict
            if "input_schema" in workflow:
                return workflow.get("input_schema") or {}
            # 直接是 graph dict (无 input_schema)
            if "nodes" in workflow or "edges" in workflow:
                return {}
        return {}

    @staticmethod
    def _extract_workflow_id(workflow: Any) -> Optional[str]:
        """从 Workflow 对象 / dict 提取 workflow_id"""
        if hasattr(workflow, "id") and not isinstance(workflow, dict):
            return workflow.id
        if isinstance(workflow, dict):
            return workflow.get("id") or workflow.get("workflow_id")
        return None

    @staticmethod
    def _apply_input_schema(
        input_schema: dict, inputs: Dict[str, Any]
    ) -> Dict[str, Any]:
        """应用 input_schema 默认值, 合并用户输入"""
        normalized = {}
        variables = (input_schema or {}).get("variables") or []
        for var in variables:
            if not isinstance(var, dict):
                continue
            name = var.get("name")
            if not name:
                continue
            normalized[name] = inputs.get(name, var.get("default"))
        # 用户传入的额外变量也保留 (允许 schema 之外的输入)
        for k, v in inputs.items():
            if k not in normalized:
                normalized[k] = v
        return normalized

    @staticmethod
    def _now_iso() -> str:
        """当前 UTC 时间 ISO 字符串 (含时区)"""
        return datetime.now(timezone.utc).isoformat()
