# 告警规则手册

AgentValue-AI 生产环境的三条核心告警,规则文件 [monitoring/alerts.yml](../monitoring/alerts.yml),由 Prometheus 按 group interval(30s) 评估，持续 5m(for) 满足条件才触发(避免瞬时抖动误报)。

告警链路:`backend /metrics` → Prometheus 抓取 + 规则评估 → (Alertmanager 未内置,启用通知时另行部署)→ 通知渠道。

> 当前未部署 Alertmanager,Prometheus 已评估告警状态(可在 `/alerts` 页面查看 FIRING 告警),但不会主动推送通知。启用邮件 / 钉钉 / 飞书通知时,部署 Alertmanager 并在 `monitoring/prometheus.yml` 的 `alerting` 段取消注释。

---

## 部署形态

生产环境用 `docker-compose.prod.yml` 一键起 Prometheus + Grafana:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

- Prometheus:`http://localhost:9090`(抓 backend `/metrics`,评估 `alerts.yml`)
- Grafana:`http://localhost:3000`(默认 admin/admin,生产用 `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` 覆盖)
- Grafana 启动时自动加载:Prometheus 数据源 + AgentValue-AI 看板(`grafana/provisioning` + `grafana/dashboard.json`)

---

## 告警一览

| 告警 | 阈值 | 严重度 | 窗口 | 指标 |
|---|---|---|---|---|
| EmpvalueEvaluationFailureRateHigh | 失败率 >5% | critical | 5m | `agentvalue_evaluation_failures_total` / `agentvalue_evaluations_total` |
| EmpvalueEvaluationP99LatencyHigh | P99 >3s | warning | 5m | `agentvalue_evaluation_duration_seconds` |
| EmpvalueLlmFailureRateHigh | 失败率 >10% | critical | 5m | `agentvalue_llm_requests_total` |

---

## 1. EmpvalueEvaluationFailureRateHigh(评估失败率 >5%)

### 触发条件

5 分钟窗口内,评估失败数占总尝试数(成功 + 失败)比例超过 5%。

```promql
(
  sum(rate(agentvalue_evaluation_failures_total[5m]))
  /
  clamp_min(
    sum(rate(agentvalue_evaluations_total[5m]))
    + sum(rate(agentvalue_evaluation_failures_total[5m])),
    0.001
  )
) > 0.05
```

`clamp_min` 兜底分母为 0(冷启动无评估)的情况,避免除零。

### 指标语义

- `agentvalue_evaluations_total{status, model_tier}`:成功产出评估结果的计数,`status` 为评估终态(ai_drafted / manager_review / hr_audit / approved / rejected)。
- `agentvalue_evaluation_failures_total{reason}`:评估任务失败计数,`reason` 三类:
  - `graph_error` —— 评估图执行返回错误(LLM 调用失败、护栏拦截等)
  - `no_result` —— 图执行完成但未生成 parsed_evaluation
  - `exception` —— 处理过程抛异常

### 影响

评估产出受阻,员工 / 主管看不到当期评估,审批流断链。失败率 >5% 说明不是偶发,是系统性问题。

### 排查步骤

1. Prometheus 查 `sum by (reason) (agentvalue_evaluation_failures_total)`,定位是哪类失败占主导。
2. `graph_error` 为主 → 多为 LLM 调用失败,跳到第 3 条告警排查 Provider;或护栏拦截,查 backend 日志 `grep "输入被拦截"`。
3. `no_result` 为主 → LLM 返回了内容但解析失败,查 backend 日志 `grep "解析"` 与 `llm_raw_output`,多为模型输出不符合 JSON Schema。
4. `exception` 为主 → 服务端异常,查 backend 日志 `grep "评估处理失败"`,看堆栈。
5. 临时止血:在 AdminModel 页面把模型档位降到稳定的 L0(云端),或暂停评估入口。

---

## 2. EmpvalueEvaluationP99LatencyHigh(评估 P99 耗时 >3s)

### 触发条件

5 分钟窗口内,按 `model_tier` 分组的评估耗时 P99 超过 3 秒。

```promql
histogram_quantile(
  0.99,
  sum by (le, model_tier) (rate(agentvalue_evaluation_duration_seconds_bucket[5m]))
) > 3
```

按 `model_tier` 分组,定位是哪个档位慢。

### 指标语义

`agentvalue_evaluation_duration_seconds{model_tier}`:单次评估从图执行到产出结果的耗时(秒),Histogram 默认桶。

### 影响

评估慢直接影响异步 job 完成时间,主管审批队列堆积,试点用户体感差。P99 >3s 通常意味着本地模型推理瓶颈或云端 API 排队。

### 排查步骤

1. Prometheus 查 `histogram_quantile(0.99, sum by (le, model_tier)(rate(agentvalue_evaluation_duration_seconds_bucket[5m])))`,看哪个 `model_tier` 慢。
2. L2/L3(本地模型)慢 → 检查 GPU 利用率(`nvidia-smi`),本地模型推理是瓶颈;考虑降档到 L0(云端)或扩容 GPU。
3. L0(云端)慢 → 检查云端 API 限流 / 网络,Provider 重试会放大耗时;查 `agentvalue_llm_requests_total` 是否伴随失败率上升。
4. 持续慢且无法降档 → 在 `docker-compose.prod.yml` 扩容 backend 副本(`--scale backend=N`),Redis 任务队列已就绪可分担。

---

## 3. EmpvalueLlmFailureRateHigh(LLM 调用失败率 >10%)

### 触发条件

5 分钟窗口内,LLM 调用失败数占总调用数比例超过 10%。

```promql
(
  sum(rate(agentvalue_llm_requests_total{status="error"}[5m]))
  /
  clamp_min(sum(rate(agentvalue_llm_requests_total[5m])), 0.001)
) > 0.10
```

`status="error"` 对应 Provider 重试耗尽(`openai_provider` MAX_RETRIES=3 指数退避后仍失败)。

### 指标语义

`agentvalue_llm_requests_total{model_tier, status}`:LLM 调用计数,`status` 为 `success`(成功)或 `error`(重试耗尽失败)。

### 影响

LLM 调用失败直接导致评估 `graph_error` 失败,连锁触发第 1 条告警。失败率 >10% 说明 Provider 不可用或限流严重。

### 排查步骤

1. Prometheus 查 `sum by (model_tier, status) (rate(agentvalue_llm_requests_total[5m]))`,定位故障档位。
2. L0(云端)失败 → 检查 API Key 是否失效 / 余额耗尽 / 触发速率限制;在 AdminModel 页面切换 Provider 配置或降档到本地。
3. L2/L3(本地)失败 → 检查 LM Studio / 本地推理服务是否存活,`curl` 探测本地模型端点。
4. 全档位失败 → 多半是网络或配置问题,查 backend `.env` 的模型配置段,确认 endpoint / key 正确。
5. 止血:临时切到稳定的 Mock 档位(`demo_mode`)暂停真实评估,或暂停评估入口直到 Provider 恢复。

---

## 告警通知接入(可选)

部署 Alertmanager 后,Prometheus 把 FIRING 告警推给 Alertmanager,由其按路由规则分发:

```yaml
# monitoring/prometheus.yml 取消注释
alerting:
  alertmanagers:
    - static_configs:
        - targets: ['alertmanager:9093']
```

Alertmanager 配置(单独部署,不在本仓库)按 `severity` 路由:`critical` 走电话 / 钉钉机器人,@值班人员;`warning` 走邮件 / 飞书群。本仓库只管告警规则定义,通知渠道由运维侧 Alertmanager 配置。
