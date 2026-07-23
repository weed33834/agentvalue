"""
Provider 模板 seed 数据

对标 Dify Provider Schema (https://github.com/langgenius/dify/blob/main/api/core/model_runtime/model_providers)

每个 provider 声明:
- supported_model_types: 支持的模型类型 (llm/embedding/rerank/vision)
- configurate_methods: 配置方式 (predefined-model / customizable-model)
- provider_credential_schema: 凭证表单 schema (动态表单)
- model_credential_schema: 模型凭证表单 schema (customizable 才有)

模型声明 (ModelTemplate):
- features: chat / vision / function_calling / stream_tool_call
- model_properties: mode (chat/completion), context_size, max_tokens
- parameter_rules: 推理参数 schema
"""

from typing import Any, Dict, List

# ============================================================
# Provider 模板 (ProviderTemplate)
# ============================================================

OPENAI_PROVIDER_TEMPLATE: Dict[str, Any] = {
    "provider": "openai",
    "label": {"zh": "OpenAI", "en": "OpenAI"},
    "description": {
        "zh": "OpenAI GPT 系列模型,支持 Chat / Vision / Function Calling",
        "en": "OpenAI GPT series with Chat / Vision / Function Calling",
    },
    "icon_small": "openai_s.svg",
    "icon_large": "openai_l.svg",
    "background": "#10A37F",
    "supported_model_types": ["llm", "embedding", "rerank", "vision"],
    "configurate_methods": ["predefined-model", "customizable-model"],
    "provider_credential_schema": {
        "credential_form_schemas": [
            {
                "variable": "api_key",
                "label": {"zh": "API Key", "en": "API Key"},
                "type": "secret-input",
                "required": True,
                "placeholder": {"zh": "sk-...", "en": "sk-..."},
            },
            {
                "variable": "api_base",
                "label": {"zh": "API Base", "en": "API Base"},
                "type": "text-input",
                "required": False,
                "default": "https://api.openai.com/v1",
                "placeholder": {
                    "zh": "留空使用默认 https://api.openai.com/v1",
                    "en": "Leave empty for default",
                },
            },
            {
                "variable": "organization",
                "label": {"zh": "Organization ID", "en": "Organization ID"},
                "type": "text-input",
                "required": False,
            },
        ]
    },
    "model_credential_schema": {
        "model": {
            "label": {"zh": "模型名", "en": "Model Name"},
            "placeholder": {"zh": "如 gpt-4o-mini", "en": "e.g. gpt-4o-mini"},
        },
        "credential_form_schemas": [
            {
                "variable": "api_key",
                "label": {"zh": "API Key", "en": "API Key"},
                "type": "secret-input",
                "required": True,
            },
            {
                "variable": "api_base",
                "label": {"zh": "API Base", "en": "API Base"},
                "type": "text-input",
                "required": False,
            },
        ],
    },
}

ANTHROPIC_PROVIDER_TEMPLATE: Dict[str, Any] = {
    "provider": "anthropic",
    "label": {"zh": "Anthropic", "en": "Anthropic"},
    "description": {
        "zh": "Anthropic Claude 系列模型,支持 Chat / Vision / Tool Use",
        "en": "Anthropic Claude series with Chat / Vision / Tool Use",
    },
    "icon_small": "anthropic_s.svg",
    "icon_large": "anthropic_l.svg",
    "background": "#F0F0EB",
    "supported_model_types": ["llm", "vision"],
    "configurate_methods": ["predefined-model", "customizable-model"],
    "provider_credential_schema": {
        "credential_form_schemas": [
            {
                "variable": "api_key",
                "label": {"zh": "API Key", "en": "API Key"},
                "type": "secret-input",
                "required": True,
                "placeholder": {"zh": "sk-ant-...", "en": "sk-ant-..."},
            },
            {
                "variable": "api_base",
                "label": {"zh": "API Base", "en": "API Base"},
                "type": "text-input",
                "required": False,
                "default": "https://api.anthropic.com",
            },
        ]
    },
    "model_credential_schema": {
        "model": {
            "label": {"zh": "模型名", "en": "Model Name"},
            "placeholder": {"zh": "如 claude-3-5-sonnet-20241022", "en": ""},
        },
        "credential_form_schemas": [
            {
                "variable": "api_key",
                "label": {"zh": "API Key", "en": "API Key"},
                "type": "secret-input",
                "required": True,
            },
        ],
    },
}

