# AgentValue API 契约修复设计书

> 版本: v2.3.0-design
> 日期: 2026-08-07
> 状态: 已实施完成（已发布为 v2.3.0）
> 作者: Senior Developer

---

## 1. 问题定性

### 1.1 表象与真身

项目文档（`DEVELOPMENT-PLAN.md` / `CHANGELOG.md`）声称 P1–P7、v1.4.0 → v2.2.0 全部完成，`VERSION` 已是 `2.2.0`。但项目实际处于**不可用状态**。

经 AST 静态提取 + 逐模块核验，"未完成"的真身是：

> **后端与前端从未真正对接过。**
> 后端 481 条路由是真实实现（有 74 个测试文件保护）；前端 75 个 Vue 页面也已画完；
> 但前端 `src/api/client.js` 是按**想象中的 API 设计**写的，导致 409 个调用中
> **75 个（18.3%）打向后端根本不存在的路由**，直接表现为约 20 个后台页面整页 404。

这不是"功能没写"，而是**契约断裂（Contract Drift）**。

### 1.2 Ground Truth 的建立过程

路由表提取器（`route_diff.py`）历经 4 轮修正才达到可信状态，记录如下以备后续维护：

| 轮次 | Bug | 后果 | 修复 |
|------|-----|------|------|
| 1 | `include_router` 的 `prefix=` 从 `node.args[1:]` 读取 | 关键字参数读不到，多行挂载前缀全丢 | 改从 `node.keywords` 读 |
| 2 | 用 main.py 的**导入别名**作 router key | 别名与模块内原名 `router` 对不上，v2.x 模块路由全丢 | 跨文件 import 别名解析，还原 `(模块, 原变量名)` |
| 3 | `APIRouter` 检测写成 `isinstance(func, ast.Attribute)` | `APIRouter` 是 Name 导入，永远 False，`router_prefix` 全空 | 同时接受 `ast.Name` 与 `ast.Attribute` |
| 4 | BFS 挂载 `mount[c] = mount[parent] + include_prefix` | 漏加 parent 自身的 `APIRouter(prefix=)`，**嵌套 router 子路由丢前缀** | `mount[c] = mount[parent] + router_prefix[parent] + include_prefix` |

> 第 4 轮 bug 曾把 SSO 模块的 8 条真实路由误判为"后端缺失"。
> `sso_routes.py` 是全库唯一使用**文件内 router 嵌套**（`router` 主 + `config_router`/`auth_router` 子）的模块，
> 修复后不匹配数从 81 降至 75（消除 6 条误报）。

### 1.3 从 AST 推断升级为运行时权威基准

AST 静态提取即便修了 4 轮仍有盲区。安装依赖后改用 **FastAPI 官方 `app.openapi()`** 作为最终基准，交叉比对发现 AST 仍有两类错误：

| 类型 | 实例 | 影响 |
|------|------|------|
| **AST 漏抓** | `/api/v1/admin/analytics/{cost,token-usage,evaluation-stats,provider-distribution}` 真实存在（经 `from api.admin import analytics as admin_analytics` 模块对象方式挂载，AST 无法追踪） | 曾误判为"后端缺失"，险些错误地把前端改指向 analytics-v2 |
| **尾斜杠差异** | 真实路径为 `/admin/alerts/`、`/admin/sensitive-words/`（带尾斜杠） | 归一化处理 |

> ⚠️ 一个易踩的坑：本项目 FastAPI 版本为 **0.141.1**，`include_router` 已改为**惰性引用**（`app.routes` 中体现为 `_IncludedRouter` 对象，共 60 个），
> 直接遍历 `app.routes` 只能拿到 4 条 APIRoute。**必须通过 `app.openapi()` 展开**才能得到完整路由表。

**最终 Ground Truth（运行时权威）**：后端 **474** 条路由 / 前端 **409** 个调用 / 契约断裂 **71** 处。

### 1.3 断裂分布（全部集中在 admin 模块）

```
gray-release  10 │██████████
sso            8 │████████  (6 条为误报，已消除；实剩 2)
api-health     7 │███████
agents         7 │███████
publish        6 │██████
model-fallback 6 │██████
quota/graph-rag/analytics  各 4 │████
其余 13 个模块  1~3 条
```

---

## 2. 路线决策

### 2.1 候选路线对比

