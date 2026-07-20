# AgentValue-AI 内部试点 Runbook

> **版本：** v1.0  
> **适用对象：** 产品经理、试点团队主管、HRBP、系统管理员  
> **目标：** 指导 1-2 个团队完成内部试点，建立“反馈 → 重评 → Prompt 优化”的闭环，收集真实数据迭代系统

---

## 一、试点目标与范围

### 1.1 目标
- 验证“录入 → AI 清洗 → 审批 → 入库”全流程在真实工作数据下的可用性。
- 验证双视角输出（员工视图建设性 / 管理视图尖锐性）的语气分离与证据引用准确率。
- 建立反馈闭环：员工申诉 → 主管复核 → 必要时重评 → Prompt 版本回归后发布。

### 1.2 范围
- **试点规模：** 1-2 个团队，每队 8-15 人。
- **周期：** 连续 4 个评估周期（建议 4 周）。
- **角色：** 试点员工、一线主管、HRBP、系统管理员各 1 名。

---

## 二、试点就绪清单（Go/No-Go）

试点启动前逐项确认，全部打勾才能 Go。

### 2.1 环境与部署
- [ ] Docker Compose 部署通过，前端 `http://localhost`、后端 `http://localhost:8000/health` 可访问。
- [ ] `backend/.env` 已配置模型档位与 API Key（试点建议混合模式：L2/L3 本地 + L0 云端）。
- [ ] `AUTH_DEMO_MODE=false`（生产配置，禁用 header 伪造身份）。
- [ ] `JWT_SECRET_KEY` 已替换为强随机值。
- [ ] 数据库已执行迁移：`alembic upgrade head`。

### 2.2 数据与账号
- [ ] 演示账号已初始化：`POST /api/v1/auth/seed-demo-users`。
- [ ] 试点员工、主管、HRBP 真实账号已创建并分配正确角色。
- [ ] 公司知识库（评分标准、价值观）已录入 `company_kb`。

### 2.3 质量基线
- [ ] 回归评估集已通过：`python -m eval.evaluate --mock`（15/15 通过）。
- [ ] 真实模型回归已跑通：`python -m eval.evaluate --tier L0`（如有 API Key）。
- [ ] 后端单元测试通过：`python -m pytest tests -q`。

### 2.4 合规与告知
- [ ] 《AI 评估知情同意书》已向试点员工签署。
- [ ] 《人工最终决策声明》已在系统与文档中体现。
- [ ] 员工已知晓查看权限、申诉渠道、数据留存策略。

### 2.5 可观测性
- [ ] Langfuse 已接入（可选），可追踪 Agent 执行轨迹。
- [ ] 审计日志可查询：`GET /api/v1/admin/audit-logs`。

---

## 三、试点执行流程

### 3.1 每周标准节奏

| 阶段 | 时间 | 动作 | 负责人 |
|---|---|---|---|
| 录入 | 周一-周五 | 员工提交日报/任务进度 | 试点员工 |
| 触发评估 | 周五 | 系统按周期触发 AI 评估，生成草稿 | 系统 |
| 主管审批 | 周五-周六 | 主管初审，可打回/编辑/驳回/提交 HR | 一线主管 |
| HR 复核 | 高风险项 | 低分或 critical 风险自动进 HR 队列 | HRBP |
| 发布 | 周日 | approved 后员工可见员工视图 | 系统 |
| 反馈收集 | 次周一 | 员工查看反馈，可申诉 | 试点员工 |

### 3.2 反馈闭环（核心）

员工对评估结果有异议时：

1. **提交申诉：** 员工在前端“反馈申诉”页提交 `appeal`，评估状态自动从 `approved`/`rejected` 回到 `manager_review`。
2. **主管复核：** 主管在待审批列表查看申诉理由，决定：
   - 维持原结论 → `approve`/`reject`
   - 重新评估 → 调用 `POST /evaluations/{id}/re-evaluate`，系统基于原始输入重新生成 AI 草稿（高风险自动路由 HR）。
3. **员工追踪：** 员工在“我的反馈与申诉记录”面板查看处理状态（`主管复核中` → `已通过`/`已驳回`）。
4. **审计留痕：** 全程操作计入审计日志，可追溯。

> 关键约束：AI 不直接产生人事决策，所有结果必须经人工审批（计划书 11.3）。

### 3.3 反馈收集机制

