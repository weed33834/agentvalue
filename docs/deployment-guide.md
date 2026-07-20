# AgentValue-AI 企业部署手册

> **版本：** v1.1.0  
> **适用对象：** 系统管理员、DevOps、安全合规人员  
> **目标：** 指导企业完成 AgentValue-AI 的内网/云端/混合部署

---

## 一、部署模式选择

| 模式 | 适用场景 | 数据是否出域 | 推荐规模 | 硬件要求 |
|---|---|---|---|---|
| **本地模式** | 高保密、无互联网环境 | 否 | ≤ 200 人 | 1× RTX 4090 / 64GB RAM |
| **云端模式** | 追求最强推理能力、快速试点 | 是 | 任意 | 应用服务器 16C32G |
| **混合模式** | 常规工作负载本地处理，复杂任务上云 | 部分 | 200-2000 人 | 本地 GPU + 云端 API |

> 拿不准的话先上混合模式：常规评估走本地 L2/L3，复杂多模态分析再调云端 L0，成本和保密两头都不亏。

---

## 二、环境准备

### 2.1 基础依赖

- Docker Engine ≥ 24.0
- Docker Compose ≥ 2.20
- （可选）NVIDIA Container Toolkit（使用本地 GPU 时）

### 2.2 模型接入准备

**云端模型（L0）**

需准备以下任一 OpenAI 兼容接口：

- OpenAI API
- DeepSeek API
- 阿里云百炼
- 硅基流动

获取 API Key 后填入 `backend/.env` 的 `CLOUD_API_KEY`。

**本地模型（L1/L2/L3）**

推荐使用 LM Studio 或 Ollama：

1. 在内网服务器安装 LM Studio / Ollama；
2. 下载对应模型，如 `Qwen2.5-7B-Instruct`；
3. 启动本地 API 服务，默认地址 `http://localhost:1234/v1`；
4. 在 `backend/.env` 中配置 `LOCAL_BASE_URL` 与 `LOCAL_MODEL_L2`。

### 2.3 Embedding 服务

默认使用云端 Embedding（如 `text-embedding-3-small`）。如需本地部署：

- 使用 LM Studio / Ollama / Xinference 启动兼容接口；
- 在 `backend/.env` 中配置 `EMBEDDING_BASE_URL` 与 `EMBEDDING_MODEL`。

---

## 三、Docker Compose 部署

### 3.1 快速启动

```bash
# 1. 克隆仓库
git clone https://gitcode.com/badhope/agentvalue.git
cd AgentValue-AI

# 2. 配置环境变量
cp backend/.env.example backend/.env
# 编辑 backend/.env，填入实际 API Key 与模型地址

# 3. 启动全部服务
docker compose up -d --build
```

### 3.2 服务清单

| 服务 | 容器名 | 端口 | 说明 |
|---|---|---|---|
| backend | agentvalue-backend | 8000 | FastAPI 后端 |
| frontend | agentvalue-frontend | 80 | Vue3 前端（Nginx） |
| redis | agentvalue-redis | 6379 | 缓存 |

### 3.3 持久化卷

| 卷名 | 用途 |
|---|---|
| agentvalue_db_data | SQLite 数据库 |
| agentvalue_chroma_data | 向量库 |
| agentvalue_redis_data | Redis 数据 |
| agentvalue_attachments | 本地附件（如上传的截图、语音、PDF） |

### 3.4 健康检查

```bash
curl http://localhost:8000/health
```

预期返回：

```json
{"status": "ok"}
```

### 3.5 数据库迁移（可选）

容器首次启动时会在 `lifespan` 中自动创建数据表（基于 SQLAlchemy metadata），因此快速体验无需手动迁移。

如需显式使用 Alembic 管理版本（推荐生产环境），配置文件位于 `backend/alembic.ini`，迁移脚本位于 `backend/alembic/versions/`：

```bash
# 进入 backend 目录后执行
cd backend
python scripts/migrate.py upgrade
# 或
alembic -c alembic.ini upgrade head
```

> `backend/alembic.ini` 中的数据库连接串会被 `backend/alembic/env.py` 自动替换为 `backend/.env` 里的 `DATABASE_URL`。

### 3.6 生产环境 Compose

基础 `docker-compose.yml` 面向**开发与演示**：SQLite、本地附件目录、无 GPU，开箱即用，但没有生产级的数据持久化和扩展能力。生产部署叠加 `docker-compose.prod.yml` override，补齐 PostgreSQL、MinIO、GPU 三项。

**启动命令(双文件叠加):**

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

**override 提供的生产扩展:**

