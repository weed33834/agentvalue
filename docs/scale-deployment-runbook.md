# AgentValue-AI 规模化部署 Runbook

> **版本：** v1.0
> **编写日期：** 2026-07-03
> **适用对象：** 部署架构师、SRE、HRIS 管理员、各 BU 的推广负责人
> **前置文档：** [pilot-runbook.md](pilot-runbook.md)、[deployment-guide.md](deployment-guide.md)、[architecture-notes.md](architecture-notes.md)
> **目标：** 把试点里跑通的东西，按可控节奏扩到 3-5 个团队、再到整个 BU、再到全公司，过程中不把"评估质量"和"员工信任"这两样最贵的东西赔掉。

---

## 一、规模化前提

这份 Runbook 不是"想扩就能扩"的许可状。能往下走，是因为前面几件事已经被证明了：

- **试点已通过退出标准**：[pilot-runbook.md](pilot-runbook.md) 第六节那几条（证据准确率 ≥85%、申诉时效 ≤3 工作日、0 起 AI 直接决策）在真实团队里跑满 4 周达标，而不是在 Mock 数据上自娱自乐。
- **5 个规模档位都验证过可用**：从十几人的初创团队，到阿里/腾讯/谷歌这种十万级体量，部署拓扑和评估链路都演练过。规模化不是赌一把"大公司能不能跑"，而是"在已经验证过的档位之间平滑放大"。
- **反馈闭环已经在运转**：申诉 → 主管复核 → 必要时重评 → Prompt 回归 → 发布，这条链路在试点期已经形成肌肉记忆。规模化的本质是把这套肌肉复制到更多团队，而不是临时搭一套。

**三波推广节奏**（详见第六节）：

| 波次 | 范围 | 观察期 | 报表频率 | 风险预算 |
|---|---|---|---|---|
| 第一波 | 单团队 → 3-5 个团队（同公司、同 BU 内） | 2 周 | 周报 | 低，出了问题回退到单团队成本可控 |
| 第二波 | 跨 BU / 跨公司主体 | 4 周 | 双周报 | 中，数据隔离和权限矩阵是主要风险点 |
| 第三波 | 全量推广 | 持续 | 月报 | 高，重在公平性指标和 SLA 守住 |

> 三波之间不是按"日历"推进，是按"指标"推进。第一波没过退出标准就硬上第二波，等于把试点期的债放大十倍。这点在第六节回滚机制里会反复强调。

---

## 二、多团队数据隔离

`tenant_id` 字段已通过 alembic 迁移落库，多团队数据隔离以 `tenant_id` 为权威边界。早期的 employee_id 前缀编码过渡方案已退役（仅作历史数据兼容），新数据一律以 `tenant_id` 字段为准。这不是技术债的妥协——隔离边界现在有数据库层面的强约束兜底。

四层隔离，缺一不可：

### 2.1 数据库层：tenant_id 字段隔离公司/BU

`tenant_id` 字段已落库，每条员工/评估记录按租户归属打上 `tenant_id`。employee_id 仍沿用 `{TENANT}-E{序号}` 的展示格式以便人工识别，但权威边界是 `tenant_id` 字段：

| tenant_id | 含义 | 示例 employee_id |
|---|---|---|
| `ALI` | 阿里主体 | `ALI-E001`、`ALI-E042` |
| `TC` | 腾讯主体 | `TC-E001` |
| `GG` | 谷歌主体 | `GG-E007` |
| `STARTUP-A` | 初创团队 A | `STARTUP-A-E001` |

**查询约定**：所有按团队/BU 聚合的查询，必须在 WHERE 条件里带 `tenant_id = :tenant_id`（或 `tenant_id IN (...)`）。这个约束靠 code review 守，CI 里加一条 lint 规则：跨租户查询（不带 `tenant_id` 过滤的 SELECT）直接报错。

> `tenant_id` 字段提供数据库层面的强约束，相比退役的前缀方案，隔离不再只靠约定兜底。早期前缀编码的历史数据已通过迁移回填 `tenant_id`，前缀仅作展示兼容。`tenant_id` 值表归 admin 维护，新增走变更流程，**禁止任何团队私自定义新 tenant_id**。

### 2.2 向量库层：按 tenant 分 collection

ChromaDB 这边用 collection 名做隔离边界，一个 tenant 一个 collection：

