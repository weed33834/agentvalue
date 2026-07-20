# P2 深水区设计 — Provider CRUD / Prompt Playground / 流式响应

> 本文档基于 P0/P1 已落地的版本,继续向 Dify / Coze / Langfuse 完整功能深度对标。
> 所有设计决策均来自联网调研(2025-2026),引用见第十一节。

---

## 一、目标与对标对象

| 模块 | 对标对象 | 关键能力差距(本次补齐) |
|---|---|---|
| 模型供应商管理 | Dify Model Providers + LiteLLM Provider 注册 | 目前仅 OpenAICompatible 单实现,无 Provider 注册表 UI,无多凭证轮询,无凭证 mask 显示 |
| Prompt Playground | Langfuse Playground + Dify Prompt IDE | 目前只有"渲染预览",没有"输入变量→点 Run→流式看 LLM 输出"的交互式 Playground |
| 流式响应 | sse-starlette + LangGraph astream_events v3 | 目前全量返回,无 SSE 流式,无背压缓冲,无 tool_calls 流式 delta |
| 评估流水线流式 | Langfuse Experiments + Datasets | 评估主路径仍全量调用 LLM,无法看到节点级进度 |

---

## 二、模型供应商管理(Provider CRUD)

### 2.1 数据库表结构(8 张表)

借鉴 Dify v1.13 双层架构(Service + ProviderManager + Factory),简化为单租户单进程版本:

```sql
-- 1. Provider 模板表(静态注册,seed 数据)
CREATE TABLE provider_templates (
    id              UUID PRIMARY KEY,
    provider        VARCHAR(64) NOT NULL UNIQUE,        -- 'openai' / 'anthropic' / 'ollama' / 'azure'
    label           JSONB NOT NULL,                     -- {"zh": "OpenAI", "en": "OpenAI"}
    description     JSONB,
    icon_small      VARCHAR(255),                       -- 图标 URL 或 data URL
    icon_large      VARCHAR(255),
    background       VARCHAR(16),                       -- 卡片背景色
    supported_model_types TEXT[] NOT NULL,              -- ['llm','embedding','rerank','vision']
    configurate_methods  TEXT[] NOT NULL,               -- ['predefined-model','customizable-model']
    provider_credential_schema JSONB NOT NULL,          -- 凭证表单 schema
    model_credential_schema    JSONB,                   -- customizable 才有
    is_builtin     BOOLEAN NOT NULL DEFAULT TRUE,
    enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. 租户 Provider 绑定 + 激活凭证指针
CREATE TABLE tenant_providers (
    id              UUID PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    provider        VARCHAR(64) NOT NULL,
    provider_type   VARCHAR(16) NOT NULL DEFAULT 'custom', -- custom | system
    is_valid        BOOLEAN NOT NULL DEFAULT FALSE,
    last_used_at    TIMESTAMPTZ,
    active_credential_id UUID,                          -- 指向 tenant_provider_credentials.id
    preferred_type  VARCHAR(16) DEFAULT 'custom',
    enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(tenant_id, provider, provider_type)
);

-- 3. 多凭证存储(支持负载均衡)
CREATE TABLE tenant_provider_credentials (
    id              UUID PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    provider        VARCHAR(64) NOT NULL,
    credential_name VARCHAR(128) NOT NULL,
    encrypted_config TEXT NOT NULL,                      -- AES-256-GCM 加密的 JSON
    user_id         VARCHAR(64),
    visibility      VARCHAR(32) NOT NULL DEFAULT 'team',
    is_valid        BOOLEAN NOT NULL DEFAULT FALSE,
    last_validated_at TIMESTAMPTZ,
    cooldown_until  TIMESTAMPTZ,                        -- 失败冷却到期时间
    failure_count   INT NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_tpc_tid_provider ON tenant_provider_credentials(tenant_id, provider);

-- 4. 模型启用表
CREATE TABLE tenant_provider_models (
    id              UUID PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    provider        VARCHAR(64) NOT NULL,
    model_name      VARCHAR(128) NOT NULL,
    model_type      VARCHAR(32) NOT NULL,                -- llm/embedding/rerank/vision
    active_credential_id UUID,
    is_valid        BOOLEAN NOT NULL DEFAULT FALSE,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    load_balancing_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(tenant_id, provider, model_name, model_type)
);

-- 5. 模型级多凭证(用于 customizable-model + LB)
CREATE TABLE tenant_provider_model_credentials (
    id              UUID PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    provider        VARCHAR(64) NOT NULL,
    model_name      VARCHAR(128) NOT NULL,
    model_type      VARCHAR(32) NOT NULL,
    credential_name VARCHAR(128) NOT NULL,
    encrypted_config TEXT NOT NULL,
    is_valid        BOOLEAN NOT NULL DEFAULT FALSE,
    cooldown_until  TIMESTAMPTZ,
    failure_count   INT NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 6. 默认模型(每 tenant 每 model_type 唯一)
CREATE TABLE tenant_default_models (
    id          UUID PRIMARY KEY,
    tenant_id   VARCHAR(64) NOT NULL,
    model_type  VARCHAR(32) NOT NULL,
    provider    VARCHAR(64) NOT NULL,
    model_name  VARCHAR(128) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(tenant_id, model_type)
);

-- 7. 模型能力声明(预定义模型,seed 数据)
CREATE TABLE model_templates (
    id              UUID PRIMARY KEY,
    provider        VARCHAR(64) NOT NULL,
    model           VARCHAR(128) NOT NULL,
    label           JSONB NOT NULL,
    model_type      VARCHAR(32) NOT NULL,
    features        TEXT[],                            -- ['chat','vision','function_calling']
    model_properties JSONB NOT NULL,                    -- {mode, context_size, max_tokens}
    parameter_rules JSONB,                              -- 推理参数 schema
    pricing         JSONB,                             -- {input_per_1k, output_per_1k, currency}
    enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(provider, model, model_type)
);

-- 8. 健康检查记录
CREATE TABLE provider_health_checks (
    id              UUID PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    provider        VARCHAR(64) NOT NULL,
    credential_id   UUID,
    model_name      VARCHAR(128),
    status          VARCHAR(16) NOT NULL,               -- healthy | degraded | down
    latency_ms      INT,
    error_message   TEXT,
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_phc_tid ON provider_health_checks(tenant_id, provider, checked_at DESC);
```

