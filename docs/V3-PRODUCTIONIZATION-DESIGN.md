# AgentValue v3.0 落地化设计书

> 状态：✅ 已实施完成 (v2.4.0 落地, 4 条工作流全部交付并通过验证)
> 目标版本：v3.0.0（生产化里程碑，代码以 v2.4.0 发布，后续小步迭代至 v3.0.0）
> 设计日期：2026-08-08
> 实施完成：2026-08-08

---

## 1. 背景与目标

v2.3.0 完成了前后端契约清零（490 路由 / 441 调用 / 0 漂移），**接口层是通的**。
但一次全量代码审计（后端 362 文件 / 13 万行，前端 103 文件 / 3.5 万行）暴露出
**"接口通但业务不落地"** 的结构性问题：表建好了没人写、开关声明了走的是 dummy、
中间件挂了但不 gate 任何端点。

本设计书的目标是把 AgentValue 从 **"功能演示完整"** 推到 **"生产可落地"**，
并对标同类产品补齐基础/进阶/高级三层能力。

### 1.1 对标基准

| 赛道 | 对标产品 | AgentValue 需对齐的能力 |
|---|---|---|
| LLM 可观测 | LangSmith / Langfuse / Arize Phoenix / Helicone | 原生 Trace/Span 存储、成本账本、瀑布图、回放 |
| 评估平台 | Braintrust / Promptfoo / Ragas / DeepEval | 实验对比、RAGAS 指标、回归门禁、逐样本 diff |
| Agent 平台 | Dify / Coze / LangGraph Platform / Flowise | 可视化编排、MCP、记忆管理、沙箱隔离 |
| 企业集成 | Segment / Svix / Zapier | 出站 Webhook（签名+重试+死信）、开放 API + SDK |
| 合规治理 | SOC2 / 等保 2.0 | 审计防篡改链、租户隔离、细粒度配额 |

---

## 2. 审计结论：三类致命缺口

### 2.1 「表建好了，但从来没人写」

| 缺口 | 证据 | 后果 |
|---|---|---|
| 成本账本 | `models/conversation_analytics.py` 与 `quota_models.BillingRecord` 定义完整，但 `record_metrics` 全库**零调用方** | 所有成本/用量看板永远空白 |
| Trace 存储 | 仅 `models.py:541` 一个 `trace_ids: JSON` 外链 Langfuse，无 span 表 | 自托管即失去全部链路追踪 |

### 2.2 「声明了能力，实际走 dummy / 静默降级」

| 缺口 | 证据 | 后果 |
|---|---|---|
| Embedding | `core/embeddings.py:101,116` 无 key 或调用失败均**返回零向量** | RAG 静默失效，检索结果无意义且无告警 |
| Rerank | `core/config.py:249` 默认 `rerank_provider="dummy"`，且 `HybridSearchService` 路径**根本不过 rerank** | 检索精度停留在 BM25+向量朴素融合 |
| OCR | `doc_parsing_service.py:44` 声明 `"ocr"` 策略，实现里只有 pdfplumber 文本抽取 | 扫描件 PDF 必然失败 |

### 2.3 「中间件挂了，但不产生任何约束」

| 缺口 | 证据 | 后果 |
|---|---|---|
| API Key | `ApiKeyMiddleware` 注入 `api_key_id`，但**无任何路由消费它** | API Key 形同虚设，无法对外开放 |
| 租户隔离 | `analytics_service.py`(965 行) / `hybrid_search_service.py`(936 行) 中 `tenant_id` 出现 **0 次** | 跨租户数据泄露 |
| 限流 | slowapi 进程内内存计数 | 多副本部署即失效 |
| 出站 Webhook | `alert_service.py:494` 单条 POST 无重试；`publish_service.py` 的飞书/钉钉 URL 是**字符串拼接伪造的** | 集成能力不可用 |

---

## 3. 实施方案（4 条并行工作流）

为避免并行开发的合并冲突，引入 `backend/api/feature_registry.py` 声明式路由注册中心，
`main.py` 只保留一次 `register_feature_routers(app)`；各工作流在注册表中占用**独立槽位**。
Alembic 迁移预分配**线性 revision 链**，杜绝多 head。

```
o6p7q8r9s0t1 (v2.3 head)
   └─ p1a1trace000  [WS-1] 原生 Trace/Span + 成本账本
        └─ p2b2evalexp0  [WS-2] 实验对比 + RAGAS
             └─ p3c3webhook0 [WS-3] 出站 Webhook + API Key Scope
                  └─ p4d4govern0 [WS-4] 租户隔离 + 审计链 + 配额
```

### WS-1 原生可观测性（对标 Langfuse / LangSmith）

**新增数据模型** `models/trace_models.py`
- `TraceRecord`：`trace_id` / `tenant_id` / `name` / `kind` / `status` / `duration_ms` /
  `total_prompt_tokens` / `total_completion_tokens` / `total_cost` / `user_id` / `session_id` / `tags`
- `SpanRecord`：`span_id` / `parent_span_id` / `trace_id` / `kind`(llm·tool·retriever·agent·chain·http) /
  `input` / `output` / `model` / `prompt_tokens` / `completion_tokens` / `cost` / `error` / `attributes`

