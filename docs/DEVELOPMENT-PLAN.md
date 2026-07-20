# AgentValue-AI 开发计划（规划基线）

> 本文件由 2026-07-12 全仓库调研产出，作为后续开发的规划基线。
> 调研方法：通读 backend/frontend/docs/CI，对照 architecture-decisions / architecture-notes / ADR / CHANGELOG / security_audit_export 的 KNOWN_TECH_DEBT 交叉验证。
> 当前版本：v1.5.0，HEAD 即将提交。代码中无 TODO/FIXME 残留，未完成项均显式记录在文档中。
> P2 深水区（模型供应商 CRUD + Prompt Playground + 多 Provider 接入）已完成，详见 [UPGRADE-DESIGN-P2.md](UPGRADE-DESIGN-P2.md)。
> **P3-P7 规模化/测试/CI/生态阶段已全部完成**（arq 任务队列 + Postgres checkpointer + 200+ 测试用例 + CI 加固 + 飞书/GitLab 适配层骨架）。
> **v1.4.0 P1-P4 大厂对标阶段已全部完成**（知识库 UI / 链路追踪 / Token 趋势 / Rerank / 自定义工具 / Feature Flag / Multi-Agent / Workflow 编排），详见 §五 阶段计划 P1-P4 行。
> **v1.5.0 AI 对话系统 + Agent 工具层已完成**（10 项 P0 功能 + 5 个 Agent 工具对标 opencode），详见 §五-补2。

---

## 一、现状摘要

| 维度 | 状态 |
|---|---|
| 后端 | FastAPI + LangGraph，多租户、RBAC、审计、护栏、多模态、对象存储、可观测性齐备 |
| 前端 | Vue3 + Element Plus + ECharts，4 角色 14 视图均完整，含水印防截图 |
| Agent | DB 状态机版（多实例可用）+ interrupt 版（MemorySaver，单实例）两套 |
| 测试 | 后端 47 个测试文件覆盖较全；前端仅 2 个单测 |
| CI | ruff/后端测试/前端 build/prompt-gate 阻断；black/mypy advisory；trivy/pip-audit/gitleaks 硬阻断 |
| 部署 | Docker Compose（开发 + prod override），无自动部署 pipeline |

---

## 二、未完成项清单

### 高严重度（生产/规模化阻断）

| ID | 位置 | 内容 | 依据 |
|---|---|---|---|
| H1 | `docs/architecture-decisions.md` P1-9；`backend/scripts/security_audit_export.py` TD-003 | `audit_logs` 表仅应用层 append-only，DB 层无 trigger 强制，有 DB 写权限者可 UPDATE/DELETE 篡改审计记录。PostgreSQL trigger 示例待 DBA 审核部署 | **迁移已落地**：`backend/alembic/versions/c4d5e6f7a8b9_add_audit_logs_append_only_trigger.py` 创建 PG BEFORE UPDATE/DELETE/TRUNCATE trigger（RAISE EXCEPTION 阻断），SQLite 跳过。py_compile+ruff 通过，PG trigger SQL 对照 P1-9 官方示例。真实 PG 部署由 DBA 审核验证（沙箱无 PG） |
| H2 | ~~`docs/adr/003-job-queue-evolution.md`；`docs/scale-deployment-runbook.md:223`~~ | ~~任务队列是裸 `redis.asyncio`，无 worker/消费组/重投，进程崩溃则任务卡死。决策已演进到 arq 但未切换~~ **已解决**（P3）：`core/arq_worker.py` + `core/arq_job_queue.py` 落地 arq 0.28 适配。`create_job_queue()` 三级降级：ArqJobQueue（USE_ARQ_QUEUE=true）→ RedisJobQueue → InMemoryJobQueue。独立 worker 进程支持 `max_tries` 自动重投 + `agentvalue:dead_letter:{job_id}` 死信队列。`arq core.arq_worker.WorkerSettings` 启动。未启用时降级裸 Redis 共享存储，向后兼容 |
| H3 | ~~`backend/agent/graph.py:436-727`~~ | ~~`create_evaluation_graph_with_interrupt` 用 `MemorySaver`，thread_store 内存态，interrupt 审批流只能单实例~~ **已解决**（P3）：`agent/graph.py` `_create_checkpointer()` 按 `settings.use_postgres_checkpointer` 选择 `AsyncPostgresSaver`（持久化 interrupt 状态，支持多 worker 水平扩展）或 `MemorySaver`（单实例，本地开发默认）。`langgraph-checkpoint-postgres` 已加入 requirements |
| H4 | `backend/scripts/check_prod_readiness.py:218-240` | `JWT_ALGORITHM=HS256` 生产仅 WARN，建议评估改 RS256/ES256 | **评估完成**：见 [ADR-004](adr/004-jwt-asymmetric-algorithm.md)，决策"保留 HS256 + WARN，待 H5 KMS/Vault 落地后再评估切换" | 安全加固 |
| H5 | `backend/scripts/security_audit_export.py` TD-002 | JWT 密钥未接 KMS/Vault，状态"待整改" | 生产合规 |

