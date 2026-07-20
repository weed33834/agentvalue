# 开发规范

AgentValue-AI 贡献者每次动手都要遵守的硬性要求。文档不长,但每一条都是踩过坑换来的——
文档和代码脱节、远程有 PR 没人看、提交里混进密钥、注释写得像机器生成,这些低级错误
用一份 checklist 兜住就够了。

> 首次贡献请先读 [CONTRIBUTING.md](../CONTRIBUTING.md) 了解 fork → PR 的完整流程。
> 本文只讲动手时的代码与提交规范。

---

## 1. 提交前自检清单

每次 `git commit` 前逐项过一遍,不达标不提交。`pre-commit` hooks 会自动跑前两项,但剩下的需要自己把关。

### 代码质量

```bash
cd backend && python -m pytest tests --ignore=tests/e2e --ignore=tests/perf -q
cd backend && python -m pytest tests/e2e -q
cd frontend && npm run build
cd backend && python -m eval.evaluate --mock --compare v0.1   # 改动涉及 Prompt 时
```

- [ ] 上述命令全部 exit 0
- [ ] 无新增 skip / xfail,无被注释掉的测试
- [ ] 新功能有对应测试覆盖

### 安全

- [ ] `git diff --cached | grep -iE "api_key|secret|password|token|ghp_|sk-"` 无命中
- [ ] `.env` / `*.token` / `*.key` / `*.pem` / `secrets/` 没被 `git add` 进来
- [ ] 未引入新的弱默认凭据,未放宽生产守护(若不确定,跑 `python scripts/check_prod_readiness.py`)

### 文档一致性

- [ ] 改了功能就改对应文档,文档不能滞后于代码
- [ ] 引用的文件路径真实存在
- [ ] CHANGELOG.md 的记录与实际代码状态对得上

### 去 AI 感

- [ ] 没有机械化主语("本系统/本模块/本文件"作陈述句主语)
- [ ] 没有标签式注释("功能:xxx" "说明:xxx")
- [ ] 没有 emoji 装饰(代码注释里;文档里除 checkbox 的 `[x]/[ ]` 外不放 emoji)
- [ ] 没有"总而言之/综上所述/强大的/完善的"这类套话和过度形容词
- [ ] 注释只写"为什么"不写"是什么",简单函数不写 docstring

---

## 2. 提交规范

遵循 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/v1.0.0/):

```
<type>: <简述>

<可选正文,说明改了什么、为什么>
```

`type` 取 `feat / fix / docs / refactor / test / chore / perf / ci`。一次提交一个逻辑单元,
不要把不相关的东西混在一起。PR 的标题与首个 commit 的首行建议保持一致。

---

## 3. 分支策略

社区贡献者通过 fork + feature 分支 + PR 提交改动,具体流程见 [CONTRIBUTING.md](../CONTRIBUTING.md)。

- 始终从最新 `main` 切分支,命名 `feat/xxx` / `fix/xxx` / `docs/xxx`
- 临时分支用完立刻删,不要在 fork 中留 stale 分支
- 主仓库的 `main` 分支不接受直接推送,只能通过 PR 合并

---

## 4. 变更日志维护

`CHANGELOG.md` 是活文档,遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

- 完成功能/修复:在 `## [Unreleased]` 段对应分组(Added/Changed/Fixed/Removed/Security)追加条目
- 发布版本:把 `## [Unreleased]` 改为 `## [vX.Y.Z] - YYYY-MM-DD`,新建空 Unreleased 段
- 记录前必须跑通相关测试,确保"能运行、能跑通、测试无误"

---

## 5. 密钥管理

**绝不**把密钥、token、`.env` 文件、`*.pem` 等加入 commit。

- `.gitignore` 已覆盖 `*.token / *.key / *.pem / .env / .env.* / secrets/`
- `.env.example` 中的占位值(`your-*-key` / `change-this-...`)是给使用者的示例,**不是**可用凭据
- 复制示例代码时,确认占位值已替换为真实地址或再次脱敏
- pre-commit 已配置 `check-added-large-files`,大文件不会误入;但密钥扫描需要贡献者自己把关
- 若历史已误提交密钥,请按 [SECURITY.md](../SECURITY.md) 流程报告,不要直接 `git push -f` 覆盖

---

## 6. Prompt 改动规范

评估 Prompt 是本项目的核心资产,改动需额外谨慎:

- 每次改动 `backend/prompts/daily_evaluation.md` 时,同步在 `backend/prompts/versions/` 归档新版本快照
- 跑 `python -m eval.evaluate --mock --compare <上一版本>`,exit 0 才能合并
- 三视图(员工 / 主管 / 审计)措辞分离是核心设计,改动时务必保持
- "AI 不做人事决策"是硬约束,Prompt 不能引导 LLM 直接给出最终结论

详见 [Prompt 工程规范](prompt-engineering-spec-v1.md)。

---

## 7. 测试规范

- 测试与生产代码同 PR 提交,不分开
- `backend/tests/` 放单测,`backend/tests/e2e/` 放基于 TestClient 的 API 级联调(无需浏览器)
- `backend/tests/perf/` 放 locust 性能测试,默认不纳入常规跑批
- mock 优先用 `unittest.mock` / pytest fixture,不要在测试里起真实外部服务
- 测试数据用 `backend/data/pilot/` 下的合成数据(详见该目录 README),不要复制真实员工信息

---

## 8. 国际化

- 代码注释与文档默认中文
- 面向国际社区的元数据(README 顶部徽章、commit message 的 type 前缀、CI job 名)用英文
- 评估输出三视图的中文措辞是产品定位的一部分,目前不计划做 i18n