```python
# backend/agent/memory.py（示意，实际接入在 Phase 6.2/10.3）
def get_collection(chroma_client, tenant_id: str):
    # collection 名固定前缀 agentvalue_，避免和别的业务撞名
    return chroma_client.get_or_create_collection(
        name=f"agentvalue_{tenant_id}",
        embedding_function=embedding_fn,
    )
```

**为什么用 collection 而不是 metadata 过滤**：collection 是物理隔离，租户间检索互不影响，也不会因为某个租户数据暴涨拖慢别人的召回。代价是 collection 数量上去后元数据管理变重——但我们场景里租户数量是有限的（几十个 BU 级别），不是 SaaS 那种几万租户，这个代价可以接受。

> 如果未来租户数量过千，再回头评估 metadata 过滤 + 单 collection 的方案。现在不预先优化。

### 2.3 文件存储层：按 tenant 分 bucket prefix

附件上传走 `AttachmentStorage` 抽象（Phase 7.3 已落地），S3 key 统一加租户前缀：

```
s3://{bucket}/{tenant_id}/{evaluation_id}/{filename}
# 例: s3://agentvalue-attachments/ALI/eval-2026-07-001/screenshot_01.png
```

**签名 URL 的范围**：生成预签名 URL 时，policy 必须把 key 前缀限定在 `{tenant_id}/` 下，禁止生成跨租户的下载链接。这条在 `tests/test_storage.py` 的路径遍历用例基础上，规模化阶段补一条"跨租户访问拒绝"用例。

> 不按租户开独立 bucket，是因为 MinIO 集群下 bucket 数量过多会让分布式一致性的开销上来。前缀隔离 + 签名 URL policy 是更轻的选择。

### 2.4 配置层：每 tenant 独立的 MODEL_TIER / Prompt 版本

不同团队的数据敏感度、预算、对延迟的容忍度都不一样。给每个 tenant 一份独立配置，支持 A/B 测试：

```yaml
# config/tenants/ALI.yaml
tenant_id: ALI
model_tier: L2            # 标准，本地推理为主
prompt_version: v1.0      # 跑得稳的版本
ab_test:
  enabled: true
  candidate_version: v1.0 # 50% 流量灰度（如需 A/B 新版本,先在 prompts/versions/ 归档新版本再灰度）
  ramp: 0.5
```

**为什么允许 tenant 之间 Prompt 版本不一致**：这其实是规模化的好处之一——激进团队可以先吃新版本的灰度，保守团队停在 v1.0，互不拖累。但反过来，**版本不能完全碎片化**：我们要求每个 tenant 的活跃 Prompt 版本不超过 2 个（一个 stable + 一个灰度 candidate），不然可观测性和审计会乱套。

---

## 三、权限矩阵

权限不是按"角色"一张表能讲完的，得把角色、操作、数据范围三维交叉。下面这张表是基准，多团队特殊场景在 3.2 单列。

### 3.1 基准权限矩阵

| 角色 \ 数据范围 | 自己 | 本团队 | 本部门 | 全公司 |
|---|---|---|---|---|
| **employee（员工）** | 查看 ✓ | ✗ | ✗ | ✗ |
| | 申诉 ✓ | | | |
| | 提交反馈 ✓ | | | |
| **manager（主管）** | 查看 ✓ | 查看 ✓ | 查看 ✓（仅汇总，不展开个人） | ✗ |
| | 申诉 ✓ | 评估 ✓（复核/打回/驳回） | ✗ | |
| | | 审批 ✓ | | |
| | | 申诉处理 ✓ | | |
| **hr（HRBP）** | 查看 ✓ | 查看 ✓（高风险） | 查看 ✓ | 查看 ✓（汇总） |
| | 申诉 ✓ | 审批 ✓（高风险复核） | 审批 ✓（高风险复核） | ✗ |
| | | 申诉处理 ✓ | | |
| | | 公平性审计 ✓ | 公平性审计 ✓ | 公平性审计 ✓ |
| **admin（管理员）** | 查看 ✓ | 查看 ✓ | 查看 ✓ | 查看 ✓ |
| | 申诉 ✓ | 配置 ✓ | 配置 ✓ | 配置 ✓ |
| | | 模型档位管理 ✓ | 模型档位管理 ✓ | 模型档位管理 ✓ |
| | | 审计日志 ✓ | 审计日志 ✓ | 审计日志 ✓ |

**几条硬约束（和试点期一致，不因规模化放宽）**：

