# 架构决断记录 (Architecture Decisions)

本文件记录 AgentValue-AI 在演进过程中做出的有意识取舍与决断,供后续维护者理解"为什么是这样"以及"何时该重新评估"。每条决断包含:背景、决策、理由、当前状态、重新评估触发条件。

> 与 `docs/adr/` 的关系:ADR 记录单点的"决策-否决项-后果"标准 ADR 模板;本文件聚焦跨模块的演进取舍与已知技术债边界。

---

## P1-16 LangGraph 工作流:选型偏重,短期保留

### 背景

`backend/agent/graph.py` 使用 LangGraph 实现 9 节点的固定评估工作流:

```
input_sanitizer → data_cleaning → retrieve_context → build_prompt
→ call_llm → parse_output → (manager_review | hr_audit) → finalize
```

带中断版本 `create_evaluation_graph_with_interrupt` 还使用了 LangGraph 原生 `interrupt()` + `MemorySaver` checkpointer。

### 当前状态

- 当前是**固定 workflow**(节点拓扑在编译期确定,无动态路由/tool calling)
- 仅 `parse_output` 后的 `manager_review_gate` / `review_gate` 做条件分支(按 score / risk_flags 选 manager_review 或 hr_audit)
- 没有真正的 Agent 行为(LLM 不决定下一步调用哪个 tool,工具调用由代码编排)
- LangGraph 的 checkpointer / interrupt / time-travel 等高级能力,当前仅 interrupt 版本用到

### 决策

- **短期保留**:已有 800+ 测试覆盖 `graph.py`,迁移成本高;LangGraph 的 interrupt 能力支撑了审批中断需求
- **长期评估两条路径**:
  1. **升级为真 Agent**:接入 `BaseProvider.function_calling`(见 P1-17),让 LLM 自主决定是否查 KB / 重算 / 调工具,适用场景为高复杂度评估
  2. **降级为纯函数链**:若发现 Agent 行为始终不需要,移除 LangGraph 依赖,用普通 async 函数链 + 显式状态机实现,降低依赖与冷启动开销

### 重新评估触发条件

- 出现需要 LLM 自主多轮工具调用的需求(如"让 LLM 决定是否要查额外历史")
- LangGraph 版本升级带来破坏性变更,维护成本超过收益
- 冷启动时间成为瓶颈(LangGraph 依赖链较重)

---

## P1-17 Provider 死代码:streaming / embeddings / function_calling

### 背景

`backend/core/providers/base.py` 与 `backend/core/providers/openai_provider.py` 定义了 5 个能力扩展方法:

| 方法 | 行号(openai_provider.py) | 生产主链路是否调用 |
|---|---|---|
| `chat_completion` | L62 | ✅ 是(`agent/graph.py` 的 `_call_llm_with_fallback`) |
| `chat_completion_structured` | L123 | 部分(结构化输出场景) |
| `health_check` | L197 | ✅ 是(`ModelRouter.get_provider_with_fallback`) |
| `chat_completion_stream` | L219 | ❌ 否 |
| `embeddings_create` | L287 | ❌ 否(embedding 走 `core/embeddings.py` 独立路径) |
| `function_calling` | L326 | ❌ 否 |
| `vision_completion` | L389 | 部分(多模态 extractors) |

### 当前状态

`chat_completion_stream` / `embeddings_create` / `function_calling` 三个方法在**生产主链路未被调用**,属于"已实现但未接入"的死代码。

### 决策

- **保留**这三个方法,作为未来真 Agent 升级(P1-16 路径 1)的基础设施
- 在 `base.py` 与 `openai_provider.py` 的方法 docstring 已注明"子类未实现时 raise NotImplementedError",不破坏既有子类
- **加注释说明**"当前未接入主链路",避免后续维护者误以为它们在跑(本次未直接改源码注释,因源码不在本任务文件清单;在此文档化提示)

### 重新评估触发条件

- 真正接入 function_calling(升级为 Agent)
- 或确认长期不会用,则删除以减少 surface area

---

## P2-2 Settings 扁平结构:60+ 字段

### 背景

`backend/core/config.py` 的 `Settings` 类承载 60+ 配置字段,涵盖:LLM / Embedding / 数据库 / Redis / JWT / CORS / 多租户 / 向量库 / 限流 / 审计 等。

### 当前状态

所有配置扁平挂在一个 `Settings` 实例上,通过 `get_settings()` 单例获取。

### 决策

