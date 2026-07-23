"""
模型负载均衡服务

对标阿里百炼 AI 网关 GPU 感知负载均衡：
- 模型实例 CRUD（全部 tenant_id 过滤）
- 健康检查：发送简单请求到 /models 端点，记录延迟与状态
- 实例选择：支持 round_robin/weighted/least_connections/latency_aware 四种策略
- 并发计数：acquire/release 维护 current_load（线程安全，asyncio.Lock）
- 负载均衡配置 CRUD

事务边界: 传入 session 时由调用方控制 commit；未传入 session 时内部自建会话并 commit。
select_instance 使用 asyncio.Lock 保证并发安全。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import AsyncSessionLocal
from models.model_load_balancer_models import LoadBalancerConfig, ModelInstance

logger = logging.getLogger(__name__)


class ModelLoadBalancerService:
    """模型负载均衡服务

    支持两种使用模式:
    1. 路由层: ModelLoadBalancerService(session) 配合 get_db 依赖
    2. 内部调用: ModelLoadBalancerService() 无 session，内部自建会话并自动 commit

    select_instance 使用 asyncio.Lock 保证并发安全。
    """

    # 全局锁，保证 select_instance/acquire/release 的原子性
    _lock: asyncio.Lock = asyncio.Lock()
    # 轮询计数器（config_name -> index）
    _rr_counters: Dict[str, int] = {}

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
    # 模型实例 CRUD
    # ============================================================

    async def create_instance(
        self,
        tenant_id: str,
        name: str,
        provider: str,
        model_name: str,
        base_url: Optional[str] = None,
        api_key_ref: Optional[str] = None,
        weight: int = 1,
        max_concurrent: int = 10,
    ) -> Dict[str, Any]:
        """创建模型实例"""
        if provider not in ("openai", "local", "azure", "anthropic"):
            raise ValueError(f"不支持的 provider 类型: {provider}")
        if not name or not name.strip():
            raise ValueError("实例名称不能为空")

        session = await self._get_session()
        try:
            instance = ModelInstance(
                tenant_id=tenant_id,
                name=name.strip(),
                provider=provider,
                model_name=model_name,
                base_url=base_url,
                api_key_ref=api_key_ref,
                weight=weight,
                max_concurrent=max_concurrent,
                current_load=0,
                health_status="healthy",
            )
            session.add(instance)
            await session.flush()
            await self._commit_if_owned()
            logger.info(
                "租户 %s 创建模型实例 %s (%s/%s)",
                tenant_id,
                name,
                provider,
                model_name,
            )
            return self._serialize_instance(instance)
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def get_instance(
        self, instance_id: int, tenant_id: str
    ) -> Optional[Dict[str, Any]]:
        """获取模型实例详情"""
        session = await self._get_session()
        try:
            instance = await self._get_instance_owned(session, instance_id, tenant_id)
            return self._serialize_instance(instance) if instance else None
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def list_instances(
        self, tenant_id: str, enabled_only: bool = False
    ) -> List[Dict[str, Any]]:
        """列出当前租户的模型实例"""
        session = await self._get_session()
        try:
            stmt = (
                select(ModelInstance)
                .where(ModelInstance.tenant_id == tenant_id)
                .order_by(ModelInstance.id.asc())
            )
            if enabled_only:
                stmt = stmt.where(ModelInstance.enabled.is_(True))
            result = await session.execute(stmt)
            return [self._serialize_instance(i) for i in result.scalars().all()]
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def update_instance(
        self, instance_id: int, tenant_id: str, **kwargs: Any
    ) -> Optional[Dict[str, Any]]:
        """更新模型实例"""
        allowed = {
            "name",
            "provider",
            "model_name",
            "base_url",
            "api_key_ref",
            "weight",
            "max_concurrent",
            "enabled",
        }
        session = await self._get_session()
        try:
            instance = await self._get_instance_owned(session, instance_id, tenant_id)
            if instance is None:
                return None
            for key, value in kwargs.items():
                if key in allowed and value is not None:
                    setattr(instance, key, value)
            await session.flush()
            await self._commit_if_owned()
            return self._serialize_instance(instance)
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def delete_instance(self, instance_id: int, tenant_id: str) -> bool:
        """删除模型实例"""
        session = await self._get_session()
        try:
            instance = await self._get_instance_owned(session, instance_id, tenant_id)
            if instance is None:
                return False
            await session.delete(instance)
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

    # ============================================================
    # 健康检查
    # ============================================================

    async def health_check(
        self, instance_id: int, tenant_id: str
    ) -> Dict[str, Any]:
        """检查单个实例健康状态

        发送简单请求到 /models 端点，记录延迟与状态，
        更新 health_status 和 avg_latency_ms。

        Returns:
            {"instance_id": int, "healthy": bool, "latency_ms": float, "status": str}
        """
        session = await self._get_session()
        try:
            instance = await self._get_instance_owned(session, instance_id, tenant_id)
            if instance is None:
                raise ValueError(f"实例 {instance_id} 不存在")

            # 健康检查：请求 /models 端点
            base_url = (instance.base_url or "").rstrip("/")
            if not base_url:
                instance.health_status = "unhealthy"
                instance.last_health_check = datetime.now(timezone.utc)
                instance.avg_latency_ms = None
                await self._commit_if_owned()
                return {
                    "instance_id": instance_id,
                    "healthy": False,
                    "latency_ms": None,
                    "status": "unhealthy",
                    "error": "base_url 未配置",
                }

            health_url = f"{base_url}/models"
            start_time = time.monotonic()
            healthy = False
            error_msg: Optional[str] = None

            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(health_url)
                    healthy = resp.status_code == 200
                    if not healthy:
                        error_msg = f"HTTP {resp.status_code}"
            except Exception as e:
                error_msg = str(e)
                healthy = False

            latency_ms = (time.monotonic() - start_time) * 1000

            # 更新实例状态
            instance.last_health_check = datetime.now(timezone.utc)
            instance.avg_latency_ms = round(latency_ms, 2)
            if healthy:
                # 延迟 > 5000ms 标记为 degraded
                if latency_ms > 5000:
                    instance.health_status = "degraded"
                else:
                    instance.health_status = "healthy"
            else:
                instance.health_status = "unhealthy"

            await self._commit_if_owned()

            logger.info(
                "实例 %s 健康检查: status=%s, latency=%.2fms",
                instance_id,
                instance.health_status,
                latency_ms,
            )
            return {
                "instance_id": instance_id,
                "healthy": healthy,
                "latency_ms": round(latency_ms, 2),
                "status": instance.health_status,
                "error": error_msg,
            }
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def health_check_all(self, tenant_id: str) -> Dict[str, Any]:
        """检查当前租户所有启用的实例健康状态"""
        session = await self._get_session()
        try:
            instances = (
                await session.execute(
                    select(ModelInstance).where(
                        ModelInstance.tenant_id == tenant_id,
                        ModelInstance.enabled.is_(True),
                    )
                )
            ).scalars().all()
        finally:
            if self._owns_session:
                await self._close_if_owned()

        results = []
        for inst in instances:
            try:
                result = await self.health_check(inst.id, tenant_id)
                results.append(result)
            except Exception as e:
                results.append(
                    {
                        "instance_id": inst.id,
                        "healthy": False,
                        "status": "unhealthy",
                        "error": str(e),
                    }
                )

        healthy_count = sum(1 for r in results if r.get("healthy"))
        return {
            "tenant_id": tenant_id,
            "total": len(results),
            "healthy": healthy_count,
            "unhealthy": len(results) - healthy_count,
            "results": results,
        }

    # ============================================================
    # 实例选择与并发控制
    # ============================================================

    async def select_instance(
        self, config_name: str, tenant_id: str
    ) -> Optional[Dict[str, Any]]:
        """根据负载均衡策略选择实例

        线程安全（asyncio.Lock），根据 strategy 选择最优实例:
        - round_robin:       轮询
        - weighted:          按权重随机
        - least_connections: 最少连接数
        - latency_aware:     最低延迟优先

        Args:
            config_name: 负载均衡配置名称
            tenant_id: 租户 ID

        Returns:
            选中的实例 dict，无可用实例时返回 None
        """
        async with self._lock:
            session = await self._get_session()
            try:
                # 查找配置
                config = (
                    await session.execute(
                        select(LoadBalancerConfig).where(
                            LoadBalancerConfig.tenant_id == tenant_id,
                            LoadBalancerConfig.name == config_name,
                            LoadBalancerConfig.enabled.is_(True),
                        )
                    )
                ).scalar_one_or_none()

                if config is None:
                    logger.warning(
                        "负载均衡配置 %s 不存在或未启用 (租户: %s)",
                        config_name,
                        tenant_id,
                    )
                    return None

                # 获取配置中关联的启用且健康的实例
                instance_ids = [i["instance_id"] for i in config.instances]
                if not instance_ids:
                    return None

                instances = (
                    await session.execute(
                        select(ModelInstance).where(
                            ModelInstance.tenant_id == tenant_id,
                            ModelInstance.id.in_(instance_ids),
                            ModelInstance.enabled.is_(True),
                            ModelInstance.health_status.in_(["healthy", "degraded"]),
                            ModelInstance.current_load < ModelInstance.max_concurrent,
                        )
                    )
                ).scalars().all()

                if not instances:
                    logger.warning(
                        "配置 %s 无可用实例 (全部不健康或达到最大并发)", config_name
                    )
                    return None

                # 构建权重映射
                weight_map = {
                    i["instance_id"]: i.get("weight", 1) for i in config.instances
                }

                selected: Optional[ModelInstance] = None

                if config.strategy == "round_robin":
                    # 轮询：维护计数器
                    key = f"{tenant_id}:{config_name}"
                    idx = self._rr_counters.get(key, 0)
                    # 过滤掉不在可用列表中的
                    available = [inst for inst in instances]
                    if available:
                        selected = available[idx % len(available)]
                        self._rr_counters[key] = (idx + 1) % len(available)

                elif config.strategy == "weighted":
                    # 按权重随机
                    weighted: List[ModelInstance] = []
                    for inst in instances:
                        w = weight_map.get(inst.id, inst.weight)
                        weighted.extend([inst] * max(1, w))
                    if weighted:
                        selected = random.choice(weighted)

                elif config.strategy == "least_connections":
                    # 最少连接数
                    selected = min(instances, key=lambda i: i.current_load)

                elif config.strategy == "latency_aware":
                    # 最低延迟优先（延迟相同则最少连接）
                    selected = min(
                        instances,
                        key=lambda i: (
                            i.avg_latency_ms or float("inf"),
                            i.current_load,
                        ),
                    )

                else:
                    # 默认 round_robin
                    selected = instances[0] if instances else None

                if selected is None:
                    return None

                return self._serialize_instance(selected)
            finally:
                if self._owns_session:
                    await self._close_if_owned()

    async def acquire(self, instance_id: int, tenant_id: str) -> bool:
        """增加实例并发计数（线程安全）

        Returns:
            True 表示成功获取（未超过 max_concurrent），False 表示已达上限
        """
        async with self._lock:
            session = await self._get_session()
            try:
                instance = await self._get_instance_owned(session, instance_id, tenant_id)
                if instance is None:
                    return False
                if instance.current_load >= instance.max_concurrent:
                    return False
                instance.current_load += 1
                await self._commit_if_owned()
                logger.debug(
                    "实例 %s 并发计数 +1 (current=%s/%s)",
                    instance_id,
                    instance.current_load,
                    instance.max_concurrent,
                )
                return True
            finally:
                if self._owns_session:
                    await self._close_if_owned()

    async def release(self, instance_id: int, tenant_id: str) -> bool:
        """减少实例并发计数（线程安全）

        Returns:
            True 表示成功释放，False 表示实例不存在
        """
        async with self._lock:
            session = await self._get_session()
            try:
                instance = await self._get_instance_owned(session, instance_id, tenant_id)
                if instance is None:
                    return False
                if instance.current_load > 0:
                    instance.current_load -= 1
                await self._commit_if_owned()
                logger.debug(
                    "实例 %s 并发计数 -1 (current=%s/%s)",
                    instance_id,
                    instance.current_load,
                    instance.max_concurrent,
                )
                return True
            finally:
                if self._owns_session:
                    await self._close_if_owned()

    # ============================================================
    # 负载均衡配置 CRUD
    # ============================================================

    async def create_lb_config(
        self,
        tenant_id: str,
        name: str,
        strategy: str,
        instances: List[Dict[str, Any]],
        enabled: bool = True,
    ) -> Dict[str, Any]:
        """创建负载均衡配置"""
        if strategy not in (
            "round_robin",
            "weighted",
            "least_connections",
            "latency_aware",
        ):
            raise ValueError(f"不支持的策略: {strategy}")
        if not name or not name.strip():
            raise ValueError("配置名称不能为空")
        if not instances:
            raise ValueError("实例列表不能为空")

        session = await self._get_session()
        try:
            config = LoadBalancerConfig(
                tenant_id=tenant_id,
                name=name.strip(),
                strategy=strategy,
                instances=instances,
                enabled=enabled,
            )
            session.add(config)
            await session.flush()
            await self._commit_if_owned()
            logger.info(
                "租户 %s 创建负载均衡配置 %s (策略: %s)",
                tenant_id,
                name,
                strategy,
            )
            return self._serialize_config(config)
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def get_lb_config(
        self, config_id: int, tenant_id: str
    ) -> Optional[Dict[str, Any]]:
        """获取负载均衡配置详情"""
        session = await self._get_session()
        try:
            config = await self._get_config_owned(session, config_id, tenant_id)
            return self._serialize_config(config) if config else None
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def list_lb_configs(
        self, tenant_id: str, enabled_only: bool = False
    ) -> List[Dict[str, Any]]:
        """列出当前租户的负载均衡配置"""
        session = await self._get_session()
        try:
            stmt = (
                select(LoadBalancerConfig)
                .where(LoadBalancerConfig.tenant_id == tenant_id)
                .order_by(LoadBalancerConfig.id.asc())
            )
            if enabled_only:
                stmt = stmt.where(LoadBalancerConfig.enabled.is_(True))
            result = await session.execute(stmt)
            return [self._serialize_config(c) for c in result.scalars().all()]
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def update_lb_config(
        self, config_id: int, tenant_id: str, **kwargs: Any
    ) -> Optional[Dict[str, Any]]:
        """更新负载均衡配置"""
        allowed = {"name", "strategy", "instances", "enabled"}
        session = await self._get_session()
        try:
            config = await self._get_config_owned(session, config_id, tenant_id)
            if config is None:
                return None
            for key, value in kwargs.items():
                if key in allowed and value is not None:
                    setattr(config, key, value)
            await session.flush()
            await self._commit_if_owned()
            return self._serialize_config(config)
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def delete_lb_config(self, config_id: int, tenant_id: str) -> bool:
        """删除负载均衡配置"""
        session = await self._get_session()
        try:
            config = await self._get_config_owned(session, config_id, tenant_id)
            if config is None:
                return False
            await session.delete(config)
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

    # ============================================================
    # 内部工具
    # ============================================================

    @staticmethod
    async def _get_instance_owned(
        session: AsyncSession, instance_id: int, tenant_id: str
    ) -> Optional[ModelInstance]:
        result = await session.execute(
            select(ModelInstance).where(
                ModelInstance.id == instance_id,
                ModelInstance.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _get_config_owned(
        session: AsyncSession, config_id: int, tenant_id: str
    ) -> Optional[LoadBalancerConfig]:
        result = await session.execute(
            select(LoadBalancerConfig).where(
                LoadBalancerConfig.id == config_id,
                LoadBalancerConfig.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _serialize_instance(instance: ModelInstance) -> Dict[str, Any]:
        return {
            "id": instance.id,
            "tenant_id": instance.tenant_id,
            "name": instance.name,
            "provider": instance.provider,
            "model_name": instance.model_name,
            "base_url": instance.base_url,
            "api_key_ref": instance.api_key_ref,
            "weight": instance.weight,
            "max_concurrent": instance.max_concurrent,
            "current_load": instance.current_load,
            "health_status": instance.health_status,
            "last_health_check": instance.last_health_check.isoformat()
            if instance.last_health_check
            else None,
            "avg_latency_ms": instance.avg_latency_ms,
            "enabled": instance.enabled,
            "created_at": instance.created_at.isoformat()
            if instance.created_at
            else None,
            "updated_at": instance.updated_at.isoformat()
            if instance.updated_at
            else None,
        }

    @staticmethod
    def _serialize_config(config: LoadBalancerConfig) -> Dict[str, Any]:
        return {
            "id": config.id,
            "tenant_id": config.tenant_id,
            "name": config.name,
            "strategy": config.strategy,
            "instances": config.instances,
            "enabled": config.enabled,
            "created_at": config.created_at.isoformat()
            if config.created_at
            else None,
            "updated_at": config.updated_at.isoformat()
            if config.updated_at
            else None,
        }
