# AgentValue-AI 后端

后端服务。FastAPI 负责 REST 接口与 RBAC，LangGraph 跑评估状态机（含 interrupt 审批中断点），ModelRouter 按档位在本地与云端模型间分流。

---

## 目录结构

```
backend/
├── agent/            # LangGraph Agent 工作流
│   ├── graph.py      # 评估状态机与 interrupt 审批流
│   ├── langchain_tools.py  # Agent 内置工具(v1.5.0): bash / read_file / write_file / list_directory / web_fetch + calculator / datetime / employee_history / company_kb
│   ├── session_prompt.py   # AI 对话 ReAct 循环(v1.5.0): 手动 ReAct + SSE 事件流 + 工具执行
│   ├── session_processor.py # 对话处理器(v1.5.0): SSE 流式 + 工具调用 + 消息持久化
│   ├── prompt_loader.py
│   ├── state.py
│   └── tools.py      # Agent 可调用的记忆/知识库工具
├── api/              # FastAPI 路由
│   ├── chat.py       # AI 对话系统(v1.5.0): sessions / messages / regenerate / auto-title / feedback / search
│   ├── auth_routes.py
│   ├── deps.py
│   └── routes.py
├── auth/             # JWT 认证与 RBAC
│   ├── jwt_handler.py
│   ├── password.py
│   └── rbac.py
├── core/             # 基础设施
│   ├── config.py
│   ├── database.py
│   ├── embeddings.py
│   ├── model_router.py
│   ├── multimodal/   # 多模态清洗与抽取
│   ├── providers/    # 模型 Provider 抽象
│   └── tracing.py
├── data/             # 演示数据
├── eval/             # LLM 回归评估脚本
├── memory/           # 向量记忆封装
├── models/           # SQLAlchemy 数据模型(含 chat_session / chat_message / chat_part v1.5.0)
├── prompts/          # Prompt 文件
├── schemas/          # Pydantic Schema
├── services/         # 业务服务(含 chat_service.py v1.5.0)
└── tests/            # 测试用例
```

---

## 本地开发

### 安装依赖

```bash
cd backend
pip install -r requirements.txt
```

### 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入模型 API Key
```

### 启动服务

```bash
uvicorn main:app --reload --port 8000
```

---

## 数据库迁移

项目使用 [Alembic](https://alembic.sqlalchemy.org/) 管理数据库结构迁移。

### 常用命令

```bash
# 查看当前版本
alembic current

# 查看迁移历史
alembic history

# 升级到最新版本
alembic upgrade head

# 回退一个版本
alembic downgrade -1

# 根据模型变更自动生成迁移脚本
alembic revision --autogenerate -m "描述本次变更"
```

也可以使用封装脚本：

```bash
python scripts/migrate.py upgrade
python scripts/migrate.py current
python scripts/migrate.py history
python scripts/migrate.py revision -m "描述本次变更" --autogenerate
```

数据库连接串从 `core.config.get_settings().database_url` 读取，可通过环境变量 `DATABASE_URL` 覆盖。

---

## 主要 API 概览

### 认证

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/auth/login` | 邮箱 + 密码登录 |
| POST | `/api/v1/auth/register` | 注册新用户 |
| POST | `/api/v1/auth/refresh` | 刷新 token |
| POST | `/api/v1/auth/logout` | 登出并吊销 token |
| GET | `/api/v1/auth/me` | 当前用户信息 |
| POST | `/api/v1/auth/seed-demo-users` | 初始化演示账号 |

