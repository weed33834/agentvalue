"""
AgentValue-AI 应用配置
优先从环境变量读取，本地开发可使用 .env 文件。
"""

from functools import lru_cache
from typing import Literal, Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置"""

    model_config = SettingsConfigDict(
        # .env.runtime 由 admin LLM 配置 API 写入（gitignored），优先级高于 .env
        env_file=(".env", ".env.runtime"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "AgentValue-AI"
    debug: bool = False

    # 数据库：默认 SQLite（异步），生产可改为 postgresql+asyncpg://...
    database_url: str = "sqlite+aiosqlite:///./agentvalue_ai.db"

    # 任务队列 Redis：配置后启用 RedisJobQueue,多实例共享异步评估任务状态
    # 留空则降级为 InMemoryJobQueue(单实例,测试与本地开发默认)
    redis_url: Optional[str] = None

    # P3 规模化就绪:是否启用 arq 任务队列 + 自动重投 + 死信队列
    # 未启用时降级到 RedisJobQueue(裸 redis.asyncio 共享存储,无 worker 进程)
    # 启用时需另行启动 arq worker: `arq core.arq_worker.WorkerSettings`
    use_arq_queue: bool = False
    # arq worker 重投次数(不含首次),到顶后入死信队列
    arq_max_tries: int = 3
    # arq 任务超时(秒),超时 worker 取消并触发重投(最后一次后入死信)
    arq_job_timeout: float = 600.0

    # P3 规模化就绪:LangGraph checkpointer 持久化
    # 未设置时降级到 MemorySaver(单实例限制,见 H3)
    # 启用时使用 Postgres checkpointer,DATABASE_URL 必须是 postgresql://
    use_postgres_checkpointer: bool = False

    # 模型档位强制设定，可选 auto / L0 / L1 / L2 / L3
    model_tier: Literal["auto", "L0", "L1", "L2", "L3"] = "auto"

    # 通用云端 API 配置（OpenAI 兼容，可用于 DeepSeek / 阿里云百炼 / 硅基流动等）
    cloud_api_key: Optional[str] = None
    cloud_base_url: str = "https://api.openai.com/v1"
    cloud_model: str = "gpt-4o-mini"

    # 兼容旧版 OpenAI 命名（未设置 cloud_* 时兜底使用）
    openai_api_key: Optional[str] = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"

    # 本地 LM Studio / Ollama 配置（OpenAI 兼容接口）
    local_base_url: str = "http://localhost:1234/v1"
    local_api_key: Optional[str] = None
    local_model_l1: str = "qwen2.5-0.5b-instruct"
    local_model_l2: str = "qwen2.5-7b-instruct"
    local_model_l3: str = "qwen2.5-14b-instruct"

    # Embedding 配置（OpenAI 兼容接口）
    embedding_api_key: Optional[str] = None
    embedding_base_url: Optional[str] = None
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536

    # 向量库配置
    vector_store_dir: str = "./chroma_db"

    # 知识库分块配置(P1-1 知识库管理 UI)
    # chunk_size: 单块字符数,后续 reindex 按此切分长文档
    # chunk_overlap: 相邻块字符重叠,提升跨块语义连续性
    chunk_size: int = 800
    chunk_overlap: int = 100

    # 附件存储目录（路径遍历防护白名单根目录）
    attachment_dir: str = "./attachments"

    # 多模态抽取配置(Phase 7.1 + Phase 10 真实接入)
    # OCR 后端: none(默认不启用) / tesseract / cloud
    ocr_provider: str = "none"
    ocr_lang: str = "chi_sim+eng"
    ocr_cloud_provider: str = "aliyun"  # aliyun / baidu(legacy,保留兼容)
    ocr_cloud_secret_key: Optional[str] = None
    # ASR 后端: dummy(默认占位) / whisper
    asr_provider: str = "dummy"
    whisper_model: str = "base"
    # OCR/ASR 置信度阈值: 低于该值标记需人工复核
    multimodal_confidence_threshold: float = 0.7

    # 云端 OCR 配置(Phase 10 多模态真实接入)
    # ocr_provider == "cloud" 时生效,基于 OpenAI 兼容 vision API
    ocr_cloud_api_key: Optional[str] = None
    ocr_cloud_base_url: Optional[str] = "https://api.openai.com/v1"
    ocr_cloud_model: str = "gpt-4o-mini"

    # 云端 ASR 配置(Phase 10 多模态真实接入)
    # asr_provider == "whisper" 时生效,基于 OpenAI 兼容 audio transcription API
    asr_cloud_api_key: Optional[str] = None
    asr_cloud_base_url: Optional[str] = "https://api.openai.com/v1"
    asr_cloud_model: str = "whisper-1"

    # S3 兼容对象存储(MinIO)：配置 s3_endpoint 后附件走对象存储，未配置降级到本地目录
    s3_endpoint: Optional[str] = None
    s3_access_key: Optional[str] = None
    s3_secret_key: Optional[str] = None
    s3_bucket: str = "agentvalue-attachments"
    s3_secure: bool = True

    # 默认推理参数
    temperature: float = 0.1
    max_tokens: int = 4096
    # LLM 单次请求超时（秒）：评估类请求 prompt 较长，免费/自托管服务响应较慢时需调大
    llm_request_timeout: float = 120.0

    # Langfuse 可观测性配置
    langfuse_public_key: Optional[str] = None
    langfuse_secret_key: Optional[str] = None
    langfuse_host: str = "https://cloud.langfuse.com"

    # P1 工具管理: MCP 服务器配置 (JSON 字符串,参考 langchain-mcp-adapters MultiServerMCPClient)
    # 示例: {"jira":{"transport":"streamable_http","url":"http://localhost:8001/mcp"},
    #        "fs":{"transport":"stdio","command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","./data"]}}
    mcp_servers: Optional[str] = None
    # 启用的内置工具(逗号分隔),缺省全部启用。
    # 可选: employee_history,company_kb,calculator,datetime
    enabled_tools: Optional[str] = None
    # ReAct Agent 最大迭代次数(防止死循环)
    react_agent_max_iterations: int = 10

    # JWT 认证配置：生产环境必须通过环境变量设置强随机密钥
    jwt_secret_key: Optional[str] = None
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440  # 24 小时
    # 演示模式：开启时允许通过 x-user-role / x-user-id header 伪造身份（仅开发/测试用）
    auth_demo_mode: bool = False

    # 运行环境标识：仅当值为 "production" 时启用生产安全校验；
    # 不设或非 production 时不做任何校验，确保开发与测试环境不受影响。
    agentvalue_env: Optional[str] = None

    # 数据留存策略（Phase 9.3）：GDPR/个保法要求原始输入 2 年、评估 5 年，
    # 到期先归档缓冲 30 天再删除，避免误删与申诉期数据缺失。
    retention_raw_input_days: int = 730
    retention_evaluation_days: int = 1825
    retention_archive_buffer_days: int = 30

    # 字段级加密(用于 DB 中 manager_view/audit 敏感字段),base64 编码的 32 字节密钥
    # 未配置时降级为明文(仅开发模式可接受,生产必须配置)
    # 生成方法: python -c "import base64,os; print(base64.b64encode(os.urandom(32)).decode())"
    field_encryption_key: Optional[str] = None

    # ===== KMS / Vault 集成 (H5: 消除密钥明文配置) =====
    # field_encryption_backend: 字段加密后端选择
    #   - "env"   : 传统模式,从 field_encryption_key 读取 (向后兼容,默认)
    #   - "vault" : HashiCorp Vault Transit Engine + Envelope Encryption (推荐生产)
    #   - "aws"   : AWS KMS Envelope Encryption (aioboto3,原生 async)
    #   - "aliyun": 阿里云 KMS (国内合规)
    #   - "local" : 本地 KMS 模拟 (开发/测试,等价 env)
    field_encryption_backend: str = "env"

    # Vault 配置 (field_encryption_backend=vault 时必填)
    vault_addr: Optional[str] = None
    # vault_auth_method: "token" | "approle" | "kubernetes"
    vault_auth_method: str = "token"
    vault_token: Optional[str] = None  # auth_method=token 时使用
    vault_role_id: Optional[str] = None  # auth_method=approle 时使用
    vault_secret_id: Optional[str] = None  # auth_method=approle 时使用
    vault_k8s_role: Optional[str] = None  # auth_method=kubernetes 时使用
    vault_namespace: Optional[str] = None
    vault_kv_mount: str = "secret"  # KV v2 mount point
    vault_transit_mount: str = "transit"  # Transit engine mount point
    vault_field_kek_name: str = "agentvalue-field-kek"  # Transit key name for field encryption
    vault_jwt_key_path: str = "agentvalue/jwt-signing-key"  # KV v2 path for JWT secret
    vault_verify_tls: bool = True

    # AWS KMS 配置 (field_encryption_backend=aws 时必填)
    aws_kms_key_id: Optional[str] = None  # alias/agentvalue-field-kek 或 key ARN
    aws_kms_region: Optional[str] = None  # 默认从环境推断

    # 阿里云 KMS 配置 (field_encryption_backend=aliyun 时必填)
    aliyun_kms_key_id: Optional[str] = None
    aliyun_kms_endpoint: Optional[str] = None  # kms.<region>.aliyuncs.com

    # DEK 缓存配置 (Envelope Encryption 性能关键)
    # 参考 AWS Encryption SDK 安全阈值: max_age 必填, max_messages/max_bytes 限制单 DEK 用量
    kms_dek_cache_ttl_seconds: int = 300  # 5 分钟,过期重新生成 DEK
    kms_dek_cache_max_size: int = 1000  # LRU 容量上限
    kms_dek_cache_max_messages: int = 100  # 单 DEK 最多加密 100 条消息
    kms_dek_cache_max_bytes: int = 64 * 1024 * 1024  # 单 DEK 最多加密 64MB

    # 护栏误报率统计开关
    guard_rails_metrics_enabled: bool = True

    # CORS 允许的源: 默认仅本地前端开发端口
    # 生产部署需通过环境变量 CORS_ORIGINS 配置为前端实际域名(逗号分隔)
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # JWT audience / issuer(P3-N4): 配置后 jwt.decode 做校验,防 token 跨服务复用
    # 留空时不校验(向后兼容),生产建议配置
    jwt_audience: Optional[str] = None
    jwt_issuer: Optional[str] = None
    # 时钟漂移容忍秒数(分布式部署多节点时钟未同步时避免误判过期)
    jwt_leeway_seconds: int = 30

    # Vision 模型配置(P1-2): ModelRouter 注入 Provider 时使用
    vision_model: str = "gpt-4o-mini"

    # /metrics 端点鉴权(P0 修复): 默认 "ip"(仅允许 127.0.0.1 + RFC1918 私网段),
    # "token" 走 Bearer 校验(需配 METRICS_BEARER_TOKEN),"none" 关闭鉴权(仅本地开发)
    metrics_auth_mode: str = "ip"
    metrics_bearer_token: Optional[str] = None
    # IP 白名单(逗号分隔 CIDR 或单 IP),仅 metrics_auth_mode=ip 时生效,
    # 留空时仅放行 loopback + RFC1918(默认安全)
    metrics_allowed_ips: Optional[str] = None

    # P2-1: Prometheus 查询地址(供 admin/analytics 时序聚合使用)
    # 默认本地 Prometheus,生产部署需通过环境变量 PROMETHEUS_URL 配置实际地址
    prometheus_url: str = "http://localhost:9090"

    # P2-2: Rerank Provider 抽象(对标 Dify Rerank)
    # dummy(默认) / cohere / jina / bge
    # 未配置或 dummy 时 retrieve_context 行为完全等价于未启用 rerank(向后兼容)
    rerank_provider: str = "dummy"
    rerank_api_key: Optional[str] = None
    # 覆盖默认 endpoint(cohere: https://api.cohere.ai, jina: https://api.jina.ai)
    rerank_base_url: Optional[str] = None
    # 覆盖默认模型(cohere: rerank-multilingual-v3.0, jina: jina-reranker-v2-base-multilingual)
    rerank_model: Optional[str] = None
    # retrieve_context 默认 top_k(rerank 返回的文档数)
    rerank_top_k: int = 5

    @model_validator(mode="after")
    def _enforce_prod_demo_mode_guard(self) -> "Settings":
        """
        生产环境守护：当处于生产环境（AGENTVALUE_ENV=production）且开启演示模式时，
        直接禁止实例化，避免身份伪造能力泄漏到生产。

        安全设计要点：
        - 仅当 agentvalue_env == "production" 时才校验，其余情况（含默认 None）完全放行；
        - 现有测试套件不设置 AGENTVALUE_ENV，且 conftest 通过 monkeypatch 在已实例化
          对象上修改 auth_demo_mode（model_config 未开启 validate_assignment），
          不会再次触发本校验器，故对现有测试零影响。
        - 仅做 auth_demo_mode 硬失败(身份伪造 = 灾难级)；JWT/CORS/field_encryption_key
          等其余生产检查由 scripts/check_prod_readiness.py 作为 advisory gatekeeper
          返回 PASS/WARN/FAIL,分层设计避免本校验器抢占脚本检查项导致测试无法构造场景。
        """
        if self.agentvalue_env == "production" and self.auth_demo_mode:
            raise ValueError("生产环境禁止开启 AUTH_DEMO_MODE(auth_demo_mode)")
        return self


@lru_cache()
def get_settings() -> Settings:
    return Settings()