| 扩展项 | 替代/增强 | 说明 |
|---|---|---|
| PostgreSQL (`postgres:15-alpine`) | 替代 SQLite | 生产关系型数据库,支持并发与备份;backend 的 `DATABASE_URL` 自动切换为 `postgresql+asyncpg://...` |
| MinIO (`minio/minio:latest`) | 替代/补充本地附件目录 | S3 兼容对象存储,端口 9000(API)/9001(Console);配置 S3 endpoint 后附件走 MinIO,否则降级本地挂载 |
| NVIDIA GPU | 加速本地模型推理 | 通过 `deploy.resources.reservations.devices` 挂载,需先安装 NVIDIA Container Toolkit |

**凭据注入:** PostgreSQL 与 MinIO 的默认凭据仅用于快速体验,生产环境务必通过 `backend/.env` 或外部密钥管理服务覆盖 `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` / `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`。

> 第四章给的是手动改 `docker-compose.yml` 的零散片段，这里则是封装好的 override 文件。优先用 override，别去污染基础 compose。

### 3.7 对象存储配置(附件 S3 化)

`docker-compose.prod.yml` 已提供 MinIO 服务，后端通过 `core/storage.py` 的存储抽象接入：配置了 S3 endpoint 且能连通时附件走 MinIO，否则自动降级到本地 `ATTACHMENT_DIR`，连接失败仅记 warning 不影响服务启动。

**启用对象存储**，在 `backend/.env`(或 compose environment)中配置：

```env
S3_ENDPOINT=minio:9000
S3_ACCESS_KEY=your-minio-access-key
S3_SECRET_KEY=your-minio-secret-key
S3_BUCKET=agentvalue-attachments
S3_SECURE=false
```

| 变量 | 说明 |
|---|---|
| `S3_ENDPOINT` | MinIO API 地址(不含 scheme)；留空则使用本地 `ATTACHMENT_DIR` |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | MinIO 访问凭据，与 `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` 一致 |
| `S3_BUCKET` | 默认 `agentvalue-attachments`；不存在时后端首次连接自动创建 |
| `S3_SECURE` | 是否使用 HTTPS，容器内通信通常 `false` |

**降级策略：** 未配置 `S3_ENDPOINT` 或连接 MinIO 失败时，附件自动落回本地 `ATTACHMENT_DIR`，服务始终可用，无需人工干预。附件上传端点 `POST /api/v1/attachments` 返回的 `url` 可供 `/inputs` 引用。

---

## 四、生产环境增强

### 4.1 使用 PostgreSQL 替代 SQLite

编辑 `backend/.env`：

```env
DATABASE_URL=postgresql+asyncpg://user:password@postgres:5432/agentvalue
```

并在 `docker-compose.yml` 中加入 PostgreSQL 服务：

```yaml
  postgres:
    image: postgres:15-alpine
    container_name: agentvalue-postgres
    environment:
      POSTGRES_USER: user
      POSTGRES_PASSWORD: password
      POSTGRES_DB: agentvalue
    volumes:
      - agentvalue_pg_data:/var/lib/postgresql/data
    restart: unless-stopped

volumes:
  agentvalue_pg_data:
```

### 4.2 使用 MinIO 作为对象存储

用于存储原始附件（截图、语音、PDF）。在 `docker-compose.yml` 中加入：

```yaml
  minio:
    image: minio/minio:latest
    container_name: agentvalue-minio
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    volumes:
      - agentvalue_minio_data:/data
    ports:
      - "9000:9000"
      - "9001:9001"
    restart: unless-stopped

volumes:
  agentvalue_minio_data:
```

### 4.3 启用 GPU（本地模型）

在 `docker-compose.yml` 的 backend 服务中添加：