### 评估核心

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/inputs` | 提交日报/任务/附件等原始输入 |
| POST | `/api/v1/evaluations` | 异步触发评估，返回 job_id |
| GET | `/api/v1/evaluations/jobs/{job_id}` | 查询评估任务状态 |
| GET | `/api/v1/evaluations/{id}` | 查询评估结果（按角色过滤） |
| GET | `/api/v1/evaluations/{id}/employee-view` | 员工视图 |
| GET | `/api/v1/evaluations/{id}/manager-view` | 管理视图 |
| GET | `/api/v1/evaluations/{id}/audit-logs` | 评估审计日志 |
| GET/POST | `/api/v1/evaluations/{id}/feedback` | 查询/提交员工反馈 |
| POST | `/api/v1/evaluations/{id}/approve` | 主管审批通过 |
| POST | `/api/v1/evaluations/{id}/reject` | 驳回评估 |
| POST | `/api/v1/evaluations/{id}/request-hr-review` | 请求 HR 复核 |
| POST | `/api/v1/evaluations/{id}/require-reeval` | 要求重新评估 |
| POST | `/api/v1/evaluations/{id}/appeal` | 员工申诉 |
| POST | `/api/v1/evaluations/{id}/re-evaluate` | 基于反馈重新评估 |
| GET | `/api/v1/employees/{id}/feedback` | 员工反馈/申诉处理进度 |

### 附件

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/attachments` | 上传附件（走 AttachmentStorage 抽象，本地或 S3/MinIO） |

### 知识库

| 方法 | 路径 | 说明 |
|---|---|---|
| POST/GET | `/api/v1/kb` | 新建/查询知识库条目 |
| GET/DELETE | `/api/v1/kb/{id}` | 查询/删除指定条目 |

### 周期

| 方法 | 路径 | 说明 |
|---|---|---|
| POST/GET | `/api/v1/periods` | 新建/查询评估周期 |
| GET | `/api/v1/periods/{period}` | 查询指定周期 |
| POST | `/api/v1/periods/{period}/close` | 关闭周期 |

### 水印

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/watermark/verify` | 校验水印（防截图溯源） |

### 租户

| 方法 | 路径 | 说明 |
|---|---|---|
| POST/GET | `/api/v1/tenants` | 新建/查询租户 |
| GET | `/api/v1/tenants/{id}` | 查询指定租户 |
| PUT | `/api/v1/tenants/{id}/status` | 更新租户状态 |

### 看板与分析

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/employees/{id}/dashboard` | 个人成长看板 |
| GET | `/api/v1/employees/{id}/history` | 跨周期能力演进 |
| GET/POST | `/api/v1/teams/{id}/analytics` | 团队分析 |
| GET | `/api/v1/manager/dashboard` | 主管工作台 |
| GET | `/api/v1/manager/pending-approvals` | 待审批列表 |
| GET | `/api/v1/hr/audit-queue` | HR 复核队列 |

### 高级分析

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/analytics/team-roi` | 团队 ROI 九宫格 |
| GET | `/api/v1/analytics/growth-path/{id}` | 员工成长路径推荐 |
| GET | `/api/v1/analytics/attrition-risk` | 离职风险预测 |

### 管理后台

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/admin/model-status` | 模型状态与推荐档位 |
| POST | `/api/v1/admin/model-switch` | 手动切换模型档位 |
| GET | `/api/v1/admin/audit-logs` | 审计日志查询 |

### AI 对话系统 (v1.5.0)

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/chat/sessions` | 创建会话(可指定 model_name) |
| GET | `/api/v1/chat/sessions` | 查询当前用户会话列表 |
| GET | `/api/v1/chat/sessions/{id}` | 查询会话详情 |
| PATCH | `/api/v1/chat/sessions/{id}` | 更新会话(标题 / model_name) |
| DELETE | `/api/v1/chat/sessions/{id}` | 删除会话 |
| POST | `/api/v1/chat/sessions/{id}/messages` | 发送消息(SSE 流式响应) |
| POST | `/api/v1/chat/sessions/{id}/regenerate` | 重新生成最后一条回复 |
| DELETE | `/api/v1/chat/sessions/{id}/messages/{msg_id}` | 删除消息(级联删除 parts) |
| POST | `/api/v1/chat/sessions/{id}/auto-title` | LLM 自动生成会话标题 |
| POST | `/api/v1/chat/sessions/{id}/messages/{msg_id}/feedback` | 点赞/点踩反馈 |
| GET | `/api/v1/chat/sessions/search?q=keyword` | 搜索会话(标题模糊匹配) |

**Agent 工具(v1.5.0,对标 opencode)**

| 工具 | 说明 | 安全约束 |
|---|---|---|
| `bash` | 执行 shell 命令 | 30s 超时 + 5000 字符输出截断 |
| `read_file` | 读取文件内容 | 5000 字符截断 |
| `write_file` | 写入文件 | 自动创建父目录 |
| `list_directory` | 列出目录内容 | - |
| `web_fetch` | 抓取网页 | HTML → 纯文本 + 5000 字符截断 |
| `calculator` | 数学计算 | - |
| `get_current_datetime` | 获取日期时间 | - |
| `get_employee_history` | 查询员工历史评估 | 业务工具 |
| `query_company_kb` | 查询公司知识库 | 业务工具 |

工具经 `ToolRegistry` 统一管理,可通过 `enabled_tools` 配置开关。

### LangGraph 原生 interrupt 审批流

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/evaluations-interrupt` | 启动带 interrupt 的评估 |
| GET | `/api/v1/evaluations-interrupt/{thread_id}/state` | 查询中断状态 |
| POST | `/api/v1/evaluations-interrupt/{thread_id}/resume` | 恢复并提交审批决策 |

