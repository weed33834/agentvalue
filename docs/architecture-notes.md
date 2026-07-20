# 架构实现说明（Architecture Notes）

这里记录的是 AgentValue-AI 落地过程中，实际实现相对于原始设计计划的几处**偏差与演进决策**。写出来的目的很实在：让设计和代码保持可追溯的一致，免得后来人按设计字面描述去找根本不存在的代码、白白踩坑。

所有偏差均为**有意识的取舍**，已在下文给出理由、影响与后续演进路径。当前 MVP 阶段不引入重依赖，优先保证测试轻量与可移植性。

---

## 1. FEEDBACK_COLLECT 节点的实现方式（对应计划书 4.4）

### 计划书描述

原始设计描述的 Agent 工作流（LangGraph）如下：

```
[RAW_DATA] → [DATA_CLEANING] → [CONTEXT_RETRIEVAL] → [AI_PROCESSING]
→ [AI_DRAFTED] → [MANAGER_REVIEW] → [HR_AUDIT] → [APPROVED]
→ [FEEDBACK_COLLECT]   ← 收集员工反馈与申诉
```

其中 `[FEEDBACK_COLLECT]` 被画作评估主流程内的一个节点。

### 实际实现

**实际实现见本文件，未在 `backend/agent/graph.py` 中实现为图节点。** 反馈与申诉改由 **API 层端点** 实现，作为评估主流程之外的异步入口：

| 能力 | 实际实现端点（`backend/api/routes.py`） | 说明 |
|---|---|---|
| 员工提交反馈 | `POST /api/v1/evaluations/{evaluation_id}/feedback` | 写入 Feedback 表，type=feedback |
| 员工申诉 | `POST /api/v1/evaluations/{evaluation_id}/appeal` | 状态机回退到 manager_review，并写入 type=appeal 的 Feedback 记录 |
| 基于反馈重评 | `POST /api/v1/evaluations/{evaluation_id}/re-evaluate` | 收集原始输入 + 反馈，重新跑评估图生成新 AI 草稿 |
| 反馈查询 | `GET /api/v1/evaluations/{evaluation_id}/feedback`、`GET /api/v1/employees/{employee_id}/feedback` | 员工/管理端追踪申诉处理进度 |

### 偏差理由

1. **反馈是异步触发，而非评估流内节点。** 计划书把 FEEDBACK_COLLECT 画作 APPROVED 之后的串行节点，但实际业务中员工反馈/申诉发生在评估**发布之后**的任意时间点（可能数小时甚至数天后），且并非每条评估都会触发。若将其做成图内节点，主评估流会被迫阻塞等待一个大概率不发生的事件，语义错配。
2. **API 层实现更灵活。** 反馈入口需要独立的鉴权（员工只能对本人评估申诉）、审计日志、状态机回退与 Feedback 表持久化，这些都是 API 层的职责，而非图节点的职责。
3. **不阻塞主评估流。** 评估图跑完 APPROVED 即结束，员工反馈通过独立端点进入，由 `re-evaluate` 端点按需重新触发评估图，二者解耦。
4. **避免破坏现有测试。** `graph.py` 已被 800+ 个测试覆盖，强行插入 FEEDBACK_COLLECT 节点会破坏现有图结构与测试断言。

### 结论

计划书 4.4 的 FEEDBACK_COLLECT 节点在实现上**等价收敛**为 API 层的一组 REST 端点 + Feedback 表 + 状态机回退动作，功能完整且可审计。计划书的流程图应理解为业务流程图而非严格的图节点拓扑。

---

## 2. TestContainers 集成测试策略（对应计划书 L4）

### 计划书描述

计划书提到使用 TestContainers 进行集成测试（真实 PostgreSQL / ChromaDB 容器）。

### 实际实现

**当前 MVP 不引入 TestContainers 依赖。** 集成测试采用更轻量的方案：

- **数据库**：内存 SQLite（`aiosqlite`），通过 `core/database.py` 的 `AsyncSessionLocal` 在测试 fixture 中替换为内存引擎，无需真实 PostgreSQL 容器。
- **向量库**：`DummyEmbeddingFunction` + 内存 ChromaDB / 内存向量存储，避免依赖真实嵌入模型与持久化 ChromaDB 服务。
- **模型**：`MockProvider`（见 `eval/evaluate.py`），不调用真实 LLM API。

### 偏差理由

1. **沙箱环境无法运行 Docker-in-Docker。** CI 沙箱与本地开发环境通常不具备 Docker 守护进程，TestContainers 依赖 Docker API 启动容器，在沙箱中无法运行。
2. **TestContainers 是重依赖。** 它会拉起真实容器、占用端口与内存，单次测试启动耗时数十秒，显著拖慢 800+ 个测试的反馈循环。
3. **内存方案已能覆盖核心逻辑。** 当前测试重点验证业务逻辑（评估流、审批状态机、反馈链路、护栏），内存 SQLite + DummyEmbedding 足以覆盖；真实容器仅在验证数据库方言差异、ChromaDB 持久化、并发连接池等基础设施行为时才有必要。