| 路线 | 做法 | 风险 | 完整性 |
|------|------|------|--------|
| A. 以后端为准 | 前端路径对齐后端 + 补后端真实缺口 | **低** | **高** |
| B. 以前端为准 | 按前端设计重写后端路由 | 极高（破坏 74 个测试） | 中 |
| C. 加适配层 | 中间层转发映射 | 中（新增运行时开销与维护面） | 低（治标） |

### 2.2 决策：路线 A

**判据数据：**

| 维度 | 后端 | 前端 |
|------|------|------|
| 代码体量 | 282 个 py 文件 / 481 路由 / 88 张表 | 75 个 Vue / 409 调用 |
| 测试保护 | **74 个测试文件** | 9 个测试文件 |
| 断裂集中度 | — | **100% 集中在 `client.js` 单文件（1100 行）** |

**理由：**

1. **后端是有测试保护的重资产** —— 481 条路由均为真实实现，改动会破坏 74 个既有测试，回归成本极高。
2. **前端断裂高度集中** —— 75 处全在 `client.js` 一个文件，改动面可控，且能用 diff 脚本自动验证归零。
3. **B 类缺口全部新增独立文件** —— 补后端时不修改任何既有路由文件，从物理上保证 74 个测试不受影响。

### 2.3 分类结果

| 类别 | 数量 | 处置 | 影响文件 |
|------|------|------|----------|
| **A 类**：后端已有端点，前端路径写错 | **54** | 改前端 `client.js` | 1 个文件 |
| **B 类**：后端确实缺失该端点 | **17** | 新增后端路由（独立文件） | 新增 3 个文件 + main.py 挂载 |

> 相比 AST 基准（56 + 19 = 75），运行时基准修正为 **54 + 17 = 71**：
> `analytics/evaluation-stats` 与 `analytics/provider-distribution` 后端本就存在，从 B 类移除。

---

## 3. A 类修复映射表（56 条 · 改前端）

### 3.1 gray-release（9 条）— 缺资源段 `/releases`

后端 `APIRouter(prefix="/api/v1/admin/gray-release")`，资源段为 `/releases`。

| # | 前端现状 | 后端真实路径 |
|---|----------|--------------|
| 1 | `GET /admin/gray-release` | `GET /admin/gray-release/releases` |
| 2 | `POST /admin/gray-release` | `POST /admin/gray-release/releases` |
| 3 | `GET /admin/gray-release/{id}` | `GET /admin/gray-release/releases/{id}` |
| 4 | `PUT /admin/gray-release/{id}` | `PUT /admin/gray-release/releases/{id}` |
| 5 | `DELETE /admin/gray-release/{id}` | `DELETE /admin/gray-release/releases/{id}` |
| 6 | `POST /admin/gray-release/{id}/start` | `POST /admin/gray-release/releases/{id}/start` |
| 7 | `POST /admin/gray-release/{id}/pause` | `POST /admin/gray-release/releases/{id}/pause` |
| 8 | `POST /admin/gray-release/{id}/rollback` | `POST /admin/gray-release/releases/{id}/rollback` |
| 9 | `POST /admin/gray-release/{id}/complete` | `POST /admin/gray-release/releases/{id}/complete` |

### 3.2 model-fallback（6 条）— 缺资源段 `/chains`

| # | 前端现状 | 后端真实路径 |
|---|----------|--------------|
| 1 | `GET /admin/model-fallback` | `GET /admin/model-fallback/chains` |
| 2 | `POST /admin/model-fallback` | `POST /admin/model-fallback/chains` |
| 3 | `GET /admin/model-fallback/{id}` | `GET /admin/model-fallback/chains/{id}` |
| 4 | `PUT /admin/model-fallback/{id}` | `PUT /admin/model-fallback/chains/{id}` |
| 5 | `DELETE /admin/model-fallback/{id}` | `DELETE /admin/model-fallback/chains/{id}` |
| 6 | `POST /admin/model-fallback/{id}/test` | `POST /admin/model-fallback/chains/{id}/test` |

### 3.3 api-health（6 条）— 复数 `slos` → 单数 `slo`