GEMINI_PROVIDER_TEMPLATE: Dict[str, Any] = {
    "provider": "gemini",
    "label": {"zh": "Google Gemini", "en": "Google Gemini"},
    "description": {
        "zh": "Google Gemini 系列模型,支持 Chat / Vision / Function Calling",
        "en": "Google Gemini series with Chat / Vision / Function Calling",
    },
    "icon_small": "gemini_s.svg",
    "icon_large": "gemini_l.svg",
    "background": "#4285F4",
    "supported_model_types": ["llm", "embedding", "vision"],
    "configurate_methods": ["predefined-model", "customizable-model"],
    "provider_credential_schema": {
        "credential_form_schemas": [
            {
                "variable": "api_key",
                "label": {"zh": "API Key", "en": "API Key"},
                "type": "secret-input",
                "required": True,
                "placeholder": {"zh": "AIza...", "en": "AIza..."},
            },
        ]
    },
    "model_credential_schema": {
        "model": {
            "label": {"zh": "模型名", "en": "Model Name"},
            "placeholder": {"zh": "如 gemini-1.5-pro", "en": ""},
        },
        "credential_form_schemas": [
            {
                "variable": "api_key",
                "label": {"zh": "API Key", "en": "API Key"},
                "type": "secret-input",
                "required": True,
            },
        ],
    },
}

OLLAMA_PROVIDER_TEMPLATE: Dict[str, Any] = {
    "provider": "ollama",
    "label": {"zh": "Ollama (本地)", "en": "Ollama (Local)"},
    "description": {
        "zh": "Ollama 本地模型,支持自托管 Llama / Qwen / Mistral 等",
        "en": "Ollama local models: Llama / Qwen / Mistral",
    },
    "icon_small": "ollama_s.svg",
    "icon_large": "ollama_l.svg",
    "background": "#000000",
    "supported_model_types": ["llm", "embedding"],
    "configurate_methods": ["customizable-model"],
    "provider_credential_schema": {
        "credential_form_schemas": [
            {
                "variable": "api_base",
                "label": {"zh": "API Base", "en": "API Base"},
                "type": "text-input",
                "required": True,
                "default": "http://localhost:11434",
                "placeholder": {"zh": "http://localhost:11434", "en": ""},
            }
        ]
    },
    "model_credential_schema": {
        "model": {
            "label": {"zh": "模型名", "en": "Model Name"},
            "placeholder": {"zh": "如 llama3.2 / qwen2.5", "en": ""},
        },
        "credential_form_schemas": [
            {
                "variable": "api_base",
                "label": {"zh": "API Base", "en": "API Base"},
                "type": "text-input",
                "required": True,
            },
        ],
    },
}

PROVIDER_TEMPLATES: List[Dict[str, Any]] = [
    OPENAI_PROVIDER_TEMPLATE,
    ANTHROPIC_PROVIDER_TEMPLATE,
    GEMINI_PROVIDER_TEMPLATE,
    OLLAMA_PROVIDER_TEMPLATE,
]


# ============================================================
# 模型模板 (ModelTemplate)
# ============================================================


