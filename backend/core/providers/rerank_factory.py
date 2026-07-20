"""
Rerank Provider 工厂 (P2-2)

按 settings.rerank_provider 字段选择具体实现:
- dummy (默认): DummyRerankProvider, 保持原顺序, 向后兼容
- cohere: CohereRerankProvider
- jina: JinaRerankProvider
- bge: BGERerankProvider (本地, 需 sentence-transformers)

未配置或凭证缺失时降级到 DummyRerankProvider, 不阻断主流程。
"""

import logging
from typing import Optional

from core.config import Settings
from core.providers.rerank_provider import (
    BGERerankProvider,
    CohereRerankProvider,
    DummyRerankProvider,
    JinaRerankProvider,
    RerankProvider,
)

logger = logging.getLogger(__name__)


def create_rerank_provider(settings: Settings) -> RerankProvider:
    """按 settings 创建 RerankProvider 实例

    选择逻辑:
    1. settings.rerank_provider 为 "dummy" / None / 未配置 → DummyRerankProvider
    2. "cohere" / "jina": 凭证缺失时降级 Dummy 并 warning
    3. "bge": 依赖缺失时降级 Dummy 并 warning (不强制安装 transformers)
    4. 未知 provider 名: 降级 Dummy 并 warning

    返回的实例应缓存 (类似 model_router 模式), 避免每次 retrieve_context 都重建。
    """
    provider_name = (getattr(settings, "rerank_provider", None) or "dummy").lower()
    api_key = getattr(settings, "rerank_api_key", None)
    base_url = getattr(settings, "rerank_base_url", None)
    model = getattr(settings, "rerank_model", None)

    if provider_name == "dummy":
        return DummyRerankProvider()

    if provider_name == "cohere":
        if not api_key:
            logger.warning(
                "rerank_provider=cohere 但 rerank_api_key 未配置, 降级使用 DummyRerankProvider"
            )
            return DummyRerankProvider()
        try:
            return CohereRerankProvider(
                api_key=api_key, model=model, base_url=base_url
            )
        except Exception as e:
            logger.warning(
                "CohereRerankProvider 初始化失败, 降级 Dummy: %s", e
            )
            return DummyRerankProvider()

    if provider_name == "jina":
        if not api_key:
            logger.warning(
                "rerank_provider=jina 但 rerank_api_key 未配置, 降级使用 DummyRerankProvider"
            )
            return DummyRerankProvider()
        try:
            return JinaRerankProvider(
                api_key=api_key, model=model, base_url=base_url
            )
        except Exception as e:
            logger.warning(
                "JinaRerankProvider 初始化失败, 降级 Dummy: %s", e
            )
            return DummyRerankProvider()

    if provider_name == "bge":
        try:
            return BGERerankProvider(model=model, base_url=base_url, api_key=api_key)
        except NotImplementedError as e:
            # 依赖缺失, 不强制安装, 降级 Dummy
            logger.warning(
                "BGERerankProvider 不可用 (%s), 降级使用 DummyRerankProvider", e
            )
            return DummyRerankProvider()
        except Exception as e:
            logger.warning(
                "BGERerankProvider 初始化失败, 降级 Dummy: %s", e
            )
            return DummyRerankProvider()

    # 未知 provider 名: 降级 Dummy
    logger.warning(
        "未知 rerank_provider=%r, 降级使用 DummyRerankProvider", provider_name
    )
    return DummyRerankProvider()