```yaml
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

并确保已安装 NVIDIA Container Toolkit。

### 4.4 HTTPS 与反向代理

生产环境应在 Nginx / Traefik / APISIX 后面部署，配置 TLS 1.3：

```nginx
server {
    listen 443 ssl http2;
    server_name agentvalue.example.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;
    ssl_protocols TLSv1.3;

    location / {
        proxy_pass http://agentvalue-frontend;
    }

    location /api/ {
        proxy_pass http://agentvalue-backend:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## 五、配置管理

### 5.1 环境变量清单

| 变量 | 必填 | 说明 |
|---|---|---|
| `MODEL_TIER` | 否 | 模型档位：auto / L0 / L1 / L2 / L3 |
| `CLOUD_API_KEY` | 使用云端时 | 云端模型 API Key |
| `CLOUD_BASE_URL` | 使用云端时 | 云端模型 Base URL |
| `CLOUD_MODEL` | 使用云端时 | 云端模型名 |
| `OPENAI_API_KEY` | 使用旧版 OpenAI 时 | 兼容旧命名，未配置 `CLOUD_*` 时兜底 |
| `OPENAI_BASE_URL` | 使用旧版 OpenAI 时 | 兼容旧命名 |
| `OPENAI_MODEL` | 使用旧版 OpenAI 时 | 兼容旧命名 |
| `LOCAL_BASE_URL` | 使用本地时 | 本地模型 API 地址 |
| `LOCAL_API_KEY` | 使用本地时 | 本地模型 API Key |
| `LOCAL_MODEL_L1` | 使用本地时 | L1 档位模型名 |
| `LOCAL_MODEL_L2` | 使用本地时 | L2 档位模型名 |
| `LOCAL_MODEL_L3` | 使用本地时 | L3 档位模型名 |
| `EMBEDDING_API_KEY` | 是 | Embedding 服务 Key |
| `EMBEDDING_BASE_URL` | 是 | Embedding 服务地址 |
| `EMBEDDING_MODEL` | 否 | Embedding 模型名，默认 `text-embedding-3-small` |
| `EMBEDDING_DIMENSIONS` | 否 | Embedding 维度，默认 `1536` |
| `DATABASE_URL` | 否 | 数据库连接串 |
| `VECTOR_STORE_DIR` | 否 | 向量库存储路径 |
| `ATTACHMENT_DIR` | 否 | 附件本地存储根目录 |
| `REDIS_URL` | 否 | 任务队列 Redis 连接串（如 `redis://redis:6379/0`），配置后启用多副本共享；未配置降级为进程内内存（仅单实例） |
| `TEMPERATURE` | 否 | LLM 推理温度，默认 `0.1` |
| `MAX_TOKENS` | 否 | LLM 最大输出 token，默认 `4096` |
| `JWT_SECRET_KEY` | 是 | JWT 签名密钥，生产环境必须修改 |
| `JWT_ALGORITHM` | 否 | JWT 算法，默认 `HS256` |
| `JWT_EXPIRE_MINUTES` | 否 | Token 有效期，默认 `1440` |
| `AUTH_DEMO_MODE` | 否 | 演示模式，生产必须 `false` |
| `AGENTVALUE_ENV` | 否 | 环境标识，设为 `production` 才触发 `demo_mode` 守护（生产必须设） |
| `LANGFUSE_PUBLIC_KEY` | 否 | Langfuse 公钥 |
| `LANGFUSE_SECRET_KEY` | 否 | Langfuse 私钥 |
| `LANGFUSE_HOST` | 否 | Langfuse 地址 |

### 5.2 模型档位策略

```env
# 完全本地保密
MODEL_TIER=L2

# 混合：自动选择
MODEL_TIER=auto

# 云端最强能力
MODEL_TIER=L0
```

---

## 六、初始化与验证

### 6.1 创建演示账号

> 注意：`seed-demo-users` 接口仅在 `AUTH_DEMO_MODE=true` 时可用。启动前请在 `backend/.env` 中设置：
>
> ```env
> AUTH_DEMO_MODE=true
> ```
> 生产环境此接口不可达，无需删除（见下方安全说明）。

```bash
curl -X POST http://localhost:8000/api/v1/auth/seed-demo-users
```

默认账号：

| 邮箱 | 角色 | 默认密码 |
|---|---|---|
| employee@agentvalue.ai | employee | agentvalue123 |
| manager@agentvalue.ai | manager | agentvalue123 |
| hr@agentvalue.ai | hr | agentvalue123 |
| admin@agentvalue.ai | admin | agentvalue123 |

> **安全说明（双层守护，生产环境接口天然不可达）：**
> 1. `core/config.py` 的 `_enforce_prod_demo_mode_guard`：当 `AGENTVALUE_ENV=production` 且
>    `AUTH_DEMO_MODE=true` 时，硬拒绝 `Settings` 实例化（应用启动直接失败）；
> 2. `api/auth_routes.py` 的 `seed_demo_users` 接口：未开启 `auth_demo_mode` 时返回 403。
>
> 因此生产环境无法调用此接口，无需手动删除。演示账号默认密码 `agentvalue123` 较弱，
> 仅供本地联调，正式环境请勿使用演示账号。

### 6.2 提交一条测试输入

```bash
curl -X POST http://localhost:8000/api/v1/inputs \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <employee_token>" \
  -d '{
    "employee_id": "E1001",
    "period": "2026-W25",
    "type": "daily_report",
    "content": "本周完成了用户画像模块重构，性能提升40%；辅导两名新人完成Code Review。"
  }'
```

### 6.3 触发一次评估

```bash
curl -X POST http://localhost:8000/api/v1/evaluations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <employee_token>" \
  -d '{
    "employee_id": "E1001",
    "period": "2026-W25"
  }'
```

返回 `job_id`，通过 `/api/v1/evaluations/jobs/{job_id}` 查询结果。

---

## 七、监控与运维

### 7.1 日志查看

```bash
# 全部服务日志
docker compose logs -f

# 仅后端
docker compose logs -f backend
```

### 7.2 备份策略

| 数据 | 备份方式 | 频率 |
|---|---|---|
| SQLite / PostgreSQL | 数据库备份 | 每日 |
| ChromaDB 向量库 | 卷快照 | 每日 |
| Redis | AOF + RDB | 每日 |
| 原始附件 | MinIO/OSS 跨区域复制 | 实时 |

### 7.3 升级流程

```bash
# 1. 拉取新版本
git pull origin main

# 2. 重新构建并启动
docker compose down
docker compose up -d --build

# 3. 验证健康检查
curl http://localhost:8000/health
```

### 7.4 任务队列与横向扩展

> 任务队列已支持 Redis 后端，配置 `REDIS_URL` 即可启用多副本共享，未配置时降级为进程内内存（仅单实例）。

**当前形态:**

- 评估任务通过 `JobQueue` 抽象层（`backend/core/job_queue.py`）管理，`create_job_queue` 按 `settings.redis_url` 自动选择后端：
  - **配置 `REDIS_URL`**（生产/多实例）：任务状态写入 Redis，多个 backend 副本共享，`docker compose up --scale backend=N` 可水平扩展；
  - **未配置 `REDIS_URL`**（本地开发/单测）：降级为进程内内存（`InMemoryJobQueue`），仅适合单实例。
- `docker-compose.yml` 与 `docker-compose.prod.yml` 的 backend environment 已显式设置 `REDIS_URL=redis://redis:6379/0`，配合已就绪的 `redis` 服务即可启用。
- 启动时 `create_job_queue` 会探测 Redis 可达性，不可达则降级内存并告警，避免 Redis 抖动直接拖垮启动。

**多副本部署:**

```bash
# 启用 Redis 任务队列后可直接扩容
docker compose up -d --scale backend=3 --build
```

多副本共享 Redis 中的任务状态，轮询 `/api/v1/evaluations/jobs/{job_id}` 不再受请求落到哪个副本的限制。

**单实例并发上限建议:**

| 资源档位 | 建议并发评估数 | 说明 |
|---|---|---|
| CPU only(无 GPU) | ≤ 10 | 受本地 LLM 推理吞吐限制,且依赖云端模型时受 API 速率限制 |
| 单卡 GPU(如 RTX 4090) | ≤ 50 | 本地 L2/L3 模型推理 + 云端 L0 兜底 |
| 多卡 GPU / 云端为主 | ≤ 100 | 主要受云端 API 配额与数据库连接池约束 |

> 超过上述上限时，应优先扩容 backend 副本（Redis 任务队列已就绪），并关注云端 API 配额与数据库连接池上限。

### 7.5 监控与告警（Prometheus + Grafana）

生产 compose 已内置 Prometheus + Grafana，一键启动即开箱可用：

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

- **Prometheus** `http://localhost:9090`：抓 backend `/metrics`，评估 `monitoring/alerts.yml` 告警规则
- **Grafana** `http://localhost:3000`：启动自动加载 Prometheus 数据源 + AgentValue-AI 看板（provisioning 免手动 Import）；prod compose 强制必填 `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD`（无默认值，未设置则容器启动失败）

三条核心告警（持续 5m 触发，规则见 `monitoring/alerts.yml`，排查步骤见 [告警规则手册](alerting-rules.md)）：

| 告警 | 阈值 | 严重度 |
|---|---|---|
| 评估失败率 | >5% | critical |
| 评估 P99 耗时 | >3s | warning |
| LLM 调用失败率 | >10% | critical |

> Alertmanager 未内置。Prometheus 已评估告警状态（`/alerts` 页面可查 FIRING），启用邮件/钉钉/飞书通知时另行部署 Alertmanager 并在 `monitoring/prometheus.yml` 的 `alerting` 段取消注释。

---

## 八、故障排查

### 8.1 后端无法启动

- 检查 `.env` 是否配置正确；
- 查看 `docker compose logs backend`；
- 确认数据库目录权限。

### 8.2 模型调用失败

- 检查 API Key 是否有效；
- 检查模型 Base URL 是否可达；
- 查看 `/api/v1/admin/model-status` 当前档位。

### 8.3 前端无法访问 API

- 确认 CORS 配置包含前端域名；
- 检查 Nginx 反向代理配置；
- 确认后端服务健康。

---

## 九、安全基线

- 所有服务仅暴露必要端口；
- 使用 TLS 1.3 加密传输；
- 数据库密码、API Key 通过环境变量或密钥管理服务注入；
- 定期更新基础镜像与依赖；
- 启用审计日志并归档。

---

## 十、获取支持

- 项目仓库：https://gitcode.com/badhope/agentvalue
- 问题反馈：通过仓库 Issue 提交