### 2.2 凭证加密方案

复用现有 `core/encryption.py` 的 AES-256-GCM(不引入 Dify 的 RSA PKCS1_OAEP,因 AES 性能更好且复用现有实现):
- Master Key:`settings.master_encryption_key`(env 注入,不入库)
- DEK:每 tenant 一个,用 Master Key 加密后存 `tenants.encryption_key` 字段
- 凭证:用 tenant DEK + AES-256-GCM 加密,存 `tenant_provider_credentials.encrypted_config`
- Redis 缓存:`provider_credentials:{tenant_id}:{provider}:{credential_id}` TTL 60s

### 2.3 API 路由(RESTful,贴合 Dify 风格)

前缀:`/api/v1/admin/model-providers`,所有接口需 `Role.ADMIN` + `slowapi` 限流。

| HTTP | 路由 | 用途 |
|---|---|---|
| GET | `/providers` | 列出所有 provider 模板(支持 `?model_type=llm` 过滤) |
| GET | `/providers/{provider}` | 取单个 provider 详情(schema + 模型列表) |
| GET | `/workspaces/current/providers` | 取当前 tenant 已配置的 provider(含状态、凭证清单、模型清单) |
| POST | `/workspaces/current/providers/{provider}/preferred-type` | 启用/禁用 |
| GET | `/workspaces/current/providers/{provider}/credentials` | 取凭证列表(返回 mask 值) |
| POST | `/workspaces/current/providers/{provider}/credentials` | 创建凭证 |
| PUT | `/workspaces/current/providers/{provider}/credentials/{credential_id}` | 更新凭证 |
| DELETE | `/workspaces/current/providers/{provider}/credentials/{credential_id}` | 删除凭证 |
| POST | `/workspaces/current/providers/{provider}/credentials/{credential_id}/activate` | 切换激活凭证 |
| POST | `/workspaces/current/providers/{provider}/credentials/validate` | 测试连接(不入库) |
| GET | `/workspaces/current/providers/{provider}/models` | 列出该 provider 下所有模型 |
| POST | `/workspaces/current/providers/{provider}/models` | 添加自定义模型 |
| DELETE | `/workspaces/current/providers/{provider}/models/{model_id}` | 删除模型 |
| POST | `/workspaces/current/providers/{provider}/models/{model_id}/toggle` | 启用/禁用模型 |
| POST | `/workspaces/current/providers/{provider}/models/{model_id}/load-balancing/toggle` | 开关 LB |
| GET | `/workspaces/current/providers/{provider}/models/{model_id}/credentials` | 取模型凭证列表 |
| POST | `/workspaces/current/providers/{provider}/models/{model_id}/credentials` | 创建模型凭证 |
| DELETE | `/workspaces/current/providers/{provider}/models/{model_id}/credentials/{credential_id}` | 删除 |
| POST | `/workspaces/current/providers/{provider}/models/{model_id}/credentials/{credential_id}/activate` | 切换激活 |
| POST | `/workspaces/current/providers/{provider}/models/{model_id}/credentials/validate` | 测试模型连接 |
| GET | `/workspaces/current/providers/{provider}/models/{model_id}/parameter-rules` | 取推理参数规则 |
| GET | `/workspaces/current/default-models` | 取默认模型列表 |
| POST | `/workspaces/current/default-models` | 设置默认模型 |
| GET | `/workspaces/current/providers/{provider}/health-checks` | 取健康检查历史 |
| POST | `/workspaces/current/providers/{provider}/health-check` | 触发一次主动健康检查 |

