# ADR-002:代码仓库集成方案选型(GitLab 优先)

- **状态:** 已接受(Accepted)
- **日期:** 2026-07-03
- **决策者:** 集成架构组
- **关联:** Phase 7.2 代码仓库集成

## 上下文

代码贡献是工程师评估的核心输入之一——光看日报不够,日报是员工自述,代码是客观留痕。要采集的数据维度:commit、Merge Request(等价 PR)、Code Review 评论与 approve。候选三家:GitLab、GitHub、Gitea。权衡四件事:

1. **企业内网部署**:中大型企业的代码资产绝大多数在内网自托管,数据不能出域。这是硬约束。
2. **API 丰富度**:能否覆盖 commit / MR / review 三个维度,字段是否够用。
3. **API 限制**:rate limit 与 webhook 支持情况。
4. **数据合规**:代码是公司核心资产,采集什么、不采集什么必须想清楚。

约束同 ADR-001:复用 `POST /api/v1/inputs`。`type` 是自由字符串(默认 `daily_report`),新增 `code_contribution` 类型即可,无需改 schema;`content` 上限 10000 字符,影响采集策略(见下文)。

## 决策

**首期选 GitLab 自托管,GitHub 作为云端补充(二期),Gitea 暂不排期。**

理由:

- **GitLab 是企业内网自托管的主流**。中大型企业代码资产不出域的硬约束下,GitLab CE/EE 自托管是事实标准,覆盖目标客户群最广。
- **API 全面**。`/api/v4` 覆盖 commit(`repository/commits`)、MR(`merge_requests` + `approvals`)、review(`merge_requests/:iid/notes`),三个维度字段都够,不用拼凑。
- **数据不出域**。自托管实例,webhook 与 API 调用都在内网闭环,不经过公网。
- **GitHub 二期**。GitHub 在开源与云端创业公司强,但目标客户(中大型企业)内网自托管占比低,GitHub Enterprise 自托管成本高。作为云端补充放在二期,按试点团队实际代码托管位置决定。
- **Gitea 不排期**。轻量但 API 弱(MR/review 字段不全),中大型企业用得少,投入产出比低。

## 集成方式

复用 `/api/v1/inputs`,`type=code_contribution`。两种采集模式并存,按场景选:

**模式 A:webhook 推送(实时)** —— GitLab 项目配置 webhook,`push` / `merge_request` / `note` 事件触发回调,适配层转成 `code_contribution` 输入。

**模式 B:定时拉取(兜底)** —— 每小时调 GitLab API 拉最近 commit/MR,补 webhook 丢失的事件(网络抖动、GitLab 重启等)。两种模式不互斥,webhook 为主、定时拉取为校验兜底,靠 `commit_sha` 幂等去重。

```
GitLab 自托管实例
   │
   ├─ webhook(push / merge_request / note 事件,带 X-Gitlab-Token 校验)
   │     ▼
   │   AgentValue-AI 仓库适配层
   │     │  格式转换 + 服务态鉴权(系统级 JWT / 内部 API Key)
   │     │  构造 CreateInputRequest{type=code_contribution, content=结构化摘要 JSON}
   │     ▼
   │   POST /api/v1/inputs → 护栏 → 审计 → 入库
   │
   └─ 定时拉取(每小时,/api/v4 拉最近 commit,兜底)
         ▼
       AgentValue-AI 仓库适配层 → 同上
                ▼
            评估流(按 period 聚合)
```

工程上必须说清楚的点:

- **webhook 鉴权**。GitLab 回调带 `X-Gitlab-Token`,适配层必须校验,否则任意人都能伪造 commit 事件污染评估。
- **`/inputs` 的服务态鉴权**。同 ADR-001,适配层调 `/inputs` 需系统级凭证(`/inputs` 有 `require_role`,webhook 无员工身份),不能裸调。
- **`content` 上限 10000 字符**。一个工程师一周的 commit 摘要可能超限,适配层需按 period 聚合 + 截断/分条,不能整把塞进去。
- **author 邮箱 → employee_id 映射**。GitLab commit author email 不等于 AgentValue-AI employee_id,需映射表;多账号、企业邮箱与个人邮箱混用要单独合并到同一 employee_id。

## 隐私考虑(重要)

**只采集 commit message + 文件变更统计,不采集代码内容(diff / 源码)。**

这条是硬线,理由:

1. **代码是公司核心资产**,进入评估系统的 LLM 上下文就有泄露面——LLM 调用链一旦外泄或日志被扒,源码就跟着出去了。评估系统没必要背这个风险。
2. **评估关心的是"贡献量与协作行为"**,不是"代码质量本身"。代码质量应由 Code Review 工具(sonarqube 之类)和人工 review 判断,不进员工评估 LLM。
3. **可解释性**。commit message + 文件变更统计(新增/删除行数、改动文件数、文件类型)足以支撑"做了什么、改动规模"的评估叙事,且对员工可解释、可申诉。源码内容进了评估反而说不清,申诉都没法对账。

具体采集字段(写入 `content`,结构化 JSON):

| 字段 | 说明 | 示例 |
|---|---|---|
| commit_sha | 提交哈希 | `a1b2c3d` |
| commit_message | 提交信息(首行 + 正文截断) | `feat: 新增日报导出` |
| author_email | 提交者邮箱(用于映射) | `zhang@company.com` |
| files_changed | 改动文件数 | `5` |
| additions / deletions | 增删行数 | `+120 / -30` |
| file_types | 文件类型分布 | `{"py": 3, "md": 2}` |

**明确不采集**:diff 内容、源码片段、文件完整路径(只采路径统计与类型分布)。

## 后果

**正面:**

- 自托管 GitLab 数据不出域,合规风险最低。
- 代码贡献闭环用最小改动跑通,主链路复用。
- 隐私边界清晰(只采元数据),员工可解释、审计可追溯。

**负面 / 需接受:**

- 首期只支持 GitLab。GitHub 为主的团队(云端开源协作型)需等二期。
- **GitLab API rate limit 需处理**。自托管默认约 600 req/min/IP,大型仓库全量回填会触限。适配层需做分页 + 退避 + 增量拉取(记 `last_seen_commit`),不能一把梭。
- author 邮箱 → employee_id 映射是脏活,需要 HR 侧维护映射表,多账号场景要兜底合并。
- webhook 与定时拉取并存,重复去重压力靠 `commit_sha` 幂等,适配层需保证。

## 备选方案

| 方案 | 评估 | 不选的原因 |
|---|---|---|
| **GitHub** | 云端主流,API 成熟,webhook 稳 | 企业内网自托管占比低(GitHub Enterprise 成本高);目标客户群匹配差;作为二期云端补充 |
| **Gitea** | 轻量、自托管友好 | API 弱(MR/review 字段不全),中大型企业用得少,采集维度不够 |
| **手动导入 CSV** | 零开发成本 | 不可持续,每周人工导出导入,落地必塌;仅作无人对接时的临时兜底 |
| **多仓库平台同时上** | 覆盖最广 | 首期铺多套适配层,鉴权/去重/映射都要做多份;先验证 GitLab 再扩 |