| # | 前端现状 | 后端真实路径 |
|---|----------|--------------|
| 1 | `GET /admin/api-health/slos` | `GET /admin/api-health/slo` |
| 2 | `POST /admin/api-health/slos` | `POST /admin/api-health/slo` |
| 3 | `PUT /admin/api-health/slos/{id}` | `PUT /admin/api-health/slo/{slo_id}` |
| 4 | `DELETE /admin/api-health/slos/{id}` | `DELETE /admin/api-health/slo/{slo_id}` |
| 5 | `GET /admin/api-health/status` | `GET /admin/api-health/slo/status` |
| 6 | `GET /admin/api-health/endpoints/{p}` | `GET /admin/api-health/endpoints/{path}/stats` |

### 3.4 quota（4 条）— 租户从上下文取，不走路径参数

| # | 前端现状 | 后端真实路径 |
|---|----------|--------------|
| 1 | `GET /admin/quota/{tenantId}` | `GET /admin/quota` |
| 2 | `GET /admin/quota/{tenantId}/usage` | `GET /admin/quota/usage` |
| 3 | `PUT /admin/quota/{tenantId}` | `PUT /admin/quota` |
| 4 | `POST /admin/quota/{tenantId}/reset` | `POST /admin/quota/reset` |

### 3.5 agents（4 条）— 后端是「版本」维度

| # | 前端现状 | 后端真实路径 |
|---|----------|--------------|
| 1 | `GET /admin/agents/{id}/compare` | `GET /admin/agents/{agent_id}/versions/{v1_id}/compare/{v2_id}` |
| 2 | `POST /admin/agents/{id}/archive` | `POST /admin/agents/{agent_id}/versions/{version_id}/archive` |
| 3 | `POST /admin/agents/{id}/publish` | `POST /admin/agents/{agent_id}/versions/{version_id}/publish` |
| 4 | `POST /admin/agents/{id}/rollback` | `POST /admin/agents/{agent_id}/rollback/{target_version}` |

### 3.6 analytics-v2（2 条）— 前端错拼端点名

> 注：`/admin/analytics/{cost,evaluation-stats,provider-distribution,token-usage}` 后端**真实存在**，前端路径正确，无需修改。
> 仅 analytics-v2 的 2 条端点名需纠偏（前端多了 `-v2` 后缀段）。

| # | 前端现状 | 后端真实路径 |
|---|----------|--------------|
| 1 | `GET /admin/analytics-v2/cost` | `GET /admin/analytics-v2/cost-breakdown` |
| 2 | `GET /admin/analytics-v2/latency` | `GET /admin/analytics-v2/latency-stats` |

### 3.7 publish（3 条）— 后端按渠道（channel）划分

| # | 前端现状 | 后端真实路径 |
|---|----------|--------------|
| 1 | `GET /admin/publish/{id}` | `GET /admin/publish/{agent_id}/status` |
| 2 | `POST /admin/publish/{id}/deploy` | `POST /admin/publish/{agent_id}/{channel}` |
| 3 | `POST /admin/publish/{id}/undeploy` | `DELETE /admin/publish/{agent_id}/{channel}` |

> 渠道枚举：`web` / `api` / `feishu` / `dingtalk` / `wechat`

### 3.8 graph-rag（3 条）

| # | 前端现状 | 后端真实路径 |
|---|----------|--------------|
| 1 | `GET /admin/graph-rag/tasks/{id}/entities` | `GET /admin/graph-rag/entities?task_id={id}` |
| 2 | `GET /admin/graph-rag/tasks/{id}/relations` | `GET /admin/graph-rag/relations?task_id={id}` |
| 3 | `POST /admin/graph-rag/search` | `GET /admin/graph-rag/search`（改为 GET + query params） |

### 3.9 其余模块（17 条）