- **短期保留扁平结构**:向后兼容(`get_settings().cloud_api_key` 等调用遍布全代码库),拆分需要大范围迁移
- **长期按领域拆分**为嵌套子配置:
  - `settings.llm.cloud_api_key` / `settings.llm.local_model_l2`
  - `settings.db.url` / `settings.redis.url`
  - `settings.tenant.*`
  - `settings.security.jwt_*`
- 拆分时提供 `@property` 兼容旧扁平访问,渐进迁移

### 重新评估触发条件

- 配置字段超过 100,查找/分组成本明显
- 引入新的配置领域(如多 region 路由)导致 Settings 进一步膨胀

---

## P2-3 ChromaDB 单机模式:多实例部署上限

### 背景

`backend/memory/vector_store.py` 使用 ChromaDB 的 `PersistentClient`(本地文件模式),向量库目录由 `settings.vector_store_dir` 指定。

### 当前状态

- 单实例部署:所有 Pod 共享同一 `vector_store_dir`(或各自独立,数据不共享)
- ChromaDB 在单机模式下不保证多进程并发写入安全

### 决策

- 文档化**单实例部署上限**:当前架构仅支持单实例(或单写多读 via 共享卷)的向量库
- 多实例横向扩展需迁移到分布式向量库(Qdrant / Milvus / Weaviate)或 ChromaDB 的分布式模式
- 迁移时 `core/embeddings.py` 的 embedding function 接口可复用,仅替换底层 client

### 重新评估触发条件

- 单实例向量库吞吐 / 容量成为瓶颈
- 需要多 Pod 同时写入向量库

---

## P2-4 ModelRouter 健康度:进程内

### 背景

`backend/core/model_router.py` 的健康度评分(`_health_history` deque + `get_health_score`)是**进程内状态**,各 Pod 独立维护各自的滑动窗口。

### 当前状态

- 健康度评分仅反映当前 Pod 视角的 Provider 可达性
- `runtime_reselect` 基于进程内健康度做档位降级

### 决策

- **多实例部署时强制 `model_tier=L0`(云端)**:避免各 Pod 健康度不一致导致档位决策分叉
- 多实例时**关闭健康度探测**(`get_provider_with_fallback` 跳过 `health_check`),直接返回云端档位,降低探测开销与不一致
- 单实例可继续用本地档位(L1/L2/L3) + 健康度降级

### 重新评估触发条件

- 需要多实例 + 本地档位混用(此时需把健康度共享到 Redis)

---

## P1-9 audit_logs append-only:生产 DB 层加 trigger

### 背景

`audit_logs` 表记录所有敏感操作(评估创建/审批/越权尝试等),是合规审计的核心证据。应用层通过 `services/audit_service.py` 的 `log()` 只做 INSERT,不提供 UPDATE/DELETE 接口。

### 当前状态

- 应用层保证 append-only,但 DB 层无强制约束
- 拥有 DB 写权限的角色仍可 `UPDATE audit_logs SET ...` 或 `DELETE FROM audit_logs ...` 篡改/删除记录

### 决策

- 文档化**生产建议**:在 DB 层加 trigger 阻止对 `audit_logs` 表的 UPDATE / DELETE 操作
- PostgreSQL 示例 trigger(参考,需 DBA 审核后部署):

```sql
CREATE OR REPLACE FUNCTION block_audit_log_modification() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit_logs is append-only: % not allowed', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_no_update
  BEFORE UPDATE ON audit_logs
  FOR EACH ROW EXECUTE FUNCTION block_audit_log_modification();

CREATE TRIGGER audit_log_no_delete
  BEFORE DELETE ON audit_logs
  FOR EACH ROW EXECUTE FUNCTION block_audit_log_modification();

-- 如需保留 TRUNCATE 防护:
CREATE TRIGGER audit_log_no_truncate
  BEFORE TRUNCATE ON audit_logs
  FOR EACH STATEMENT EXECUTE FUNCTION block_audit_log_modification();
```

- 退役/归档场景通过**专用迁移脚本**临时禁用 trigger,迁移后立即恢复,而非保留 DELETE 权限

### 重新评估触发条件

- 生产 DB 部署时(必须评估是否落地 trigger)
- 数据保留期到期需归档时(走专用流程)

---

## 关联文档

- `docs/adr/001-im-integration.md` — IM 集成决策
- `docs/adr/002-code-repo-integration.md` — 代码仓库集成决策
- `docs/adr/003-job-queue-evolution.md` — Job Queue 演进决策
- `docs/architecture-notes.md` — 实现偏差与演进记录(FEEDBACK_COLLECT / TestContainers 等)
- `docs/scale-deployment-runbook.md` — 多实例扩容 runbook
