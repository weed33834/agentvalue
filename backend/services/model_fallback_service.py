"""
模型 Fallback 策略服务

对标阿里百炼 AI 网关秒级容灾：主模型故障时按 fallback chain 依次切换备用模型，
成功即返回，触发事件写入审计日志便于复盘。

核心方法:
- execute_with_fallback: 按 fallback chain 依次尝试候选模型，成功即返回
- create_chain / update_chain / delete_chain / list_chains / get_chain: CRUD（全部 tenant_id 过滤）

事务边界: 传入 session 时由调用方控制 commit；未传入 session 时内部自建会话并 commit。
所有方法接受 tenant_id 参数并过滤查询。
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import AsyncSessionLocal
from core.tenant_context import get_current_tenant
from models.model_fallback import FallbackChain
from models.models import DEFAULT_TENANT_ID

logger = logging.getLogger(__name__)


class ModelFallbackService:
    """模型 Fallback 策略服务

    支持两种使用模式:
    1. 路由层: ModelFallbackService(session) 配合 get_db 依赖，事务由路由控制
    2. 内部调用: ModelFallbackService() 无 session，内部自建会话并自动 commit
    """

    def __init__(self, session: Optional[AsyncSession] = None):
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> AsyncSession:
        if self._session is not None:
            return self._session
        self._session = AsyncSessionLocal()
        self._owns_session = True
        return self._session

    async def _commit_if_owned(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.commit()

    async def _close_if_owned(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    # ============================================================
    # Fallback 执行
    # ============================================================

    async def execute_with_fallback(
        self,
        tier: str,
        messages: List[Dict[str, Any]],
        tenant_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """按 fallback chain 依次尝试候选模型，成功即返回。

        选取策略:
        - 在当前租户的启用链中，优先选取主档位（chain_config 首项 tier）匹配 tier
          且 priority 最高的链；若无匹配则选取 priority 最高的启用链。
        - 按 chain_config 顺序依次尝试，命中即返回。
        - 每次切换到下一候选模型时记录 fallback 触发事件到审计日志。

        Args:
            tier: 期望的主模型档位（L0/L1/L2/L3），用于匹配 fallback 链
            messages: 对话消息列表 [{"role": "user", "content": "..."}]
            tenant_id: 租户 ID（未传则取当前上下文）
            **kwargs: 透传给 provider 的额外参数（如 temperature）

        Returns:
            {"content": str, "model": str, "tier": str, "provider": str,
             "attempts": [...], "fallback_triggered": bool}

        Raises:
            RuntimeError: 所有候选模型均失败
        """
        tid = tenant_id or get_current_tenant()
        chain = await self._select_chain(tid, tier)
        if chain is None or not chain.chain_config:
            raise RuntimeError(f"租户 {tid} 未配置可用的 fallback 链（tier={tier}）")

        # 延迟导入避免循环依赖
        from core.model_router import ModelRouter
        from core.providers.base import ChatMessage

        router = ModelRouter()
        attempts: List[Dict[str, Any]] = []
        fallback_triggered = False

        for idx, entry in enumerate(chain.chain_config):
            entry_tier = entry.get("tier", tier)
            provider_name = entry.get("provider", "unknown")
            model_name = entry.get("model", "unknown")
            timeout = entry.get("timeout", 30)
            max_retries = entry.get("max_retries", 1)

            # 切换到非主候选（idx > 0）即触发 fallback，记录审计事件
            if idx > 0 and not fallback_triggered:
                fallback_triggered = True
                prev_error = (
                    attempts[-1].get("error", "primary_failed")
                    if attempts
                    else "primary_failed"
                )
                await self._record_fallback_event(
                    tid,
                    chain,
                    from_entry=chain.chain_config[0],
                    to_entry=entry,
                    reason=prev_error,
                )

            attempt: Dict[str, Any] = {
                "index": idx,
                "tier": entry_tier,
                "provider": provider_name,
                "model": model_name,
                "status": "pending",
            }

            try:
                provider = router.get_provider(entry_tier)  # type: ignore[arg-type]
                chat_messages = [
                    ChatMessage(
                        role=m.get("role", "user"), content=m.get("content", "")
                    )
                    for m in messages
                ]
                # 按 max_retries 重试
                last_err: Optional[Exception] = None
                for _ in range(max(1, int(max_retries))):
                    try:
                        result = await provider.chat_completion(chat_messages)
                        attempt["status"] = "success"
                        attempt["model"] = result.model or model_name
                        attempts.append(attempt)
                        await self._commit_if_owned()
                        return {
                            "content": result.content,
                            "model": result.model or model_name,
                            "tier": entry_tier,
                            "provider": provider_name,
                            "attempts": attempts,
                            "fallback_triggered": fallback_triggered,
                        }
                    except Exception as e:  # noqa: BLE001
                        last_err = e
                        logger.warning(
                            "fallback 链 %s 候选 %s(%s) 调用失败: %s",
                            chain.name,
                            provider_name,
                            model_name,
                            e,
                        )
                # 重试耗尽
                attempt["status"] = "error"
                attempt["error"] = str(last_err) if last_err else "unknown"
                attempts.append(attempt)
            except Exception as e:  # noqa: BLE001
                # provider 获取失败等
                attempt["status"] = "error"
                attempt["error"] = str(e)
                attempts.append(attempt)
                logger.warning(
                    "fallback 链 %s 候选 %s(%s) 不可用: %s",
                    chain.name,
                    provider_name,
                    model_name,
                    e,
                )

        await self._commit_if_owned()
        raise RuntimeError(
            f"fallback 链 '{chain.name}' 所有候选模型均失败: "
            f"{[a.get('error') for a in attempts if a.get('status') == 'error']}"
        )

    async def _select_chain(self, tenant_id: str, tier: str) -> Optional[FallbackChain]:
        """选取当前租户匹配 tier 的最高优先级启用链。"""
        session = await self._get_session()
        try:
            result = await session.execute(
                select(FallbackChain)
                .where(
                    FallbackChain.tenant_id == tenant_id,
                    FallbackChain.enabled.is_(True),
                )
                .order_by(FallbackChain.priority.desc(), FallbackChain.id.asc())
            )
            chains = result.scalars().all()
            if not chains:
                return None
            # 优先选取主档位（首项 tier）匹配的链
            for c in chains:
                cfg = c.chain_config or []
                if cfg and cfg[0].get("tier") == tier:
                    return c
            # 无匹配则返回最高优先级链
            return chains[0]
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def _record_fallback_event(
        self,
        tenant_id: str,
        chain: FallbackChain,
        from_entry: Dict[str, Any],
        to_entry: Dict[str, Any],
        reason: str,
    ) -> None:
        """记录 fallback 触发事件到审计日志（失败不阻断主流程）。"""
        try:
            from services.audit_service import AuditService

            session = await self._get_session()
            audit = AuditService(session)
            await audit.log(
                actor_id="system",
                action="model_fallback_triggered",
                details={
                    "tenant_id": tenant_id,
                    "chain_id": chain.id,
                    "chain_name": chain.name,
                    "from_tier": from_entry.get("tier"),
                    "from_model": from_entry.get("model"),
                    "to_tier": to_entry.get("tier"),
                    "to_model": to_entry.get("model"),
                    "reason": reason,
                },
                tenant_id=tenant_id,
            )
            await self._commit_if_owned()
        except Exception:
            logger.exception("记录 fallback 审计事件失败（不阻断主流程）")

    # ============================================================
    # CRUD
    # ============================================================

    async def create_chain(
        self,
        tenant_id: str,
        name: str,
        chain_config: List[Dict[str, Any]],
        description: Optional[str] = None,
        enabled: bool = True,
        priority: int = 0,
    ) -> Dict[str, Any]:
        """创建 fallback 链。"""
        session = await self._get_session()
        try:
            chain = FallbackChain(
                tenant_id=tenant_id,
                name=name,
                description=description,
                chain_config=chain_config,
                enabled=enabled,
                priority=priority,
            )
            session.add(chain)
            await session.flush()
            await self._commit_if_owned()
            logger.info("租户 %s 创建 fallback 链 %s", tenant_id, name)
            return self._serialize(chain)
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def update_chain(
        self, chain_id: int, tenant_id: str, **kwargs: Any
    ) -> Optional[Dict[str, Any]]:
        """更新 fallback 链（仅允许更新本租户链）。"""
        allowed = {
            "name",
            "description",
            "chain_config",
            "enabled",
            "priority",
        }
        session = await self._get_session()
        try:
            chain = await self._get_chain_owned(session, chain_id, tenant_id)
            if chain is None:
                return None
            for key, value in kwargs.items():
                if key in allowed and value is not None:
                    setattr(chain, key, value)
            await session.flush()
            await self._commit_if_owned()
            return self._serialize(chain)
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def delete_chain(self, chain_id: int, tenant_id: str) -> bool:
        """删除 fallback 链（仅允许删除本租户链）。"""
        session = await self._get_session()
        try:
            chain = await self._get_chain_owned(session, chain_id, tenant_id)
            if chain is None:
                return False
            await session.delete(chain)
            await session.flush()
            await self._commit_if_owned()
            return True
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def list_chains(
        self, tenant_id: str, enabled_only: bool = False
    ) -> List[Dict[str, Any]]:
        """列出当前租户的 fallback 链（按 priority 降序）。"""
        session = await self._get_session()
        try:
            stmt = (
                select(FallbackChain)
                .where(FallbackChain.tenant_id == tenant_id)
                .order_by(FallbackChain.priority.desc(), FallbackChain.id.asc())
            )
            if enabled_only:
                stmt = stmt.where(FallbackChain.enabled.is_(True))
            result = await session.execute(stmt)
            return [self._serialize(c) for c in result.scalars().all()]
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def get_chain(
        self, chain_id: int, tenant_id: str
    ) -> Optional[Dict[str, Any]]:
        """获取 fallback 链详情（仅本租户）。"""
        session = await self._get_session()
        try:
            chain = await self._get_chain_owned(session, chain_id, tenant_id)
            return self._serialize(chain) if chain else None
        finally:
            if self._owns_session:
                await self._close_if_owned()

    # ============================================================
    # 内部工具
    # ============================================================

    @staticmethod
    async def _get_chain_owned(
        session: AsyncSession, chain_id: int, tenant_id: str
    ) -> Optional[FallbackChain]:
        result = await session.execute(
            select(FallbackChain).where(
                FallbackChain.id == chain_id,
                FallbackChain.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _serialize(chain: FallbackChain) -> Dict[str, Any]:
        return {
            "id": chain.id,
            "tenant_id": chain.tenant_id,
            "name": chain.name,
            "description": chain.description,
            "chain_config": chain.chain_config,
            "enabled": chain.enabled,
            "priority": chain.priority,
            "created_at": chain.created_at.isoformat() if chain.created_at else None,
            "updated_at": chain.updated_at.isoformat() if chain.updated_at else None,
        }