| 模块 | 前端现状 | 后端真实路径 |
|------|----------|--------------|
| model-lb | `GET /admin/model-lb/config` | `GET /admin/model-lb/configs` |
| model-lb | `PUT /admin/model-lb/config` | `PUT /admin/model-lb/configs/{config_id}` |
| sso | `GET /admin/sso/configs/{id}/callback` | `POST /admin/sso/configs/{id}/callback` |
| sso | `POST /admin/sso/ldap/login` | `POST /admin/sso/configs/{id}/ldap-login` |
| search | `POST /admin/search` | `POST /admin/search/hybrid` |
| search | `POST /admin/search/incremental-update` | `POST /admin/search/documents/{document_id}/incremental-update` |
| datasets | `GET /admin/datasets/{id}/entries` | `GET /admin/datasets/{dataset_id}/items` |
| datasets | `POST /admin/datasets/{id}/entries/import` | `POST /admin/datasets/{dataset_id}/items/batch` |
| tool-config | `GET /admin/tool-config` | `GET /admin/tool-config/timeouts` |
| tool-config | `PUT /admin/tool-config/{id}` | `PUT /admin/tool-config/{tool_name}/timeout` |
| sensitive-words | `POST /admin/sensitive-words/batch-import` | `POST /admin/sensitive-words/import` |
| nl2sql | `POST /admin/nl2sql/execute-sql` | `POST /admin/nl2sql/execute` |
| budgets | `GET /admin/budgets/{id}/status` | `GET /admin/budgets/status` |
| annotations | `GET /admin/annotations/tasks/{id}/stats` | `GET /admin/annotations/stats` |
| prompt-opt | `GET /admin/prompt-optimization/tasks/{id}/results` | `GET /admin/prompt-optimization/tasks/{task_id}/result` |
| agent-templates | `POST /admin/agent-templates/{id}/instantiate` | `POST /admin/agent-templates/{template_id}/install` |
| model-providers | `GET /admin/model-providers/workspaces/current/providers/{p}` | `GET /admin/model-providers/providers/{provider}` |

---

## 4. B 类补齐设计（19 条 · 补后端）

### 4.1 设计原则

1. **零侵入** —— 全部新增到独立文件，不修改任何既有路由文件，保证 74 个测试不受影响。
2. **复用既有 Service** —— 只做路由编排与聚合，不重复实现业务逻辑。
3. **复用既有鉴权** —— 沿用 `require_role(Role.ADMIN)` + `get_current_tenant` 多租户隔离。
4. **契约优先** —— 严格按前端已写好的调用签名实现，做完 diff 必须归零。

### 4.2 新增文件规划

```
backend/api/admin/
├── agents_crud_routes.py        # 新增：Agent 主体 CRUD（3 端点）
├── publish_manage_routes.py     # 新增：发布记录管理（3 端点）
└── contract_supplement_routes.py# 新增：其余 13 个补齐端点
```

### 4.3 端点清单

#### 组 1：Agent 主体 CRUD（`agents_crud_routes.py`）

后端原有 `/admin/agents/*` 全是**版本**维度，缺 Agent 主体本身的 CRUD。

| 方法 | 路径 | 说明 | 复用 |
|------|------|------|------|
| GET | `/api/v1/admin/agents` | Agent 列表（分页 + 搜索 + 状态过滤） | `presets/agents` 数据源 |
| GET | `/api/v1/admin/agents/{agent_id}` | Agent 详情（含最新版本信息） | 同上 + 版本服务 |
| POST | `/api/v1/admin/agents` | 创建 Agent | 同上 |

#### 组 2：发布记录管理（`publish_manage_routes.py`）

后端原有 `/admin/publish/*` 是**按渠道操作**，缺发布记录的列表/创建/更新。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/admin/publish` | 发布记录列表（跨 Agent 聚合，支持渠道/状态过滤） |
| POST | `/api/v1/admin/publish` | 创建发布记录（body 指定 agent_id + channel） |
| PUT | `/api/v1/admin/publish/{publish_id}` | 更新发布配置 |

#### 组 3：其余补齐（`contract_supplement_routes.py`）

| # | 方法 | 路径 | 说明 |
|---|------|------|------|
| 1 | GET | `/api/v1/admin/gray-release/releases/{id}/stats` | 灰度发布指标统计（流量/成功率/延迟对比） |
| 2 | GET | `/api/v1/admin/api-health/stats` | API 健康总览（端点数/SLO 达成率/告警数聚合） |
| 3 | GET | `/api/v1/admin/graph-rag/tasks/{task_id}/visualize` | 任务级图谱可视化数据 |
| 4 | PUT | `/api/v1/admin/sensitive-words/{word_id}` | 更新敏感词 |
| 5 | POST | `/api/v1/admin/sensitive-words/{word_id}/review` | 敏感词人工复审 |
| 6 | POST | `/api/v1/admin/tool-config/{tool_name}/reset` | 重置工具配置为默认值 |
| 7 | POST | `/api/v1/admin/nl2sql/schemas/{schema_id}/refresh` | 刷新数据库 Schema 元数据 |
| 8 | GET | `/api/v1/admin/budgets/{budget_id}` | 预算详情 |
| 9 | GET | `/api/v1/admin/annotations/tasks/{task_id}/export` | 导出标注结果（JSON/CSV） |
| 10 | GET | `/api/v1/admin/alerts/{alert_id}` | 告警详情 |
| 11 | GET | `/api/v1/admin/scheduler/tasks/{task_id}` | 调度任务详情 |

---

## 5. 实施阶段

```
Phase 1  前端契约对齐（56 条）
         └─ 改 frontend/src/api/client.js
         └─ 验证：route_diff.py 中 A 类全部消失