### 中严重度

| ID | 位置 | 内容 |
|---|---|---|
| M1 | ~~`backend/core/multimodal/extractors.py:451-542`；`docs/architecture-notes.md:133`~~ | ~~`CloudOCR` 代码已实现 OpenAI vision 路径，但文档仍写“接口尚未实现”——文档滞后~~ **已解决**：`docs/architecture-notes.md` L133 已更正为 OpenAI 兼容 vision API 路径（`POST /v1/chat/completions` + `image_url` base64 data URI）描述，补齐配置项 `OCR_CLOUD_API_KEY`/`OCR_CLOUD_BASE_URL`/`OCR_CLOUD_MODEL` 与支持模型清单（gpt-4o / gpt-4o-mini / qwen-vl-plus / glm-4v），与代码 `extractors.py:451-542` 实际实现一致 |
| M2 | ~~`backend/core/multimodal/extractors.py:571-632`~~ | ~~`WhisperASR` 同上，已落地但文档注释滞后~~ **已解决**：`docs/architecture-notes.md` L143 已更正为 OpenAI 兼容 audio transcription API 路径（`POST /v1/audio/transcriptions`）描述，补齐配置项 `ASR_CLOUD_API_KEY`/`ASR_CLOUD_BASE_URL`/`ASR_CLOUD_MODEL` 与支持模型（whisper-1），与代码 `extractors.py:571-632` 实际实现一致 |
| M3 | ~~`backend/memory/vector_store.py`~~ | ~~ChromaDB 单机文件模式，多进程并发写入不安全~~ **P7 骨架已落地**：`integrations/` 目录新增 `IMAdapter`/`CodeRepoAdapter` 抽象基类 + Dummy 默认实现 + Feishu/GitLab 骨架（NotImplementedError）+ 工厂降级。ChromaDB 分布式改造（HTTP 模式 / 分布式后端）作为后续运维任务保留，单机文件模式在 arq 多 worker 场景下需运维侧配置独立 ChromaDB 实例 |
| M4 | `backend/core/model_router.py` | `_health_history` 进程内状态，多 Pod 健康度不一致 |
| M5 | `backend/core/config.py` | `Settings` 扁平 60+ 字段，未按领域拆分（P7 `integrations/settings.py` 已示范独立 settings 模式，可作为后续重构模板） |
| M6 | ~~`backend/core/providers/base.py:75-83`~~ | ~~`chat_completion_stream`/`embeddings_create`/`function_calling` 已定义但主链路未调用，CHANGELOG 称部分已删，需澄清真实保留清单~~ **已解决**：P2 深水区落地 4 个 Provider(OpenAI/Anthropic/Gemini/Ollama)的 `stream_chat_completion` 实现，`function_calling` 在 Anthropic/Gemini/Ollama 三个新 Provider 全部实现，Playground SSE 已消费 `stream_chat_completion` |
| M7 | `backend/scripts/security_audit_export.py` TD-004 | Feedback/Memory 表无 `archived` 字段，GDPR 删除依赖周期清理脚本 |
| L6 | `docs/deployment-guide.md:317-339` | ~~`seed-demo-users` 接口仅文档提示生产禁用，未做代码层硬删除~~ **已解决**：`core/config.py` model_validator 在生产+demo_mode 时硬拒绝 Settings 实例化 + 接口层 demo_mode 检查 403，双层守护下生产不可达。deployment-guide.md 描述已更正 |