### 2.4 前端实现

页面路由:`/admin/providers`(Vue3 + Element Plus)

主要组件树:
```
AdminProviders.vue
├── ProviderTabs (All / LLM / Embedding / Rerank / Vision)
├── ProviderGrid (el-card 网格)
│   └── ProviderCard
│       ├── ProviderHeader (icon + label + status badge)
│       ├── ProviderActions (Setup / Add Model / Disable)
│       └── ModelsList (展开后)
│           ├── ModelRow (name + features icons + toggle)
│           └── ModelCredentialList
├── CredentialDialog (动态 schema 表单)
│   ├── DynamicField (按 schema type 渲染)
│   │   ├── SecretInput (mask 显示 sk-****1234)
│   │   ├── TextInput
│   │   ├── SelectInput
│   │   └── SwitchInput
│   ├── ValidateButton (调 /validate,显示 ✅/❌)
│   └── SaveButton (验证通过后保存)
├── ModelDialog (添加自定义模型)
├── LoadBalancingPanel (一个模型的多凭证管理)
└── HealthCheckDialog (查看历史 + 触发检查)
```

关键交互:
1. **首次进入**:并行拉 `GET /providers`(模板) + `GET /workspaces/current/providers`(已配置) → 合并渲染卡片
2. **Setup Provider**:点卡片 → 弹 CredentialDialog → 动态渲染 `provider_credential_schema` → 填写 → 点 Validate 测试 → 通过 → Save
3. **添加模型**(customizable-model):点 "Add Model" → 弹 ModelDialog → 选 model_type → 填 model_name → 填 `model_credential_schema` → Validate → Save
4. **多凭证**:provider 详情页 "Credentials" tab → 列表 → "Add Credential" → "Activate" 切换 → 删除/编辑
5. **负载均衡**:模型详情页 → "Load Balancing" 开关 → 开启后显示凭证列表 + 添加按钮
6. **健康状态**:卡片上绿/黄/红圆点,hover 显示最近检查结果

凭证 Mask 显示:
- `type: secret-input` 字段 → 截断为 `sk-****1234`(前 2 + 后 4,中间 4 星)
- 编辑时 placeholder 显示 mask,用户输入新值才覆盖
- DB 永远存密文,API 永远返回 mask

### 2.5 健康检查策略

- **被动熔断**:每次 invoke 失败(429/401/5xx)→ `failure_count++` + `cooldown_until = now + 60s`(Redis 共享)
- **主动 ping**:`POST /health-check` 接口触发,对 enabled provider/credential 调一次 `validate`,结果写 `provider_health_checks` 表
- **聚合状态**:
  - `healthy`:最近一次主动 ping 成功
  - `degraded`:最近一次失败但冷却未到期
  - `down`:连续 3 次主动 ping 失败

### 2.6 多 Provider 实现(新增)

在 `backend/core/providers/` 下新增:
- `anthropic_provider.py` — Anthropic Claude(支持 messages API + system prompt)
- `gemini_provider.py` — Google Gemini(支持 generateContent + safety_settings)
- `ollama_provider.py` — Ollama 本地(支持 /api/chat + /api/embeddings)
- `bedrock_provider.py` — AWS Bedrock(支持 converse + invoke_model)