def _llm_param_rules() -> List[Dict[str, Any]]:
    """LLM 通用推理参数规则"""
    return [
        {
            "name": "temperature",
            "label": {"zh": "温度", "en": "Temperature"},
            "type": "float",
            "min": 0.0,
            "max": 2.0,
            "default": 0.3,
            "help": {
                "zh": "0 = 确定性,2 = 高随机性",
                "en": "0 = deterministic, 2 = high randomness",
            },
        },
        {
            "name": "max_tokens",
            "label": {"zh": "最大 Token", "en": "Max Tokens"},
            "type": "int",
            "min": 1,
            "max": 32768,
            "default": 4096,
        },
        {
            "name": "top_p",
            "label": {"zh": "Top P", "en": "Top P"},
            "type": "float",
            "min": 0.0,
            "max": 1.0,
            "default": 1.0,
        },
    ]


MODEL_TEMPLATES: List[Dict[str, Any]] = [
    # ========== OpenAI ==========
    {
        "provider": "openai",
        "model": "gpt-4o",
        "label": {"zh": "GPT-4o", "en": "GPT-4o"},
        "model_type": "llm",
        "features": ["chat", "vision", "function_calling", "stream_tool_call"],
        "model_properties": {
            "mode": "chat",
            "context_size": 128000,
            "max_tokens": 16384,
        },
        "parameter_rules": _llm_param_rules(),
        "pricing": {
            "input_per_1k": 0.0025,
            "output_per_1k": 0.01,
            "currency": "USD",
        },
    },
    {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "label": {"zh": "GPT-4o mini", "en": "GPT-4o mini"},
        "model_type": "llm",
        "features": ["chat", "vision", "function_calling", "stream_tool_call"],
        "model_properties": {
            "mode": "chat",
            "context_size": 128000,
            "max_tokens": 16384,
        },
        "parameter_rules": _llm_param_rules(),
        "pricing": {
            "input_per_1k": 0.00015,
            "output_per_1k": 0.0006,
            "currency": "USD",
        },
    },
    {
        "provider": "openai",
        "model": "text-embedding-3-small",
        "label": {"zh": "Text Embedding 3 Small", "en": "Text Embedding 3 Small"},
        "model_type": "embedding",
        "features": [],
        "model_properties": {"mode": "embedding", "max_chunks": 32, "dimensions": 1536},
        "parameter_rules": [],
        "pricing": {"input_per_1k": 0.00002, "output_per_1k": 0, "currency": "USD"},
    },
    {
        "provider": "openai",
        "model": "text-embedding-3-large",
        "label": {"zh": "Text Embedding 3 Large", "en": "Text Embedding 3 Large"},
        "model_type": "embedding",
        "features": [],
        "model_properties": {"mode": "embedding", "max_chunks": 32, "dimensions": 3072},
        "parameter_rules": [],
        "pricing": {"input_per_1k": 0.00013, "output_per_1k": 0, "currency": "USD"},
    },
    # ========== Anthropic ==========
    {
        "provider": "anthropic",
        "model": "claude-3-5-sonnet-20241022",
        "label": {"zh": "Claude 3.5 Sonnet", "en": "Claude 3.5 Sonnet"},
        "model_type": "llm",
        "features": ["chat", "vision", "function_calling", "stream_tool_call"],
        "model_properties": {
            "mode": "chat",
            "context_size": 200000,
            "max_tokens": 8192,
        },
        "parameter_rules": _llm_param_rules(),
        "pricing": {
            "input_per_1k": 0.003,
            "output_per_1k": 0.015,
            "currency": "USD",
        },
    },
    {
        "provider": "anthropic",
        "model": "claude-3-5-haiku-20241022",
        "label": {"zh": "Claude 3.5 Haiku", "en": "Claude 3.5 Haiku"},
        "model_type": "llm",
        "features": ["chat", "vision", "function_calling"],
        "model_properties": {
            "mode": "chat",
            "context_size": 200000,
            "max_tokens": 8192,
        },
        "parameter_rules": _llm_param_rules(),
        "pricing": {
            "input_per_1k": 0.0008,
            "output_per_1k": 0.004,
            "currency": "USD",
        },
    },
    {
        "provider": "anthropic",
        "model": "claude-3-opus-20240229",
        "label": {"zh": "Claude 3 Opus", "en": "Claude 3 Opus"},
        "model_type": "llm",
        "features": ["chat", "vision", "function_calling"],
        "model_properties": {
            "mode": "chat",
            "context_size": 200000,
            "max_tokens": 4096,
        },
        "parameter_rules": _llm_param_rules(),
        "pricing": {
            "input_per_1k": 0.015,
            "output_per_1k": 0.075,
            "currency": "USD",
        },
    },
    # ========== Gemini ==========
    {
        "provider": "gemini",
        "model": "gemini-1.5-pro",
        "label": {"zh": "Gemini 1.5 Pro", "en": "Gemini 1.5 Pro"},
        "model_type": "llm",
        "features": ["chat", "vision", "function_calling", "stream_tool_call"],
        "model_properties": {
            "mode": "chat",
            "context_size": 2000000,
            "max_tokens": 8192,
        },
        "parameter_rules": _llm_param_rules(),
        "pricing": {
            "input_per_1k": 0.00125,
            "output_per_1k": 0.005,
            "currency": "USD",
        },
    },
    {
        "provider": "gemini",
        "model": "gemini-1.5-flash",
        "label": {"zh": "Gemini 1.5 Flash", "en": "Gemini 1.5 Flash"},
        "model_type": "llm",
        "features": ["chat", "vision", "function_calling"],
        "model_properties": {
            "mode": "chat",
            "context_size": 1000000,
            "max_tokens": 8192,
        },
        "parameter_rules": _llm_param_rules(),
        "pricing": {
            "input_per_1k": 0.000075,
            "output_per_1k": 0.0003,
            "currency": "USD",
        },
    },
    {
        "provider": "gemini",
        "model": "text-embedding-004",
        "label": {"zh": "Text Embedding 004", "en": "Text Embedding 004"},
        "model_type": "embedding",
        "features": [],
        "model_properties": {"mode": "embedding", "max_chunks": 32, "dimensions": 768},
        "parameter_rules": [],
        "pricing": {"input_per_1k": 0, "output_per_1k": 0, "currency": "USD"},
    },
]