### 企业管理功能 (v2.1.0)

基于对 Coze、Dify、百度千帆、阿里百炼、腾讯混元、FastGPT、RagFlow、MaxKB、Bisheng、Langfuse、LobeChat 等 12 个平台 × 14 个维度的深度调研,两轮补齐 22 个功能模块、65+ 文件、20 张数据库表、200+ API 端点,并完成 19 项安全加固。以下为各模块 API 基路径(端点 CRUD/查询/操作均在对应前缀下)。

**第一轮: 企业管理功能 (11 项)**

| 模块 | API 基路径 | 说明 |
|---|---|---|
| 混合检索 | `/api/v1/admin/search` | BM25 + 向量 RRF 融合 + 元数据过滤 + 增量更新 |
| 配额管理 | `/api/v1/admin/quota` | 按 tenant 日请求/token 配额 + 用量统计 + 重置 |
| 成本预算告警 | `/api/v1/admin/budgets` | 月度/日度预算 + 阈值告警通知 |
| API 计费账单 | `/api/v1/admin/billing` | 汇总 + 按用户/端点聚合 + CSV/JSON 导出 |
| Agent 版本管理 | `/api/v1/admin/agents` | 版本 CRUD + 发布 + 回滚 + 对比 + 归档 |
| 多渠道发布 | `/api/v1/admin/publish` | 飞书/微信/钉钉/Web/API |
| 工具执行超时 | `/api/v1/admin/tool-config` | 通用工具超时管理 + per-tool 配置 |
| 敏感词管理 | `/api/v1/admin/sensitive-words` | AC 自动机 + 文本审核 + 导入导出 |
| 告警通知通道 | `/api/v1/admin/alerts` | 飞书交互卡片/邮件 HTML/Webhook POST |
| 工作流增强 | `/api/v1/admin/workflows` | loop 循环节点 + parallel 并行节点 |
| 按 user_id 限流 | (限流中间件) | x-user-id header 识别用户级限流 |

**第二轮: 评测/安全/集成 (11 项)**

