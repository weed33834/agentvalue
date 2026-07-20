# AgentValue-AI 综合升级方案设计文档

> 状态: Draft (已通过真实联网研究 + 完整代码阅读产出)
> 日期: 2026-07-12
> 关联: [DEVELOPMENT-PLAN.md](./DEVELOPMENT-PLAN.md)、[AGENTS.md](../../AI/AGENTS.md)

## 一、研究依据(真实联网搜索)

本文档所有方案均基于真实联网搜索,非训练知识回忆。主要参考来源:

### 1.1 模型管理 / 路由 / 熔断

- **LiteLLM 官方 - Redis Circuit Breaker**: https://docs.litellm.ai/blog/redis-circuit-breaker
  - 核心模式: CLOSED → OPEN → HALF-OPEN 状态机,5 次连续失败熔断,60s 探活恢复
  - Redis 跨副本共享熔断状态,避免雪崩后雷群
- **LiteLLM Routing Architecture**: https://markaicode.com/architecture/litellm-routing-architecture/
  - 评分函数: `latency_P50 × cost_per_token × remaining_capacity%`
  - HTTPX AsyncClient 连接池 max_connections=200
  - 二级限流: 全局滑动窗口(Redis) + 本地令牌桶
- **LiteLLM Fallbacks 官方文档**: https://docs.litellm.ai/docs/proxy/reliability
  - 三类 fallback: `content_policy_fallbacks` / `context_window_fallbacks` / `fallbacks`
  - `default_fallbacks` 兜底所有模型组配置错误
- **Fallback Routing on Failure (生产调优)**: https://theneuralbase.com/ai-apis-comparison/learn/intermediate/fallback-routing-on-failure/
  - 关键调优: timeout=3s(默认 10s 太长)、allowed_fails=2(默认 5 太宽松)、reset_timeout=60s
  - 能力校验: GPT-4(128K)→Llama(4K) fallback 需检查 context window,避免静默截断
- **Dify 多模型切换机制**: https://blog.csdn.net/weixin_42613017/article/details/155966738
  - 适配器模式: 每个模型封装成标准化接口
  - 配置外置化: YAML/JSON 文件存节点配置,支持版本追踪与回滚
  - 模型路由策略: 中文优先通义、高价值客户走 GPT-4、成本敏感走 Turbo、主模型失败自动降级

### 1.2 Prompt 管理

- **Langfuse A/B Testing 官方文档**: https://langfuse.com/docs/prompt-management/features/a-b-testing
  - Label 机制: 同名 prompt 多版本打 `prod-a`/`prod-b` label,运行时随机选
  - 关联 trace: `langfuse_prompt=selected_prompt` 自动绑定到 generation 做指标对比
- **Langfuse Core Concepts**: https://langfuse.com/docs/prompt-management/data-model
  - Version(不可变历史) + Label(指向版本的指针) 双层模型
  - 标签: `production`(生产默认)、`latest`(最新)、自定义(灰度/租户/A_B)
  - Prompt 缓存: TTL 机制,首次更新可能延迟
  - 三类变量: Variables / Prompt References / Message Placeholders
- **Langfuse Prompt Version Control**: https://langfuse.com/docs/prompt-management/features/prompt-version-control
  - Protected Labels (RBAC): viewer/member 不能改 `production` label,需 admin/owner
  - 一键回滚: 重新指 `production` label 到旧版本
  - Prompt Diff View: 版本间差异展示

### 1.3 工具管理与 MCP

- **LangGraph ToolNode 官方文档**: https://github.langchain.ac.cn/langgraph/how-tos/tool-calling/
  - `@tool` 装饰器定义工具,自动从 docstring + type hint 生成 schema
  - `model.bind_tools([tools])` 强制模型输出 OpenAI Tool Call 结构化数据
  - `ToolNode([tools])` 作为图节点,自动解析 tool_calls 并执行
  - `tools_condition` 路由: `agent → tools → agent` 循环(ReAct 模式)
  - 支持并行工具调用
  - **关键**: 工具函数必须幂等(可能被 checkpoint 恢复时重试)
- **LangChain MCP Adapters**: https://docs.langchain.com/oss/python/langchain/mcp
  - `pip install langchain-mcp-adapters`
  - `MultiServerMCPClient` 支持 stdio / HTTP / SSE 三种传输
  - 默认无状态(每次工具调用创建新 ClientSession)
  - 支持 Tools / Resources / Prompts 三类原语
  - FastMCP 创建自定义 server: `@mcp.tool()` 装饰器

