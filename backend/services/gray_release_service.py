"""灰度发布 / 蓝绿部署服务

提供 Agent 版本的灰度发布能力 (对标 Bisheng / Langfuse Canary 发布):
- 创建 / 列出 / 获取灰度发布策略
- 启动 / 暂停 / 完成 / 回滚灰度发布
- 路由决策 route_request: 根据灰度策略决定使用哪个版本

状态机:
  draft (草稿) → active (灰度中) → completed (完成, 100% 切换)
                  ↕ paused (暂停)
  active / paused → rolled_back (回滚)

事务边界由路由层控制 (service 层不 commit)。
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.gray_release_models import GrayRelease

logger = logging.getLogger(__name__)

# 允许的发布类型
RELEASE_TYPE_CANARY = "canary"
RELEASE_TYPE_BLUE_GREEN = "blue_green"
RELEASE_TYPE_ROLLING = "rolling"
RELEASE_TYPES = {RELEASE_TYPE_CANARY, RELEASE_TYPE_BLUE_GREEN, RELEASE_TYPE_ROLLING}

# 允许的发布状态
STATUS_DRAFT = "draft"
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_ROLLED_BACK = "rolled_back"
# 终态: 不可再流转
TERMINAL_STATUSES = {STATUS_COMPLETED, STATUS_ROLLED_BACK}


class GrayReleaseService:
    """灰度发布管理服务 (数据库实现)"""

    def __init__(self, session: AsyncSession):
        self.session = session

    # ===================== CRUD =====================

    async def create_release(
        self,
        name: str,
        agent_id: int,
        version_id: int,
        release_type: str,
        traffic_percentage: int,
        config: Optional[dict] = None,
        *,
        tenant_id: str = "default",
        description: Optional[str] = None,
    ) -> GrayRelease:
        """创建灰度发布策略 (初始状态 draft)

        Args:
            name: 灰度发布名称。
            agent_id: Agent 预设 ID。
            version_id: 新版本 ID (灰度目标版本)。
            release_type: 发布类型 canary / blue_green / rolling。
            traffic_percentage: 灰度流量百分比 (0-100)。
            config: 灰度配置 (如 blue_green 的 blue/green 版本映射)。
            tenant_id: 租户 ID。
            description: 备注 / 描述。

        Returns:
            创建的 GrayRelease 对象。

        Raises:
            ValueError: 参数非法或存在进行中的灰度发布。
        """
        if release_type not in RELEASE_TYPES:
            raise ValueError(
                f"不支持的发布类型: {release_type}, 可选: {sorted(RELEASE_TYPES)}"
            )
        if not 0 <= traffic_percentage <= 100:
            raise ValueError("traffic_percentage 必须在 0-100 之间")
        if not name or not name.strip():
            raise ValueError("灰度发布名称不能为空")

        # 同一 Agent 不允许同时存在 active / paused 状态的灰度发布
        existing = await self.get_active_release(agent_id, tenant_id=tenant_id)
        if existing is not None:
            raise ValueError(
                f"Agent {agent_id} 已存在进行中的灰度发布 (id={existing.id}, "
                f"status={existing.status}), 请先完成或回滚"
            )

        release = GrayRelease(
            tenant_id=tenant_id,
            name=name.strip(),
            agent_id=agent_id,
            version_id=version_id,
            release_type=release_type,
            traffic_percentage=traffic_percentage,
            status=STATUS_DRAFT,
            config=config or {},
            description=description,
        )
        self.session.add(release)
        await self.session.flush()
        await self.session.refresh(release)
        logger.info(
            "创建灰度发布 id=%s agent_id=%s version_id=%s type=%s tenant=%s",
            release.id,
            agent_id,
            version_id,
            release_type,
            tenant_id,
        )
        return release

    async def get_release(
        self, release_id: int, *, tenant_id: str = "default"
    ) -> Optional[GrayRelease]:
        """获取灰度发布详情 (实体)

        Args:
            release_id: 灰度发布 ID。
            tenant_id: 租户 ID。

        Returns:
            GrayRelease 实体, 不存在返回 None。
        """
        return (
            await self.session.execute(
                select(GrayRelease).where(
                    GrayRelease.id == release_id,
                    GrayRelease.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()

    async def list_releases(
        self,
        *,
        status: Optional[str] = None,
        tenant_id: str = "default",
        agent_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """列出灰度发布 (按创建时间倒序)

        Args:
            status: 按状态过滤 (None 表示全部)。
            tenant_id: 租户 ID。
            agent_id: 按 Agent 过滤 (可选)。

        Returns:
            灰度发布 dict 列表。
        """
        stmt = select(GrayRelease).where(GrayRelease.tenant_id == tenant_id)
        if status is not None:
            stmt = stmt.where(GrayRelease.status == status)
        if agent_id is not None:
            stmt = stmt.where(GrayRelease.agent_id == agent_id)
        stmt = stmt.order_by(GrayRelease.created_at.desc())
        result = await self.session.execute(stmt)
        releases = result.scalars().all()
        return [self._release_to_dict(r) for r in releases]

    async def update_release(
        self,
        release_id: int,
        *,
        traffic_percentage: Optional[int] = None,
        status: Optional[str] = None,
        config: Optional[dict] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
        tenant_id: str = "default",
    ) -> GrayRelease:
        """更新灰度发布 (流量百分比 / 状态 / 配置等)

        仅非终态 (completed / rolled_back) 的发布可更新。

        Args:
            release_id: 灰度发布 ID。
            traffic_percentage: 新的流量百分比。
            status: 新状态 (用于直接状态流转)。
            config: 新的灰度配置。
            name: 新名称。
            description: 新描述。
            tenant_id: 租户 ID。

        Returns:
            更新后的 GrayRelease 对象。

        Raises:
            ValueError: 发布不存在或处于终态。
        """
        release = await self.get_release(release_id, tenant_id=tenant_id)
        if release is None:
            raise ValueError(f"灰度发布 {release_id} 不存在")
        if release.status in TERMINAL_STATUSES:
            raise ValueError(f"灰度发布 {release_id} 处于终态 ({release.status}), 不可更新")

        if traffic_percentage is not None:
            if not 0 <= traffic_percentage <= 100:
                raise ValueError("traffic_percentage 必须在 0-100 之间")
            release.traffic_percentage = traffic_percentage
        if status is not None:
            release.status = status
        if config is not None:
            release.config = config
        if name is not None:
            if not name.strip():
                raise ValueError("灰度发布名称不能为空")
            release.name = name.strip()
        if description is not None:
            release.description = description

        await self.session.flush()
        await self.session.refresh(release)
        logger.info(
            "更新灰度发布 id=%s traffic=%s status=%s tenant=%s",
            release_id,
            release.traffic_percentage,
            release.status,
            tenant_id,
        )
        return release

    async def delete_release(
        self, release_id: int, *, tenant_id: str = "default"
    ) -> bool:
        """删除灰度发布 (仅 draft / rolled_back 可删除)

        Args:
            release_id: 灰度发布 ID。
            tenant_id: 租户 ID。

        Returns:
            是否删除成功。

        Raises:
            ValueError: 发布不存在或处于活跃状态。
        """
        release = await self.get_release(release_id, tenant_id=tenant_id)
        if release is None:
            raise ValueError(f"灰度发布 {release_id} 不存在")
        # 活跃中的发布不允许直接删除, 需先回滚或完成
        if release.status in {STATUS_ACTIVE, STATUS_PAUSED}:
            raise ValueError(
                f"灰度发布 {release_id} 处于 {release.status} 状态, 请先暂停/回滚/完成后再删除"
            )
        await self.session.delete(release)
        await self.session.flush()
        logger.info("删除灰度发布 id=%s tenant=%s", release_id, tenant_id)
        return True

    # ===================== 状态流转 =====================

    async def start_release(
        self, release_id: int, *, tenant_id: str = "default"
    ) -> GrayRelease:
        """启动灰度发布 (draft / paused → active)

        Args:
            release_id: 灰度发布 ID。
            tenant_id: 租户 ID。

        Returns:
            更新后的 GrayRelease 对象。

        Raises:
            ValueError: 发布不存在或状态不允许启动。
        """
        release = await self.get_release(release_id, tenant_id=tenant_id)
        if release is None:
            raise ValueError(f"灰度发布 {release_id} 不存在")
        if release.status in TERMINAL_STATUSES:
            raise ValueError(f"灰度发布 {release_id} 处于终态 ({release.status}), 不可启动")
        if release.status == STATUS_ACTIVE:
            raise ValueError(f"灰度发布 {release_id} 已处于 active 状态")

        # 启动前校验: 同一 Agent 不能有其他 active 发布
        existing = await self.get_active_release(release.agent_id, tenant_id=tenant_id)
        if existing is not None and existing.id != release.id:
            raise ValueError(
                f"Agent {release.agent_id} 已存在进行中的灰度发布 (id={existing.id})"
            )

        release.status = STATUS_ACTIVE
        if release.started_at is None:
            release.started_at = datetime.now(timezone.utc)
        await self.session.flush()
        await self.session.refresh(release)
        logger.info("启动灰度发布 id=%s tenant=%s", release_id, tenant_id)
        return release

    async def pause_release(
        self, release_id: int, *, tenant_id: str = "default"
    ) -> GrayRelease:
        """暂停灰度发布 (active → paused)

        暂停后 route_request 不再将流量导入新版本 (返回 None, 由调用方走默认版本)。

        Args:
            release_id: 灰度发布 ID。
            tenant_id: 租户 ID。

        Returns:
            更新后的 GrayRelease 对象。

        Raises:
            ValueError: 发布不存在或状态不允许暂停。
        """
        release = await self.get_release(release_id, tenant_id=tenant_id)
        if release is None:
            raise ValueError(f"灰度发布 {release_id} 不存在")
        if release.status != STATUS_ACTIVE:
            raise ValueError(
                f"灰度发布 {release_id} 当前状态为 {release.status}, 仅 active 可暂停"
            )
        release.status = STATUS_PAUSED
        await self.session.flush()
        await self.session.refresh(release)
        logger.info("暂停灰度发布 id=%s tenant=%s", release_id, tenant_id)
        return release

    async def complete_release(
        self, release_id: int, *, tenant_id: str = "default"
    ) -> GrayRelease:
        """完成灰度发布 (100% 流量切换到新版本, 置为 completed)

        Args:
            release_id: 灰度发布 ID。
            tenant_id: 租户 ID。

        Returns:
            更新后的 GrayRelease 对象。

        Raises:
            ValueError: 发布不存在或处于终态。
        """
        release = await self.get_release(release_id, tenant_id=tenant_id)
        if release is None:
            raise ValueError(f"灰度发布 {release_id} 不存在")
        if release.status in TERMINAL_STATUSES:
            raise ValueError(
                f"灰度发布 {release_id} 处于终态 ({release.status}), 不可完成"
            )
        # 完成时流量置为 100%, 表示全量切换到新版本
        release.traffic_percentage = 100
        release.status = STATUS_COMPLETED
        release.completed_at = datetime.now(timezone.utc)
        await self.session.flush()
        await self.session.refresh(release)
        logger.info("完成灰度发布 id=%s (100%% 切换) tenant=%s", release_id, tenant_id)
        return release

    async def rollback_release(
        self, release_id: int, *, tenant_id: str = "default"
    ) -> GrayRelease:
        """回滚灰度发布 (流量切回基准版本, 置为 rolled_back)

        回滚后 route_request 不再导入新版本。

        Args:
            release_id: 灰度发布 ID。
            tenant_id: 租户 ID。

        Returns:
            更新后的 GrayRelease 对象。

        Raises:
            ValueError: 发布不存在或已处于终态。
        """
        release = await self.get_release(release_id, tenant_id=tenant_id)
        if release is None:
            raise ValueError(f"灰度发布 {release_id} 不存在")
        if release.status in TERMINAL_STATUSES:
            raise ValueError(
                f"灰度发布 {release_id} 处于终态 ({release.status}), 不可回滚"
            )
        # 回滚: 流量置为 0, 状态置为 rolled_back
        release.traffic_percentage = 0
        release.status = STATUS_ROLLED_BACK
        release.completed_at = datetime.now(timezone.utc)
        await self.session.flush()
        await self.session.refresh(release)
        logger.info("回滚灰度发布 id=%s tenant=%s", release_id, tenant_id)
        return release

    async def get_active_release(
        self, agent_id: int, *, tenant_id: str = "default"
    ) -> Optional[GrayRelease]:
        """获取 Agent 当前进行中的灰度发布 (active / paused)

        一个 Agent 同时只能有一个 active / paused 状态的灰度发布。

        Args:
            agent_id: Agent 预设 ID。
            tenant_id: 租户 ID。

        Returns:
            GrayRelease 实体, 无则返回 None。
        """
        return (
            await self.session.execute(
                select(GrayRelease).where(
                    GrayRelease.tenant_id == tenant_id,
                    GrayRelease.agent_id == agent_id,
                    GrayRelease.status.in_([STATUS_ACTIVE, STATUS_PAUSED]),
                )
            )
        ).scalar_one_or_none()

    # ===================== 路由决策 =====================

    async def route_request(
        self, agent_id: int, *, tenant_id: str = "default"
    ) -> Optional[int]:
        """路由决策: 根据灰度策略决定使用哪个版本

        核心方法: 请求执行时调用, 返回应使用的 version_id。
        若无灰度发布或未命中灰度且无基准版本, 返回 None (调用方使用默认版本)。

        策略:
        - canary: random.random() < traffic_percentage/100 → 新版本;
                  否则返回 config.baseline_version (基准版本, 可为 None)
        - blue_green: 根据 config.current 返回对应版本
              (current=blue → config.blue_version; current=green → config.green_version)
        - rolling: 与 canary 相同的概率路由 (流量百分比由外部逐步调大)

        Args:
            agent_id: Agent 预设 ID。
            tenant_id: 租户 ID。

        Returns:
            version_id (新版本或基准版本), None 表示使用默认版本。
        """
        release = await self.get_active_release(agent_id, tenant_id=tenant_id)
        # 无进行中的灰度发布 → 返回 None, 调用方走默认版本
        if release is None:
            return None
        # 暂停状态的发布: 不导入新版本流量, 返回基准版本 (若有)
        if release.status == STATUS_PAUSED:
            return release.config.get("baseline_version")

        if release.release_type == RELEASE_TYPE_BLUE_GREEN:
            return self._route_blue_green(release)
        # canary 与 rolling 走概率路由
        return self._route_canary(release)

    def _route_canary(self, release: GrayRelease) -> Optional[int]:
        """canary / rolling 概率路由

        random.random() < traffic_percentage/100 → 命中新版本;
        否则走基准版本 (config.baseline_version, 可为 None)。
        """
        if random.random() < release.traffic_percentage / 100.0:
            return release.version_id
        # 未命中灰度 → 基准版本
        return release.config.get("baseline_version")

    def _route_blue_green(self, release: GrayRelease) -> Optional[int]:
        """blue_green 整体切换路由

        根据 config.current 返回对应版本:
        - current=blue → config.blue_version
        - current=green → config.green_version
        """
        current = release.config.get("current", "blue")
        if current == "green":
            return release.config.get("green_version")
        return release.config.get("blue_version")

    # ===================== 序列化 =====================

    @staticmethod
    def _release_to_dict(r: GrayRelease) -> Dict[str, Any]:
        """GrayRelease → dict"""
        return {
            "id": r.id,
            "tenant_id": r.tenant_id,
            "name": r.name,
            "agent_id": r.agent_id,
            "version_id": r.version_id,
            "release_type": r.release_type,
            "traffic_percentage": r.traffic_percentage,
            "status": r.status,
            "config": r.config,
            "description": r.description,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