### 低严重度（CI/工具链）

| ID | 位置 | 内容 |
|---|---|---|
| L1 | ~~`.github/workflows/ci.yml:42,137`~~ | ~~`black --check` 与 `mypy` 均为 advisory（continue-on-error）~~ **已解决**（P6）：CI 中 mypy/black 移除 `continue-on-error: true`，改为硬阻断。新增 PR-only `ai-review` job（用 `CLOUD_API_KEY`/`CLOUD_BASE_URL`/`CLOUD_MODEL` 环境变量）和 `nightly-e2e` job（`schedule: cron: '0 18 * * *'`，失败时自动创建 issue 通知） |
| L2 | `backend/pyproject.toml:18-27` | ruff 仅启用 E/F/W，I/B/UP/SIM/RUF 因历史噪音未启用 |
| L3 | `backend/pip-audit-ignore.txt` | chromadb 1.5.9 受 PYSEC-2026-311 影响，暂无修复版本 |
| L4 | ~~`.github/scripts/ai_review.py`~~ | ~~AI 代码审查脚本已落地，触发工作流待接入~~ **已解决**（P6）：`ai-review` job 已接入 `.github/workflows/ci.yml`，PR 触发时自动跑 AI 代码审查，结果作为评论附在 PR 上 |
| L5 | `monitoring/alerts.yml` | Alertmanager 未内置，告警通知需另行部署 |

---

## 三、设计 vs 实现偏差