async def seed_provider_templates(session) -> None:
    """初始化 Provider 模板 + 模型模板到 DB(幂等)。

    Args:
        session: SQLAlchemy AsyncSession
    """
    import uuid

    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from models.provider_models import ModelTemplate, ProviderTemplate

    # 判断是否 SQLite(开发环境)
    is_sqlite = "sqlite" in str(session.bind.url).lower()

    for tmpl in PROVIDER_TEMPLATES:
        provider = tmpl["provider"]
        # 查询是否已存在
        stmt = select(ProviderTemplate).where(ProviderTemplate.provider == provider)
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing is None:
            # 插入新模板
            row = ProviderTemplate(
                id=str(uuid.uuid4()),
                provider=provider,
                label=tmpl["label"],
                description=tmpl.get("description"),
                icon_small=tmpl.get("icon_small"),
                icon_large=tmpl.get("icon_large"),
                background=tmpl.get("background"),
                supported_model_types=tmpl["supported_model_types"],
                configurate_methods=tmpl["configurate_methods"],
                provider_credential_schema=tmpl["provider_credential_schema"],
                model_credential_schema=tmpl.get("model_credential_schema"),
                is_builtin=True,
                enabled=True,
            )
            session.add(row)

    for m in MODEL_TEMPLATES:
        stmt = select(ModelTemplate).where(
            ModelTemplate.provider == m["provider"],
            ModelTemplate.model == m["model"],
            ModelTemplate.model_type == m["model_type"],
        )
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing is None:
            row = ModelTemplate(
                id=str(uuid.uuid4()),
                provider=m["provider"],
                model=m["model"],
                label=m["label"],
                model_type=m["model_type"],
                features=m.get("features", []),
                model_properties=m["model_properties"],
                parameter_rules=m.get("parameter_rules", []),
                pricing=m.get("pricing"),
                enabled=True,
            )
            session.add(row)

    await session.commit()