### 演进路径

TestContainers 作为**生产环境可选增强**保留：当需要验证 PostgreSQL 方言特异性（如 JSONB 索引、并发锁）或 ChromaDB 真实持久化行为时，可在独立的 `tests/integration/` 目录下引入 TestContainers，并标注 `@pytest.mark.integration`，默认不纳入常规 CI 流水线，避免拖慢开发反馈循环。当前 MVP 阶段不引入。

---

## 3. 任务队列现状与演进路径（对应计划书 M3）

### 现状

Phase 6 已实现 `JobQueue` 抽象层（`backend/core/job_queue.py`），提供 **InMemory（测试/本地）与 Redis（生产）双后端**：

- `create_job_queue(settings)` 按 `settings.redis_url` 自动选择后端：未配置 `redis_url` 时返回 `InMemoryJobQueue`；配置后返回 `RedisJobQueue`。
- **启动时探测 Redis 可达性**：`create_job_queue` 会同步 ping 一次 Redis，不可达则**降级为内存队列**并记录告警，避免 Redis 抖动直接拖垮启动。
- 路由层（`backend/api/routes.py`）通过模块级 `job_queue = create_job_queue(get_settings())` 持有实例，`_JobStoreCompat` 适配器在它之上保留了原有 `_update_job` / `get_evaluation_job` 等函数签名，API 契约不变。
- 配置 `REDIS_URL` 即启用多实例：多个 backend 副本共享 Redis 中的任务状态，水平扩展不再受单实例内存割裂限制。

### 多实例启用方式

`docker-compose.yml` 与 `docker-compose.prod.yml` 的 backend environment 已显式设置 `REDIS_URL=redis://redis:6379/0`，配合已就绪的 `redis` 服务即可启用。未配置 `REDIS_URL`（如本地 `uvicorn` 开发、单测）时自动降级为进程内内存，仅适合单实例。

### 演进路径

- **任务重试与死信**：当前 `RedisJobQueue` 已解决多实例共享与状态持久化，后续可在此基础上引入 `arq` / `redis-stream` 获得重试、死信、优先级、并发控制等能力。
- **接口稳定**：`job_store` 的读写仍封装在 `_JobStoreCompat` 之后，底层存储切换不影响 API 契约。

---

## 文件清单与引用

- 原始设计第 4.4 节：Agent 工作流（含 FEEDBACK_COLLECT 节点描述）
- `backend/api/routes.py`：FEEDBACK_COLLECT 实际端点、模块级 `job_queue`
- `backend/core/job_queue.py`：`JobQueue` 抽象层、`InMemoryJobQueue` / `RedisJobQueue` 双后端
- `backend/agent/graph.py`：评估主流程图（不含 FEEDBACK_COLLECT 节点）
- `backend/eval/evaluate.py`：MockProvider、回归评估框架
- `docker-compose.yml`：Redis 服务（已配置 `REDIS_URL`，承载任务队列）
- `backend/tests/conftest.py`：内存 SQLite + DummyEmbedding 测试 fixture

---

## 多模态抽取器架构（对应 Phase 7.1）

### 选型与取舍

`backend/core/multimodal/extractors.py` 在原有 6 类抽取器（Text/Table/Image/Audio/Pdf/Unknown）基础上补完真实提取能力，原则是 **MVP 不引入重系统依赖**，能力缺失时优雅降级为占位文本而非抛异常，保证评估主流程不崩。

| 类型 | 选型 | 依赖 | 不可用时降级 |
|---|---|---|---|
| PDF | **pypdf**（纯 Python、无系统依赖） | `pypdf>=4.0`（在 requirements） | 提示安装 pypdf / pdfplumber |
| Image OCR | **Tesseract**（pytesseract 调系统二进制） | `pytesseract>=0.3.10` + 系统包 `tesseract-ocr`（apt 安装，不入 requirements） | 返回 `[OCR 不可用,需安装 tesseract]` |
| Audio ASR | **接口预留**（MVP 不接 whisper/funasr） | 无 | DummyASR 返回占位提示；WhisperASR 占位不崩 |

### PDF：pypdf 为主，pdfplumber 为兼容回退

`PdfExtractor` 优先尝试 pdfplumber（若环境已装，对扫描件/复杂版式兼容更好），不可用则回退 pypdf。生产 requirements 仅声明 pypdf，故实际走 pypdf——满足“纯 Python、无系统依赖”的 MVP 选型。处理要点：