### 1.4 综合参考(已确认)

- Dify 适配器模式 + 配置外置 + 可视化编排 + 多 Key 轮询 + 模型路由
- Langfuse 版本/标签双层模型 + A/B 测试 + 缓存 + Protected Labels + Diff View
- LiteLLM 状态机熔断 + Redis 共享状态 + 评分函数 + 二级限流
- LangGraph ToolNode + bind_tools + ReAct + 幂等工具
- MCP 开放协议 + MultiServer 客户端 + 三种传输

## 二、完整 Gap 分析(agentvalue-ai 现状 vs 主流方案)

### 2.1 模型管理 Gap

| # | 主流方案能力 | agentvalue-ai 现状 | Gap 严重度 |
|---|---|---|---|
| M1 | 多 Provider 抽象 + 注册表(OpenAI/Anthropic/Gemini/Ollama/Bedrock) | 仅 `OpenAICompatibleProvider` 单实现 | **高** |
| M2 | 状态机熔断(CLOSED/OPEN/HALF-OPEN,5 次失败熔断,60s 探活) | 仅 deque 滑动窗口记录成功率,无熔断 | **P0** |
| M3 | Redis 跨副本共享熔断状态 | 进程内 deque,多实例不共享 | **高** |
| M4 | 评分路由函数(latency × cost × capacity) | 仅 VRAM/RAM 静态档位推荐 | **中** |
| M5 | 三类 fallback(content_policy/context_window/general) | 仅一维降级链 L0→L3→L2→L1 | **中** |
| M6 | Function Calling / Tool Calling 透传 | BaseProvider 无 function_calling 方法 | **P0** |
| M7 | 流式响应(stream) + 背压缓冲 | 完全无流式 | **中** |
| M8 | 多 API Key 轮询 + 配额追踪 | 单 key 配置,无配额 | **中** |
| M9 | Token 用量按租户统计 | LLM_TOKEN_USAGE_TOTAL 无 tenant_id label | **高** |
| M10 | Provider CRUD + 管理后台 | 仅 admin LLM 配置 API 改 settings | **中** |
| M11 | 健康检查缓存(避免每次都打 /models) | 每次 `get_provider_with_fallback` 都打 | **中** |
| M12 | Context Window 校验(fallback 防截断) | 无 | **中** |

### 2.2 Prompt 管理 Gap

| # | 主流方案能力 | agentvalue-ai 现状 | Gap 严重度 |
|---|---|---|---|
| P1 | DB 存储 + 管理后台 | 仅文件 `prompts/{name}.md` | **P0** |
| P2 | Version(不可变历史) + Label(指针)双层模型 | 仅文件版本快照 `versions/{name}_v{X.Y}.md` | **高** |
| P3 | A/B 测试(label prod-a/prod-b 随机选) | 无 | **高** |
| P4 | 灰度发布(canary 按租户/百分比) | 无 | **中** |
| P5 | 一键回滚 | 无 | **高** |
| P6 | Prompt Diff View | 无 | **中** |
| P7 | Trace 自动绑定 | Langfuse generation 已埋点但 prompt 版本未关联 | **中** |
| P8 | Protected Labels (RBAC) | 无 | **低** |
| P9 | 三类变量(Variables/Prompt References/Message Placeholders) | 仅正则替换 5 个固定占位符 | **中** |
| P10 | 缓存 TTL | 无 | **低** |
| P11 | 评估集成(版本→指标对比) | 无 | **中** |
| P12 | 在线 Playground | 无 | **低** |

### 2.3 工具管理 Gap

| # | 主流方案能力 | agentvalue-ai 现状 | Gap 严重度 |
|---|---|---|---|
| T1 | `@tool` 装饰器 + 自动 schema | 仅 2 个 ABC(MemoryStore/CompanyKB) + Dummy | **P0** |
| T2 | `bind_tools()` 透传给模型 | 完全无 | **P0** |
| T3 | `ToolNode` 图节点 + ReAct 循环 | graph.py 全部 inline 闭包,固定 9 节点流水线 | **P0** |
| T4 | 工具权限 + 审计 | 无 | **中** |
| T5 | 工具错误自纠正(模型重试参数) | 无 | **中** |
| T6 | MCP 集成(Client 接入外部 server) | 无 | **中** |
| T7 | MCP Server 暴露自身能力 | 无 | **低** |
| T8 | 工具版本/注册表 | 无 | **中** |
| T9 | 工具执行超时 | 无 | **中** |
| T10 | 工具幂等性保证 | 无 | **中** |