- AI 不直接产生人事决策，所有评估必须经人工审批。任何角色都没有"AI 直出即生效"的权限。
- 员工视图（`employee_view`）只有员工本人 + 其审批链可见；管理视图（`manager_view`）不向员工暴露。这条在 RBAC 里是字段级控制，不是页面级。
- 跨数据范围的访问必须留审计日志，admin 也不能无痕查任意员工评估。

### 3.2 多团队特殊场景

矩阵的基准是"单线汇报 + 单一团队"假设，规模化后必然碰到矩阵式管理，得专门处理：

**场景 A：双线汇报员工**

实线主管 + 虚线项目经理都要能查看该员工的评估，但权限不对等：

| 角色 | 查看权限 | 评估权限 | 申诉处理权限 |
|---|---|---|---|
| 实线主管 | 全量 | ✓（复核/打回/驳回/提交 HR） | ✓ |
| 虚线项目经理 | 仅本项目中该员工的相关评估 | 仅"打回要求重评"（不能直接 approve） | ✗（只能转交实线主管） |

**实现**：在 employee 的汇报关系里增加 `primary_manager_id` + `secondary_managers: List`，RBAC 查询时对 secondary 放宽查看、收紧审批。secondary 的"打回"动作走 `re-evaluate` 端点但状态不进 `approved`，必须回流实线主管。

**场景 B：借调员工**

借调期间，原团队主管 + 借调团队主管都需可见：

| 阶段 | 原团队主管 | 借调团队主管 |
|---|---|---|
| 借调期间 | 查看 ✓（关注长期表现） | 查看 ✓ + 评估 ✓（日常复核归借调团队） |
| 借调结束 | 评估权限恢复 ✓ | 查看权限回收（历史评估归档可查，新评估不可见） |

**实现**：借调关系记录为 `secondment(employee_id, from_team, to_team, start, end)`，RBAC 按当前日期判断归属。借调结束后，借调团队主管对该员工的"新评估"查看权限自动失效，但历史评估作为只读归档保留——这点是劳动合规要求，不能为了清爽就删掉。

> 这两个场景在试点期没碰到（试点是单一团队），是规模化才冒出来的。建议第二波推广前先在沙箱里把这两个场景的角色切换测一遍，别等真上线了才发现 RBAC 查询漏了 secondary_manager。

---

## 四、部署拓扑

按团队规模分四档，每档给关键配置片段。**不要在低档位硬塞高档位的复杂度**——200 人的团队上 k8s HPA 是给自己找麻烦。档位选择参考 [deployment-guide.md](deployment-guide.md) 第一节的部署模式表，这里按"规模"维度再切一刀。

### 4.1 单实例（≤200 人）

适用：初创团队、单 BU 试点。最简拓扑，能跑就行。

```
1 backend (FastAPI) + 1 frontend (Nginx 静态) + SQLite + 内存队列
```

```yaml
# docker-compose.simple.yml（关键片段）
services:
  backend:
    build: ./backend
    environment:
      - DATABASE_URL=sqlite+aiosqlite:///./data/agentvalue.db
      - REDIS_URL=  # 留空/不配 = 内存队列
      - MODEL_TIER=L2
    ports: ["8000:8000"]
  frontend:
    build: ./frontend
    ports: ["80:80"]
    depends_on: [backend]
```

> 内存队列 + SQLite 的组合在试点 Runbook 里就用过，规模 ≤200 时队列积压基本不会发生。但记得：SQLite 的写并发是瓶颈，如果一周一次批量评估同时进来几十条，加个 `WAL` 模式（`PRAGMA journal_mode=WAL`）能缓解。再大就该升档了。

### 4.2 小集群（200-2000 人）

适用：中型 BU、单公司主体。引入持久化中间件。

```
1 backend + 1 frontend + PostgreSQL + Redis + MinIO
```

```yaml
# docker-compose.small.yml（关键片段，相对 prod.yml 简化）
services:
  backend:
    build: ./backend
    environment:
      - DATABASE_URL=postgresql+asyncpg://agentvalue:***@postgres:5432/agentvalue
      - REDIS_URL=redis://redis:6379/0  # 配值 = Redis 队列
      - S3_ENDPOINT=http://minio:9000
      - S3_BUCKET=agentvalue-attachments
    depends_on: [postgres, redis, minio]
  postgres:
    image: postgres:16
    volumes: ["pg_data:/var/lib/postgresql/data"]
  redis:
    image: redis:7
  minio:
    image: minio/minio
    command: server /data
volumes:
  pg_data:
```