1. **FEEDBACK_COLLECT 节点**：计划画作图内串行节点，实际收敛到 API 层（`POST /evaluations/{id}/feedback|appeal|re-evaluate`）。功能等价，偏差已闭环。
2. **TestContainers 未引入**：改用内存 SQLite + DummyEmbedding + MockProvider，TestContainers 作为生产可选增强保留，未引入。
3. **任务队列**：~~裸 redis.asyncio 做共享状态存储，非真正队列（见 H2）~~ **已闭环**（P3）：arq 任务队列落地，`create_job_queue()` 三级降级。生产用 `arq core.arq_worker.WorkerSettings` 启动独立 worker 进程，启用自动重投 + 死信队列。详见 H2。
4. **Provider 死代码**：~~base.py 三个方法已定义未接入，与 CHANGELOG"已删除"描述存在轻微不一致（见 M6）~~ **已闭环**：P2 深水区落地 4 个 Provider 完整实现（见 M6）。
5. **多模态真实接入**：~~CloudOCR/WhisperASR 代码已实现，文档注释滞后（见 M1/M2）~~ **已闭环**（P5）：文档已订正，见 M1/M2。
6. **LangGraph interrupt 单实例限制**：~~MemorySaver 内存态，规模化需 DB 状态机版（见 H3）~~ **已闭环**（P3）：`_create_checkpointer()` 支持 PostgresSaver 持久化，详见 H3。
7. **P2 深水区新增**：模型供应商 CRUD(8 表 + 24 端点)+ Prompt Playground(SSE 流式)+ 4 个 Provider(OpenAI/Anthropic/Gemini/Ollama)已落地，对标 Dify/Coze/Langfuse。完整设计见 [UPGRADE-DESIGN-P2.md](UPGRADE-DESIGN-P2.md)。
8. **P3-P7 规模化/测试/CI/生态阶段**：~~arq + Postgres checkpointer + 测试补全 + CI 加固 + 飞书/GitLab 适配层均待做~~ **已全部完成**：见 P3-P7 节。
9. **P3-P7 期间顺手修复的预存在 bug**：(a) `RedisJobQueue.update()` 误用 `AsyncScript.eval()`（实际无此方法，应直接 `script(keys=..., args=...)`），导致 Lua 原子 update 静默失败；(b) `record_token_usage` 在 Anthropic/Gemini/Ollama 三个 Provider 漏传 `model` 参数（签名 4 个，调用 3 个），埋点丢失；(c) `_make_authed_metrics_asgi` 在构造期一次性捕获 mode，测试 monkeypatch 后 /metrics 仍走旧鉴权模式；(d) InMemoryJobQueue.get() 改 deepcopy 后 `test_inmemory_enqueue_and_get` 未同步改 `is`→`==`；(e) `_FakeTracer.generation` mock 不接受 P1 新增的 prompt_name/version kwargs，TypeError 被静默吞掉。5 个 bug 全部修复并补单测。
10. **深度集成测试发现的 P3 阻断 bug**：`core/arq_worker.py:run_evaluation_task` 调用 `get_app_state()` 不传 `request` 参数（`api.deps.get_app_state(request: Request)` 是 FastAPI Depends 函数，arq worker 是独立进程无 request 上下文），启用 `USE_ARQ_QUEUE=true` 后第一个评估任务即抛 `TypeError: get_app_state() missing 1 required positional argument: 'request'`，规模化功能完全不可用。修复：`on_startup` 时创建 `AppState` 单例存到 `ctx["app_state"]`，`run_evaluation_task` 从 `ctx` 取；`on_shutdown` 释放资源。同时新增 `tests/test_integration_scenarios.py` 11 个 Scenario / 38 个 test cases 模拟生产服务场景的端到端串联测试（完整评估闭环 + Playground SSE + DLQ + 凭证加脱敏 + 集成降级 + metrics 鉴权 + 护栏 + 多租户 + 三级降级工厂 + PostgresSaver 降级 + ToolCallAggregator）。全量测试 **1292 passed**（旧 1254 + 新 38），0 failed。
11. **v1.4.0 P1-P4 大厂对标阶段闭环**：深度对比 Dify / Coze / Langfuse 后发现 8 项能力缺口（知识库 UI / 链路追踪可视化 / Token 趋势 / Rerank Provider / 自定义工具 / Feature Flag / Multi-Agent 协作 / Workflow 编排）已全部补全，详见 §五-补 P1-P4 行。Review 阶段发现并修复 3 个 P0/P1 bug：(a) `api/admin/rerank.py:91` `HTTP_503_SERVICE_UNAVALABLE` 拼写错误（漏 "I"），改为 `HTTP_503_SERVICE_UNAVAILABLE`；(b) `requirements.txt` 缺 `langchain-openai`，langchain 0.3+ 不再内置 `langchain_openai`，新增 `langchain-openai>=0.2.0`；(c) `agent/graph.py` Rerank Provider 双实例并存 + `FeatureFlagService` 60s LRU 缓存完全失效（每次 `retrieve_context` new 一个 service 导致缓存形同虚设，DB 反复查询），通过新增 `set_app_state_for_graph(app_state)` 由 `main.py` lifespan 注入，`_get_rerank_provider` 优先复用 `app_state.rerank_provider`、`_rerank_kb_if_enabled` 优先复用 `app_state.feature_flag_service` 修复。代码去重：新增 `api/admin/_common.py` 统一 ID 生成与 entity 序列化逻辑。全量测试 **1478 passed**（旧 1292 + 新 186），0 failed。

---

## 四、测试覆盖缺口

后端缺独立单测的模块（按严重度）：