### 2.4 调试与可观测性 Gap

| # | 主流方案能力 | agentvalue-ai 现状 | Gap 严重度 |
|---|---|---|---|
| G1 | trace_id 通过 Filter 注入日志 record | contextvar 已 set 但 logging.Formatter 不读 | **P0** |
| G2 | /metrics 端点鉴权 | 直接挂载无鉴权,内网可抓 | **P0** |
| G3 | /metrics IP 白名单或 token | 无 | **P0** |
| G4 | LLM_TOKEN_USAGE_TOTAL 加 tenant_id label | 缺 | **高** |
| G5 | Prompt 版本绑定到 trace | 无 | **中** |
| G6 | Token 成本仪表盘 | 无 | **中** |
| G7 | Sentry 异常上报 | 无 | **低** |
| G8 | OTel 标准化 | 仅 Langfuse | **低** |
| G9 | Trace Replay(重放历史 trace) | 无 | **低** |
| G10 | 在线调试(改 prompt/参数实时跑) | 无 | **低** |
| G11 | _tenant_cache 从内存迁 Redis | 内存 | **中** |
| G12 | 限流(rate_limit decorator) | 所有 API 端点无 | **P0** |

### 2.5 评估框架 Gap

| # | 主流方案能力 | agentvalue-ai 现状 | Gap 严重度 |
|---|---|---|---|
| E1 | 评估数据集管理 | 无 | **中** |
| E2 | 自定义 Evaluator(规则/LLM-as-judge) | 无 | **中** |
| E3 | 评估运行 + 历史对比 | 无 | **中** |
| E4 | CI/CD 集成(prompt 变更自动跑) | 无 | **低** |
| E5 | RAG 评估(RAGAS: faithfulness/answer_relevancy) | 无 | **低** |

## 三、P0 关键 Bug 修复方案(立即实施)

### 3.1 trace_id 注入日志(P0-G1)

**问题**: `_current_trace_id` contextvar 已在 `tracing.py` line 81 set,但 `logging_config.py` 的 `StructuredJsonFormatter` 只从 `record.__dict__` 读 extra 字段,不会读 contextvar。导致 trace_id 永远进不了日志。

**修复方案**(参考 Langfuse/Loki 标准做法): 在 `logging_config.py` 添加 `TraceContextFilter`,挂在 root logger 上,`filter()` 方法从 `tracing.tracer.current_trace_id()` 读 contextvar,注入到 `record.trace_id`。这样所有 logger(包括第三方库)都能自动带 trace_id,无需业务代码改 extra。

### 3.2 /metrics 鉴权(P0-G2/G3)

**问题**: `metrics.py` line 299-306 `setup_metrics` 直接 `app.mount("/metrics", make_asgi_app())`,绕过 FastAPI 依赖注入,任何能访问 API 的人都能抓走所有指标(包括评估量、token 用量、健康度等业务敏感数据)。

**修复方案**(参考 LiteLLM/Langfuse 生产实践): 在挂载前包一层 Starlette `Middleware` 或自定义 ASGI app,做两种鉴权之一:
- 内网部署: IP 白名单(默认仅 127.0.0.1 + RFC1918 私网段)
- 公网部署: Bearer token 校验,token 从环境变量 `METRICS_BEARER_TOKEN` 读
- 通过环境变量 `METRICS_AUTH_MODE=none|ip|token` 控制,默认 `ip`(向后兼容本地开发)

### 3.3 Redis Job Queue 竞态(P0-H2)

**问题**: `RedisJobQueue.update` line 151-161 实现"读-改-写"非原子,两个并发 update 会丢更新。`InMemoryJobQueue.get` 返回引用(line 89 注释明确说"保持就地变更语义"),Redis 返回拷贝,两套实现语义不一致,业务代码切换实现时会出现"Redis 下状态丢失"的隐性 bug。

**修复方案**(参考 LiteLLM Redis 操作实践): 用 Redis Lua 脚本做原子合并 update:
```lua
local cur = redis.call('GET', KEYS[1])
if not cur then return 0 end
local obj = cjson.decode(cur)
for k, v in pairs(ARGV) do obj[k] = v end
obj['updated_at'] = ARGV[#ARGV]
redis.call('SET', KEYS[1], cjson.encode(obj))
return 1
```
同时让 `InMemoryJobQueue.get` 也返回深拷贝,统一两套实现语义,消除"切换实现就坏"的隐患。