- **结构化收集：** 员工通过 `/feedback` 端点提交反馈（`type=feedback`），用于评估质量改进。
- **申诉收集：** `type=appeal` 的申诉记录关联评估当前状态，HRBP 可按月统计申诉率与处理时效。
- **主管侧反馈：** 每两周一次主管座谈会，记录对管理视图实用性、风险诊断准确性的定性反馈。
- **指标采集：** 试点期间跟踪下表指标，对照成功标准（计划书三）。

| 指标 | 目标 | 采集方式 |
|---|---|---|
| 员工周活跃率 | ≥ 70% | 审计日志统计录入行为 |
| 证据引用准确率 | ≥ 85% | 人工抽检 10% 评估 |
| 结构化输出合规率 | ≥ 95% | 回归评估集 |
| 申诉处理时效 | ≤ 3 个工作日 | 申诉记录时间差 |
| AI 直接决策占比 | 0% | 审计日志核验 |

---

## 四、Prompt 持续优化与变更门禁

反馈驱动的 Prompt 优化必须通过回归门禁才能发布，避免“改一处坏一片”。

### 4.1 版本管理约定
- 当前版本：`prompts/daily_evaluation.md`（头部 `版本： vX.Y`）。
- 历史快照：`prompts/versions/daily_evaluation_vX.Y.md`，每次发布前归档。
- Prompt 变更经 Git 管理，每次变更需通过回归评估集（计划书十四）。

### 4.2 变更门禁流程
1. **修改 Prompt：** 编辑 `prompts/daily_evaluation.md`，更新头部版本号。
2. **本地回归：** `python -m eval.evaluate --mock` 确认 15/15 通过。
3. **版本对比门禁：** 与上一发布版本对比，确认无回归：
   ```bash
   python -m eval.evaluate --mock --compare v0.1 --output reports/compare_v0.2.json
   ```
   - `has_regression=false` → 可发布。
   - `has_regression=true` → **禁止发布**，逐条查看 `regressions` 修复后重跑。
4. **归档：** 发布后将当前版本快照到 `prompts/versions/`。
5. **审计：** Prompt 变更记录写入 ADR（Architecture Decision Record）。

### 4.3 回归判定规则
- 用例在目标版本上由通过变为失败 → 回归。
- 分数下降超过阈值（默认 5 分）→ 回归。
- 退出码：无回归返回 0（CI 可放行），有回归返回 1（CI 阻断）。

---

## 五、回滚预案

### 5.1 评估异常回滚
- **AI 输出质量异常：** 主管 `reject` 评估，触发重评或人工兜底。
- **批量错误：** 系统管理员锁定模型档位（`POST /admin/model-switch`）降级，排查 Prompt/模型问题。

### 5.2 Prompt 回滚
- 新版本上线后若回归门禁漏判的问题暴露，立即将 `prompts/daily_evaluation.md` 还原为上一快照：
  ```bash
  cp prompts/versions/daily_evaluation_v0.1.md prompts/daily_evaluation.md
  ```
- 重启后端生效，并通过审计日志通告受影响评估。

### 5.3 服务回滚
- Docker 镜像版本化，异常时回滚到上一镜像：`docker compose down && docker compose up -d --build <prev_tag>`。
- 数据库迁移支持 downgrade：`alembic downgrade -1`（谨慎，先备份）。

---

## 六、试点退出标准

试点 4 周后，满足以下条件视为成功，可扩大推广：
- 就绪清单持续全部通过。
- 证据引用准确率 ≥ 85%（人工抽检）。
- 申诉处理时效 ≤ 3 个工作日。
- 0 起 AI 直接决策事件。
- 试点员工满意度调研（净推荐）中性偏正。

不满足则延长 1-2 周针对性改进后复评。

---

## 附录：相关接口速查

| 用途 | 方法 | 路径 |
|---|---|---|
| 提交反馈 | POST | `/api/v1/evaluations/{id}/feedback` |
| 提交申诉 | POST | `/api/v1/evaluations/{id}/appeal` |
| 重新评估 | POST | `/api/v1/evaluations/{id}/re-evaluate` |
| 查询员工反馈记录 | GET | `/api/v1/employees/{id}/feedback` |
| 查询评估反馈记录 | GET | `/api/v1/evaluations/{id}/feedback` |
| 评估审计日志 | GET | `/api/v1/evaluations/{id}/audit-logs` |
| 管理端审计日志 | GET | `/api/v1/admin/audit-logs` |
