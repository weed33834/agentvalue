"""
Embedding 服务：基于 OpenAI 兼容接口的统一 embedding 客户端。
支持云端（OpenAI / DeepSeek / 阿里云百炼等）和本地模型。
"""

import logging
from typing import List, Optional

from openai import AsyncOpenAI

from core.config import Settings, get_settings

logger = logging.getLogger(__name__)


class EmbeddingClient:
    """OpenAI 兼容 embedding 客户端

    兼容 ChromaDB 1.x EmbeddingFunction 协议（name/is_legacy/supported_spaces/__call__）。
    实际向量计算走 async `embed`/`embed_query`，由调用方预计算后通过 `embeddings=`/
    `query_embeddings=` 传入 ChromaDB；`__call__` 仅在 ChromaDB 内部 fallback 路径触发，
    本实现中通过同步事件循环调度 async `embed`，避免协议缺失导致 collection 校验失败。
    """

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        # AsyncOpenAI client 按 (api_key, base_url) 签名缓存，配置变更时按需重建
        # 支持 admin LLM 配置 API 运行时修改 embedding 相关字段后立即生效
        self._client_signature: Optional[tuple] = None
        self.client: Optional[AsyncOpenAI] = None
        self._has_real_key = False
        self.model = self.settings.embedding_model
        self.dimensions = self.settings.embedding_dimensions
        self._rebuild_client_if_needed()

    def _rebuild_client_if_needed(self) -> None:
        """检查 settings 中 embedding 相关字段是否变化，按需重建 AsyncOpenAI client。

        admin 通过 PUT /admin/llm-config 修改 embedding_api_key/base_url 后，
        下次 embed/embed_query 调用会自动检测并重建 client，无需重启。
        """
        api_key = (
            self.settings.embedding_api_key
            or self.settings.cloud_api_key
            or self.settings.openai_api_key
        )
        base_url = (
            self.settings.embedding_base_url
            or self.settings.cloud_base_url
            or self.settings.openai_base_url
        )
        signature = (api_key, base_url)
        if signature != self._client_signature:
            kwargs: dict = {}
            if base_url:
                kwargs["base_url"] = base_url
            kwargs["api_key"] = api_key or "dummy-key"
            self._has_real_key = bool(api_key)
            self.client = AsyncOpenAI(**kwargs)
            self._client_signature = signature
        # model/dimensions 每次都实时读（轻量）
        self.model = self.settings.embedding_model
        self.dimensions = self.settings.embedding_dimensions

    # ── ChromaDB 1.x EmbeddingFunction 协议 ──────────────────────────
    def name(self) -> str:
        return "agentvalue_openai_compatible"

    def is_legacy(self) -> bool:
        return False

    def supported_spaces(self) -> List[str]:
        return ["cosine", "l2", "ip"]

    def __call__(self, input: List[str]) -> List[List[float]]:
        """ChromaDB 同步 fallback 路径(实际向量已由 embed_query 预计算传入)

        P0 修复: 原实现在事件循环内返回零向量,ChromaDB fallback 路径触发的
        query 会被存为零向量,后续 cosine 相似度全为 0(与任何向量都不相关),
        导致 RAG 检索静默失效。业务无感知会持续用错向量。

        现在改为 raise,让调用方明确知道调用方式不对,应改用 embed_query 预计算。
        """
        import asyncio

        loop = asyncio.get_event_loop()
        if loop.is_running():
            # P0 修复: 不再返回零向量(静默数据损坏),直接抛错让调用方修代码路径
            raise RuntimeError(
                "EmbeddingClient.__call__ 不能在事件循环内同步调用,"
                "请改用 await embed_query() 预计算向量后传给 ChromaDB"
            )
        return loop.run_until_complete(self.embed(input))

    # ── 异步 API（项目主路径）─────────────────────────────────────────
    async def embed(self, texts: List[str]) -> List[List[float]]:
        """对文本列表进行 embedding，返回向量列表；失败时降级为零向量不阻断主流程"""
        if not texts:
            return []
        self._rebuild_client_if_needed()
        if not self._has_real_key:
            logger.warning("未配置 embedding key，返回零向量")
            return [[0.0] * self.dimensions for _ in texts]
        try:
            response = await self.client.embeddings.create(
                model=self.model,
                input=list(texts),
            )
            vectors = [item.embedding for item in response.data]
            for i, vec in enumerate(vectors):
                if len(vec) != self.dimensions:
                    logger.warning(
                        f"embedding 维度不一致: expected={self.dimensions}, got={len(vec)} for text[{i}]"
                    )
            return vectors
        except Exception as e:
            logger.warning(f"embedding 调用失败，降级为零向量: {e}")
            return [[0.0] * self.dimensions for _ in texts]

    async def embed_query(self, text: str) -> List[float]:
        """对单条查询文本进行 embedding"""
        vectors = await self.embed([text])
        return vectors[0]
