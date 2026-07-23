"""Agent 版本管理服务

提供 Agent 预设的版本管理能力:
- 创建版本 (自动递增版本号)
- 列出 / 获取版本详情
- 发布版本到指定渠道 (委托 PublishService)
- 回滚到指定版本 (基于历史版本创建新版本)
- 对比两个版本差异
- 归档版本

事务边界由路由层控制 (service 层不 commit)。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.agent_version import AgentPublishTarget, AgentVersion
from models.prompt_template import AgentPreset

logger = logging.getLogger(__name__)

# 允许的版本状态
VERSION_STATUS_DRAFT = "draft"
VERSION_STATUS_PUBLISHED = "published"
VERSION_STATUS_ARCHIVED = "archived"

# 允许的发布渠道
PUBLISH_CHANNELS = {"feishu", "wechat", "dingtalk", "web", "api"}


class AgentVersionService:
    """Agent 版本管理服务 (数据库实现)"""

    def __init__(self, session: AsyncSession):
        self.session = session

    # ===================== 版本 CRUD =====================

    async def create_version(
        self,
        agent_id: int,
        *,
        tenant_id: str = "default",
        system_prompt: Optional[str] = None,
        tools_config: Optional[list] = None,
        model_config: Optional[dict] = None,
        temperature: int = 70,
        changelog: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> AgentVersion:
        """创建新版本 (自动递增版本号)

        若未提供 system_prompt 等字段, 则从 AgentPreset 当前配置继承。
        新版本默认状态为 draft。

        Args:
            agent_id: Agent 预设 ID。
            tenant_id: 租户 ID。
            system_prompt: 系统提示词 (None 时从 AgentPreset 继承)。
            tools_config: 工具配置 (None 时继承)。
            model_config: 模型配置 (None 时继承)。
            temperature: 温度 (默认 70)。
            changelog: 变更日志。
            created_by: 创建人 ID。

        Returns:
            创建的 AgentVersion 对象。
        """
        # 查询 AgentPreset, 用于继承配置
        preset = await self.session.get(AgentPreset, agent_id)
        if preset is None:
            raise ValueError(f"Agent 预设 {agent_id} 不存在")

        # 计算下一个版本号 (同一 agent_id + tenant_id 下最大版本号 + 1)
        max_version = (
            await self.session.execute(
                select(func.max(AgentVersion.version_number)).where(
                    AgentVersion.agent_id == agent_id,
                    AgentVersion.tenant_id == tenant_id,
                )
            )
        ).scalar()
        next_version = (max_version or 0) + 1

        # 继承未提供的字段
        if system_prompt is None:
            system_prompt = preset.system_prompt
        if tools_config is None:
            tools_config = preset.enabled_tools or []
        if model_config is None:
            model_config = {
                "model_tier": preset.model_tier,
                "temperature": preset.temperature,
            }

        version = AgentVersion(
            agent_id=agent_id,
            tenant_id=tenant_id,
            version_number=next_version,
            system_prompt=system_prompt,
            tools_config=tools_config or [],
            model_config=model_config or {},
            temperature=temperature,
            status=VERSION_STATUS_DRAFT,
            changelog=changelog,
            created_by=created_by,
        )
        self.session.add(version)
        await self.session.flush()
        logger.info(
            "创建 Agent 版本 agent_id=%s version=%s tenant=%s",
            agent_id,
            next_version,
            tenant_id,
        )
        return version

    async def list_versions(
        self, agent_id: int, *, tenant_id: str = "default"
    ) -> List[Dict[str, Any]]:
        """列出指定 Agent 的所有版本 (按版本号倒序)

        Args:
            agent_id: Agent 预设 ID。
            tenant_id: 租户 ID。

        Returns:
            版本 dict 列表。
        """
        result = await self.session.execute(
            select(AgentVersion)
            .where(
                AgentVersion.agent_id == agent_id,
                AgentVersion.tenant_id == tenant_id,
            )
            .order_by(AgentVersion.version_number.desc())
        )
        versions = result.scalars().all()
        return [self._version_to_dict(v) for v in versions]

    async def get_version(
        self, version_id: int, *, tenant_id: str = "default"
    ) -> Optional[Dict[str, Any]]:
        """获取版本详情

        Args:
            version_id: 版本 ID (主键)。
            tenant_id: 租户 ID。

        Returns:
            版本 dict, 不存在返回 None。
        """
        version = (
            await self.session.execute(
                select(AgentVersion).where(
                    AgentVersion.id == version_id,
                    AgentVersion.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if version is None:
            return None
        return self._version_to_dict(version)

    async def get_version_entity(
        self, version_id: int, *, tenant_id: str = "default"
    ) -> Optional[AgentVersion]:
        """获取版本 ORM 实体 (内部使用)"""
        return (
            await self.session.execute(
                select(AgentVersion).where(
                    AgentVersion.id == version_id,
                    AgentVersion.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()

    # ===================== 发布 / 归档 / 回滚 =====================

    async def publish_version(
        self,
        version_id: int,
        targets: List[str],
        *,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """发布版本到指定渠道

        为每个目标渠道创建/更新 AgentPublishTarget 记录, 状态置为 pending。
        实际渠道发布逻辑由 PublishService 处理 (路由层调用)。
        本方法仅负责版本状态流转 + 发布目标记录管理。

        Args:
            version_id: 版本 ID。
            targets: 目标渠道列表, 如 ["feishu", "web", "api"]。
            tenant_id: 租户 ID。

        Returns:
            {"version": ..., "targets": [...]} 发布结果。
        """
        version = (
            await self.session.execute(
                select(AgentVersion).where(
                    AgentVersion.id == version_id,
                    AgentVersion.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if version is None:
            raise ValueError(f"版本 {version_id} 不存在")

        # 校验渠道
        invalid = [t for t in targets if t not in PUBLISH_CHANNELS]
        if invalid:
            raise ValueError(f"不支持的发布渠道: {invalid}, 可选: {PUBLISH_CHANNELS}")

        # 版本状态流转: draft → published
        if version.status == VERSION_STATUS_ARCHIVED:
            raise ValueError("已归档的版本不能发布")
        version.status = VERSION_STATUS_PUBLISHED
        version.published_at = datetime.now(timezone.utc)

        created_targets: List[Dict[str, Any]] = []
        for channel in targets:
            # 查询是否已有该渠道的发布记录 (同一 agent_id + channel + tenant_id 唯一)
            existing = (
                await self.session.execute(
                    select(AgentPublishTarget).where(
                        AgentPublishTarget.agent_id == version.agent_id,
                        AgentPublishTarget.channel == channel,
                        AgentPublishTarget.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()

            if existing:
                # 更新已有记录: 指向新版本, 重置状态
                existing.version_id = version_id
                existing.status = "pending"
                existing.published_at = None
                existing.error_message = None
                target = existing
            else:
                # 创建新的发布记录
                target = AgentPublishTarget(
                    agent_id=version.agent_id,
                    tenant_id=tenant_id,
                    version_id=version_id,
                    channel=channel,
                    config={},
                    status="pending",
                )
                self.session.add(target)
            await self.session.flush()
            created_targets.append(self._target_to_dict(target))

        logger.info(
            "发布 Agent 版本 version_id=%s targets=%s tenant=%s",
            version_id,
            targets,
            tenant_id,
        )
        return {
            "version": self._version_to_dict(version),
            "targets": created_targets,
        }

    async def rollback(
        self,
        agent_id: int,
        target_version: int,
        *,
        tenant_id: str = "default",
        created_by: Optional[str] = None,
    ) -> AgentVersion:
        """回滚到指定版本

        基于历史版本创建一个新版本 (内容复制自目标版本), 版本号自增。
        这保证历史不可变 + 回滚可追溯。

        Args:
            agent_id: Agent 预设 ID。
            target_version: 目标版本号 (version_number, 非主键 id)。
            tenant_id: 租户 ID。
            created_by: 操作人 ID。

        Returns:
            新创建的回滚版本。
        """
        # 查询目标版本
        target = (
            await self.session.execute(
                select(AgentVersion).where(
                    AgentVersion.agent_id == agent_id,
                    AgentVersion.version_number == target_version,
                    AgentVersion.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if target is None:
            raise ValueError(f"Agent {agent_id} 的版本 {target_version} 不存在")

        # 基于目标版本创建新版本
        new_version = await self.create_version(
            agent_id,
            tenant_id=tenant_id,
            system_prompt=target.system_prompt,
            tools_config=target.tools_config,
            model_config=target.model_config,
            temperature=target.temperature,
            changelog=f"回滚到版本 {target_version}",
            created_by=created_by,
        )
        logger.info(
            "回滚 Agent agent_id=%s 到版本 %s, 新版本号 %s",
            agent_id,
            target_version,
            new_version.version_number,
        )
        return new_version

    async def compare_versions(
        self, v1_id: int, v2_id: int, *, tenant_id: str = "default"
    ) -> Dict[str, Any]:
        """对比两个版本差异

        对比 system_prompt / tools_config / model_config / temperature。

        Args:
            v1_id: 版本 1 ID。
            v2_id: 版本 2 ID。
            tenant_id: 租户 ID。

        Returns:
            {"v1": ..., "v2": ..., "diff": {...}}
        """
        v1 = (
            await self.session.execute(
                select(AgentVersion).where(
                    AgentVersion.id == v1_id,
                    AgentVersion.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        v2 = (
            await self.session.execute(
                select(AgentVersion).where(
                    AgentVersion.id == v2_id,
                    AgentVersion.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if v1 is None:
            raise ValueError(f"版本 {v1_id} 不存在")
        if v2 is None:
            raise ValueError(f"版本 {v2_id} 不存在")

        diff: Dict[str, Any] = {}
        # system_prompt 差异
        if v1.system_prompt != v2.system_prompt:
            diff["system_prompt"] = {
                "v1": v1.system_prompt,
                "v2": v2.system_prompt,
                "changed": True,
            }
        # tools_config 差异
        if v1.tools_config != v2.tools_config:
            diff["tools_config"] = {
                "v1": v1.tools_config,
                "v2": v2.tools_config,
                "changed": True,
            }
        # model_config 差异
        if v1.model_config != v2.model_config:
            diff["model_config"] = {
                "v1": v1.model_config,
                "v2": v2.model_config,
                "changed": True,
            }
        # temperature 差异
        if v1.temperature != v2.temperature:
            diff["temperature"] = {
                "v1": v1.temperature,
                "v2": v2.temperature,
                "changed": True,
            }

        return {
            "v1": self._version_to_dict(v1),
            "v2": self._version_to_dict(v2),
            "diff": diff,
            "has_changes": bool(diff),
        }

    async def archive_version(
        self, version_id: int, *, tenant_id: str = "default"
    ) -> AgentVersion:
        """归档版本

        将版本状态置为 archived, 归档后不可再发布。

        Args:
            version_id: 版本 ID。
            tenant_id: 租户 ID。

        Returns:
            更新后的 AgentVersion 对象。
        """
        version = (
            await self.session.execute(
                select(AgentVersion).where(
                    AgentVersion.id == version_id,
                    AgentVersion.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if version is None:
            raise ValueError(f"版本 {version_id} 不存在")
        if version.status == VERSION_STATUS_ARCHIVED:
            raise ValueError("版本已处于归档状态")
        version.status = VERSION_STATUS_ARCHIVED
        await self.session.flush()
        logger.info("归档 Agent 版本 version_id=%s", version_id)
        return version

    # ===================== 发布目标查询 =====================

    async def list_publish_targets(
        self, agent_id: int, *, tenant_id: str = "default"
    ) -> List[Dict[str, Any]]:
        """列出指定 Agent 的所有发布目标记录"""
        result = await self.session.execute(
            select(AgentPublishTarget)
            .where(
                AgentPublishTarget.agent_id == agent_id,
                AgentPublishTarget.tenant_id == tenant_id,
            )
            .order_by(AgentPublishTarget.channel)
        )
        targets = result.scalars().all()
        return [self._target_to_dict(t) for t in targets]

    async def get_publish_target(
        self, agent_id: int, channel: str, *, tenant_id: str = "default"
    ) -> Optional[AgentPublishTarget]:
        """获取指定 Agent + 渠道的发布目标实体"""
        return (
            await self.session.execute(
                select(AgentPublishTarget).where(
                    AgentPublishTarget.agent_id == agent_id,
                    AgentPublishTarget.channel == channel,
                    AgentPublishTarget.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()

    async def update_publish_target(
        self,
        target: AgentPublishTarget,
        *,
        status: Optional[str] = None,
        config: Optional[dict] = None,
        error_message: Optional[str] = None,
    ) -> AgentPublishTarget:
        """更新发布目标记录 (供 PublishService 调用)"""
        if status is not None:
            target.status = status
        if config is not None:
            target.config = config
        if error_message is not None:
            target.error_message = error_message
        if status == "published":
            target.published_at = datetime.now(timezone.utc)
        await self.session.flush()
        return target

    # ===================== 序列化 =====================

    @staticmethod
    def _version_to_dict(v: AgentVersion) -> Dict[str, Any]:
        """AgentVersion → dict"""
        return {
            "id": v.id,
            "agent_id": v.agent_id,
            "version_number": v.version_number,
            "system_prompt": v.system_prompt,
            "tools_config": v.tools_config,
            "model_config": v.model_config,
            "temperature": v.temperature,
            "status": v.status,
            "changelog": v.changelog,
            "created_by": v.created_by,
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "published_at": v.published_at.isoformat() if v.published_at else None,
        }

    @staticmethod
    def _target_to_dict(t: AgentPublishTarget) -> Dict[str, Any]:
        """AgentPublishTarget → dict"""
        return {
            "id": t.id,
            "agent_id": t.agent_id,
            "version_id": t.version_id,
            "channel": t.channel,
            "config": t.config,
            "status": t.status,
            "published_at": t.published_at.isoformat() if t.published_at else None,
            "error_message": t.error_message,
        }