| 模块 | API 基路径 | 说明 |
|---|---|---|
| 模型 Fallback 策略 | `/api/v1/admin/model-fallback` | 故障自动切换备用模型,降级链配置 (对标阿里百炼秒级容灾) |
| 会话分析看板 | `/api/v1/admin/analytics-v2` | Token 趋势/P50/P95/P99 延迟/错误率/成本分解/异常检测 (对标 Langfuse) |
| API 健康监控 + SLO | `/api/v1/admin/api-health` | 端点健康/SLO 定义与达成监控 |
| 数据集管理 | `/api/v1/admin/datasets` | 测试集/训练集/评测集 CRUD + 批量导入/导出 (对标 Langfuse/阿里百炼) |
| LLM-as-a-Judge 自动评测 | `/api/v1/admin/llm-judge` | LLM 裁判多维度评分 + 异步后台执行 (对标 Langfuse) |
| RAG 质量评测 | `/api/v1/admin/rag-eval` | Precision/Recall/MRR/NDCG + 答案溯源 (对标 RagFlow) |
| 人工标注工具 | `/api/v1/admin/annotations` | HITL 分配/标注/统计,评测闭环 (对标 Langfuse) |
| SSO 单点登录 | `/api/v1/admin/sso` | OAuth2/SAML/LDAP 三协议支持 (对标 Dify/Bisheng) |
| Agent 模板市场 | `/api/v1/admin/agent-templates` | 分类/搜索/评价/安装/公开市场 (对标 Coze/LobeChat) |
| NL2SQL 自然语言转 SQL | `/api/v1/admin/nl2sql` | 表白名单 + SQL 注入防护 + tenant_id 自动注入 (对标 RagFlow) |
| 深度文档解析 | `/api/v1/admin/doc-parsing` | PDF/DOCX/XLSX/PPTX 解析 + 表格提取 + 版面分析 (对标 RagFlow DeepDoc) |

**安全加固 (19 项)**

- C1-C3: 多租户隔离 (tenant_id 添加到所有新模型)
- C4: API Key 不明文持久化 (hash + prefix 存储)
- C5: 代码沙箱移除 getattr/hasattr/isinstance
- C1+H3 (v2): NL2SQL 跨租户数据泄露 (表白名单 + tenant_id 自动注入)
- C2 (v2): 文档解析文件路径遍历 (realpath + 白名单目录)
- H1-H4: IDOR 越权修复 (budget/quota/billing 路由)
- H1 (v2): OAuth2 state 服务端校验 (存储 + 验证 + 过期)
- H2 (v2): LDAP 搜索过滤器注入 (RFC 4515 转义)
- H4 (v2): SSO 认证端点权限修复 (认证流程端点公开)
- H5: 检索 collection 跨租户访问修复
- H5 (v2): 异步任务竞态条件 (原子状态更新)
- H7: 工作流 HTTP 节点 SSRF 防护
- H8: 循环节点迭代上限 + 执行超时
- M1-M2: 文档解析租户上下文 + 查询过滤

> 端到端测试: 111 个用例全部通过 (37 第一轮 + 74 第二轮);安全验证覆盖 SQL 注入/DROP/路径遍历/SSO 密钥脱敏,全部拦截。

---

## 角色与权限

| 角色 | 权限 |
|---|---|
| employee | 查看自己的员工视图、提交输入、反馈申诉 |
| manager | 审批、查看管理视图、团队分析 |
| hr | 复核异常评估、查看审计日志 |
| admin | 模型切换、查看全部审计日志 |

---

## 测试

```bash
# 单元测试（默认跑批，pytest.ini 已通过 --ignore=tests/perf 自动排除性能测试）
python -m pytest tests -q

# 只跑 E2E 测试（基于 FastAPI TestClient 的 API 级联调,无需浏览器）
python -m pytest -m e2e -q

# 只跑单测、显式排除 E2E
python -m pytest --ignore=tests/e2e -q

# 使用 Mock Provider 跑通评估流程（无需 API Key）
python -m eval.evaluate --mock

# 性能测试（locust 需单独起服务，不纳入常规跑批）
locust -f tests/perf/locustfile.py
```

> 备注：`pytest.ini` 中已配置 `addopts = --ignore=tests/perf`，即常规 `pytest tests` 会自动跳过 `tests/perf`；同时注册了 `e2e` marker，可用 `pytest -m e2e` 精确筛选 E2E 用例。`tests/perf` 为 locust 性能测试，需要先单独启动被测服务后再运行，故不纳入常规跑批。

---

## 模型档位

| 档位 | 场景 | 模型示例 |
|---|---|---|
| auto | 根据硬件自动推荐 | - |
| L0 | 云端大模型 | GPT-4o / DeepSeek-V3 |
| L1 | 边缘小模型 | Qwen2.5-0.5B |
| L2 | 标准本地模型 | Qwen2.5-7B |
| L3 | 本地旗舰模型 | Qwen2.5-14B |

---

## 环境变量说明

详见 [.env.example](.env.example)。