**取舍**：这档 backend 还是单实例，靠 Redis 队列扛住异步评估的削峰。单实例的风险是后端挂了就全停，但 200-2000 人规模下，一个能跑的后端 + 健康检查 + 自动重启，可用性够用。要上多副本得连 interrupt 工作流的内存态问题一起解决（见 [architecture-notes.md](architecture-notes.md) 技术债表），现在不值得为它上 LB。

### 4.3 中集群（2000-10000 人）

适用：大型公司主体。开始上多副本和主从。

```
2 backend (LB) + 1 frontend (CDN) + PostgreSQL (主从) + Redis (哨兵) + MinIO (集群)
```

```yaml
# docker-compose.mid.yml（关键片段）
services:
  backend:
    build: ./backend
    deploy:
      replicas: 2
    environment:
      - DATABASE_URL=postgresql+asyncpg://agentvalue:***@pg-primary:5432/agentvalue
      - DATABASE_REPLICA_URL=postgresql+asyncpg://agentvalue:***@pg-replica:5432/agentvalue
      - REDIS_URL=redis://redis-sentinel:26379/0  # 哨兵模式，配值 = Redis 队列
  pg-primary:
    image: postgres:16
  pg-replica:
    image: postgres:16
    environment:
      - PRIMARY_HOST=pg-primary
  redis-sentinel:
    image: redis:7
    command: redis-sentinel /etc/redis/sentinel.conf
```

**取舍**：这档引入主从读写分离——写走主库，读（查询评估历史、统计报表）走从库。但 LangGraph 的 interrupt 工作流（演示性质，见架构说明）在这档仍然只能跑单实例，所以审批流走的是数据库状态机版本 `create_evaluation_graph`，不是 interrupt 版本。这个取舍要写进部署文档的"已知限制"。

### 4.4 大集群（10000+ 人）

适用：阿里/腾讯/谷歌级。上 k8s。

```yaml
# k8s/backend-deployment.yaml（关键片段）
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agentvalue-backend
spec:
  replicas: 3
  template:
    spec:
      containers:
        - name: backend
          image: agentvalue-backend:v1.0.0
          resources:
            requests: { cpu: "1", memory: "2Gi" }
            limits: { cpu: "2", memory: "4Gi" }
          readinessProbe:
            httpGet: { path: /health, port: 8000 }
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: agentvalue-backend-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: agentvalue-backend
  minReplicas: 3
  maxReplicas: 20
  metrics:
    - type: Resource
      resource: { name: cpu, target: { type: Utilization, averageUtilization: 70 } }
```

```yaml
# k8s/postgres-statefulset.yaml（读写分离关键片段）
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: postgres
spec:
  replicas: 2  # 1 主 1 从，主挂自动提升从
  serviceName: postgres-headless
```

**配套中间件**：PostgreSQL 读写分离（主写从读，CloudNativePG 或 Patroni 管理故障转移）、Redis Cluster（至少 3 主 3 从）、MinIO 分布式（4 节点起，纠删码）。

**取舍**：大集群下公平性审计的跨租户聚合查询会变重，建议把公平性报表的 ETL 跑在只读从库上，别和在线评估抢主库资源。这是 10000+ 规模才值得做的优化，前面三档不用管。

---

## 五、培训材料清单

培训不是"发了文档就行"，得按角色配不同时长、不同深度的材料。原则是**短而准**——员工没耐心看 60 分钟视频，管理员也不能只看 15 分钟就上手。

### 5.1 员工培训（15 分钟视频）

**目标**：让员工会用系统、看得懂自己的评估、知道怎么申诉。不教原理，只教动作。

大纲：
1. （3 分钟）为什么要做这个评估——AI 只是辅助，最终决策是人，不是给你打标签
2. （4 分钟）怎么写一份有效的日报——结构化、带证据、避免流水账；正反例对比
3. （4 分钟）怎么看自己的评估——员工视图三个板块怎么读，growth_areas 怎么用来改进
4. （4 分钟）怎么申诉——什么情况该申诉、申诉不是投诉、申诉后会发生什么（状态机流转）

### 5.2 主管培训（30 分钟视频）

**目标**：让主管会复核、能识别评估偏差、会处理申诉。这层最关键，主管用不好，整个系统的信任链就断了。

