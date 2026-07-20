"""
Feature Flag Service SDK (P3-2: 应用级功能开关, 对标 Langfuse Feature Flag)

提供运行时功能开关判定, 支持:
- 精确受众: 指定 tenant_id / user_id 直接命中
- 百分比灰度: hash(user_id or tenant_id) % 100 < percentage
- LRU 缓存: 60s TTL, 减少 DB 压力, update/delete 后自动失效

判定规则 (is_enabled):
1. flag 不存在或 enabled=False → False
2. user_id 在 target_user_ids → True
3. tenant_id 在 target_tenant_ids → True
4. rollout_percentage > 0 → hash(user_id or tenant_id) % 100 < percentage → True
5. 默认 False

集成示例:
    flag_service = FeatureFlagService(session_factory)
    if await flag_service.is_enabled("use_rerank_v2", tenant_id="tenant_a"):
        # 启用 rerank v2 路径
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.feature_flag import FeatureFlag

logger = logging.getLogger(__name__)


# 缓存 TTL (秒): 60s 内重复查询走缓存, 减少 DB 压力
_CACHE_TTL_SECONDS = 60

# 允许的 category 白名单
_ALLOWED_CATEGORIES = {"general", "model", "agent", "feature"}


class FeatureFlagService:
    """Feature Flag 服务 (SDK)

    通过 session_factory 获取 DB session, 业务层与路由层共用同一实例。
    LRU 缓存: 60s TTL, update/delete 后自动清缓存, 保证配置变更即时生效。
    """

    def __init__(self, session_factory: Callable[[], AsyncSession]):
        """初始化

        Args:
            session_factory: 返回 AsyncSession 的工厂 (通常是 AsyncSessionLocal)
        """
        self._session_factory = session_factory
        # 缓存: key → FeatureFlag (or None 表示不存在)
        self._cache: Dict[str, Optional[FeatureFlag]] = {}
        # 缓存过期时间: key → expiry_ts
        self._cache_expiry: Dict[str, float] = {}

    # ----------------------------------------------------------
    # 缓存管理
    # ----------------------------------------------------------

    def _cache_get(self, key: str) -> tuple:
        """从缓存读取

        Returns:
            (hit, value): hit=True 表示命中缓存 (value 可能为 None 表示"flag 不存在"),
                          hit=False 表示未缓存或已过期
        """
        expiry = self._cache_expiry.get(key)
        if expiry is None:
            return (False, None)  # 未缓存
        if time.time() > expiry:
            # 过期, 清理
            self._cache.pop(key, None)
            self._cache_expiry.pop(key, None)
            return (False, None)
        return (True, self._cache.get(key))

    def _cache_set(self, key: str, flag: Optional[FeatureFlag]) -> None:
        """写入缓存 (flag=None 表示"flag 不存在", 也缓存以避免重复查 DB)"""
        self._cache[key] = flag
        self._cache_expiry[key] = time.time() + _CACHE_TTL_SECONDS

    def invalidate(self, key: Optional[str] = None) -> None:
        """清除缓存

        Args:
            key: 指定 key 时只清该 key; None 时清空全部缓存
        """
        if key is None:
            self._cache.clear()
            self._cache_expiry.clear()
        else:
            self._cache.pop(key, None)
            self._cache_expiry.pop(key, None)

    # ----------------------------------------------------------
    # 核心判定
    # ----------------------------------------------------------

    @staticmethod
    def _hash_bucket(identifier: str) -> int:
        """对标识 (user_id / tenant_id) 做稳定 hash, 返回 0-99 的桶号

        用 sha256 取前 8 字节 → int → mod 100, 保证 0-99 均匀分布且跨进程稳定。
        """
        h = hashlib.sha256(identifier.encode("utf-8")).digest()
        # 取前 8 字节 (64-bit), 避免 int 过大
        bucket = int.from_bytes(h[:8], byteorder="big") % 100
        return bucket

    async def is_enabled(
        self,
        key: str,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> bool:
        """检查 flag 是否启用

        规则:
        1. flag 不存在或 enabled=False → False
        2. user_id 在 target_user_ids → True
        3. tenant_id 在 target_tenant_ids → True
        4. rollout_percentage > 0 → hash(user_id or tenant_id) % 100 < percentage
        5. 默认 False

        Args:
            key: flag 的业务 key
            tenant_id: 可选, 租户 ID
            user_id: 可选, 用户 ID

        Returns:
            bool: 是否启用
        """
        flag = await self.get_flag(key)
        if flag is None or not flag.enabled:
            return False

        # 规则 2: 精确用户命中
        if user_id and user_id in (flag.target_user_ids or []):
            return True

        # 规则 3: 精确租户命中
        if tenant_id and tenant_id in (flag.target_tenant_ids or []):
            return True

        # 规则 4: 百分比灰度
        if flag.rollout_percentage > 0:
            # 优先用 user_id 做分流 (粒度更细), 无则用 tenant_id
            identifier = user_id or tenant_id
            if identifier:
                bucket = self._hash_bucket(identifier)
                if bucket < flag.rollout_percentage:
                    return True

        # 规则 5: 默认 False
        return False

    async def explain(
        self,
        key: str,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """解释判定结果 (用于 admin /check 端点)

        Returns:
            {enabled: bool, reason: str}
        """
        flag = await self.get_flag(key)
        if flag is None:
            return {"enabled": False, "reason": "flag_not_found"}
        if not flag.enabled:
            return {"enabled": False, "reason": "flag_disabled"}

        if user_id and user_id in (flag.target_user_ids or []):
            return {"enabled": True, "reason": "target_user_hit"}
        if tenant_id and tenant_id in (flag.target_tenant_ids or []):
            return {"enabled": True, "reason": "target_tenant_hit"}

        if flag.rollout_percentage > 0:
            identifier = user_id or tenant_id
            if identifier:
                bucket = self._hash_bucket(identifier)
                if bucket < flag.rollout_percentage:
                    return {
                        "enabled": True,
                        "reason": "rollout_percentage_hit",
                        "bucket": bucket,
                        "percentage": flag.rollout_percentage,
                    }
                else:
                    return {
                        "enabled": False,
                        "reason": "rollout_percentage_miss",
                        "bucket": bucket,
                        "percentage": flag.rollout_percentage,
                    }

        return {"enabled": False, "reason": "default_off"}

    # ----------------------------------------------------------
    # CRUD
    # ----------------------------------------------------------

    async def get_flag(self, key: str) -> Optional[FeatureFlag]:
        """获取单个 flag (走缓存)

        Args:
            key: flag 的业务 key

        Returns:
            FeatureFlag 或 None (不存在时)
        """
        # 1. 先查缓存 (None 也缓存以避免 DB 中不存在的 key 重复打 DB)
        hit, cached = self._cache_get(key)
        if hit:
            return cached
        # 2. 查 DB
        async with self._session_factory() as session:
            flag = await session.get(FeatureFlag, key)
        # 3. 写缓存
        self._cache_set(key, flag)
        return flag

    async def list_flags(
        self, category: Optional[str] = None
    ) -> List[FeatureFlag]:
        """列出所有 flag (支持 category 过滤)

        列表查询不走缓存 (避免缓存膨胀), 直接查 DB。
        """
        async with self._session_factory() as session:
            stmt = select(FeatureFlag)
            if category:
                stmt = stmt.where(FeatureFlag.category == category)
            stmt = stmt.order_by(FeatureFlag.created_at.desc())
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def create_flag(
        self,
        key: str,
        description: str = "",
        enabled: bool = False,
        rollout_percentage: int = 0,
        target_tenant_ids: Optional[List[str]] = None,
        target_user_ids: Optional[List[str]] = None,
        category: str = "general",
    ) -> FeatureFlag:
        """创建 flag

        Args:
            key: 业务 key (主键, 不可改)
            description: 用途描述
            enabled: 全局开关
            rollout_percentage: 灰度百分比 0-100
            target_tenant_ids: 精确受众租户列表
            target_user_ids: 精确受众用户列表
            category: 分类 (general/model/agent/feature)

        Returns:
            创建后的 FeatureFlag

        Raises:
            ValueError: 参数非法
        """
        # 参数校验
        if not key or not key.strip():
            raise ValueError("key 不能为空")
        if not (0 <= rollout_percentage <= 100):
            raise ValueError("rollout_percentage 必须在 0-100 之间")
        if category not in _ALLOWED_CATEGORIES:
            raise ValueError(
                f"category 必须为 {_ALLOWED_CATEGORIES} 之一, 实际: {category}"
            )

        async with self._session_factory() as session:
            existing = await session.get(FeatureFlag, key)
            if existing is not None:
                raise ValueError(f"flag {key!r} 已存在")
            flag = FeatureFlag(
                key=key,
                description=description,
                enabled=enabled,
                rollout_percentage=rollout_percentage,
                target_tenant_ids=target_tenant_ids or [],
                target_user_ids=target_user_ids or [],
                category=category,
            )
            session.add(flag)
            await session.commit()
            await session.refresh(flag)
        # 新建后清缓存 (虽然不存在旧值, 仍清避免边界)
        self.invalidate(key)
        return flag

    async def update_flag(
        self, key: str, **fields: Any
    ) -> Optional[FeatureFlag]:
        """更新 flag (任意字段)

        Args:
            key: 业务 key
            **fields: 可更新字段 (description/enabled/rollout_percentage/
                     target_tenant_ids/target_user_ids/category)

        Returns:
            更新后的 FeatureFlag 或 None (不存在时)

        Raises:
            ValueError: 参数非法
        """
        if not fields:
            raise ValueError("未提供任何更新字段")

        # 参数校验
        if "rollout_percentage" in fields:
            rp = fields["rollout_percentage"]
            if rp is not None and not (0 <= rp <= 100):
                raise ValueError("rollout_percentage 必须在 0-100 之间")
        if "category" in fields:
            cat = fields["category"]
            if cat is not None and cat not in _ALLOWED_CATEGORIES:
                raise ValueError(
                    f"category 必须为 {_ALLOWED_CATEGORIES} 之一, 实际: {cat}"
                )
        if "target_tenant_ids" in fields and fields["target_tenant_ids"] is None:
            fields["target_tenant_ids"] = []
        if "target_user_ids" in fields and fields["target_user_ids"] is None:
            fields["target_user_ids"] = []

        async with self._session_factory() as session:
            flag = await session.get(FeatureFlag, key)
            if flag is None:
                return None
            for k, v in fields.items():
                if hasattr(flag, k) and k != "key":  # key 不可改
                    setattr(flag, k, v)
            await session.commit()
            await session.refresh(flag)
        # 更新后清缓存
        self.invalidate(key)
        return flag

    async def delete_flag(self, key: str) -> bool:
        """删除 flag

        Returns:
            True 表示已删除, False 表示不存在
        """
        async with self._session_factory() as session:
            flag = await session.get(FeatureFlag, key)
            if flag is None:
                return False
            await session.delete(flag)
            await session.commit()
        # 删除后清缓存
        self.invalidate(key)
        return True

    async def toggle_flag(self, key: str, enabled: bool) -> Optional[FeatureFlag]:
        """切换 enabled 状态 (便捷方法)

        Returns:
            更新后的 FeatureFlag 或 None (不存在时)
        """
        return await self.update_flag(key, enabled=enabled)