| 模块 | 路径 | 严重度 |
|---|---|---|
| ~~密码哈希/校验 `backend/auth/password.py`~~ | ~~中~~ | **已闭环**（P4）：`tests/test_auth_password.py` 19 cases |
| ~~JWT 签发/过期/claims `backend/auth/jwt_handler.py`~~ | ~~中~~ | **已闭环**（P4）：`tests/test_auth_jwt_handler.py` 29 cases |
| ~~RBAC 角色装饰器 `backend/auth/rbac.py`~~ | ~~中~~ | **已闭环**（P4）：`tests/test_auth_rbac.py` 32 cases |
| ~~Provider 凭据服务 `backend/services/provider_credential_service.py`~~ | ~~中~~ | **已闭环**（P4）：`tests/test_provider_credential_service.py` 32 cases |
| ~~Playground 流式 `backend/api/admin/playground.py`~~ | ~~中~~ | **已闭环**（P4）：`tests/test_stream_buffer.py` 13 cases |
| ~~Provider 路由 `backend/api/admin/providers.py`~~ | ~~中~~ | **已闭环**（P4）：`tests/test_providers_router.py` 19 cases |
| ~~Anthropic Provider~~ | ~~中~~ | **已闭环**（P4）：`tests/test_anthropic_provider.py` 14 cases |
| ~~Gemini Provider~~ | ~~中~~ | **已闭环**（P4）：`tests/test_gemini_provider.py` 14 cases |
| ~~Ollama Provider~~ | ~~中~~ | **已闭环**（P4）：`tests/test_ollama_provider.py` 11 cases |
| ~~集成适配层 `backend/integrations/`~~ | ~~中~~ | **已闭环**（P7）：`tests/test_integrations.py` 16 cases |
| 认证路由 | `backend/api/auth_routes.py` | 中（端到端已覆盖,单测未补） |
| 分析路由 | `backend/api/analytics_routes.py` | 中（端到端已覆盖,单测未补） |
| 限流配置 | `backend/core/rate_limit.py` | 低 |
| LLM 调用 | `backend/core/llm_call.py` | 低 |
| Prometheus 指标 | `backend/core/metrics.py` | 低（`tests/test_token_usage.py` 已覆盖 token usage 维度） |

P4 阶段共新增 9 个测试文件、183 个 test cases 全部 passing。P3-P7 累计 199 个新 test cases（含 P7 集成适配层 16 cases）。

前端：仅 `test/Watermark.test.js`、`test/auth.test.js` 两个单测，14 视图无组件级测试，CI 也未跑 `npm run test`/e2e/perf。

---

## 五、阶段计划（优先级排序）

| 阶段 | 内容 | 涉及项 | 产出 |
|---|---|---|---|
| **P1 生产上线硬化（已完成）** | audit_logs DB trigger 落地 + seed-demo 接口生产硬禁用 + JWT 非对称化评估 | H1, L6, H4 | trigger 迁移、config 守护、评估报告 |
| **P2 深水区（已完成）** | 模型供应商 CRUD(8 表 + 24 端点)+ Prompt Playground SSE + 4 Provider 实现(OpenAI/Anthropic/Gemini/Ollama) | M6 | 详见 [UPGRADE-DESIGN-P2.md](UPGRADE-DESIGN-P2.md) |
| **P3 规模化就绪（已完成）** | arq 任务队列（独立 worker + max_tries 重投 + 死信队列）+ Postgres checkpointer（interrupt 状态持久化，多 worker 水平扩展） | H2, H3 | `core/arq_worker.py` + `core/arq_job_queue.py` + `agent/graph.py:_create_checkpointer()` + `requirements.txt`（arq / langgraph-checkpoint-postgres）|
| **P4 测试补全（已完成）** | auth 三件套（password/jwt_handler/rbac）+ Provider 凭据服务 + Playground 流式 + Provider 路由 + 3 个新 Provider（Anthropic/Gemini/Ollama）独立单测 | 测试缺口 | 9 个新测试文件、183 个 test cases 全部 passing |
| **P5 文档同步（已完成）** | CloudOCR/WhisperASR 状态更正 | M1, M2 | `docs/architecture-notes.md` 订正 |
| **P6 CI 加固（已完成）** | mypy/black 改硬阻断；ai_review 触发；nightly e2e | L1, L4, L5 | `.github/workflows/ci.yml` 更新：移除 continue-on-error、新增 ai-review job、新增 nightly-e2e job（schedule trigger） |
| **P7 架构债/生态（已完成）** | 飞书 IM 适配层 + GitLab 集成适配层 + Dummy 默认实现 + 工厂降级 | M3, Roadmap | `backend/integrations/` 8 文件：base/dummy/feishu/gitlab/factory/settings/__init__，16 个新 test cases |

