# ADR-003:任务队列演进选型(arq 优先,当前保留裸 redis.asyncio)

- **状态:** 已接受(Accepted)
- **日期:** 2026-07-04
- **决策者:** 后端架构组
- **关联:** Phase 6.1 任务队列演进

## 上下文

Phase 6.1 把 `api/routes.py` 的模块级 `job_store: Dict` 抽成了 `JobQueue`,提供 InMemory(测试/本地)与 Redis(多实例生产)两套实现,解除了"评估任务状态只能单实例可见"的约束。但当前 `RedisJobQueue` 的实现是**裸 `redis.asyncio`**:把每个 job 序列化成 JSON 存到 `agentvalue:job:{job_id}` 这个 key,本质是**共享状态存储**,不是真正的任务队列——没有独立 worker、没有消费组、没有失败重投。

这埋着一个隐患:评估任务由创建它的那个 API 进程在后台跑(`BackgroundTasks`),进程崩了任务就卡死,pending/running 状态烂在 Redis 里没人接手。单实例下不是问题,但规模化部署(见 [scale-deployment-runbook.md](../scale-deployment-runbook.md))按租户隔离多实例后,这个隐患会浮现。

要决定的是:真到需要多 worker 横向扩展时,任务队列底座选什么。候选四个:arq、Redis Streams(裸用)、Celery、dramatiq。

约束:

1. **必须 async**。FastAPI 全异步,LLM 调用是 I/O 密集(等模型响应),队列底座不能逼着把异步链路退化成同步线程池。
2. **复用现有 Redis**。docker-compose 已经起了 redis,不引入新中间件(RabbitMQ/Kafka 一律不考虑)。
3. **改动局部化**。`JobQueue` 抽象已经把队列语义收口,换底座不该动 `routes.py` / `evaluation_service.py`。
4. **可观测**。任务积压、失败率、处理耗时能接 Prometheus(现有 `core/metrics.py` 已经埋了 `set_active_jobs`)。

## 决策

**演进方向选 arq;当前阶段保留裸 `redis.asyncio` 实现,不立即切换。**

切换时机:当 `scale-deployment-runbook.md` 的第二波(跨 BU 多实例)实际落地、或出现"实例崩溃导致任务卡死"故障时,按本 ADR 的实现路径切 arq。

### 为什么是 arq

- **async 原生**。arq 用 `asyncio` 写 worker 函数,和 FastAPI 的异步评估链路无缝衔接,不用 `run_in_executor` 桥接同步任务。
- **复用 redis-py**。arq 底层就是 redis-py,依赖树不膨胀,连接池和现有 `RedisJobQueue` 共享一套 redis 配置。
- **worker 进程模型清晰**。`arq Worker` 独立进程消费队列,API 进程只管 enqueue,职责切开——这正是当前实现缺的"独立 worker"。
- **自带重试与结果**。`enqueue_job` 原生支持 `max_retries` / `retry_delay` / 结果回写,当前裸实现要自己写。
- **cron job 支持**。试点阶段的定时拉取兜底(ADR-002 模式 B)、公平性审计月跑、SLA 监控都能用 arq cron 统一,不用各起一个 crontab。
- **轻量**。对比 Celery,arq 没有历史包袱(beat / 复杂路由 / broker 抽象),代码量小,够用。

### 为什么不立即切

- **当前规模不需要**。第一波推广是单实例/租户,任务由创建进程跑完即可,裸 redis 共享状态足够。提前切 arq 要多运维一个 worker 进程,收益不抵成本。
- **`JobQueue` 抽象已经解耦**。换底座只需加一个 `ArqJobQueue` 实现 + 工厂分支,`routes.py` 与 `evaluation_service.py` 不动,等真需要再切不晚。
- **interrupt 工作流仍是单实例约束**。`thread_store` 还是内存态(技术债表已记录,演示性质延后),即使任务队列切了 arq,interrupt 流仍卡在单实例——切 arq 的收益被这个约束吃掉一半,不划算。等 interrupt 流也改造时一起切。

## 实现路径(切换时)

切换 arq 时不改 `JobQueue` 抽象语义,只加实现:

1. 新增 `core/job_queue_arq.py`,`ArqJobQueue` 实现 `JobQueue` 接口;`enqueue` 调 `pool.enqueue_job`,`get`/`update` 仍读写 `agentvalue:job:{job_id}`(结果状态仍走 Redis key,与 arq 结果后端并存,迁移期可对账)。
2. 评估的真正执行函数(当前在 `BackgroundTasks` 里跑的那段)抽成 `async def evaluate_job(ctx, job_id)`,注册成 arq task。
3. `create_job_queue` 工厂加分支:`AGENTVALUE_JOB_BACKEND=arq` 且 Redis 可达时返回 `ArqJobQueue`,否则保持现有逻辑。
4. `docker-compose.prod.yml` 加 `arq-worker` 服务,`command: arq worker.WorkerSettings`。
5. 灰度:先在试点的一个租户开 arq,观察任务完成率 / 失败重投 / worker 内存一周,无回归再全量。

## 后果

**正面:**

- 选型明确,真要扩展时不用再调研,直接按实现路径落地。
- async 链路不退化,与 FastAPI 一致。
- 依赖不膨胀(复用 redis-py),docker-compose 不加新中间件。

**负面 / 需接受:**

- 当前阶段仍存在"实例崩溃任务卡死"隐患,靠 `thread_store` 单实例约束兜着(该流演示性质)。规模化前必须切。
- arq 社区比 Celery 小,遇到冷门问题资料少;但 arq 代码量小,出问题能直接读源码。
- 多一个 worker 进程要运维(重启 / 监控 / 日志),`scale-deployment-runbook.md` 需补 worker 运维条目(切换时一并更新)。

## 备选方案

| 方案 | 评估 | 不选的原因 |
|---|---|---|
| **Redis Streams(裸用)** | Redis 原生 XADD/XREADGROUP,消费组扩展性强,零额外依赖 | 太底层:重试 / 死信 / 结果回写 / cron 全要自己写,等于手搓半个 arq;运维与代码成本高,收益(极致控制)当前场景用不上 |
| **Celery** | 生态成熟,文档多,重型任务队列事实标准 | 同步优先,async 支持别扭(要 `run_in_executor` 或 gevent);broker 抽象 + beat + 复杂路由对本项目过重;依赖树大,与 FastAPI 异步风格冲突 |
| **dramatiq + Redis broker** | 比 Celery 轻,API 干净 | 仍非 async 原生(dramatiq 是同步框架,有 async 扩展但非一等公民);社区比 arq 小,Redis broker 不如 arq 与 redis-py 贴合 |
| **保持裸 redis.asyncio 不演进** | 零成本,当前能跑 | 多实例下任务不可靠(进程崩 = 任务烂),规模化必塌;不可持续 |
