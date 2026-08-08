# @agentvalue/sdk

AgentValue 开放 API 的官方 TypeScript SDK（WS-3 集成与开放能力，对标 Stripe / Svix）。

- **fetch 客户端**：`X-API-Key` 鉴权，内置 429 / 5xx 指数退避重试，无第三方运行时依赖；
- **Webhook 签名校验**：`node:crypto` 实现，与平台
  `services/webhook_delivery_service.py` 的 HMAC-SHA256 配方一致。

## 安装与构建

```bash
cd sdk/typescript
npm install          # 仅需要 typescript（devDependency）
npm run build        # 产出 dist/index.js + dist/index.d.ts
```

## 快速开始

```ts
import { AgentValueClient } from "@agentvalue/sdk";

const client = new AgentValueClient({
  baseUrl: "https://av.example.com",
  apiKey: "ak_xxx",
});

const me = await client.getMe();                  // 连通性自检
const job = await client.createEvaluation("u_001", "2026-Q3");
const ev = await client.getEvaluation(job.job_id);

const agents = await client.listAgents();
const reply = await client.invokeAgent(agents.items[0].id, "本周总结", "…");

const datasets = await client.listDatasets();
const items = await client.listDatasetItems(datasets.items[0].id);

const traces = await client.listTraces({ kind: "llm" });
```

## Webhook 签名校验

出站 Webhook 请求头 `X-AgentValue-Signature` 形如 `t=1754630400,v1=9f86d0...`：

- `t`：发送时刻 unix 秒，容忍窗口 ±300s（防重放）；
- `v1`：`HMAC_SHA256(key=secret, msg=f"{t}.{raw_body}")` 的 hex。

**必须用原始请求体**，不要 JSON.parse 后再 JSON.stringify（键序/空白差异会导致校验失败）。

```ts
import { verifyWebhookSignature } from "@agentvalue/sdk";

const rawBody = await request.text();
const ok = verifyWebhookSignature({
  secret: "whsec_...",
  timestamp: Math.floor(Date.now() / 1000),
  body: rawBody,
  signature: request.headers.get("X-AgentValue-Signature") ?? "",
});
```

## 错误处理

- `ApiError`：平台返回 4xx（不重试），带 `statusCode` / `detail`；
- `RetryableError`：429 / 5xx 重试 `maxRetries` 次（默认 5，指数退避 + 抖动）后仍失败。

## 可用端点（对应 `api/public/v1_routes.py`）

| 方法 | 路径 | SDK 方法 |
|---|---|---|
| GET | `/api/public/v1/me` | `getMe()` |
| POST | `/api/public/v1/evaluations` | `createEvaluation(employeeId, period, rawInputs?)` |
| GET | `/api/public/v1/evaluations` | `listEvaluations(params?)` |
| GET | `/api/public/v1/evaluations/{id}` | `getEvaluation(evaluationId)` |
| GET | `/api/public/v1/agents` | `listAgents(params?)` |
| POST | `/api/public/v1/agents/{id}/invoke` | `invokeAgent(agentId, input, context?)` |
| GET | `/api/public/v1/datasets` | `listDatasets(params?)` |
| GET | `/api/public/v1/datasets/{id}/items` | `listDatasetItems(datasetId, params?)` |
| GET | `/api/public/v1/traces` | `listTraces(params?)` |