P3-P7 期间顺手修复的 5 个预存在 bug（详见 §三 第 9 项）：(a) `RedisJobQueue.update()` AsyncScript 误用；(b) `record_token_usage` 三个 Provider 漏传 model 参数；(c) `_make_authed_metrics_asgi` mode 构造期捕获；(d) InMemoryJobQueue.get deepcopy 后测试 `is` vs `==`；(e) `_FakeTracer.generation` mock 缺 kwargs 兼容。

每个阶段完成后按 AI 项目 AGENTS.md §7 Git 规范提交，并同步推送到 github + gitcode 两个远程。

---

## 五-补、v1.4.0 P1-P4 大厂对标阶段（已完成）

v1.4.0 在 P2 深水区与 P3-P7 规模化/测试/CI/生态之后，针对深度对比 Dify / Coze / Langfuse 后发现的 8 项能力缺口完成补全，管理后台从"基础外壳"升级为对标大厂的全功能运营平台。

| 阶段 | 内容 | 对标对象 | 产出 |
|---|---|---|---|
| **P1-1 知识库管理 UI（已完成）** | docs CRUD + reindex + 检索测试台 + chunk 配置持久化 | Dify Dataset | `api/admin/kb.py`（9 端点）+ `frontend/AdminKnowledgeBase.vue`（4 统计卡 + 3 对话框）+ chunk_size/chunk_overlap 持久化 `.env.runtime` |
| **P1-2 链路追踪可视化（已完成）** | 评估 trace 7 节点 span 树 + Gantt 时间线 + 分页列表 | Langfuse Trace / Dify Run | `api/admin/debug.py` `_build_trace_spans()` + `GET /evaluations` + `AdminTrace.vue`（el-tree + ECharts Gantt）|
| **P2-1 Token 趋势仪表盘（已完成）** | Token 用量 / 成本 / Provider 分布 / 评估统计 4 图 + 时间范围切换 | Langfuse Usage | `api/admin/analytics.py`（4 端点 + `_query_range` 优雅降级 + `MODEL_PRICING` 11 模型前缀匹配）+ `AdminMetrics.vue` 重写为 4 个 ECharts |
| **P2-2 Rerank Provider 抽象（已完成）** | 4 Provider 实现 + Factory + Dummy fallback + Feature Flag 灰度 | Dify Rerank Model | `core/providers/rerank_provider.py`（ABC + `_HTTPRerankProvider` 基类 + Cohere/Jina/BGE/Dummy）+ `rerank_factory.py` + `agent/graph.py:_rerank_kb_if_enabled` 集成 |
| **P3-1 自定义工具（已完成）** | OpenAPI JSON/YAML → LangChain Tool + 凭证加密 + 测试调用 | Dify Custom Tool | `core/tools/openapi_parser.py`（`parse_openapi_to_tools` + `build_langchain_tool`）+ `models/custom_tool.py` + `api/admin/custom_tools.py`（8 端点）+ `AdminTools.vue` 新增 "Custom Tools" tab |
| **P3-2 Feature Flag（已完成）** | 5 级规则评估 + 60s LRU 缓存 + 负缓存 + explain | LaunchDarkly / Unleash | `core/feature_flag.py`（`FeatureFlagService` + sha256 稳定哈希 + 自动失效）+ `models/feature_flag.py` + `api/admin/feature_flags.py` + `AdminFeatureFlags.vue` |
| **P4-1 Multi-Agent 协作（已完成）** | supervisor + 4 专家 + 显式 handoff + 失败隔离 | LangGraph Supervisor 多智能体 | `agent/multi_agent.py`（`create_multi_agent_graph` + `Command(goto=...)` + max_iterations 硬上限 50）+ `api/admin/multi_agent.py` + `AdminMultiAgent.vue` |
| **P4-2 工作流可视化编排（已完成）** | DAG 执行器 + 7 种节点 + 代码沙箱 + Vue Flow 画布 | Dify Workflow / Coze Bot | `core/workflow_engine.py`（Kahn 拓扑 + DFS 环检测 + AST 白名单）+ `api/admin/workflows.py`（11 端点）+ `AdminWorkflows.vue`（Vue Flow + 节点面板 + 属性面板 + 运行对话框）|