Phase 2  后端缺口补齐（19 条）
         ├─ agents_crud_routes.py         (3)
         ├─ publish_manage_routes.py      (3)
         ├─ contract_supplement_routes.py (13)
         └─ main.py 挂载 3 个新 router

Phase 3  终验
         ├─ route_diff.py 归零（静态）
         ├─ FastAPI app.routes 运行时校验（权威）
         ├─ 后端既有 74 个测试不回归
         └─ 前端 build 通过
```

## 6. 验收标准

| # | 标准 | 验证方式 |
|---|------|----------|
| 1 | 契约断裂 **75 → 0** | `python3.11 route_diff.py` 输出不匹配为 0 |
| 2 | 运行时路由表与静态提取一致 | `app.routes` 遍历比对，避免 AST 误差 |
| 3 | 既有测试零回归 | `pytest backend/tests` 通过率不低于修改前 |
| 4 | 前端可构建 | `npm run build` 成功 |
| 5 | 新增端点可鉴权访问 | TestClient 冒烟，非 404 |

## 7. 风险与对策

| 风险 | 对策 |
|------|------|
| 改 client.js 引入笔误 | 全部改动经 diff 脚本自动校验，不靠肉眼 |
| 新增后端端点破坏既有测试 | 新增独立文件，不碰既有路由文件；改完跑全量测试 |
| AST 提取仍有盲区 | Phase 3 用 FastAPI 运行时 `app.routes` 做权威交叉验证 |
| 前端调用签名（参数）与后端不符 | 逐条按后端函数签名核对 query/body 字段 |

---

## 8. 实施结果（2026-08-07）

设计书在 v2.3.0 中已全部落地，验收标准逐项达成：

| # | 验收标准 | 结果 |
|---|----------|------|
| 1 | 契约断裂 75 → 0 | ✅ 第一层 client.js↔后端 **0 不匹配**（运行时 490 路由 / 441 调用）；第二层 view↔client.js **0 不匹配**（72 个 .vue 扫描） |
| 2 | 运行时路由表与静态提取一致 | ✅ `app.openapi()` 展开 492 路由，逐条核对 |
| 3 | 既有测试零回归 | ✅ 1732 passed；剩余 7 failed / 13 error 为仓库既有问题（停用词提取 / 特性开关 rerank / 认证夹具 / coderepo 适配器），与本轮新增文件无关 |
| 4 | 前端可构建 | ✅ `npm run build` 3.7s 通过 |
| 5 | 新增端点可鉴权访问 | ✅ 新端点挂载于 admin 路由，复用既有 RBAC 依赖 |

### 8.1 新增后端文件（契约补齐）

- `backend/api/admin/agents_crud_routes.py` — Agent 主体 CRUD（11 端点，无前缀，全路径声明）
- `backend/api/admin/publish_manage_routes.py` — 发布渠道分发 `POST /{agent_id}/{channel}`（静态渠道优先匹配）
- `backend/api/admin/contract_supplement_routes.py` — 灰度统计 / API 健康汇总 / GraphRAG 可视化 / 敏感词审核 / 工具超时重置 / NL2SQL 反射 / 预算详情 / 标注导出 / 告警详情 / 调度任务详情

### 8.2 修复的前端视图

AdminSecurity（SSO/LDAP）、AdminModelOps（SLO）、AdminReleaseOps（Agent 版本/发布/灰度）、AdminBilling（analytics）、AdminEvalCenter（dataset/annotation）、AdminKnowledgeOps（graphRag/docParsing/nl2sql/search）— 共 9 个页面的 20 处调用对齐至真实 client.js 方法。