每个 provider 实现 `BaseProvider` 抽象,注册到 `ProviderRegistry` 单例,与 provider_templates 表关联。

---

## 三、Prompt Playground(交互式调试)

### 3.1 后端 API 设计(SSE)

```
POST /api/v1/admin/playground/run       # 流式 Run,SSE 响应
  body: {
    prompt_name: str,
    version: Optional[int],             # 二选一
    label: Optional[str],               # 二选一,默认 "production"
    variables: Dict[str, Any],          # 模板变量
    model_overrides: Optional[Dict],    # 覆盖 prompt.config 中的 model/temperature 等
    tools: Optional[List[str]],         # 启用的工具列表(用于 ReAct 调试)
    thread_id: Optional[str],           # 多轮对话用
  }
  resp: text/event-stream
```

### 3.2 SSE 事件协议

| event | data 示例 | 时机 |
|---|---|---|
| `trace` | `{"trace_url": "https://langfuse/...", "trace_id": "..."}` | Run 开始,Langfuse trace 创建 |
| `token` | `{"content": "你好"}` | LLM token delta(打字机效果) |
| `tool_call_start` | `{"name":"search","id":"call_abc","index":0}` | 工具调用决策完成 |
| `tool_call_delta` | `{"index":0,"args":"{\"q\":\"a"}` | arguments 增量 |
| `tool_call_end` | `{"index":0,"args":"{...}"}` | arguments 收完 |
| `tool_result` | `{"index":0,"output":...,"latency_ms":120}` | 工具返回 |
| `node` | `{"node":"retrieve","state":...}` | 节点完成(stream_mode=updates) |
| `done` | `{"output":"...","usage":{"input":100,"output":50},"latency_ms":1200}` | 完成 |
| `error` | `{"message":"...","code":"..."}` | 错误 |
| `ping` | (空) | 心跳 |

### 3.3 核心实现骨架(sse-starlette)

```python
from sse_starlette.sse import EventSourceResponse
import asyncio, json

@router.post("/playground/run")
async def playground_run(
    req: PlaygroundRunRequest,
    http_request: Request,
    app_state: AppState = Depends(get_app_state),
    _: User = Depends(require_role(Role.ADMIN)),
):
    return EventSourceResponse(
        _run_stream(req, http_request, app_state),
        ping=15,
        send_timeout=5.0,
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
        },
    )

async def _run_stream(req, http_request, app_state):
    queue: asyncio.Queue = asyncio.Queue(maxsize=16)
    stop = asyncio.Event()

    async def producer():
        try:
            await _execute_playground(req, app_state, queue)
        except Exception as e:
            await queue.put({"event": "error", "data": json.dumps({"message": str(e)})})
        finally:
            await queue.put(None)  # 哨兵

    task = asyncio.create_task(producer())
    try:
        while True:
            if await http_request.is_disconnected():
                task.cancel()
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=25)
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": ""}
                continue
            if item is None:
                break
            yield item
    except asyncio.CancelledError:
        task.cancel()
        raise
    finally:
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
```

### 3.4 _execute_playground 内部逻辑

```python
async def _execute_playground(req, app_state, queue):
    # 1. 取 prompt 版本
    version = await _resolve_prompt_version(req)
    compiled = _render_prompt(version, req.variables)

    # 2. 合并 config overrides
    config = {**(version.config or {}), **(req.model_overrides or {})}

    # 3. Langfuse trace 绑定 prompt version
    trace_url = _bind_langfuse_trace(version, req)
    await queue.put({"event": "trace", "data": json.dumps({"trace_url": trace_url})})

    # 4. 路径选择:ReAct(带 tools) vs 简单 LLM 调用
    if req.tools:
        await _run_react_stream(req, compiled, config, queue, app_state)
    else:
        await _run_simple_stream(req, compiled, config, queue, app_state)

    # 5. 完成
    await queue.put({"event": "done", "data": json.dumps({"usage": ..., "latency_ms": ...})})
```

### 3.5 前端实现

新增页面:`/admin/playground`(Vue3)