**前端基础设施同步**

- `frontend/src/api/client.js` 新增 7 个 API 客户端对象（kbAdminApi / traceAdminApi / analyticsAdminApi / rerankAdminApi / customToolAdminApi / featureFlagAdminApi / multiAgentAdminApi / workflowAdminApi）
- `frontend/src/router/index.js` 注册 5 个新路由（知识库 / 链路追踪 / 功能开关 / 多 Agent 协作 / 工作流编排）
- `frontend/src/layouts/MainLayout.vue` 新增 5 个菜单项
- `frontend/package.json` 新增 `@vue-flow/core` + `@vue-flow/background` + `@vue-flow/controls` + `@vue-flow/minimap`

**测试补全**：5 个新测试文件、186 个新 test cases 全部 passing（`test_rerank_provider.py` 29 / `test_custom_tools.py` 37 / `test_feature_flags.py` 53 / `test_multi_agent.py` 31 / `test_workflow_engine.py` 36）。后端单测累计 **1478 passed**（旧 1292 + 新 186）。

**Review 阶段发现的 3 个 P0/P1 bug（详见 §三 第 11 项）**：(a) `api/admin/rerank.py` `HTTP_503_SERVICE_UNAVALABLE` 拼写错误（漏 "I"），会抛 `AttributeError` 而非返回 503；(b) `requirements.txt` 缺 `langchain-openai`，langchain 0.3+ 不再内置 `langchain_openai`；(c) `agent/graph.py` Rerank Provider 双实例并存 + `FeatureFlagService` 60s LRU 缓存完全失效（每次 `retrieve_context` new 一个 service），通过 `set_app_state_for_graph(app_state)` 由 main.py lifespan 注入修复。

**代码去重**：新增 `api/admin/_common.py` 提供 `gen_id(prefix, hex_len)` 与 `entity_to_dict(entity, fields, *, iso_fields, extra)` 公共函数，`custom_tools.py` / `workflows.py` 的重复 `_gen_id` / `_entity_to_dict` 委托到公共实现。

---

## 五-补2、v1.5.0 AI 对话系统 + Agent 工具层（已完成）

v1.5.0 在 v1.4.0 大厂对标运营平台之上，新增完整 AI 对话系统与 Agent 工具层，使系统从"只能做员工评估"
升级为"能对话、能操作电脑的智能体"。

### 对标分析

深度对比 ChatGPT / Claude.ai / Dify / Coze / Open WebUI / LobeChat / opencode 等 7 大平台，
覆盖 7 大类 100+ 功能点，识别出 10 项 P0 级功能缺口 + 5 个 Agent 工具缺口。

### 产出清单