### 3.4 限流 decorator(P0-G12)

**问题**: `api/routes.py` 3000 行所有端点均无 `@rate_limit`,单 IP 可在 token 有效期内无限调用,触发 LLM 上游成本失控 + DB 压力。

**修复方案**(参考 LiteLLM 两级限流): 用 `slowapi`(FastAPI 标准限流库)实现:
- 全局: 每 IP 60 req/min(可配置)
- 敏感端点(/evaluations POST、/admin/* 等): 每 IP 10 req/min
- token 维度可选(后续 P2): 每 user_id 100 req/min
- Redis 不可达时降级内存限流(单实例)

### 3.5 Embedding 零向量静默数据损坏(P0-Embed)

**问题**: `embeddings.py` line 80-86 `__call__` 在事件循环内返回零向量,ChromaDB fallback 路径触发的 query 会被存为零向量,导致后续相似度检索全部失效(零向量与任何向量 cosine 相似度都是 0),业务无感知。

**修复方案**: 事件循环内检测到时,直接 `raise RuntimeError` 而不是返回零向量;调用方应通过 `embed_query` 预计算向量。`embed()` 失败时也只 `raise` 不降级零向量,让 RAG 检索直接报错而非静默返回无关结果。

## 四、P1 模型管理增强设计

### 4.1 多 Provider 抽象层(参考 Dify 适配器模式)

新增 `core/providers/anthropic_provider.py`、`gemini_provider.py`、`ollama_provider.py`、`bedrock_provider.py`,实现统一 `BaseProvider` 接口。

`BaseProvider` 新增抽象方法:
```python
async def function_calling(
    self,
    messages: List[ChatMessage],
    tools: List[Dict[str, Any]],
    tool_choice: str = "auto",
) -> ChatCompletion:
    """支持 function calling / tool calling 的对话补全"""
```

新增 `core/providers/registry.py`:
```python
class ProviderRegistry:
    """Provider 注册表:按 provider_type 字符串构造对应 Provider 实例"""
    _registry: Dict[str, Type[BaseProvider]] = {
        "openai": OpenAICompatibleProvider,
        "anthropic": AnthropicProvider,
        "gemini": GeminiProvider,
        "ollama": OllamaProvider,
        "bedrock": BedrockProvider,
    }
```

### 4.2 状态机熔断器(参考 LiteLLM)

新增 `core/circuit_breaker.py`:
```python
class CircuitBreaker:
    """状态机熔断器:CLOSED → OPEN → HALF-OPEN
    
    - 5 次连续失败 → OPEN(0ms fast-fail)
    - 60s 后 → HALF-OPEN(放一个探针)
    - 探针成功 → CLOSED; 探针失败 → OPEN
    - Redis 共享状态(多副本一致)
    """
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"
    
    failure_threshold: int = 5  # LiteLLM 默认
    recovery_timeout: int = 60  # LiteLLM 默认
```

挂到 `OpenAICompatibleProvider._retry` 外层:调用前检查熔断器状态,失败时 `record_failure`,成功时 `record_success`。

### 4.3 健康检查缓存

新增 `core/providers/health_cache.py`:缓存 30s 内 health_check 结果,避免每次 `get_provider_with_fallback` 都打 `/models`。

### 4.4 Token 用量加 tenant_id label

修改 `metrics.py` line 155-160 `LLM_TOKEN_USAGE_TOTAL` labels 加 `tenant_id`,调用方 `record_token_usage` 自动从 contextvar 取。

### 4.5 流式响应(参考 LiteLLM BackpressureBuffer)

`BaseProvider` 新增 `stream_completion` 方法,内部用 `asyncio.Queue(maxsize=64)` 做背压缓冲,防止慢消费者 OOM。这是 P2,本次不实施。

## 五、P1 Prompt 管理增强设计

### 5.1 数据库表设计(参考 Langfuse 数据模型)

新增 4 张表(新建 alembic migration `d5e6f7a8b9c0_add_prompt_management.py`):

```python
class PromptTemplate(Base):
    """Prompt 模板(逻辑实体,同名多版本)"""
    __tablename__ = "prompt_templates"
    id: str (UUID PK)
    tenant_id: str (多租户隔离)
    name: str (unique within tenant,如 "daily_evaluation")
    type: str ("text" | "chat")
    description: str
    created_at / updated_at / created_by

class PromptVersion(Base):
    """Prompt 版本(不可变历史,每次更新新建一行)"""
    __tablename__ = "prompt_versions"
    id: str (UUID PK)
    template_id: FK -> prompt_templates.id
    version: int (1, 2, 3...)
    content: Text (prompt 正文,含 {{var}} 占位符)
    config: JSON (model/temperature/max_tokens 等)
    variables_schema: JSON (变量名 + 类型 + 默认值)
    created_at / created_by
    # 不可变: 无 updated_at,无 update 接口

class PromptLabel(Base):
    """Label 指针(指向具体 version)"""
    __tablename__ = "prompt_labels"
    id: str (UUID PK)
    template_id: FK -> prompt_templates.id
    version_id: FK -> prompt_versions.id
    label: str ("production" | "latest" | "staging" | "prod-a" | "prod-b" | 租户名)
    protected: bool (RBAC: viewer/member 不能改 protected label)
    updated_at / updated_by

class PromptEvalRun(Base):
    """Prompt 评估运行(关联到 trace 与指标)"""
    __tablename__ = "prompt_eval_runs"
    id: str (UUID PK)
    template_id: FK
    version_id: FK
    dataset_id: str (评估数据集)
    status: str
    metrics: JSON (latency_p50/p95, cost, score)
    trace_ids: JSON (关联 Langfuse trace)
    created_at
```

### 5.2 DbPromptLoader

新增 `agent/db_prompt_loader.py`,与现有 `PromptLoader` 共存(后者用于本地开发 fallback):
- 启动时优先从 DB 读 `production` label 的 version
- DB 不可达或表为空时,fallback 到文件 PromptLoader
- 提供 `get_by_label(name, label)` / `get_by_version(name, version)` / `list_versions(name)` / `create_version(name, content, ...)` / `assign_label(...)` 等 API

### 5.3 A/B 测试与灰度

参考 Langfuse 模式:
- 创建两个 version,分别打 `prod-a` 和 `prod-b` label
- `DbPromptLoader.get_for_request(name, employee_id)` 按 `hash(employee_id) % 100 < rollout_pct` 决定走 a 还是 b
- 灰度: 同样机制但 label 为 `canary-10pct`,hash < 10 走新版本
- 自动绑定到 Langfuse generation: `langfuse_prompt=selected_version`

### 5.4 管理 API

新增 `api/admin/prompts.py`:
- `GET /admin/prompts` 列出所有 template
- `POST /admin/prompts/{name}/versions` 创建新版本
- `GET /admin/prompts/{name}/versions` 列出版本
- `POST /admin/prompts/{name}/labels` 分配 label
- `GET /admin/prompts/{name}/diff?from=v1&to=v2` Prompt Diff View
- `POST /admin/prompts/{name}/rollback?to=v1` 一键回滚(等价于把 production label 指过去)

所有 admin 端点要求 `require_role("admin")`。

## 六、P1 工具管理与 LangGraph 集成设计

### 6.1 LangChain Tool 抽象(参考 LangGraph 官方)

重写 `agent/tools.py`,引入 `@tool` 装饰器:

```python
from langchain_core.tools import tool

@tool
async def get_employee_history(
    employee_id: str,
    period: Optional[str] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """获取员工历史评估记录。
    
    Args:
        employee_id: 员工 ID
        period: 评估周期(可选)
        limit: 返回记录数上限
    """
    # 实际调用 MemoryStore
    ...

@tool
async def query_company_kb(query: str, top_k: int = 5) -> List[Dict[str, Any]]:
    """检索公司知识库。"""
    ...
```

### 6.2 Function Calling 透传

`OpenAICompatibleProvider.function_calling` 实现:
```python
async def function_calling(
    self,
    messages: List[ChatMessage],
    tools: List[Dict[str, Any]],  # OpenAI tool schema
    tool_choice: str = "auto",
) -> ChatCompletion:
    resp = await self._retry(lambda: self.client.chat.completions.create(
        model=self.config.model_name,
        messages=[...],
        tools=tools,
        tool_choice=tool_choice,
        ...
    ), "function calling")
    return ChatCompletion(
        content=resp.choices[0].message.content or "",
        tool_calls=resp.choices[0].message.tool_calls,  # 新增字段
        ...
    )
```

`ChatCompletion` dataclass 新增 `tool_calls: Optional[List[Dict]]` 字段。

### 6.3 ToolNode 集成(参考 LangGraph ToolNode)

`agent/graph.py` 改造分阶段:
- **本次(P1)**: 在 `build_prompt` 节点后增加可选的 `tool_call` 节点,LLM 决定是否调工具,调完返回 `finalize`。保持现有 9 节点主流程兼容。
- **后续(P2)**: 完整迁移到 `create_react_agent` ReAct 模式,`agent → tools → agent` 循环。

### 6.4 MCP 集成(参考 LangChain MCP Adapters)

本次仅打地基,不实际接入(避免依赖膨胀):
- `requirements.txt` 加 `langchain-mcp-adapters` 作为可选依赖
- 新增 `agent/mcp_client.py` 骨架,封装 `MultiServerMCPClient`,从 settings 读 MCP server 配置
- 实际接入留 P2

## 七、P1 调试与可观测性增强设计

### 7.1 trace_id 注入日志(见 3.1)

### 7.2 /metrics 鉴权(见 3.2)

### 7.3 Token 用量加 tenant_id(见 4.4)

### 7.4 Prompt 版本绑定 trace

`agent/graph.py` 的 `call_llm` 节点在调用 `LangfuseTracer.generation` 时,metadata 加入 `prompt_version_id`。

### 7.5 _tenant_cache 迁 Redis

`core/tenant_context.py` 的内存 cache 改用 Redis(本次仅打地基,留 P2)。

## 八、实施优先级与本次落地范围

### 本次必须落地(P0 + P1 关键):

1. **P0 bug 修复**(见第三节):
   - trace_id 日志注入 Filter
   - /metrics 鉴权(IP 白名单 + 可选 token)
   - Redis Job Queue 原子 update + InMemory 返回拷贝统一语义
   - slowapi 限流 decorator + 关键端点配额
   - Embedding 零向量改 raise 不降级

2. **P1 模型管理基础**:
   - `BaseProvider.function_calling` 抽象 + `OpenAICompatibleProvider` 实现
   - `ChatCompletion.tool_calls` 字段
   - 状态机熔断器 `CircuitBreaker`
   - 健康检查缓存 `HealthCheckCache`
   - LLM_TOKEN_USAGE_TOTAL 加 tenant_id label

3. **P1 Prompt 管理基础**:
   - 4 张表 migration
   - `DbPromptLoader` + 文件 fallback
   - 管理 API 端点
   - A/B 测试与灰度(基于 hash)

4. **P1 工具管理基础**:
   - `@tool` 装饰器重构 tools.py
   - ToolNode 集成到 graph.py(可选分支)
   - MCP 客户端骨架

5. **P1 可观测性**:
   - Prompt 版本绑定 trace
   - 上述 metrics/trace 修复

### 后续(P2/P3,本次不做):

- 多 Provider 实现(Anthropic/Gemini/Ollama/Bedrock)
- 流式响应 + 背压缓冲
- Provider CRUD 后台 UI
- Prompt 在线 Playground
- Trace Replay
- OTel 标准化
- 完整 ReAct 模式迁移
- 评估框架集成

## 九、回归测试策略

每个改动必须配套:
- 单元测试(mock 外部依赖)
- 不破坏现有测试(`pytest backend/tests/` 全绿)
- `ruff check` + `py_compile` 通过
- 关键路径手动跑一遍(evaluations POST、/metrics、/admin/* )

## 十、参考文档索引

- LiteLLM Circuit Breaker: https://docs.litellm.ai/blog/redis-circuit-breaker
- LiteLLM Routing: https://markaicode.com/architecture/litellm-routing-architecture/
- LiteLLM Fallbacks: https://docs.litellm.ai/docs/proxy/reliability
- Dify 多模型: https://blog.csdn.net/weixin_42613017/article/details/155966738
- Langfuse A/B: https://langfuse.com/docs/prompt-management/features/a-b-testing
- Langfuse Concepts: https://langfuse.com/docs/prompt-management/data-model
- Langfuse Version Control: https://langfuse.com/docs/prompt-management/features/prompt-version-control
- LangGraph ToolNode: https://github.langchain.ac.cn/langgraph/how-tos/tool-calling/
- LangChain MCP: https://docs.langchain.com/oss/python/langchain/mcp