大纲：
1. （6 分钟）主管在系统里的角色——复核不是橡皮图章，你的判断权重 > AI
2. （8 分钟）如何复核——证据是否真的来自员工输入、分数和证据是否匹配、双视角有没有串
3. （8 分钟）如何避免评估偏差——锚定效应、近因效应、晕轮效应在 AI 草稿里的体现，主管怎么纠偏
4. （8 分钟）如何处理申诉——先看申诉理由再决定维持/重评、申诉记录要留痕、转 HR 的触发条件

### 5.3 HR 培训（45 分钟视频）

**目标**：让 HRBP 会审计、会处理高风险评估、能读懂公平性报告。HR 是合规底线。

大纲：
1. （10 分钟）HR 在系统里的角色——高风险评估的复核者、公平性的守门人、不是评分参与者
2. （10 分钟）如何审计——审计日志查什么、水印防截图怎么用、跨租户访问怎么核验
3. （12 分钟）如何处理高风险评估——critical 风险标记的判定、risk_flags 解读、HR 复核后的处置选项（不含直接人事决定）
4. （13 分钟）公平性报告解读——部门间 max_gap 怎么看、性别/职级差异趋势怎么看、阈值告警怎么响应

### 5.4 管理员培训（60 分钟实操）

**目标**：让管理员能独立部署、配置、监控、排障。这层不上手练不行，纯视频学不会。

大纲：
1. （15 分钟）部署——按规模选档位、docker-compose / k8s 部署、迁移 alembic upgrade head、健康检查
2. （15 分钟）配置——.env 各项含义、tenant 配置文件、MODEL_TIER 切换、Prompt 版本与 A/B 测试
3. （15 分钟）监控——Prometheus 指标解读、Grafana 看板、告警规则配置、队列积压排查
4. （15 分钟）故障排查——评估卡住怎么定位、LLM 调用失败怎么降级、Prompt 回滚实操、数据库 downgrade 的风险

> 培训材料只给大纲，不写完整脚本——脚本会让培训变成念稿，反而僵化。每个大纲点配 2-3 个真实试点案例做引子，培训师按团队情况临场展开。所有培训材料在第一波推广前必须出齐，不允许"边推广边补培训"。

---

## 六、推广节奏与风险控制

三波推广，每波之间是"指标门禁"不是"时间门禁"。

### 6.1 第一波：3-5 个团队（同公司/同 BU）

- **范围**：从试点单团队扩到 3-5 个团队，控制在同一公司主体或同一 BU 内，避免一上来就碰跨主体隔离。
- **观察期**：2 周。覆盖两个完整的评估周期。
- **报表**：每周一出周报，对比试点期的基线。
- **准入条件**：试点退出标准全部达标（见第一节）+ 本 Runbook 第二/三节的隔离与权限在沙箱测过。
- **通过条件**（进第二波的门槛）：
  - 评估质量（证据准确率）相对试点基线下降 ≤5%
  - 申诉率 ≤10%
  - SLA 达成率（评估按时出草稿 + 申诉按时响应）≥90%

### 6.2 第二波：扩展到 1 个 BU / 跨公司主体

- **范围**：从几个团队扩到一个完整 BU，或跨到另一个公司主体（这时候 tenant 隔离真正受考验）。
- **观察期**：4 周。跨主体隔离的 bug 通常在第二个评估周期才暴露，2 周不够。
- **报表**：双周报。重点跟踪跨租户的数据访问审计日志。
- **新增风险点**：双线汇报 / 借调员工的权限（3.2 节场景）在这波首次实战，务必在周报里单列"矩阵管理场景异常"。

### 6.3 第三波：全量推广

- **范围**：全公司所有团队。
- **观察期**：持续，不设结束。转为常态化运营。
- **报表**：月报。重点从"功能可用"转向"公平性长期趋势"和"成本管控"。

### 6.4 回滚机制（强制）

任一波次出现以下任一情况，**立即暂停推广，回滚到上一波的状态**，不允许"再观察几天看看":

| 触发条件 | 阈值 | 回滚动作 |
|---|---|---|
| 评估质量下降 | 证据准确率相对基线下降 >10% | 冻结当前波次新增团队，已上线团队回退到上一稳定 Prompt 版本 |
| 申诉率飙升 | 申诉率 >15% | 暂停推广，排查是否 Prompt 改动或权限误配导致 |
| SLA 达成率塌方 | <80% | 立即降级 MODEL_TIER（如 L2→L1），同时冻结推广 |
| 公平性红线 | 部门间 max_gap >15 分或出现性别/职级系统性偏差 | 全量暂停，HR 介入，公平性审计报告提交决策层 |