布局:
```
AdminPlayground.vue
├── 左栏:配置面板
│   ├── Prompt 选择器(name + version/label)
│   ├── 模型覆盖(model/temperature/max_tokens 滑块)
│   ├── 变量编辑器(JSON editor + 单变量 input)
│   ├── 工具勾选(从 toolAdminApi.listTools 拉)
│   └── Run / Stop 按钮
├── 右栏:输出面板
│   ├── 输出区(打字机效果,token 逐字显示)
│   ├── tool_calls 展示(折叠卡片,显示工具名 + 参数 JSON + 结果)
│   ├── 节点进度(显示 graph 节点流转)
│   ├── Trace 链接(跳转 Langfuse)
│   └── 元信息(usage / latency / model)
└── 底部:历史 Run 记录(最近 20 条)
```

用 `@microsoft/fetch-event-source`(POST + Authorization header,不能用 EventSource):
```ts
import { fetchEventSource } from '@microsoft/fetch-event-source'

const ctrl = new AbortController()
await fetchEventSource('/api/v1/admin/playground/run', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${token}`,
  },
  body: JSON.stringify(payload),
  signal: ctrl.signal,
  onmessage(ev) {
    const data = JSON.parse(ev.data || '{}')
    switch (ev.event) {
      case 'trace': traceUrl.value = data.trace_url; break
      case 'token': output.value += data.content; break
      case 'tool_call_start': toolCalls.value.push({...data, args: ''}); break
      case 'tool_call_delta': toolCalls.value[data.index].args += data.args; break
      case 'tool_call_end': toolCalls.value[data.index].args = data.args; break
      case 'done': status.value = 'done'; usage.value = data.usage; break
      case 'error': status.value = 'error'; ElMessage.error(data.message); break
    }
  },
  onerror(err) { status.value = 'error'; throw err },  // throw 阻止自动重连
})
```

---

## 四、流式响应 + 背压缓冲

### 4.1 sse-starlette 集成

新增依赖:`sse-starlette>=2.0.0`

关键配置:
- `ping=15`:15s 心跳注释行,防代理超时
- `send_timeout=5.0`:写客户端超 5s 未确认 → 主动断
- `X-Accel-Buffering: no`:禁 nginx 缓冲
- `Cache-Control: no-cache`

### 4.2 背压策略

| 场景 | 策略 | maxsize |
|---|---|---|
| Playground SSE(单用户) | bounded `asyncio.Queue` | 16 |
| 评估流水线(无 SSE) | 不需要 Queue,直接 `async for` | — |
| 多用户并发 | 每连接独立 Queue | 16 |
| 慢客户端超 30s | `wait_for(put, 30)` 超时断流 + 返回 error | — |
| 工具流式输出 | 工具内部用 writer 推 custom 事件 | 共用同一 Queue |

### 4.3 客户端断连检测

关键认知(Starlette 0.35+ 的 EventSourceResponse 不会自动把断连错误传到生成器):
- 每帧前 `await request.is_disconnected()` 轮询(基于 ASGI http.disconnect)
- 捕获 `asyncio.CancelledError` 并 **reraise**(否则任务泄漏)
- 长推理单步阻塞 30s 期间无法轮询 → 用 `asyncio.wait_for(queue.get(), timeout=25)` + ping 心跳补足

### 4.4 评估流水线流式接入(非 SSE,内部消费)

```python
async def evaluate_with_streaming(prompt_name, variables):
    graph = create_evaluation_graph(...)
    # 不需要 SSE,直接 async for
    async for chunk in graph.astream(
        initial_state,
        stream_mode=["updates", "messages"],  # 同时拿节点级和 token 级
    ):
        # stream_mode="messages" → (AIMessageChunk, metadata)
        # stream_mode="updates" → {node_name: state_delta}
        ...
```

### 4.5 OpenAI tool_calls 流式 delta 处理

```python
tool_call_buffers = {}  # {index: {"name": str, "id": str, "arguments": str}}

for chunk in stream:
    if not chunk.choices:
        continue
    delta = chunk.choices[0].delta
    if delta.tool_calls:
        for tc in delta.tool_calls:
            idx = tc.index
            buf = tool_call_buffers.setdefault(idx, {"name": "", "id": "", "arguments": ""})
            if tc.id:
                buf["id"] = tc.id
            if tc.function and tc.function.name:
                buf["name"] = tc.function.name
            if tc.function and tc.function.arguments:
                buf["arguments"] += tc.function.arguments  # 字符串累加

# 流结束后统一 JSON 解析
for idx in sorted(tool_call_buffers):
    args = json.loads(tool_call_buffers[idx]["arguments"])
    dispatch_tool(tool_call_buffers[idx]["name"], args)
