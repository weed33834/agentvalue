# agentvalue

AgentValue 开放 API 的官方 Python SDK（WS-3 集成与开放能力，对标 Stripe / Svix）。

提供两类能力：

1. **开放 API 客户端**：`X-API-Key` 鉴权，内置 429 / 5xx 指数退避重试；
2. **Webhook 签名校验**：与平台 `services/webhook_delivery_service.py` 的
   `HMAC-SHA256` 签名配方逐字节一致。

## 安装

```bash
pip install ./sdk/python
```

依赖：`httpx>=0.24`（无其他重依赖）。

## 快速开始

```python
from agentvalue import Client

# base_url 指向 AgentValue 服务端，api_key 在管理后台「API Key」页面创建
client = Client(base_url="https://av.example.com", api_key="ak_xxx")

me = client.get_me()          # 连通性自检：返回租户 / scopes / 配额
assert me["tenant_id"]

job = client.create_evaluation("u_001", "2026-Q3")
ev = client.get_evaluation(job["job_id"])

agents = client.list_agents()
reply = client.invoke_agent(agents["items"][0]["id"], input="本周总结", context="…")

datasets = client.list_datasets()
items = client.list_dataset_items(datasets["items"][0]["id"])

traces = client.list_traces(kind="llm")
```

异步场景使用 `AsyncClient`（方法名相同，均为 `async def`）。

```python
from agentvalue import AsyncClient

async with AsyncClient(base_url="https://av.example.com", api_key="ak_xxx") as client:
    me = await client.get_me()
```

## Webhook 签名校验

平台出站 Webhook 请求头 `X-AgentValue-Signature` 形如 `t=1754630400,v1=9f86d0...`：

- `t`：发送时刻的 unix 秒，容忍窗口 ±300s（防重放）；
- `v1`：`HMAC_SHA256(key=secret, msg=f"{t}.{raw_body}")` 的 hex。

**必须使用原始请求体**，不要 `json.loads` 后再 `json.dumps`（键序/空白差异会导致校验失败）。

```python
from agentvalue import verify_webhook_signature

raw_body = (await request.body()).decode("utf-8")
signature = request.headers.get("X-AgentValue-Signature", "")

ok = verify_webhook_signature(
    secret=subscription.secret,   # whsec_...
    timestamp=int(time.time()),
    body=raw_body,
    signature=signature,
)
```

## 错误处理

- `ApiError`：平台返回 4xx（不重试），带 `status_code` / `detail`；
- `RetryableError`：429 / 5xx 重试 `max_retries` 次（默认 5 次，指数退避 + 抖动）后仍失败。

## SDK 目录

```
sdk/python/agentvalue/
├── __init__.py    # 导出 Client / AsyncClient / verify_webhook_signature
└── client.py      # 实现（httpx 同步 + 异步）
```

## 可用端点（对应 `api/public/v1_routes.py`）

| 方法 | 路径 | SDK 方法 |
|---|---|---|
| GET | `/api/public/v1/me` | `get_me()` |
| POST | `/api/public/v1/evaluations` | `create_evaluation(employee_id, period, raw_inputs=None)` |
| GET | `/api/public/v1/evaluations` | `list_evaluations(...)` |
| GET | `/api/public/v1/evaluations/{id}` | `get_evaluation(evaluation_id)` |
| GET | `/api/public/v1/agents` | `list_agents(...)` |
| POST | `/api/public/v1/agents/{id}/invoke` | `invoke_agent(agent_id, input, context=None)` |
| GET | `/api/public/v1/datasets` | `list_datasets(...)` |
| GET | `/api/public/v1/datasets/{id}/items` | `list_dataset_items(dataset_id, ...)` |
| GET | `/api/public/v1/traces` | `list_traces(...)` |