**新增能力**
1. `core/pricing.py`：模型定价表（$/1M tokens），支持 DB/配置覆盖，未知模型走 `default` 兜底并打点
2. `core/observe.py`：基于 `contextvars` 的 `trace_context()` / `span()` 上下文管理器 + `@observe` 装饰器，
   零侵入采集；批量异步 flush，不阻塞主链路
3. `core/llm_call.py` 接入：每次 LLM 调用落 `SpanRecord` + `ConversationMetrics` + `BillingRecord`
4. `api/admin/trace_v2_routes.py`：trace 列表（多维过滤）、瀑布图详情、统计聚合、
   成本分析（按模型/用户/日期）、Trace 导出

### WS-2 评估体系升级（对标 Braintrust / Ragas）

**RAGAS 指标** `services/ragas_metrics_service.py`
- `faithfulness`（答案是否被上下文支撑）
- `answer_relevancy`（答案与问题相关度）
- `context_precision`（检索上下文的信噪比）
- `context_recall`（检索是否覆盖 ground truth）
- `answer_correctness`（与标准答案的语义+事实一致性）

**实验对比** `models/experiment_models.py` + `services/experiment_service.py`
- `Experiment` / `ExperimentRun` / `ExperimentRunItem`（逐样本结果）
- Run A vs Run B：指标 delta、**逐样本回归清单**（improved / regressed / unchanged）、
  bootstrap 置信区间与显著性判定（无 scipy 依赖，纯 stdlib 实现）
- CI 门禁：`eval/evaluate.py --compare --fail-under` 接入 GitHub Actions

### WS-3 集成与开放能力（对标 Svix / Stripe API）

**出站 Webhook** `models/webhook_subscription.py` + `services/webhook_delivery_service.py`
- 订阅注册表（url / events[] / secret / headers / enabled）
- HMAC-SHA256 签名（`X-AgentValue-Signature`, `t=<ts>,v1=<sig>` 防重放）
- 指数退避重试（1s→2s→4s→…，最多 6 次）+ 死信队列 + 手动重放
- 投递日志（请求/响应/耗时/状态码）

**开放 API** `api/public/v1/*`
- `ApiKey` 增加 `scopes` / `rate_limit` / `expires_at`
- `require_api_key(scopes=[...])` 依赖，真正 gate 端点
- Python SDK（`sdk/python/agentvalue/`）+ TypeScript SDK（`sdk/typescript/`）

### WS-4 企业级治理加固

1. **租户隔离**：补齐 8 个缺 `tenant_id` 的模型；为 `analytics` / `hybrid_search` /
   `multi_vector` 三个服务补过滤；增加 SQLAlchemy 全局查询守卫（缺租户条件时告警/拦截）
2. **审计防篡改**：`AuditLog` 增加 `prev_hash` / `entry_hash`，形成哈希链 + 完整性校验端点
3. **Redis 限流**：令牌桶算法，支持 租户 / API Key / 端点 / 用户 四维配额，多副本一致
4. **沙箱加固**：`code_interpreter` 增加 `resource.setrlimit`（AS / CPU / NOFILE / FSIZE）+ 进程组隔离
5. **可靠后台任务**：`llm_judge` / `rag_eval` / `graph_rag` / `doc_parsing` 从裸 `asyncio.create_task`
   迁移到 arq，获得崩溃恢复与自动重投

### WS-5 前端补齐

1. **i18n**：接入 `vue-i18n`，zh-CN / en-US / ja-JP 三语言 + Element Plus 语言包
2. **共享组件库** `components/common/`：`PageHeader` / `DataTable` / `CrudDialog` /
   `StatCard` / `EmptyState` / `FilterBar`，收敛 33 个 admin 页的重复样板
3. **新增页面**：Trace v2（瀑布图+成本）、实验对比、Webhook 订阅、用户组/ABAC、
   合规中心、洞察问答、证据链、Prompt 自动优化、工具配置、会话分享页、404 页
4. **实时推送**：通知/告警从 45s 轮询升级为 SSE
5. **修复**：Chat 模型列表接真实 provider、桌面端停止生成通知后端、移动端占位页补齐

---

## 4. 验收标准

| 维度 | 标准 |
|---|---|
| 契约 | 两层契约漂移保持 **0**（client.js↔后端、view↔client.js）|
| 迁移 | Alembic 单 head，`upgrade head` → `downgrade base` 往返通过 |
| 测试 | 后端 pytest 无新增失败；新增能力配套单测 |
| 构建 | `npm run build` 通过 |
| 真实性 | 无新增 dummy / 静默降级路径；降级必须打点或告警 |
| 仓库 | 清理构建产物与无用文件，`.gitignore` 覆盖完整，三平台同步推送 |

---

## 5. 风险与对策

| 风险 | 对策 |
|---|---|
| 观测埋点拖慢主链路 | span 异步批量 flush + 失败不抛出，采样率可配 |
| 租户守卫误伤存量查询 | 先 warn-only 模式跑一轮，确认无误后再切 enforce |
| Embedding 从静默降级改为 fail-fast 引发存量报错 | 配置项 `embedding_strict_mode`，默认 true，可回退 |
| 定价表过期 | 定价支持 DB 覆盖 + 管理页维护，代码内置仅作兜底 |