```

### 4.6 Provider 流式接口扩展

`BaseProvider` 新增 `stream_chat_completion` 抽象方法:

```python
async def stream_chat_completion(
    self,
    prompt: str,
    model: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: Optional[int] = None,
    tools: Optional[List[Dict]] = None,
) -> AsyncIterator[StreamChunk]:
    """流式返回 chunk,StreamChunk 含 content / tool_calls delta / finish_reason"""
    raise NotImplementedError
```

`StreamChunk` schema:
```python
@dataclass
class StreamChunk:
    content: Optional[str] = None              # 文本 delta
    tool_calls: Optional[List[ToolCallDelta]] = None  # tool_calls delta
    finish_reason: Optional[str] = None        # stop | tool_calls | length
    usage: Optional[Dict] = None               # 仅最后一个 chunk 有
```

---

## 五、Trace Replay(简版)

Langfuse 自身支持 Trace Replay,agentvalue-ai 不重复造轮子,只做:
- `GET /api/v1/admin/debug/evaluation/{id}/replay` — 重放某次评估的输入 + prompt 版本,产生新 trace
- 前端 Debug 页加 "Replay" 按钮,跳转 Playground 自动填充历史输入

---

## 六、实施计划(本次落地范围)

### 6.1 后端

1. **数据库迁移**:8 张新表(`backend/alembic/versions/xxx_p2_provider_crud.py`)
2. **模型定义**:`backend/models/provider_models.py`(与现有 models.py 分文件)
3. **Provider 模板 seed**:`backend/core/providers/seed/templates/*.yaml`(openai/anthropic/gemini/ollama)
4. **Provider CRUD API**:`backend/api/admin/providers.py`(24 个端点)
5. **多 Provider 实现**:`backend/core/providers/anthropic_provider.py` + `gemini_provider.py` + `ollama_provider.py`
6. **凭证加密集成**:复用 `core/encryption.py`,加 tenant DEK 层
7. **健康检查**:被动熔断(已有 CircuitBreaker)+ 主动 ping 接口
8. **Playground SSE**:`backend/api/admin/playground.py`(POST /run,EventSourceResponse)
9. **流式 Provider 接口**:`BaseProvider.stream_chat_completion` + 各 provider 实现
10. **tool_calls delta 聚合**:`backend/core/providers/stream_buffer.py`
11. **main.py 注册新路由**

### 6.2 前端

1. **API client 扩展**:`providerAdminApi` + `playgroundApi`(client.js)
2. **路由**:`/admin/providers` + `/admin/playground`(router/index.js)
3. **侧边栏**:新增 "供应商管理" + "Playground" 菜单(MainLayout.vue)
4. **AdminProviders.vue**:卡片网格 + 动态 schema 表单 + 凭证管理 + 模型管理 + 健康检查
5. **AdminPlayground.vue**:配置面板 + 输出面板 + tool_calls 展示 + 历史 Run
6. **fetch-event-source 依赖**:`package.json` 加 `@microsoft/fetch-event-source`
7. **AdminDebug.vue 增强**:加 Replay 按钮 → 跳转 Playground

### 6.3 验证

- `py_compile` + `ruff check` 全部新文件通过
- `npm run build` + `eslint` 通过
- 手动验证关键路径:Provider CRUD → 凭证加密 → 测试连接 → Playground Run

---

## 七、不做的(明确边界)

- **OTel 标准化**:目前 Langfuse tracer 满足需求,OTel 留 P3
- **完整 ReAct 模式迁移**:ReAct 仅在 Playground/调试入口可用,评估主路径仍用固定 9 节点图
- **评估框架 Experiments**:PromptEvalRun 表已建,但批量评测 UI 留 P3
- **插件系统**:不引入 Dify 的 plugin daemon,静态 YAML + DB 覆盖足够

---

## 八、风险与对策

| 风险 | 对策 |
|---|---|
| sse-starlette 与 GZipMiddleware 冲突 | 禁用 GZipMiddleware 或加 SSE 路由白名单 |
| nginx buffering 导致流式失效 | 加 `X-Accel-Buffering: no` + `proxy_buffering off` |
| 客户端断连后任务泄漏 | `CancelledError` 必须 reraise + finally 取消 task |
| 多用户并发背压 | 每连接独立 `asyncio.Queue(maxsize=16)` |
| tool_calls JSON 不完整 | 流结束后才 `json.loads`,加 30s 超时熔断 |
| 凭证泄露 | API 永远返回 mask,DB 永远密文,Redis TTL 60s |
| 多 Provider 实现工作量大 | 优先 OpenAI/Anthropic/Ollama,Bedrock 留 P3 |

---

## 九、依赖更新

### 9.1 backend/requirements.txt 新增

```
sse-starlette>=2.0.0
anthropic>=0.40.0
google-generativeai>=0.8.0
ollama>=0.4.0
```

### 9.2 frontend/package.json 新增

```
"@microsoft/fetch-event-source": "^2.0.1"
```

---

## 十、API 响应格式约定

### 10.1 Provider 列表响应

```json
{
  "data": [
    {
      "provider": "openai",
      "label": {"zh": "OpenAI", "en": "OpenAI"},
      "description": {...},
      "icon_small": "...",
      "icon_large": "...",
      "background": "#10A37F",
      "supported_model_types": ["llm", "embedding", "vision"],
      "configurate_methods": ["predefined-model"],
      "provider_credential_schema": {...},
      "models": {
        "llm": {
          "gpt-4o": {
            "model": "gpt-4o",
            "label": {...},
            "model_type": "llm",
            "features": ["chat", "vision", "function_calling"],
            "model_properties": {"mode": "chat", "context_size": 128000}
          }
        }
      },
      "status": "active",
      "status_info": "active",
      "custom_configuration": {
        "provider": {
          "credentials": [
            {"credential_id": "uuid", "name": "API KEY 1", "is_active": true}
          ]
        },
        "models": [...]
      },
      "preferred_provider_type": "custom"
    }
  ]
}
```

### 10.2 凭证 Mask 显示

- `type: secret-input` 字段 → `sk-****1234`(前 2 + 后 4,中间 4 星)
- 编辑时 placeholder 显示 mask
- 创建时 input 为空
- DB 永远密文,API 永远 mask

---

## 十一、参考文档索引

### Dify 模型供应商
- Dify 源码 Provider 模型:https://github.com/langgenius/dify/blob/main/api/models/provider.py
- Dify Controller:https://github.com/langgenius/dify/blob/main/api/controllers/console/workspace/model_providers.py
- Dify Service 层:https://github.com/langgenius/dify/blob/main/api/services/model_provider_service.py
- Dify Provider 领域核心:https://github.com/langgenius/dify/blob/main/api/core/provider_manager.py
- Dify Model Runtime 工厂:https://github.com/langgenius/dify/blob/main/api/core/model_runtime/model_providers/model_provider_factory.py
- Dify 前端 Provider 页面:https://github.com/langgenius/dify/tree/main/web/app/components/header/account-setting/model-provider-page
- Dify 模型供应商管理服务内幕:https://instagit.com/langgenius/dify/dify-model-provider-management-service-internals.md
- Dify Model Provider Management 架构图:https://deepwiki.com/kaznishi/dify/4.3-model-provider-management
- Dify 多 Token 供应商动态切换:https://blog.csdn.net/weixin_42350014/article/details/156281291

### Langfuse Playground
- Langfuse Prompt Management Get Started:https://langfuse.com/docs/prompts/get-started
- Langfuse Core Concepts:https://js-sdk-v4-docs-snapshot.langfuse.com/docs/prompt-management/data-model/
- Langfuse Variables in Prompts:https://langfuse.com/docs/prompt-management/features/variables
- Langfuse Overview:https://langfuse.com/docs
- Langfuse Prompt Management(mirascope):https://mirascope.com/blog/langfuse-prompt-management
- Langfuse Playground 实战:https://blog.csdn.net/gitblog_00675/article/details/150949218
- Langfuse Amazon Bedrock Integration:https://langfuse.com/docs/integrations/amazon-bedrock
- Langfuse Evaluation for OpenAI-Agents SDK:https://langfuse.com/guides/cookbook/example_evaluating_openai_agents

### Dify / Coze Prompt 调试
- Coze Debug prompts:https://docs.coze.com/guides/debug_prompts
- Dify Prompt 调试到发布:https://blog.csdn.net/weixin_35920379/article/details/156282329
- Dify Prompt 设计指南:https://nocoderi.co.jp/2025/04/02/dify%E3%81%AE%E3%83%97%E3%83%AD%E3%83%B3%E3%83%97%E3%83%88%E8%A8%AD%E8%A8%88%E3%82%AC%E3%82%A4%E3%83%89/
- Dify Error Types:https://docs.dify.ai/en/self-host/use-dify/debug/error-type

### FastAPI SSE
- FastAPI SSE 官方文档:https://fastapi.tiangolo.com/ko/tutorial/server-sent-events/
- sse-starlette 3.4.1 PyPI:https://pypi.org/project/sse-starlette/3.4.1/
- sse-starlette 源码深度解析:https://yuqingteck.blog.csdn.net/article/details/159281269
- sse-starlette Ping Configuration:https://deepwiki.com/sysid/sse-starlette/3.3-stream-generators
- sse-starlette Overview:https://deepwiki.com/sysid/sse-starlette/1-overview
- Streaming Responses in FastAPI:https://hassaanbinaslam.github.io/posts/2025-01-19-streaming-responses-fastapi.html
- FastAPI SSE AI 实时流式:https://tech-lab.sios.jp/archives/50750
- FastAPI SSE 客户端断开连接处理:https://ask.csdn.net/questions/9126086
- ConnectionResetError 排查:https://theneuralbase.com/fastapi/errors/fastapi-eventsourceresponse-sse-connection-drop/
- Cancellation handling for long-running inferences:https://theneuralbase.com/fastapi-for-ml/learn/intermediate/cancellation-handling-for-long-generations/
- Server-Sent Events(X-Accel-Buffering):https://www.stacklesson.com/react-fastapi/fastapi-websockets/ch31-lesson-05-server-sent-events/

### LangChain / LangGraph 流式
- LangChain Event Streaming(v3):https://docs.langchain.com/oss/python/langchain/event-streaming
- LangGraph Streaming:https://docs.langchain.com/oss/javascript/langgraph/streaming
- LangGraph 流式架构权威指南:https://blog.csdn.net/m0_63309778/article/details/151106004
- astream async execution:https://theneuralbase.com/langgraph/learn/beginner/astream-async-execution/
- Streaming Agent Responses 2026:https://www.callsphere.ai/blog/streaming-agent-responses-openai-langchain-2026
- LangChain Streaming Overview:https://docs.langchain.org.cn/oss/python/langchain/streaming/overview
- LangChain function calling 流式:https://python.langchain.ac.cn/docs/how_to/function_calling/

### OpenAI tool_calls 流式 delta
- OpenAI Responses API streaming field guide:https://community.openai.com/t/responses-api-streaming-the-simple-guide-to-events/1363122
- Assembling arguments from stream:https://theneuralbase.com/function-calling/learn/intermediate/assembling-arguments-from-stream/
- Accumulating tool call chunks:https://theneuralbase.com/function-calling/learn/intermediate/accumulating-tool-call-chunks/
- Detecting tool call start in stream:https://theneuralbase.com/function-calling/learn/intermediate/detecting-tool-call-start-in-stream/

### Vue3 / 前端 SSE
- 前端 SSE 实战指南:https://juejin.cn/post/7649582093870350372
- Vue3 SSE fetch + ReadableStream:https://ask.csdn.net/questions/9181893
- Vue3 与 SSE:https://www.cnblogs.com/jocelyn11/p/18245535
- SSE with fetch + ReadableStream:https://www.web-developpeur.com/en/blog/sse-fetch-readable-stream-api-key

### 背压 / 异步生成器
- Python asyncio 背压机制:https://m.php.cn/faq/2112701.html
- Reactive Streams with Python AsyncIO:https://kindatechnical.com/reactive-processing/reactive-streams-with-python-asyncio.html
- asyncio.Queue 官方文档:https://docs.python.org/3/library/asyncio-queue.html
- Streaming with Async Generators:https://www.callsphere.ai/blog/streaming-async-generators-real-time-ai-response-pipelines
- LLM Streaming Tutorial Backpressure:https://machinelearningplus.com/gen-ai/llm-streaming-python/

### LiteLLM / OpenRouter 对比
- LiteLLM Proxy 快速开始:https://docs.litellm.ai/docs/proxy/docker_quick_start
- LiteLLM 配置选项完整参考:https://www.mintlify.com/BerriAI/litellm/proxy/configs
- OpenRouter Models API:https://openrouter.ai/docs/guides/overview/models
- OpenRouter Provider Routing:https://openrouter.ai/docs/guides/routing/provider-selection
- OpenRouter Provider Routing 特性:https://openrouter.ai/docs/features/provider-routing
