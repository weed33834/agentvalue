"""v3 特性路由注册中心。

设计目的
--------
`main.py` 中已有 100+ 处 `app.include_router(...)`，继续在其中追加会造成
多人（多任务）并行开发时的合并冲突热点。本模块提供一个集中式、声明式的
注册表：新增特性只需在 ``ROUTER_SPECS`` 中追加一条 ``RouterSpec``，
``main.py`` 只保留一次 ``register_feature_routers(app)`` 调用。

约定
----
1. 每条 spec 通过 ``module`` + ``attr`` 惰性导入，避免 ``main.py`` 顶部
   import 列表继续膨胀，也避免任一特性模块 import 失败时拖垮整个应用。
2. ``optional=True`` 的 spec 在导入失败时只记录 warning 并跳过，
   用于依赖可选三方库的特性（例如需要 redis / scipy 的模块）。
   ``optional=False``（默认）导入失败时直接抛出，避免静默丢失路由。
3. 注册顺序 = 列表顺序。含动态路径段（``/{id}``）的路由应排在
   同前缀静态路由之后，防止 FastAPI 把 ``/stats`` 误匹配到 ``/{id}``。
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from fastapi import FastAPI

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RouterSpec:
    """单个特性路由的声明。

    Attributes:
        module: 模块路径，例如 ``"api.admin.trace_v2_routes"``。
        attr: 模块中 ``APIRouter`` 实例的变量名。
        tags: 传给 ``include_router`` 的 OpenAPI tags。
        optional: 为 True 时，导入失败仅告警跳过，不中断启动。
        note: 人类可读说明，便于在路由清单中溯源。
    """

    module: str
    attr: str
    tags: List[str] = field(default_factory=list)
    optional: bool = False
    note: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# 特性路由清单
# ─────────────────────────────────────────────────────────────────────────────
ROUTER_SPECS: List[RouterSpec] = [
    RouterSpec(
        module="api.admin.trace_v2_routes",
        attr="router",
        tags=["admin-traces-v2"],
        note="WS-1 原生 Trace/Span 存储 + 成本账本（对标 Langfuse / LangSmith）",
    ),
    RouterSpec(
        module="api.admin.experiment_routes",
        attr="router",
        tags=["admin-experiments"],
        note="WS-2 实验对比 + RAGAS 生成质量指标 (对标 Braintrust / Ragas)",
    ),
    # <<REGISTRY_SLOT_INTEGRATION>>
    RouterSpec(
        module="api.admin.webhook_subscription_routes",
        attr="router",
        tags=["admin-webhook-subscriptions"],
        note="WS-3 出站 Webhook 订阅/投递/重试/死信 (对标 Svix)",
    ),
    RouterSpec(
        module="api.public.v1_routes",
        attr="router",
        tags=["public-api-v1"],
        note="WS-3 公网 API Key 门控的开放 API",
    ),
    # <<REGISTRY_SLOT_GOVERNANCE>>
    RouterSpec(
        module="api.admin.audit_integrity_routes",
        attr="router",
        tags=["admin-audit-integrity"],
        note="WS-4 审计哈希链完整性校验（防篡改）+ 链尾锚定",
    ),
    RouterSpec(
        module="api.admin.rate_limit_routes",
        attr="router",
        tags=["admin-rate-limits"],
        note="WS-4 分布式限流管理（状态/桶观测/重置）",
    ),
]


def register_feature_routers(app: FastAPI) -> int:
    """按 ``ROUTER_SPECS`` 顺序挂载全部特性路由。

    Args:
        app: FastAPI 应用实例。

    Returns:
        成功挂载的路由模块数量。

    Raises:
        ImportError / AttributeError: 当 ``optional=False`` 的 spec 无法加载时。
    """
    mounted = 0
    for spec in ROUTER_SPECS:
        try:
            module = importlib.import_module(spec.module)
            router = getattr(module, spec.attr)
        except Exception as exc:  # noqa: BLE001 - 需区分 optional 行为
            if spec.optional:
                logger.warning(
                    "跳过可选特性路由 %s.%s: %s", spec.module, spec.attr, exc
                )
                continue
            logger.error("特性路由加载失败 %s.%s: %s", spec.module, spec.attr, exc)
            raise

        app.include_router(router, tags=spec.tags or None)
        mounted += 1
        logger.debug("已挂载特性路由 %s.%s (%s)", spec.module, spec.attr, spec.note)

    logger.info("特性路由注册完成: %d/%d", mounted, len(ROUTER_SPECS))
    return mounted


def describe_registry() -> List[dict]:
    """返回注册表的可序列化描述，供 ``/admin/system/features`` 之类端点使用。"""
    return [
        {
            "module": s.module,
            "attr": s.attr,
            "tags": list(s.tags),
            "optional": s.optional,
            "note": s.note,
        }
        for s in ROUTER_SPECS
    ]


__all__ = [
    "RouterSpec",
    "ROUTER_SPECS",
    "register_feature_routers",
    "describe_registry",
]