| 模块 | 内容 | 对标对象 | 产出 |
|---|---|---|---|
| **P0-1 消息复制** | 代码块复制 + 整条消息复制 | ChatGPT / Claude.ai | `MessageBubble.vue` DOM post-render injection |
| **P0-2 重新生成** | 删除末条 assistant 消息重新执行 | ChatGPT | `api/chat.py:regenerate` + `stores/chat.js:regenerate()` |
| **P0-3 编辑消息** | inline 编辑 + 删除后续重新发送 | ChatGPT | `api/chat.py:delete_message` + `MessageBubble.vue:isEditing` |
| **P0-4 Token 用量** | total_tokens 分解 + 延迟显示 | Claude.ai / Dify | `MessageBubble.vue` + `stores/chat.js:streamStartTime` |
| **P0-5 会话重命名** | 双击重命名 + LLM 自动标题 | ChatGPT | `api/chat.py:auto-title` + `ChatView.vue` |
| **P0-6 错误重试** | 错误消息旁重试按钮 | ChatGPT | `MessageBubble.vue:retry` + `stores/chat.js:retry()` |
| **P0-7 思考过程** | reasoning_content 可折叠展示 | Claude.ai / DeepSeek | `stores/chat.js:reasoning-*` 事件 + `MessageBubble.vue` |
| **P0-8 点赞点踩** | like/dislike 反馈持久化 | ChatGPT | `api/chat.py:feedback` + `ChatMessage.metadata_` |
| **P0-9 数学公式** | KaTeX 行内/块级 + Mermaid 图表 | ChatGPT / Claude.ai | `utils/markdown.js` + `package.json` 依赖 |
| **P0-10 搜索导出** | 会话搜索 + Markdown 导出 | ChatGPT | `api/chat.py:search` + `ChatView.vue` |
| **Agent bash** | 执行 shell 命令 | opencode bash | `langchain_tools.py` + 30s 超时 + 输出截断 |
| **Agent read_file** | 读取文件内容 | opencode read | `langchain_tools.py` + 5000 字符截断 |
| **Agent write_file** | 写入文件 | opencode write | `langchain_tools.py` + 自动创建父目录 |
| **Agent list_directory** | 列出目录 | opencode glob | `langchain_tools.py` |
| **Agent web_fetch** | 抓取网页 | opencode webfetch | `langchain_tools.py` + HTML→纯文本 |

### 前端组件增强

- `ToolCallCard.vue` 完整重写：可折叠输入/输出、JSON 美化、复制按钮、状态图标（执行中旋转/完成绿色/失败红色）、深色代码块主题
- `ChatInput.vue`：新增文件上传按钮（回形针图标），支持多文件、附件预览、10MB 限制
- `ChatView.vue`：对话头部模型切换下拉（8 种模型）、搜索框、导出按钮

### Bug 修复

- `agent/session_processor.py:145`：`await self.chat_svc.update_part`（await 了方法对象而非调用）的 TypeError，替换为 `await self.chat_svc.update_message_tokens(msg.id, tokens)`
- `services/chat_service.py`：新增 `update_message_tokens()` 方法
- 前端 ESLint：修复 3 处 `no-useless-assignment`（AdminWorkflows.vue / sse.js）+ 移除 ChatView.vue 未使用变量

### 测试状态

- 后端单测：1517 passed，9 failed（均为预存在的 LangGraph interrupt context / 环境依赖问题，非本次引入）
- 前端构建：✓ 通过
- 前端 ESLint：0 errors（27 pre-existing warnings 均在未修改文件中）

---

## 六、关键约束（来自 AI 项目 AGENTS.md）

- 先规划后实现，需求歧义即问，不脑补代码
- 最小变更，不顺手改未提及文件
- 失败熔断：同 Bug 连续失败 2 次或终端连续失败 3 次即停并求助
- 安全红线：禁硬编码密钥、禁自行配置 MCP、禁执行未知脚本
- 提交前 `git status` + `git diff`，绝不自动 push、绝不 `push -f`、绝不盲目 `add .`
