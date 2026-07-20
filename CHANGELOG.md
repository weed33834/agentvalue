# Changelog

本文件记录 AgentValue-AI 所有显著变更,格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/),
版本号遵循 [语义化版本](https://semver.org/lang=zh-CN/)。

## [v2.0.0] - 2026-07-20

### 竞品对标全量实现

基于对 ChatGPT、Claude、Dify、Coze、LobeChat、Open WebUI、Cursor、Cline、Aider、Lattice、15Five、Culture Amp、飞书、钉钉等 20+ 产品的深度竞品分析,完成 P1-P3 全部功能补全。本次新增 177 个 API 端点、30+ 文件、6 张数据库表。

#### P1 对话体验 (7 项)

- **语音输入 (STT)** — 浏览器 Web Speech API 实时语音转文字,降级文件上传走 OpenAI Whisper
- **语音输出 (TTS)** — 浏览器 SpeechSynthesis 朗读 AI 回复,降级走 OpenAI TTS API
- **提示词模板库** — 5 个内置模板(代码审查/周报/绩效面谈/数据分析/翻译润色),支持变量实例化
- **Agent 预设市场** — 5 个内置预设(代码助手/HR顾问/数据分析师/文案写手/技术文档专家),对标 ChatGPT GPTs
- **MCP 协议** — 会话级 MCP 工具加载,连接 400+ 外部工具服务器,前端管理 UI
- **Code Interpreter** — Python 沙箱执行(模块白名单+危险内建屏蔽+超时保护),对标 ChatGPT Advanced Data Analysis
- **对话中断/恢复** — 流式生成可随时停止,支持恢复继续

#### P2 创新功能 (3 项)

- **Artifacts 可视化** — 自动提取 AI 回复中的 HTML/SVG/Mermaid/Markdown/代码块,渲染为可交互卡片(预览/编辑/复制/全屏),对标 Claude Artifacts
- **对话式 HR 洞察** — 自然语言查询员工数据(NL→SQL→图表建议),团队洞察看板(Top5/改进/分布/对比/趋势),导出 CSV/JSON
- **Repo Map** — 代码库结构映射(目录树+符号提取),供 Agent 理解项目结构,对标 Cursor/Aider

#### P3 高级功能 (2 项)

- **Skills 系统** — 可复用技能模块(提示词+工具+schema 封装),4 个内置技能,执行引擎,对标 Claude Skills
- **AI 偏差检测** — 5 维度偏差检测(语言偏见/近因偏见/对比效应/晕轮效应/综合报告),增强公平性审计

#### 基础设施

- 6 张新数据库表: prompt_templates, agent_presets, chat_artifacts, skills, review_cycles(已有), calibration_items(已有)
- 4 个 Alembic 迁移,幂等设计,链路完整
- 177 个 API 端点,前端构建通过,后端导入验证通过

## [v1.5.0] - 2026-07-20

### Added

#### AI 对话界面全面对标 ChatGPT/Claude.ai (P0 系列 10 项)

对标 ChatGPT、Claude.ai、Dify、Coze、Open WebUI、LobeChat 等 7 大平台,
覆盖 7 大类 100+ 功能点,完成 10 项 P0 级功能补全:

**P0-1 消息复制**
- `MessageBubble.vue`: 代码块右上角注入复制按钮(DOM post-render injection);
  整条消息 hover 显示复制按钮,调用 `navigator.clipboard.writeText()`

**P0-2 重新生成回复 (Regenerate)**
- `api/chat.py`: `POST /sessions/{id}/regenerate` — 删除最后 assistant 消息,
  取倒数第二条 user 消息,重新执行 `SessionPrompt.run_loop` SSE 流式
- `stores/chat.js`: `regenerate()` — 乐观删除 + 占位 assistant message + SSE 流

**P0-3 编辑用户消息**
- `api/chat.py`: `DELETE /sessions/{id}/messages/{msg_id}` — 级联删除消息+parts
- `stores/chat.js`: `editMessage(message, newText)` — 删除后续消息后重新发送
- `MessageBubble.vue`: `isEditing` ref + `startEdit()`/`saveEdit()`/`cancelEdit()`

**P0-4 Token 用量 + 延迟显示**
- `MessageBubble.vue`: 显示 `usage.total_tokens` + `(prompt → completion)` 分解;
  延迟显示 `ms`/`s` 格式
- `stores/chat.js`: `streamStartTime` 跟踪,`finish`/`onClose` 记录 `latency`

**P0-5 会话重命名 + 自动标题**
- `api/chat.py`: `POST /sessions/{id}/auto-title` — 取前 2 条消息调用 LLM 生成 10 字标题
- `ChatView.vue`: 双击标题或点击图标重命名;`el-input` + Enter/Esc/blur 保存
- `stores/chat.js`: `_maybeAutoTitle()` — 首次对话后自动触发

**P0-6 错误重试按钮**
- `MessageBubble.vue`: 错误消息旁显示重试按钮,emit `retry` 事件
- `stores/chat.js`: `retry()` — 删除错误消息后重新生成或重新发送

**P0-7 思考过程展示 (Reasoning)**
- `stores/chat.js`: `handleEvent` 新增 `reasoning-start/delta/end` 事件处理
- `MessageBubble.vue`: 可折叠 reasoning 块,显示 AI 思考过程

**P0-8 点赞/点踩反馈**
- `api/chat.py`: `POST /sessions/{id}/messages/{msg_id}/feedback` — 存入 `metadata_["feedback"]`
- `models/chat_models.py`: `ChatMessage.metadata_` JSON 字段 + Alembic 迁移
- `MessageBubble.vue`: like/dislike 按钮,active 状态高亮

**P0-9 数学公式 + 图表渲染**
- `utils/markdown.js`: KaTeX 数学公式(`$...$` 行内, `$$...$$` 块级) + Mermaid 图表懒加载
- `package.json`: 新增 `katex`、`mermaid` 依赖
- `MessageBubble.vue`: `injectCopyButtons()` 后调用 `renderMermaid()`

**P0-10 会话搜索 + 导出**
- `api/chat.py`: `GET /sessions/search?q=keyword` — 模糊搜索会话标题
- `stores/chat.js`: `displaySessions` computed — 客户端即时过滤
- `ChatView.vue`: 搜索框 + 导出按钮(Markdown 格式下载)

#### Agent 工具层补全 (对标 opencode 5 项工具)

对标 opencode 的 13 个内置工具,新增 5 个 Agent 工具,使系统从"只能聊天"
升级为"能操作电脑的智能体":

- `agent/langchain_tools.py`: 新增 `bash`(执行 shell)、`read_file`(读取文件)、
  `write_file`(写入文件)、`list_directory`(列出目录)、`web_fetch`(抓取网页)
- `agent/session_prompt.py`: 增强系统提示词,列出 9 个可用工具和使用准则
- 共 9 个工具可用: calculator / get_current_datetime / bash / read_file /
  write_file / list_directory / web_fetch / get_employee_history / query_company_kb

#### 前端对话界面增强

- `ToolCallCard.vue` 完整重写: 可折叠输入/输出、JSON 美化、复制按钮、
  状态图标(执行中旋转/完成绿色/失败红色)、深色代码块主题
- `ChatInput.vue`: 新增文件上传按钮(回形针图标),支持多文件、附件预览、10MB 限制
- `ChatView.vue`: 对话头部模型切换下拉(8 种模型)、搜索框、导出按钮

### Fixed

- `agent/session_processor.py`: 修复 `_handle_step_finish` 中
  `await self.chat_svc.update_part` 的 TypeError(await 了方法对象而非调用),
  替换为正确的 `await self.chat_svc.update_message_tokens(msg.id, tokens)`
- `services/chat_service.py`: 新增 `update_message_tokens()` 方法
- `frontend/src/utils/sse.js`: 修复 `let data = {}` 的 no-useless-assignment lint error
- `frontend/src/views/admin/AdminWorkflows.vue`: 修复 2 处 no-useless-assignment +
  移除未使用的 `nextTick` / `useVueFlow` / `Connection` / `Monitor` 导入
- `frontend/src/views/admin/ChatView.vue`: 移除未使用的 `useRouter`、`computed`、`currentModelLabel`
- `frontend/src/components/chat/ToolCallCard.vue`: 移除未使用的 `toolIcon` computed
- `frontend/src/components/chat/MessageList.vue`: 修复 `emit` assigned but never used
  (改用 `defineEmits()` 无赋值调用)

### Changed

- 默认模型从 `gpt-4o-mini` 更改为 `DeepSeek-V4-Flash`(适配 OpenAI 兼容网关)
- 前端模型选项列表更新: 新增 DeepSeek V4 Flash/Pro、GLM 4.7/5.1、Qwen 3 Coder、
  Kimi K2.6、MiniMax M3、auto 路由
- `api/chat.py`: `CreateSessionRequest.model_name` 默认值改为 `DeepSeek-V4-Flash`
- `package.json`: 新增 `katex@^0.16.11`、`mermaid@^11.4.0` 依赖

### Docs

- `README.md`: 新增 AI 对话系统(v1.5.0)章节、更新架构图/技术栈/目录结构/Roadmap/FAQ
- `backend/README.md`: 新增 AI 对话系统 API 与 Agent 工具章节、更新目录结构
- `frontend/README.md`: 新增 AI 对话组件章节与 `/admin/chat` 路由
- `docs/DEVELOPMENT-PLAN.md`: 新增 §五-补2 v1.5.0 段落,版本号更新至 v1.5.0

## [v1.4.0] - 2026-07-13

### Added

#### P1-P4 大厂对标 8 项功能补全(对标 Dify/Coze/Langfuse)

针对深度对比 Dify / Coze / Langfuse 后发现的 8 项能力缺口完成补全,
覆盖知识库管理、链路追踪可视化、Token 趋势、Rerank Provider、自定义工具、
Feature Flag、Multi-Agent 协作、Workflow 可视化编排,管理后台从"基础外壳"
升级到对标大厂的全功能运营平台。

**P1-1 知识库管理 UI(对标 Dify Dataset)**

- `api/admin/kb.py`(9 端点):docs CRUD + reindex + test-retrieval +
  config GET/PUT,`chunk_size`/`chunk_overlap` 持久化到 `.env.runtime`
- `frontend/src/views/admin/AdminKnowledgeBase.vue`:4 个统计卡片 +
  文档表格 + 3 个对话框(创建/编辑、chunk 配置、检索测试台)

**P1-2 链路追踪可视化(对标 Langfuse Trace / Dify Run)**

- `api/admin/debug.py`:`_build_trace_spans(audit, manager_view)`
  生成 7 个节点级 span,扩展 `GET /evaluation/{id}/trace` 增加
  `spans` 数组 + `timeline` 字段(向后兼容);新增
  `GET /evaluations` 分页列表
- `frontend/src/views/admin/AdminTrace.vue`:左侧列表 +
  右侧 el-tree span 树 + ECharts Gantt 时间线

**P2-1 Token 用量趋势仪表盘(对标 Langfuse Usage)**

- `api/admin/analytics.py`(4 端点):token-usage / cost /
  provider-distribution / evaluation-stats;`_query_range(promql,
  start, end, step)` 在 Prometheus 不可用时优雅降级;`MODEL_PRICING`
  字典(11 个模型,前缀匹配算成本,如 `gpt-4o-2024-08-06` 命中
  `gpt-4o`)
- `frontend/src/views/admin/AdminMetrics.vue`:重写为 4 个 ECharts
  (line/pie/bar/pie)+ 时间范围切换器

**P2-2 Rerank Provider 抽象(对标 Dify Rerank Model)**

- `core/providers/rerank_provider.py`:`RerankProvider` ABC +
  `_HTTPRerankProvider` 基类(封装 Cohere/Jina 共同 HTTP 逻辑)+
  `CohereRerankProvider` / `JinaRerankProvider` / `BGERerankProvider`(本地
  FlagEmbedding)/ `DummyRerankProvider`(向后兼容)
- `core/providers/rerank_factory.py`:`create_rerank_provider(settings)`
  按凭证 / 依赖 / 未知名称三级 fallback 到 Dummy
- `agent/graph.py`:`_rerank_kb_if_enabled` 在 `retrieve_context`
  调 rerank,失败时 fallback 原 ChromaDB 顺序不阻断主流程;
  检查 `use_rerank_v2` Feature Flag 启用强制走 rerank 路径(灰度新模型)

**P3-1 自定义工具(对标 Dify Custom Tool:OpenAPI → LangChain Tool)**

- `core/tools/openapi_parser.py`:`parse_openapi_to_tools(spec,
  base_url, auth)` → `List[ToolSpec]`;`build_langchain_tool(spec)`
  生成 `BaseTool`(自动生成 Pydantic 入参 schema);支持 OpenAPI 3.x、
  JSON/YAML、`$ref` 解析
- `models/custom_tool.py`:`CustomTool` ORM(`auth_credentials` 用
  `FieldCipher` AES-256-GCM 加密)
- `api/admin/custom_tools.py`(8 端点):CRUD + toggle + test(实际
  HTTP 调用)+ parse(不入库预览)
- `frontend/src/views/admin/AdminTools.vue`:新增 "Custom Tools" tab

**P3-2 Feature Flag(对标 LaunchDarkly / Unleash 灰度发布)**

- `core/feature_flag.py`:`FeatureFlagService.is_enabled(key,
  tenant_id, user_id)` 5 级规则评估 — exist → enabled →
  target_user → target_tenant → percentage(`sha256` 稳定哈希保证
  同 user 跨实例结果一致);60s LRU 缓存 + 负缓存(缓存 None
  防止 DB 热点击穿);`explain(key, ...)` 返回 reason + bucket;
  update/delete/toggle 时自动失效缓存
- `models/feature_flag.py`:`FeatureFlag` ORM(key PK /
  rollout_percentage 0-100 / target_tenant_ids JSON /
  target_user_ids JSON / category)
- `frontend/src/views/admin/AdminFeatureFlags.vue`:列表 +
  创建/编辑对话框 + 测试对话框(展示 reason / bucket)

**P4-1 Multi-Agent 协作(对标 LangGraph Supervisor 多智能体)**

- `agent/multi_agent.py`:`create_multi_agent_graph(model_router,
  toolkit, prompt_loader)` 返回 LangGraph 编译图;supervisor +
  4 个专家(data_analyst / code_reviewer / risk_assessor /
  report_writer);`StateGraph + Command(goto=...)` 显式 handoff
  (避开 `langgraph.prebuilt.create_supervisor` 版本兼容问题);
  `max_iterations` 默认 10、硬上限 50;专家失败隔离
  (`artifacts[name] = {error: ...}`,其他专家继续)
- `frontend/src/views/admin/AdminMultiAgent.vue`:任务列表 +
  状态时间线 + artifacts 网格

**P4-2 工作流可视化编排(对标 Dify Workflow / Coze Bot 编排)**

- `core/workflow_engine.py`:`WorkflowEngine.execute(workflow,
  inputs, thread_id)` DAG 执行器;Kahn 拓扑排序 + DFS 环检测;
  7 种节点类型(start / llm / http / condition / code /
  knowledge / end);代码沙箱 `exec(source, {"__builtins__": {}},
  local_vars)` + 白名单;AST 条件求值(只允许
  Compare/BoolOp/BinOp/Constant/Name,禁止 Call/Attribute);
  模板渲染 `{{var}}` / `{{node_id.field}}` 点路径
- `api/admin/workflows.py`(11 端点):CRUD + toggle + run +
  runs 查询 + node-states + validate
- `frontend/src/views/admin/AdminWorkflows.vue`:Vue Flow 画布
  (`@vue-flow/core` + background/controls/minimap)+ 节点面板 +
  属性面板 + 运行对话框

**前端基础设施同步**

- `frontend/src/api/client.js`:新增 7 个 API 客户端对象
  (kbAdminApi / traceAdminApi / analyticsAdminApi /
  rerankAdminApi / customToolAdminApi / featureFlagAdminApi /
  multiAgentAdminApi / workflowAdminApi)
- `frontend/src/router/index.js`:注册 5 个新路由
- `frontend/src/layouts/MainLayout.vue`:新增 5 个菜单项
  (知识库 / 链路追踪 / 功能开关 / 多 Agent 协作 / 工作流编排)
- `frontend/package.json`:新增 `@vue-flow/core` +
  `@vue-flow/background` + `@vue-flow/controls` + `@vue-flow/minimap`

**测试补全:186 个新 test cases,后端单测累计 1478 passing**

- `tests/test_rerank_provider.py`:29 cases 覆盖 4 个 Provider 实现 +
  Dummy fallback + 异常路径
- `tests/test_custom_tools.py`:37 cases 覆盖 OpenAPI 解析 +
  LangChain Tool 生成 + CRUD + 加密凭证 + 测试调用
- `tests/test_feature_flags.py`:53 cases 覆盖 5 级规则 + 缓存 +
  负缓存 + 自动失效 + explain
- `tests/test_multi_agent.py`:31 cases 覆盖 supervisor 路由 +
  专家执行 + 失败隔离 + max_iterations 硬上限
- `tests/test_workflow_engine.py`:36 cases 覆盖 DAG 拓扑排序 +
  环检测 + 7 种节点 + 代码沙箱安全 + 模板渲染

### Fixed

#### Review 阶段发现的 3 个 P0/P1 bug

- `api/admin/rerank.py:91` `HTTP_503_SERVICE_UNAVALABLE` 拼写错误
  (漏 "I"),会抛 `AttributeError` 而非返回 503 响应;改为
  `HTTP_503_SERVICE_UNAVAILABLE`
- `requirements.txt` 缺 `langchain-openai`,`langchain>=0.3.0` 不再
  内置 `langchain_openai`,`react_agent.py:264`
  `from langchain_openai import ChatOpenAI` 会 ImportError;新增
  `langchain-openai>=0.2.0`
- `agent/graph.py` Rerank Provider 双实例并存 +
  `FeatureFlagService` 60s LRU 缓存完全失效(每次
  `retrieve_context` 都 new 一个 `FeatureFlagService`,缓存形同虚设,
  DB 反复查询);新增 `set_app_state_for_graph(app_state)` 由
  `main.py` lifespan 注入,`_get_rerank_provider` 优先复用
  `app_state.rerank_provider`,`_rerank_kb_if_enabled` 优先复用
  `app_state.feature_flag_service`(消除双实例 + 恢复 60s 缓存)

### Changed

#### 代码去重:统一 ID 生成与 entity 序列化

- `api/admin/_common.py`(新增):`gen_id(prefix, hex_len)` 与
  `entity_to_dict(entity, fields, *, iso_fields, extra)` 公共函数,
  消除 `custom_tools.py` / `workflows.py` / 其他 admin 路由重复的
  `_gen_id` / `_entity_to_dict` 实现
- `api/admin/custom_tools.py:_gen_id` 委托到 `_common.gen_id()`
- `api/admin/workflows.py:_gen_id` 委托到 `_common.gen_id(prefix=prefix)`

## [v1.3.0] - 2026-07-13

### Added

#### P3 规模化就绪:arq 任务队列 + Postgres checkpointer

完成 H2 / H3 规模化阻断项,部署形态从单实例走向多 worker 水平扩展。

**arq 任务队列(独立 worker + 自动重投 + 死信队列)**

- `core/arq_worker.py`:arq 0.28+ 适配,WorkerSettings 改为普通类(移除 ActorMeta
  元类,arq 0.26 之后已废弃)。`run_evaluation_task(ctx, job_id, ...)` 调用
  `routes._run_evaluation_job` 复用现有评估逻辑;`job_try >= max_tries` 时入死信
  队列 `agentvalue:dead_letter:{job_id}` 供运维捞取
- `core/arq_job_queue.py`:`ArqJobQueue` 适配器实现 JobQueue 接口,与
  `RedisJobQueue` 共享 `agentvalue:job:` key 前缀,状态查询完全兼容。
  `enqueue` 时:① 写 RedisJobQueue 状态供前端查询;② `arq_redis.enqueue_job()`
  入 arq 队列异步执行
- `core/job_queue.py`:`create_job_queue()` 三级降级 —
  ArqJobQueue(USE_ARQ_QUEUE=true)→ RedisJobQueue(裸 redis.asyncio)→
  InMemoryJobQueue(单实例/测试默认)。`RedisJobQueue.update()` 改用
  `AsyncScript` 直接调用 `script(keys=..., args=...)`(原误用 `.eval()` 抛
  AttributeError 被静默吞掉,导致 Lua 原子 update 失败)
- `core/config.py`:新增 `use_arq_queue` / `arq_max_tries` / `arq_job_timeout` /
  `use_postgres_checkpointer` 配置项,默认 false(本地/测试默认降级内存)

**Postgres checkpointer(interrupt 状态持久化,多 worker 水平扩展)**

- `agent/graph.py`:`_create_checkpointer()` 按 `settings.use_postgres_checkpointer`
  选择 `AsyncPostgresSaver`(interrupt 状态持久化到 PG,多 worker 共享)或
  `MemorySaver`(单实例,本地开发默认)。`from_dsn` 仅 URL 解析,模块导入期可安全调用;
  未启用时降级 MemorySaver,保持向后兼容
- `requirements.txt`:新增 `arq>=0.26.0` / `langgraph-checkpoint-postgres>=2.0.0`
  / `lupa>=2.0`(fakeredis Lua 脚本支持)

#### P4 测试补全:9 个新测试文件,183 个 test cases 全部 passing

补齐测试覆盖缺口,后端单测从覆盖率缺口转向质量基线。

- `tests/test_auth_password.py`:19 cases 覆盖 bcrypt 哈希/校验/盐/版本/异常路径
- `tests/test_auth_jwt_handler.py`:29 cases 覆盖 JWT 签发/过期/claims/黑名单/算法
- `tests/test_auth_rbac.py`:32 cases 覆盖 4 角色矩阵 + 装饰器 + tenant 边界
- `tests/test_provider_credential_service.py`:32 cases 覆盖凭证 CRUD + 加密/脱敏 + 负载均衡
- `tests/test_stream_buffer.py`:13 cases 覆盖流式 tool_call delta 拼接
- `tests/test_providers_router.py`:19 cases 覆盖 Provider 路由端点
- `tests/test_anthropic_provider.py`:14 cases 覆盖 Anthropic Provider
- `tests/test_gemini_provider.py`:14 cases 覆盖 Gemini Provider
- `tests/test_ollama_provider.py`:11 cases 覆盖 Ollama Provider

#### P5 文档同步:CloudOCR/WhisperASR 状态更正

- `docs/architecture-notes.md` L133 / L143:更正 CloudOCR(OpenAI vision 兼容路径)
  与 WhisperASR(OpenAI audio transcription 兼容路径)的 API 描述,补齐配置项与
  支持模型清单,与代码实际实现对齐

#### P6 CI 加固:mypy/black 硬阻断 + AI 代码审查 + nightly e2e

- `.github/workflows/ci.yml`:mypy/black 移除 `continue-on-error: true`,改为硬阻断
- 新增 `ai-review` job(PR-only,使用 `CLOUD_API_KEY`/`CLOUD_BASE_URL`/`CLOUD_MODEL`
  环境变量调 LLM 审查 PR diff,结果作为评论附在 PR 上)
- 新增 `nightly-e2e` job(`schedule: cron: '0 18 * * *'` 触发,失败时自动
  创建 issue 通知)
- 顶层 `on:` 新增 `schedule` 触发器

#### P7 架构债/生态:飞书 IM + GitLab 集成适配层骨架

对标 ADR-001 / ADR-002,落地集成适配层抽象与默认实现。

- `integrations/base.py`:`IMAdapter` / `CodeRepoAdapter` 抽象基类 + 数据类
  (`IMMessage` / `WebhookEvent` / `RepoInfo` / `CommitInfo` / `PRInfo`)
- `integrations/dummy.py`:`DummyIMAdapter` / `DummyCodeRepoAdapter` 默认实现,
  无配置时返回 Dummy,业务代码始终拿到可用实例
- `integrations/feishu.py`:`FeishuIMAdapter` 骨架(NotImplementedError)
- `integrations/gitlab.py`:`GitLabCodeRepoAdapter` 骨架(NotImplementedError)
- `integrations/factory.py`:`create_im_adapter()` / `create_coderepo_adapter()`
  工厂,真实适配器未实现时自动降级 Dummy(捕获 NotImplementedError)
- `integrations/settings.py`:独立 `IntegrationsSettings`(不污染 core/config.py,
  作为后续按领域拆分 Settings 的模板)
- `tests/test_integrations.py`:16 cases 覆盖 Dummy 默认行为 + 工厂降级 + 骨架异常

### Fixed

#### 深度集成测试发现的 P3 阻断 bug(本次修复)

- `core/arq_worker.py:run_evaluation_task` 调用 `get_app_state()` 不传 `request`
  参数,而 `api.deps.get_app_state(request: Request)` 是 FastAPI Depends 函数,
  arq worker 是独立进程无 request 上下文,启用 `USE_ARQ_QUEUE=true` 后第一个
  评估任务即抛 `TypeError: get_app_state() missing 1 required positional
  argument: 'request'`,规模化功能完全不可用。修复:`on_startup` 时创建
  `AppState` 单例存到 `ctx["app_state"]`,`run_evaluation_task` 从 `ctx` 取
  (兼容旧版 worker:未设置时现场创建并缓存到 ctx)
- `core/arq_worker.py:on_shutdown` 未释放 `AppState` 资源(向量库/embedding 客户端
  连接),修复:从 `ctx` 取 `app_state` 调 `close()` 释放

#### 新增深度集成测试(38 cases,模拟生产服务场景)

- `tests/test_integration_scenarios.py`:11 个 Scenario、38 个 test cases,
  端到端串联 ≥3 个子系统(不是单 unit 测),覆盖:
  - **Scenario A**:完整评估闭环 + 高风险 HR 路由 + 员工申诉回退 + 审计真实
    actor_id(P1-8 修复验证)
  - **Scenario B**:Playground SSE 流式 + 4 Provider 路由(gpt/claude/gemini/
    llama 前缀)+ tool_call delta 跨多 chunk 拼接 + SSE 事件序列(trace/token/
    tool_call_start/delta/end/done)
  - **Scenario C**:JobQueue 三级降级(InMemory/Redis+Lua/Arq)+ arq 死信队列
    (job_try >= max_tries 写 DLQ,job_try < max_tries 不写)
  - **Scenario D**:FieldCipher AES-256-GCM 往返 + Dify 风格 mask_secret
    (`sk****0xyz` 前缀 schema-aware)+ PII 多类型脱敏(手机/邮箱/身份证/银行卡
    + 嵌套 dict/list 递归)
  - **Scenario E**:集成适配层降级契约(配置凭证 → 真实适配器构造期 raise
    NotImplementedError → 工厂降级 Dummy + Dummy 方法返回约定值)
  - **Scenario F**:metrics 鉴权三模式(none/ip/token)+ token usage 4 维 label
    (tier/model/direction/tenant_id)埋点
  - **Scenario G**:输入护栏 + prompt injection 拦截 + 讨论性内容误报标记
    (would_be_false_positive)
  - **Scenario H**:多租户 contextvar 隔离 + tenant_scope 上下文管理器恢复
  - **Scenario I**:create_job_queue 工厂三级降级(无 redis_url / 不可达 /
    use_arq_queue+fakeredis)
  - **Scenario J**:PostgresSaver checkpointer 降级 MemorySaver(未启用 / PG
    驱动未装)
  - **Scenario K**:ToolCallAggregator 并行 tool_call 按 index 拼接 + JSON
    解析容错 + 实时查询累加 args

#### P3-P7 期间顺手修复的 5 个预存在 bug

- `core/job_queue.py:RedisJobQueue.update()` 误用 `AsyncScript.eval()`(实际无此
  方法,AsyncScript 只暴露 `__call__`),AttributeError 被 try/except 静默吞掉,
  导致 Lua 原子 update 完全失败,生产并发 update 会丢字段。改用 `script(keys=...,
  args=...)` 直接触发 evalsha(失败时 redis-py 自动 fallback eval)
- `core/providers/{anthropic,gemini,ollama}_provider.py` 三个 Provider 调用
  `record_token_usage(self._tier, prompt_tokens, completion_tokens)` 只传 3 个
  参数,而 `record_token_usage` 签名为 `(tier, model, prompt_tokens,
  completion_tokens, tenant_id=None)`,漏传 `model` 导致 token 用量埋点完全丢失。
  补齐 `model` 参数(优先用 API 返回的 model,回退到 `self.config.model_name`)
- `core/metrics.py:_make_authed_metrics_asgi` 在构造期一次性捕获 `mode`,测试
  monkeypatch `settings.metrics_auth_mode = "none"` 后 /metrics 仍走旧鉴权模式,
  导致 `test_evaluation_failure_metric_on_graph_error` 403 失败。改为请求时按
  当前 settings 重读 mode(生产环境 settings 一次性加载,本变更对生产无影响)
- `tests/test_job_queue.py:test_inmemory_enqueue_and_get` 测试断言 `assert got is
  job`(引用相等),但 P0 修复后 `InMemoryJobQueue.get()` 返回 `deepcopy`(与
  RedisJobQueue 语义一致),引用必然不等。改为 `assert got == job`(值相等)
- `tests/test_graph.py:_FakeTracer.generation` mock 方法签名不接受 P1 调试增强
  新增的 `prompt_name` / `prompt_version` / `prompt_version_id` / `prompt_labels`
  kwargs,TypeError 被生产代码 try/except 静默吞掉,导致 `tracer.generation()` 不
  记录,`test_call_llm_records_generation_to_tracer` 断言 0 == 1 失败。补 `**kwargs`
  兼容

#### 其他修复

- `tests/conftest.py`:`test_settings` fixture 新增 `metrics_auth_mode = "none"`
  覆盖,TestClient 的 client IP 是 "testclient" 字符串(非真实 IP),
  `_ip_allowed` 解析失败返回 False 导致 /metrics 403,测试无法读取指标断言

## [v1.2.0] - 2026-07-12

### Added

#### P2 深水区:模型供应商 CRUD + Prompt Playground(对标 Dify/Coze/Langfuse)

完整对标 Dify `model-providers` 与 Langfuse `Playground` 模块,实现多 Provider
接入、凭证加密管理、流式补全与在线 Prompt 调试。

**后端 — Provider 抽象与多 Provider 实现**

- `BaseProvider.stream_chat_completion(messages, tools, temperature, max_tokens)`
  新增流式补全抽象接口,`StreamChunk` / `ToolCallDelta` 数据类对标 OpenAI
  `stream=True` 的 delta 结构(`content` / `tool_calls[].index` /
  `tool_calls[].arguments` JSON 字符串增量)
- `OpenAICompatibleProvider.stream_chat_completion`:实现 OpenAI `stream=True`,
  处理稀疏 `delta.tool_calls`、`finish_reason`、最后 chunk 的 `usage`
- `AnthropicProvider` 全新实现:对标 Dify Anthropic provider,支持
  `chat_completion` / `stream_chat_completion`(Anthropic SSE 事件解析
  `message_start` / `content_block_start` / `content_block_delta` /
  `content_block_stop` / `message_delta` / `message_stop`)/
  `vision_completion` / `function_calling`(`input_schema` 格式转换)/
  `health_check`,系统提示拆出独立 `system` 字段
- `GeminiProvider` 全新实现:对标 Google Gemini API,
  `generateContent` / `streamGenerateContent`(SSE `alt=sse`)/
  `vision_completion`(`inlineData` base64)/
  `function_calling`(`functionDeclarations` 格式转换)/
  `health_check`(`GET /v1beta/models`),角色映射 `assistant → model`
- `OllamaProvider` 全新实现:对标 Dify Ollama provider,
  `chat_completion` / `stream_chat_completion`(NDJSON 流式解析)/
  `vision_completion`(`images` 字段,支持 llava 系列)/
  `function_calling`(Ollama 0.3.0+ 原生 `tools` 字段)/
  `health_check`(`GET /api/tags` 验证模型已 pull)
- `ToolCallAggregator`(`core/providers/stream_buffer.py`):流式
  tool_call delta 拼接器,按 `index` 累加 `arguments` JSON 字符串,
  stream 结束后 `finalize()` 统一 `json.loads`,对标 OpenAI delta assembly
  协议(并行 tool_call 用 index 区分)

**后端 — Provider 凭证管理服务**

- `ProviderCredentialService`(`core/providers/credential_service.py`):
  对标 Dify `ModelProviderService`,提供:
  - `encrypt_credential` / `decrypt_credential`:AES-256-GCM 加密(复用
    `FieldCipher`,性能优于 Dify 的 RSA PKCS1_OAEP)
  - `mask_secret` → `sk-****1234` 格式(前 2 + 后 4,中间 4 星,对标 Dify
    `secret-input` 字段脱敏)
  - `mask_credentials`:schema-aware,只脱敏 `type=secret-input` 字段
  - 多凭证负载均衡:`active_credential_id` 指针 + Redis 冷却(60s TTL,
    `_DOWN_THRESHOLD=3` 次失败触发) + round-robin
  - `record_failure` / `record_success`:被动健康检查 + 冷却记录,
    `get_active_credentials` 自动跳过冷却中凭证
  - CRUD:`create_credential`(首次创建自动激活)/ `update_credential` /
    `delete_credential` / `activate_credential`(切换活跃指针)

**后端 — 8 张新表 migration**

- `alembic/versions/e6f7a8b9c0d1_add_provider_crud.py`:幂等创建 8 张表
  (用 `_has_table()` 检查,兼容已通过 `create_all` 建表的环境):
  - `provider_templates`:内置 Provider 模板(openai/anthropic/gemini/ollama)
  - `tenant_providers`:租户级 Provider 启用/配置
  - `tenant_provider_credentials`:加密凭证存储(多凭证支持 LB)
  - `tenant_provider_models`:租户绑定模型
  - `tenant_provider_model_credentials`:模型级凭证(覆盖 Provider 级)
  - `tenant_default_models`:各 model_type 的默认模型指针
  - `model_templates`:内置模型模板(gpt-4o / claude-3-5-sonnet 等)
  - `provider_health_checks`:健康检查历史
- `models/provider_models.py`:8 个 SQLAlchemy 2.0 `Mapped` 类型 Model

**后端 — Provider 模板 seed**

- `core/providers/seed.py`:
  - 4 个 `PROVIDER_TEMPLATE`(OpenAI/Anthropic/Gemini/Ollama),每个含
    `provider_credential_schema`(`credential_form_schemas[]`,字段类型
    `secret-input` / `text-input`)
  - 10 个 `MODEL_TEMPLATES`(gpt-4o / gpt-4o-mini / text-embedding-3-small/
    large / claude-3-5-sonnet/haiku/opus / gemini-1.5-pro/flash /
    text-embedding-004),含 `features` / `model_properties` /
    `parameter_rules` / `pricing`
  - `seed_provider_templates(session)` 幂等 seeding 函数

**后端 — 24 端点 Provider CRUD API**

- `api/admin/providers.py`:对标 Dify `model-providers` 控制器,24 个端点:
  - Provider 模板:列出 / 详情
  - 租户视图:列出 / 详情(template + tenant config + credentials + models 合并视图)
  - 启用/禁用:`POST .../preferred-type {enabled:bool}`
  - 凭证 CRUD:列出 / 创建 / 更新 / 删除 / 激活 / 验证连接(不入库)
  - 模型管理:列出 / 添加 / 删除 / 启用切换 / 负载均衡切换
  - 模型凭证 CRUD:列出 / 添加 / 删除 / 激活 / 验证
  - 参数规则:`GET .../parameter-rules`
  - 默认模型:列出 / 设置
  - 健康检查:历史查询 / 主动触发
- `_validate_provider_credentials()`:provider-specific 探活
  (OpenAI `GET /models` / Anthropic `POST /v1/messages max_tokens=1` /
  Gemini `GET /v1beta/models` / Ollama `GET /api/tags`)

**后端 — Prompt Playground SSE API**

- `api/admin/playground.py` `POST /run`:对标 Langfuse Playground,
  `EventSourceResponse` 流式响应
  - SSE 配置:`ping=15` 心跳 / `send_timeout=5.0` / `X-Accel-Buffering: no`
    (禁用 nginx 缓冲,确保 token 实时下发)
  - 背压策略:`asyncio.Queue(maxsize=16)` 有界队列,
    `wait_for(queue.put, 30)` 超时主动断开
  - disconnect 检测:`request.is_disconnected()` 轮询,客户端断连立即停 LLM 调用
  - `CancelledError` reraise:finally 块正确传播取消信号,避免任务悬挂
  - SSE 事件类型:`trace` / `token` / `tool_call_start` / `tool_call_delta` /
    `tool_call_end` / `done` / `error` / `ping`
  - 执行流程:解析 Prompt 版本 → 渲染 → 按 `model_name` 路由到对应 Provider 类
    (gpt→OpenAI / claude→Anthropic / gemini→Gemini / llama→Ollama)→
    stream_chat_completion → ToolCallAggregator 拼接 tool_calls
- Provider 路由:`_get_provider_for_playground(model_name)` 优先查
  `tenant_provider_models` 找匹配凭证,失败按 model_name 前缀推断 Provider
  类,凭证从 settings 兜底(便于未配置也能跑 OpenAI)

**前端 — 管理后台两个新页面**

- `frontend/src/views/admin/AdminProviders.vue`:卡片网格(对标 Dify
  `model-providers` 主页),每个 Provider 一张卡片:
  - 启用开关(`el-switch` 调 `setPreferredType`)
  - 模型类型标签 / 凭证数 + 活跃标识 / 模型数 + 默认标识 / 健康徽章
  - 凭证管理 Dialog:动态根据 `provider_credential_schema` 生成表单
    (`secret-input` → password + show-password / `text-input` → input /
    `select` → dropdown),脱敏值 `sk****5678` 用 `.credential-code` 渲染
  - 模型管理 Dialog:启用/禁用切换 / 负载均衡切换 / 默认模型设置 /
    模型级凭证 CRUD / 参数规则查看
  - 健康检查 Dialog:手动触发 + 历史记录
- `frontend/src/views/admin/AdminPlayground.vue`:对标 Langfuse Playground
  - 左侧配置面板:Prompt 模板选择 / 版本选择(带 production/latest/canary
    标签)/ 模型选择 / 变量动态表单(从版本 `variables_schema` 解析)/
    temperature + max_tokens 调整 / 运行 + 停止 + 预览渲染按钮
  - 右侧输出面板:`fetchEventSource` POST + headers(SSE 流式 token 输出)/
    tool_calls 实时展示(增量拼接 arguments)/ trace 信息卡
    (trace_id / finish_reason / token usage)
  - 断流控制:`AbortController.abort()` 停止流,`onUnmounted` 自动清理
- `frontend/src/api/client.js`:`providerAdminApi`(24 端点)+
  `playgroundApi`
- `frontend/src/router/index.js`:`/admin/providers` + `/admin/playground`
- `frontend/src/layouts/MainLayout.vue`:侧边栏新增"模型供应商" +
  "Prompt 调试台"两个菜单项
- `frontend/package.json`:`@microsoft/fetch-event-source`(POST + 自定义
  header 的 SSE 客户端,绕开 `EventSource` 只支持 GET 的限制)

#### Provider 抽象增强

- `OpenAICompatibleProvider.chat_completion_structured(prompt, schema)`:支持
  OpenAI Structured Output (`response_format={"type":"json_schema","strict":true}`),
  替代 `json_object` 模式,提升结构化字段稳定性
- `OpenAICompatibleProvider.vision_completion(prompt, image_data, is_url)` 真正接入
  ModelRouter 的档位降级链路(经 `vision_callable` 注入 `MultimodalCleaner`)
- `OpenAICompatibleProvider.embeddings_create` / `function_calling` 方法落地,
  作为 Provider 抽象能力扩充(主链路因 workflow 设计选择保留现状,可由 graph 自行调用)

#### 安全与可观测性

- `core/utils/pii.py` PII 脱敏工具:正则识别手机/邮箱/身份证/银行卡,支持嵌套 dict/list
- `core/logging_config.py` 结构化日志配置:`setup_logging()` 支持人类可读与 JSON 两种格式,
  JSON 格式平铺 trace_id / tenant_id / user_id 关联字段
- `record_guard_check` 加 `would_be_false_positive` 参数:护栏误报率统计接入
  (`agentvalue_guard_false_positives_total` Counter),启发式识别"讨论/教学/示例"类
  prompt_injection 命中并打 false_positive 标
- `record_field_decrypt_failure` Counter + `FieldCipher.encrypt/decrypt` 失败时
  `logger.warning` 含 cipher 长度/前 8 字节 hex(不泄明文)
- `record_token_usage(tier, model, prompt_tokens, completion_tokens)` Counter
  `agentvalue_llm_token_usage_total{tier, model, direction="prompt|completion"}`:
  在 `chat_completion` / `chat_completion_structured` / `vision_completion` 返回前
  从 `resp.usage` 提取 token 用量上报
- 健康检查三端点 `/livez` `/readyz` `/healthz`,K8s readiness 探针专用,`/readyz`
  真实 ping DB + Redis + 至少一个 Provider,失败返回 503
- `core/multimodal/extractors.py` `_validate_magic_bytes()` 入口校验:
  PNG/JPEG/WebP/MP3/WAV/MP4/M4A 文件签名匹配,无效 mime 直接抛 `ValueError`,
  避免无效字节流送到 LLM API 浪费配额

#### 多模态真实接入

- `MultimodalCleaner` 配置传递完整:`ocr_api_key/ocr_base_url/ocr_model/asr_*`
  全部从 `Settings` 注入,修掉 `asr_api_key` 字段名错配(`asr_cloud_api_key`)
- `ModelRouter._build_tier_map` 在 cloud 与 local 两个 `ProviderConfig` 都注入
  `vision_model`,档位降级时视觉模型同步降级
- `ProviderConfig.vision_model` 默认 `gpt-4o-mini`,可通过 `VISION_MODEL` 环境变量覆盖

#### 多租户与审计

- 后台评估任务 `_run_evaluation_job` 加 `actor_id` 参数(由路由层从 JWT 解出),
  审计日志记录真实触发者而非硬编码 `"system"`
- `set_audit_context(actor_id, ip)` / `reset_audit_context(token)` contextvar
  接口,由 `TenantMiddleware` 在鉴权后注入,供 service 层 `audit_decorator` 读取
- `audit_action` 装饰器:业务失败时也写一条 `{action}_failed` 审计(含
  `exception_type` / `exception_msg`),供安全审计追查越权尝试/非法状态转换
- `alembic/versions/a1b2c3d4e5f6_add_tenants_table.py` tenants 表 migration,
  幂等创建 + default 租户种子行,补齐多租户 schema 缺口

#### 配置与 CI

- `core/config.py` 新增 `cors_origins` / `jwt_audience` / `jwt_issuer` /
  `jwt_leeway_seconds` / `vision_model` 配置项
- `CORS` 中间件改用配置驱动(`CORS_ORIGINS` 逗号分隔),不再硬编码 `*`
- `.github/workflows/security-scan.yml`:Trivy 文件系统扫描 + pip-audit 依赖漏洞
  + gitleaks secrets 扫描,SARIF 上传 GitHub Code Scanning(P0-5 漏洞硬阻断)
- `backend/pyproject.toml`:ruff 配置(line=100 / py310 / E F W)
- `backend/alembic/README`:alembic 初始化默认说明文档(迁移命令参考 `alembic --help`)

#### 文档与仓库治理

- 新增 `SUPPORT.md`:获取帮助渠道对号入座(Bug → Issue / 使用疑问 → Discussions /
  安全漏洞 → SECURITY.md 私密报告),补齐 GitHub 社区标准文件
- `README.md` 新增"支持"段并加入目录索引

#### PR 流程与分支保护

- 新增 `.github/scripts/ai_review.py`:AI 代码审查脚本,用 OpenAI 兼容接口审 PR diff,
  verdict=request_changes 时发 REQUEST_CHANGES 阻塞合并,approve 发 COMMENT;
  仅用标准库(urllib),异常时不阻断。触发工作流待接入(需配 `CLOUD_API_KEY` secret)
- 主分支保护收紧:`enforce_admins=true`(owner 也不能直接 push)、
  `required_approving_review_count=1`(人工 PR 需 1 位 review)、`dismiss_stale_reviews=true`
- `CONTRIBUTING.md` 更新分支策略与同步流程,说明 main 严格保护后
  助手改动一律走 PR、不再直接 push

#### 安全扫描修复(全绿)

- `security-scan.yml` Trivy action `@0.28.0`(已被删除的供应链攻击受污染标签)→ `@v0.36.0`
  (修复后安全版本,正确 `v` 前缀)
- `security-scan.yml` gitleaks 从 docker 调用改为直接安装二进制,修复 SARIF 写入
  `permission denied` + 规避 docker 镜像供应链风险
- 新增 `.gitleaks.toml`:allowlist `test_business_flow.py` 中的测试夹具假密钥
  (指向 kuncode 测试端点的 demo key,非真实凭证)
- 新增 `backend/pip-audit-ignore.txt`:忽略 `PYSEC-2026-311`(chromadb 1.5.9,
  当前最新版仍受影响,暂无修复版本;向量存储不暴露公网,接受风险)

### Changed

- PII 脱敏模式统一到 `core/utils/pii.py` 集中定义(`PII_PATTERNS` 注册表),
  `core/guards/output_guard.py` 改为从 utils 导入模式,保留各自的替换策略
  (展示用占位符 vs 日志用掩码),消除两套正则各自维护的漂移风险
- `eval/constants.py` 新增 `NEGATIVE_WORDS` 共享常量,
  `eval/evaluate.py` 与 `eval/llm_judge.py` 改为导入(原各自维护 22 项完全一致的列表)
- `api/deps.py` 新增 `assert_manager_team_access` 共享函数,
  `api/routes.py` 与 `api/analytics_routes.py` 改为复用(原两处定义几乎一致)
- `scripts/_stats_utils.py` 新增 `std`/`fmt_num` 共享函数,
  `scripts/fairness_audit.py`/`run_fairness_monthly.py`/`sla_monitor.py` 复用
  (原 `_std`/`_fmt` 在三处重复定义)
- 前端新增 `src/utils/evaluationStatus.js`(评估状态→中文标签/el-tag 类型/risk 类型映射),
  4 个 Vue 组件改为复用(原各自内联且 hr_audit 标签颜色不一致)
- 前端新增 `src/utils/echarts.js`(集中 echarts `use()` 注册),
  6 个图表组件改为 `import '@/utils/echarts'`(原各自重复注册)
- `frontend/vitest.config.js` 改用 `mergeConfig` 复用 `vite.config.js` 的
  plugins 与 alias(原重复声明)
- `agent/graph.py` 两个 graph builder 的 `call_llm` 改走 `_call_llm_with_fallback`
  helper:LLM 调用失败(exception 或 `completion.error` 非空)时调
  `model_router.runtime_reselect(tier, health_score)` 触发档位降级,再用降级档位
  重试一次(最多 1 次);失败仍走原逻辑并 `record_evaluation_failure("fallback_exhausted")`
- `agent/graph.py` 两个 `retrieve_context` 改用 `asyncio.gather(..., return_exceptions=True)`,
  单点工具失败降级为空上下文,不阻断主评估流程
- `api/routes.py` `_run_evaluation_job` 函数签名加 `actor_id` 参数,内部
  `transition_status` / `audit_service.log` 调用统一用传入的 actor_id
- `api/middleware.py` 重构为纯 ASGI 实现(规避 contextvar 跨任务传播问题),
  新增 `_extract_headers` / `_extract_jwt_payload` / `_extract_client_ip` helper,
  鉴权后注入 audit context
- `api/deps.py` `AppState` 加 `_tenant_memory_stores` / `_tenant_kb_stores` dict
  按租户懒加载向量库实例;`close()` 释放全部租户的向量库客户端
- `backend/.env.example` OCR_CLOUD_API_KEY 重复行删除,顶部加 5 条命名规范注释
- `backend/scripts/check_prod_readiness.py` 新增 `cors_origins` 检查项,
  生产环境含 `*` 或为空直接 FAIL

### Removed

- `.github/dependabot.yml` 与 `.github/workflows/dependabot-auto-merge.yml`:
  关闭 Dependabot 自动依赖更新 PR,改由维护者手动维护
  `backend/requirements.txt` / `frontend/package.json`;
  `SECURITY.md` 与 `docs/DEVELOPER_CHECKLIST.md` 同步移除 Dependabot 引用
- 清理生产死代码(经 Grep 验证生产从未调用,仅测试守护):
  `get_settings_dep`、`MultimodalCleaner.register_extractor/clean_single`+`CleanResult`、
  `create_ocr_extractor`/`create_asr_extractor`/`DummyASR`、
  `EvaluationService.get_employee_history`/`query_company_kb`、
  `AuditService.record_guard_result`、`get_current_actor_id`/`get_current_actor_ip`、
  `get_log_context`、`TokenBlacklist.is_redis_healthy`(两处)、
  Provider 扩展方法 `chat_completion_structured`/`chat_completion_stream`/
  `embeddings_create`/`function_calling` 及关联埋点
  (`LLM_STREAMING_TOTAL`/`LLM_FUNCTION_CALLS_TOTAL`/`LLM_EMBEDDINGS_TOTAL`);
  前端 `evaluationInterruptApi` 及 7 个未调用 API 方法、
  `evaluation` store 的 `error` 状态与 4 个未调用方法;
  同步删除对应测试用例与 `test_tracing.py`(被 `test_tracing_extra.py` 覆盖)、
  `test_provider_structured.py`(全测已删方法)
- 删除 `backend/data/sample_inputs.json`(零引用死文件,语义被 profiles.json + dataset.json 覆盖)
- `backend/data/pilot/` 下 25 个可再生产物(employees.json / weekly_reports / 报告 JSON,~5.8MB)
  从 git 移除并加入 .gitignore(由生成脚本产出,测试用 tmp_path 自行生成,不加载仓库文件)
- `backend/alembic/README` 从 alembic init 默认空壳改写为项目迁移说明

### Fixed

- 反馈闭环:员工 feedback 写入向量记忆(period 标记为 `feedback-{原周期}`,
  不覆盖评估记忆),下次评估 `retrieve_context` 可检索到员工历史反馈;
  `get_growth_path` 增加 `employee_voice` 字段,返回员工最近 5 条反馈,
  成长建议与员工真实诉求挂钩
- `core/multimodal/extractors.py` `_get_attachment_payload` 增加 `key` 下载分支:
  前端上传附件到对象存储后传 `key`,后端通过 `storage.download(key)` 获取二进制内容,
  修复多模态附件内容无法被 LLM 消费的断点(原仅 url 字段,抽取器不下载)
- `core/multimodal/extractors.py` 修 `asr_api_key` 字段名错配(应为 `asr_cloud_api_key`)
- `api/routes.py` 后台任务 actor_id 从硬编码 `"system"` 改为 JWT 解出的真实触发者
- `backend/.env.example` OCR_CLOUD_API_KEY 重复行(原 2 行同 key,删除多余 1 行)
- 仓库 URL 大小写统一为 `AgentValue-AI`(README / CONTRIBUTING / CHANGELOG /
  ISSUE_TEMPLATE config 中的 `agentvalue-ai` 链接);
  npm 包名 `agentvalue-ai-frontend` 与 markdown 锚点 `#agentvalue-ai` 按规范保持小写
- `docs/dev-guidelines.md` 修正失效的 `backend/tests/unit/` 引用(测试直接放 `backend/tests/`)
- `CONTRIBUTING.md` 修正 ruff 配置引用 `backend/ruff.toml` → `backend/pyproject.toml`
  (ruff 配置已统一到 pyproject.toml)
- `test_config_mgmt.py` 修正硬编码工作区路径大小写 `/workspace/agentvalue-ai` → `/workspace/AgentValue-AI`

## [v1.1.0] - 2026-07-04

### 开源发布准备

本次发布是项目从内部交付走向开源的关键节点,完成 4 路专家审计(开源就绪 / 安全 / 文档 / 代码配置)
后系统性整改,聚焦三类目标:开源法律可用、配置零信任默认、文档面向使用者。

### Added

#### 开源必备文件

- `LICENSE`:完整 MIT 协议文本(此前 README 仅一行声明,法律上等于"保留所有权利")
- `SECURITY.md`:漏洞披露流程、响应 SLA、部署侧安全清单、已知安全设计说明
- `CONTRIBUTING.md`:面向社区贡献者的 fork → PR 完整流程、提交规范、安全自检
- `CODE_OF_CONDUCT.md`:基于 Contributor Covenant 2.1 的社区行为准则
- `.github/ISSUE_TEMPLATE/`:bug_report / feature_request 模板 + config.yml(把安全报告导流到 SECURITY.md)
- `.github/PULL_REQUEST_TEMPLATE.md`:PR 自检清单(测试 / 文档 / 安全 / Prompt 影响)
- `.github/CODEOWNERS`:按路径的默认 reviewer 规则
- `.github/dependabot.yml`:pip / npm / docker / github-actions 四类依赖的周/月度自动更新
- `.editorconfig`:跨编辑器统一基础格式(Python 4 空格、JS/Vue/JSON 2 空格、Makefile tab)
- `.gitattributes`:强制 LF 行尾、声明二进制文件、修正 linguist 语言统计
- `frontend/.env.example`:此前缺失,导致 README 让用户 `cp .env.example .env` 直接报错

#### 数据与运行时

- `backend/data/pilot/README.md`:标注 5 档规模试点数据均为合成数据,非真实员工信息

### Removed

- `docs/test-plan-phase10.md`:内部阶段测试计划文档,真实模型联调阶段单独以定时任务跟踪,
  不与面向使用者的指南类文档混放
- `backend/data/security-audit-20260704.zip`:误提交的安全审计导出物,包含 RBAC 权限矩阵、
  已知漏洞清单、审计日志采样等攻击面信息。从 git 跟踪移除并加入 .gitignore / .dockerignore
- `docs/dev-guidelines.md` 中 maintainer 私有流程(GH_TOKEN / GC_TOKEN 巡检、个人邮箱、
  `badhope` / `MS33834` 内部仓库地址):开源后对社区贡献者无意义且暴露 maintainer 身份

### Changed

#### 配置默认值收紧(零信任)

- `docker-compose.prod.yml`:PostgreSQL / MinIO / Grafana 凭据从 `${VAR:-default}` 改为
  `${VAR:?must be set}`,缺失即启动报错,杜绝忘改弱默认值
- `docker-compose.yml`:开发模式注入 `JWT_SECRET_KEY` 占位值,使开箱即用的 `docker compose up`
  可正常签发 token(此前未配置导致登录全失败);占位值同时加入 `check_prod_readiness.py` 黑名单
- `docker-compose.prod.yml`:显式声明 `AGENTVALUE_ENV=production`,触发生产守护

#### 文档

- `README.md` 完全重写:新增徽章、目录、3 种快速开始路径、完整使用教程
  (初始化 → 登录 → 发起评估 → 审批 → 申诉 → 可观测性)、配置详解、Roadmap、FAQ、贡献入口
- `CHANGELOG.md`:补 Keep a Changelog 标准的版本比较链接;移除对已删除文档
  (`PROJECT-ROADMAP` / `docs/completed-work-log.md` / `docs/test-plan-phase10.md`)的过期引用
- `docs/scale-deployment-runbook.md`:Prompt 版本引用从 v0.3 订正为 v1.0(实际归档版本)
- `docs/dev-guidelines.md`:重写为面向贡献者的开发规范,移除 maintainer 私有流程与硬编码路径

### Fixed

#### 安全

- `.gitignore`:补充 `*.pem` / `*.p12` / `id_rsa*` / `id_ed25519*` / `.DS_Store` / `Thumbs.db` /
  `.ruff_cache/` / `.mypy_cache/` / `*.swp` / `.env.*` / `backend/data/security-audit-*.zip` 等遗漏模式
- `backend/.dockerignore`:排除生成的安全审计 zip,避免打入镜像
- `backend/.env.example`:MinIO 占位值从真实默认凭据 `minioadmin` 改为 `your-minio-access-key` /
  `your-minio-secret-key`,避免诱导 copy-paste 弱凭据
- `backend/scripts/check_prod_readiness.py`:JWT 黑名单加入 `dev-only-please-change-me-32chars-or-more`
  (开发 compose 占位值)与 `pilot-strong-random-secret-0x9f8e7d6c5b4a`(测试 fixture)

#### 一致性

- `frontend/package.json`:`version` 从 `0.1.0` 改为 `1.0.0` 与 v1.0.0 发布对齐;
  补 `license: MIT` / `description` 字段;移除指向缺失 eslint 配置的 `lint` script
- `backend/main.py`:FastAPI `version` 从 `0.1.0` 改为 `1.0.0`
- `backend/README.md`:移除"需安装 Playwright 浏览器"的 E2E 描述错误(实际基于 TestClient,无需浏览器)
- `backend/.env.example`:补 DummyEmbedding 维度说明与切换模型后必须重建向量库的提示

### 历史 Phase 7-9 摘要(从 v1.0.0 Unreleased 段归并)

- **Phase 7 多模态与集成扩展**:OCR / ASR / 多模态置信度;IM 与代码仓库集成 ADR(001/002);
  S3 兼容对象存储(MinIO)+ AttachmentStorage 抽象,留空降级本地 ATTACHMENT_DIR
- **Phase 8 试点运行与持续迭代**:5 档规模公司试点 + 4 周巡检;Prompt 迭代至 v1.0;
  公平性月报、申诉处理 SLA 监控、规模化部署 Runbook、Prompt 工程师认证流程
- **Phase 9 企业级增强**:多租户 tenant_id 隔离 + RBAC 数据级 + 向量库分 collection;
  高级分析(团队 ROI 九宫格、员工成长路径推荐、离职风险预测);
  数据留存自动化(GDPR/个保法)、水印防截图增强、GDPR 审计脚本、安全审计导出
- **技术债修复**:re_evaluate feedback 注入(拉 DB 历史 feedback + 合并调用方 feedback);
  JWT 黑名单 + /auth/logout 主动吊销 token;
  前端 element-plus 按需引入(unplugin-vue-components + ElementPlusResolver);
  re_evaluate 申诉内容影响重评结果——改为 build_prompt 拼接历史反馈区块注入评估上下文;
  JWT 签发后无法主动失效——加 jti claim + token_blacklist(Redis/内存双后端);
  前端 element-plus chunk 1.07MB——按需引入后拆为按需加载

## [v1.0.0] - 2026-07-02

MVP 首个正式版本:覆盖 Phase 1-5 + 补完轮 + Phase 6 关键项。

### Phase 1:项目骨架与基础设施

- 初始化 FastAPI + LangGraph + SQLAlchemy + Chroma 向量库后端骨架
- 初始化 Vue 3 + Vite + Element Plus + ECharts 前端
- 配置 docker-compose(backend / frontend / redis),支持一键本地起服务
- SQLite 异步数据库 + Alembic 迁移基线

### Phase 2:评估核心链路

- 实现 daily_evaluation Prompt(v0.1),三视图输出(员工/主管/审计)
- LangGraph 评估图:输入清洗 → 多模态提取 → LLM 评估 → 结构化解析 → 持久化
- 模型路由器(ModelRouter):按硬件档位 L0/L1/L2/L3 选择本地或云端模型
- 多模态输入清洗:附件类型白名单、超大输入拦截、Prompt 注入护栏
- 输入护栏(InputGuard):拦截恶意指令、Prompt 注入、超大附件
- 输出护栏(OutputGuard):PII 脱敏、偏见检测

### Phase 3:审批流与权限

- 评估状态机:ai_drafted → manager_review → hr_audit → approved/rejected
- 审批服务(ApprovalService):FOR UPDATE 悲观锁保证状态转换原子性
- RBAC 鉴权:employee / manager / hr / admin 四角色,字段级可见性控制
- JWT 认证 + 演示模式(仅开发/测试,生产强制关闭)
- 高风险评估自动路由至 HR 复核队列
- 员工申诉流:approved/rejected → manager_review

### Phase 4:可观测性与安全加固

- Langfuse 链路追踪集成(trace/span)
- Prometheus 指标端点(/metrics),6 项核心业务指标定义
- 审计日志(AuditService):所有写操作与敏感查看行为入审计
- 生产就绪检查脚本(check_prod_readiness.py)
- 公平性审计脚本(fairness_audit.py)

### Phase 5:回归评估与质量门禁

- LLM 输出回归评估框架(eval/evaluate.py):dataset + 规则校验 + LLM judge
- Prompt 版本对比门禁(--compare v0.1):检测 pass 回归与分数偏移
- 566 单元测试 + 16 E2E 测试,覆盖率 93%
- Locust 性能测试脚本

### 补完轮

- LangGraph 原生 interrupt human-in-the-loop 审批流(evaluations-interrupt 接口)
- 团队分析聚合接口
- 员工成长看板与跨周期能力演进
- 管理端审计日志分页查询
- 模型档位手动切换接口(含审计)

### Phase 6:MVP 关键项

#### 任务队列抽象 + Redis 化(解除单实例约束)

- 新增 `core/job_queue.py`:JobQueue 抽象基类 + InMemoryJobQueue + RedisJobQueue
- `create_job_queue` 工厂按 `settings.redis_url` 自动选择,Redis 不可达时降级内存,不崩
- `api/routes.py` 将模块级 `job_store` Dict 迁移至 `job_queue`,保持 API 行为完全一致
- `api/deps.py` AppState 注入 job_queue,与其他共享资源同生命周期管理
- `core/config.py` 新增 `redis_url` 配置项
- Redis key 前缀 `agentvalue:job:`,JSON 序列化,所有操作 try/except 降级
- 新增 19 项 job_queue 单元测试(fakeredis 覆盖 Redis 路径,不依赖真实 Redis)

#### 可观测性埋点(Prometheus 指标接入业务)

- `services/approval_service.py` transition 落库后埋点 `record_approval_transition`
- `services/evaluation_service.py` create_evaluation 埋点 `record_evaluation` + `observe_evaluation_duration`
- `api/routes.py` feedback / appeal 端点埋点 `record_feedback`
- 所有埋点 try/except 包裹,埋点失败不影响业务主流程
- 新增 `grafana/dashboard.json`:4 panel(评估吞吐 / 耗时分布 / 审批流转 / LLM 调用),数据源 Prometheus

#### CI/CD 流水线

- 新增 `.github/workflows/ci.yml`:lint(ruff + black) / backend-test / frontend-build / prompt-gate 四 job
- pip 与 npm 依赖缓存,push 到 main 与 PR 触发
- prompt-gate 跑 `python -m eval.evaluate --mock --compare v0.1`,exit 0 才通过
- 新增 `.pre-commit-config.yaml`:ruff / black / eslint / prettier / end-of-file-fixer / trailing-whitespace,仅作用于暂存文件(渐进式接入,不强制重排历史代码)
- 新增 `backend/ruff.toml`:lenient 规则集,CI 绿色 + 新增代码受约束

---

## 版本比较链接

[Unreleased]: https://gitcode.com/badhope/agentvalue/compare/v1.1.0...HEAD
[v1.1.0]: https://gitcode.com/badhope/agentvalue/releases/tag/v1.1.0
[v1.0.0]: https://gitcode.com/badhope/agentvalue/releases/tag/v1.0.0