- 多页提取按 `--- 第 N 页 ---` 拼接，最多前 20 页（`MAX_PAGES`），避免超大 PDF 撑爆 prompt；
- 空 PDF / 纯扫描件（无文本层）返回 `(无可提取文本，可能是扫描件)`；
- 加密 PDF 通过 `reader.is_encrypted` 检测，返回 `PDF 已加密，需提供密码或解除加密后重试`；
- 残缺 PDF（如 `pypdf` 抛 `PdfStreamError`）降级为 `解析失败，可尝试安装 pdfplumber 获得更好兼容性`——pdfplumber 对残缺 PDF 确实更鲁棒，提示有据。

### Image OCR：基类 + 工厂，本地优先、云端预留

`OCRExtractor` 为基类，两个子类 + 工厂按配置选择：

- `LocalTesseractOCR`：调 `pytesseract.image_to_string`。tesseract 系统二进制缺失时**返回提示文本而非抛异常**；图片解码优先 PIL，无 PIL 时落临时文件交给 tesseract。
- `CloudOCR`：已实现 OpenAI 兼容 vision API 路径（`POST /v1/chat/completions`，`image_url` 走 base64 data URI），支持 gpt-4o / gpt-4o-mini / qwen-vl-plus / glm-4v 等模型。两条调用路径：优先用注入的 `vision_callable`（来自 ModelRouter），否则退化到内置 `AsyncOpenAI` 直连 vision 接口。配置 `OCR_CLOUD_API_KEY` + `OCR_CLOUD_BASE_URL` + `OCR_CLOUD_MODEL` 后启用；未配置 API Key 且未注入 `vision_callable` 时返回占位提示并标记人工复核。
- `create_ocr_extractor(settings)`：按 `ocr_provider`（`none`/`tesseract`/`cloud`）选择，默认 `none`（不启用）。

`ImageExtractor` 优先级：注入的 `vision_callable`（云端多模态模型）> `ocr_extractor` > 占位降级。**默认 `MultimodalCleaner()` 不注入 OCR**，未配置时返回“未配置 OCR/视觉模型”——这与既有测试契约一致，避免破坏现有行为；需要 OCR 时显式注入或通过工厂装配。

### Audio ASR：接口预留，默认 Dummy

`AudioExtractor` 既是基类也是可直用的具体类（保留 `asr_callable` 注入兼容旧用法）：

- `DummyASR`：MVP 默认，返回 `[ASR 未启用,使用 Dummy 占位]` 提示文本；
- `WhisperASR`：已实现 OpenAI 兼容 audio transcription API 路径（`POST /v1/audio/transcriptions`），支持 whisper-1 等模型。Phase 10 重构后不再依赖本地 `openai-whisper` 重模型，改走云端接口。配置 `ASR_CLOUD_API_KEY` + `ASR_CLOUD_BASE_URL` + `ASR_CLOUD_MODEL` 后启用；未配置 `api_key` 时返回占位提示并标记人工复核，由上层（`MultimodalCleaner` / 调用方）决定是否降级到 `DummyASR`；
- `create_asr_extractor(settings)`：按 `asr_provider`（`dummy`/`whisper`）选择，默认 `DummyASR`。

### 多模态置信度阈值

配置项 `MULTIMODAL_CONFIDENCE_THRESHOLD`（`multimodal_confidence_threshold`，默认 `0.7`）。`LocalTesseractOCR` 通过 `pytesseract.image_to_data` best-effort 取平均置信度（0~1），低于阈值时在抽取文本末尾追加 `[置信度 X.XX 低于阈值,建议人工复核]`。ASR/云端 OCR 占位阶段无真实置信度，待真实接入后补充。

### 部署注意

- **Tesseract 需系统安装**：`apt-get install tesseract-ocr`（及所需语言包如 `tesseract-ocr-chi-sim`），不在 Python requirements 内。未安装时 OCR 自动降级，不影响其它附件类型与主流程。
- **pypdf 随 requirements 安装即可**，无系统依赖，CI 与本地零额外配置。
- **whisper/pdfplumber/Pillow 均为可选增强**，未声明在 requirements；配置后对应能力自动启用（工厂/回退逻辑探测 `import`）。

### 文件清单与引用（Phase 7.1）

- `backend/core/multimodal/extractors.py`：6 类抽取器 + `OCRExtractor`/`LocalTesseractOCR`/`CloudOCR`（OpenAI vision 路径已接入）/`DummyASR`/`WhisperASR`（OpenAI audio transcription 路径已接入） + `create_ocr_extractor`/`create_asr_extractor` 工厂
- `backend/core/multimodal/cleaner.py`：`MultimodalCleaner` 增加 `ocr_extractor` 可选注入
- `backend/core/config.py`：`ocr_provider`/`asr_provider`/`multimodal_confidence_threshold` 等配置
- `backend/tests/test_multimodal.py`：真实 PDF 提取、OCR 降级/mock、ASR 云端接入与降级、Cleaner 编排、路径遍历防护补充测试