**回滚不是失败，是机制设计的正常出口**。试点 Runbook 第五节已有 Prompt 回滚和服务回滚的具体命令，这里沿用，不重复。强调一点：回滚决策权在推广负责人 + HRBP 双签，admin 单方面不能决定"继续扛着"。

---

## 七、监控指标看板

监控分三类，业务、系统、公平性，分别回答"用得怎么样""跑得稳不稳""公不公平"三个问题。指标定义对齐 [pilot-runbook.md](pilot-runbook.md) 第三节，这里给规模化后的目标值和告警阈值。

### 7.1 业务指标

| 指标 | 定义 | 目标值 | 告警阈值 |
|---|---|---|---|
| 周活跃率 | 本周提交日报的员工 / 在册员工 | ≥70% | <50% 持续 2 周 |
| 评估数 | 本周生成的评估草稿数 | 在册员工数 × 90% | 异常波动 ±30% |
| 证据准确率 | 人工抽检 10% 评估中证据可溯源的比例 | ≥85% | <80% |
| 申诉率 | 申诉评估数 / 已发布评估数 | ≤10% | >15%（触发回滚） |
| 通过率 | 主管 approve 的评估 / 总评估 | 70-90%（异常高/低都该查） | >95% 或 <60% |
| SLA 达成率 | 按时出草稿 + 申诉按时响应的比例 | ≥90% | <80%（触发回滚） |

### 7.2 系统指标

| 指标 | 定义 | 目标值 | 告警阈值 |
|---|---|---|---|
| 评估耗时 P50 | 评估图从触发到出草稿的中位数 | <3s | — |
| 评估耗时 P95 | 95 分位 | <8s | >12s |
| 评估耗时 P99 | 99 分位 | <15s | >25s |
| 模型调用成功率 | LLM 调用成功 / 总调用 | ≥99% | <95% |
| 队列积压 | Redis 队列未消费任务数 | <50 | >200 持续 5 分钟 |
| 错误率 | 5xx 响应 / 总请求 | <0.5% | >2% |

> 系统指标走 Prometheus + Grafana。后端 `/metrics` 已挂载埋点（`backend/core/metrics.py`），Grafana Dashboard JSON 与 Prometheus 数据源 provisioning 已落地在 `grafana/` 目录（含评估吞吐/耗时、审批流转、反馈计数四个 panel），`docker-compose.prod.yml` 已内置 grafana 服务，`monitoring/alerts.yml` 已落地 3 条告警规则。规模化前确认 grafana 服务正常拉起、告警规则生效即可。

### 7.3 公平性指标

| 指标 | 定义 | 目标值 | 告警阈值 |
|---|---|---|---|
| 部门间 max_gap | 任意两部门平均分差的最大值 | <8 分 | >15 分（触发回滚） |
| 性别差异 | 同职级男女平均分差趋势 | 趋势平稳，无系统性偏离 | 单方向连续 3 周扩大 |
| 职级差异 | 同部门不同职级平均分差趋势 | 符合职级分布预期 | 异常倒挂（低职级系统性高于高职级） |

公平性指标由 `scripts/fairness_audit.py`（Phase 5 已落地）周期性产出，规模化阶段建议每评估周期跑一次，月报里呈现趋势曲线。**公平性指标的解读权在 HR，不在工程团队**——工程只负责数据准，判定是否"系统性偏差"是 HR 的专业判断。

---

## 附录：与试点 Runbook 的衔接

本文档不重复 [pilot-runbook.md](pilot-runbook.md) 的内容，只处理"从 1 到 N"的部分。两者关系：

- 试点 Runbook 管"0 到 1"——就绪检查、4 周节奏、反馈闭环、Prompt 门禁、回滚预案、退出标准。
- 本 Runbook 管"1 到 N"——多团队隔离、权限矩阵、规模档位、培训、三波推广、规模化监控。
- 公用的部分（Prompt 变更门禁、回滚命令、审计日志查询）交叉引用，不复制。

> 规模化最容易被低估的不是技术，是"信任的复制"。试点团队信任系统是因为他们参与了打磨；新团队没有这层信任，靠的是前两波的指标公开 + 培训到位 + 回滚可见。把这三件事做实，比堆多少技术细节都管用。
