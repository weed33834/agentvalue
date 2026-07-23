"""多环境管理服务

对标 Bisheng / Langfuse 的环境隔离能力:
- 创建 / 列出 / 获取 / 更新 / 删除环境
- 合并环境配置 (深度合并默认配置 + 环境覆盖)
- 部署 / 取消部署 Agent 版本到环境
- 查询部署记录 (按环境 / Agent)

事务边界由路由层控制 (service 层不 commit)。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.environment_models import Environment, EnvironmentDeployment

logger = logging.getLogger(__name__)

# 部署状态
DEPLOY_STATUS_DEPLOYED = "deployed"
DEPLOY_STATUS_UNDEPLOYED = "undeployed"
DEPLOY_STATUS_FAILED = "failed"

# 内置环境名称
ENV_NAME_DEV = "dev"
ENV_NAME_STAGING = "staging"
ENV_NAME_PROD = "prod"

# 应用级基础配置 (默认环境的配置会覆盖在此之上, 最终被具体环境配置覆盖)
# 这些是合理的默认值, 实际应以默认环境的 config 为基础
_BASE_DEFAULT_CONFIG: Dict[str, Any] = {
    "model_tier": "auto",
    "debug": False,
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """深度合并两个 dict (override 覆盖 base)

    递归合并嵌套 dict, 非 dict 值由 override 直接覆盖。
    返回新 dict, 不修改入参。

    Args:
        base: 基础配置。
        override: 覆盖配置 (优先级高)。

    Returns:
        合并后的新 dict。
    """
    result = dict(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class EnvironmentService:
    """多环境管理服务 (数据库实现)"""

    def __init__(self, session: AsyncSession):
        self.session = session

    # ===================== 环境 CRUD =====================

    async def create_environment(
        self,
        name: str,
        display_name: str = "",
        description: Optional[str] = None,
        config: Optional[dict] = None,
        variables: Optional[dict] = None,
        *,
        tenant_id: str = "default",
        is_default: bool = False,
    ) -> Environment:
        """创建环境

        同一租户下环境名称唯一。

        Args:
            name: 环境名称 (dev / staging / prod / custom)。
            display_name: 展示名称。
            description: 环境描述。
            config: 环境级配置覆盖。
            variables: 环境变量覆盖。
            tenant_id: 租户 ID。
            is_default: 是否默认环境。

        Returns:
            创建的 Environment 对象。

        Raises:
            ValueError: 名称非法或已存在同名环境。
        """
        if not name or not name.strip():
            raise ValueError("环境名称不能为空")
        name = name.strip()

        # 校验同名环境
        existing = await self._get_environment_by_name(name, tenant_id=tenant_id)
        if existing is not None:
            raise ValueError(f"环境名称 {name} 已存在")

        environment = Environment(
            tenant_id=tenant_id,
            name=name,
            display_name=display_name or name,
            description=description,
            config=config or {},
            variables=variables or {},
            is_default=is_default,
        )
        self.session.add(environment)
        await self.session.flush()
        await self.session.refresh(environment)
        logger.info(
            "创建环境 id=%s name=%s tenant=%s", environment.id, name, tenant_id
        )
        return environment

    async def get_environment(
        self, env_id: int, *, tenant_id: str = "default"
    ) -> Optional[Environment]:
        """获取环境详情 (实体)

        Args:
            env_id: 环境 ID。
            tenant_id: 租户 ID。

        Returns:
            Environment 实体, 不存在返回 None。
        """
        return (
            await self.session.execute(
                select(Environment).where(
                    Environment.id == env_id,
                    Environment.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()

    async def _get_environment_by_name(
        self, name: str, *, tenant_id: str = "default"
    ) -> Optional[Environment]:
        """按名称获取环境 (内部使用)"""
        return (
            await self.session.execute(
                select(Environment).where(
                    Environment.tenant_id == tenant_id,
                    Environment.name == name,
                )
            )
        ).scalar_one_or_none()

    async def list_environments(
        self, *, tenant_id: str = "default"
    ) -> List[Dict[str, Any]]:
        """列出所有环境 (按创建时间正序, 默认环境优先)

        Args:
            tenant_id: 租户 ID。

        Returns:
            环境 dict 列表。
        """
        result = await self.session.execute(
            select(Environment)
            .where(Environment.tenant_id == tenant_id)
            .order_by(Environment.is_default.desc(), Environment.created_at.asc())
        )
        environments = result.scalars().all()
        return [self._environment_to_dict(e) for e in environments]

    async def update_environment(
        self,
        env_id: int,
        *,
        config: Optional[dict] = None,
        variables: Optional[dict] = None,
        description: Optional[str] = None,
        display_name: Optional[str] = None,
        tenant_id: str = "default",
    ) -> Environment:
        """更新环境 (配置 / 变量 / 描述 / 展示名称)

        Args:
            env_id: 环境 ID。
            config: 新的环境级配置覆盖 (整体替换)。
            variables: 新的环境变量覆盖 (整体替换)。
            description: 新描述。
            display_name: 新展示名称。
            tenant_id: 租户 ID。

        Returns:
            更新后的 Environment 对象。

        Raises:
            ValueError: 环境不存在。
        """
        environment = await self.get_environment(env_id, tenant_id=tenant_id)
        if environment is None:
            raise ValueError(f"环境 {env_id} 不存在")

        if config is not None:
            environment.config = config
        if variables is not None:
            environment.variables = variables
        if description is not None:
            environment.description = description
        if display_name is not None:
            environment.display_name = display_name

        await self.session.flush()
        await self.session.refresh(environment)
        logger.info("更新环境 id=%s tenant=%s", env_id, tenant_id)
        return environment

    async def delete_environment(
        self, env_id: int, *, tenant_id: str = "default"
    ) -> bool:
        """删除环境 (不允许删除默认环境)

        Args:
            env_id: 环境 ID。
            tenant_id: 租户 ID。

        Returns:
            是否删除成功。

        Raises:
            ValueError: 环境不存在或为默认环境。
        """
        environment = await self.get_environment(env_id, tenant_id=tenant_id)
        if environment is None:
            raise ValueError(f"环境 {env_id} 不存在")
        if environment.is_default:
            raise ValueError(f"环境 {env_id} ({environment.name}) 为默认环境, 不允许删除")
        await self.session.delete(environment)
        await self.session.flush()
        logger.info("删除环境 id=%s tenant=%s", env_id, tenant_id)
        return True

    # ===================== 配置合并 =====================

    async def get_environment_config(
        self, env_id: int, *, tenant_id: str = "default"
    ) -> Dict[str, Any]:
        """获取环境合并后配置 (深度合并默认配置 + 环境覆盖)

        合并顺序 (优先级从低到高):
        1. 应用级基础配置 (_BASE_DEFAULT_CONFIG)
        2. 默认环境 (is_default=True) 的 config
        3. 当前环境的 config

        Args:
            env_id: 环境 ID。
            tenant_id: 租户 ID。

        Returns:
            合并后的配置 dict。

        Raises:
            ValueError: 环境不存在。
        """
        environment = await self.get_environment(env_id, tenant_id=tenant_id)
        if environment is None:
            raise ValueError(f"环境 {env_id} 不存在")

        # 基础配置
        merged = dict(_BASE_DEFAULT_CONFIG)

        # 默认环境的配置 (若当前环境本身不是默认环境, 则叠加默认环境配置)
        if not environment.is_default:
            default_env = await self._get_default_environment(tenant_id=tenant_id)
            if default_env is not None:
                merged = _deep_merge(merged, default_env.config or {})

        # 当前环境配置覆盖 (优先级最高)
        merged = _deep_merge(merged, environment.config or {})

        return merged

    async def _get_default_environment(
        self, *, tenant_id: str = "default"
    ) -> Optional[Environment]:
        """获取租户的默认环境 (内部使用)"""
        return (
            await self.session.execute(
                select(Environment).where(
                    Environment.tenant_id == tenant_id,
                    Environment.is_default == True,  # noqa: E712
                )
            )
        ).scalar_one_or_none()

    # ===================== 部署管理 =====================

    async def deploy_agent(
        self,
        env_id: int,
        agent_id: int,
        version_id: int,
        *,
        config_snapshot: Optional[dict] = None,
        deployed_by: Optional[str] = None,
        tenant_id: str = "default",
    ) -> EnvironmentDeployment:
        """部署 Agent 版本到环境

        若该 Agent 在该环境已有部署记录, 则更新为最新版本 (重新部署)。
        若存在 undeployed 记录, 则重新激活为 deployed。

        Args:
            env_id: 环境 ID。
            agent_id: Agent 预设 ID。
            version_id: 版本 ID。
            config_snapshot: 部署时的配置快照。
            deployed_by: 部署人 ID。
            tenant_id: 租户 ID。

        Returns:
            EnvironmentDeployment 对象。

        Raises:
            ValueError: 环境不存在。
        """
        environment = await self.get_environment(env_id, tenant_id=tenant_id)
        if environment is None:
            raise ValueError(f"环境 {env_id} 不存在")

        # 查询该 Agent 在该环境是否已有部署记录
        existing = await self._get_deployment_by_agent_env(
            env_id, agent_id, tenant_id=tenant_id
        )
        if existing is not None:
            # 更新已有部署记录: 指向新版本, 重新激活
            existing.version_id = version_id
            existing.status = DEPLOY_STATUS_DEPLOYED
            existing.config_snapshot = config_snapshot or {}
            existing.deployed_by = deployed_by
            existing.deployed_at = datetime.now(timezone.utc)
            existing.undeployed_at = None
            deployment = existing
        else:
            deployment = EnvironmentDeployment(
                tenant_id=tenant_id,
                environment_id=env_id,
                agent_id=agent_id,
                version_id=version_id,
                status=DEPLOY_STATUS_DEPLOYED,
                config_snapshot=config_snapshot or {},
                deployed_by=deployed_by,
            )
            self.session.add(deployment)
        await self.session.flush()
        await self.session.refresh(deployment)
        logger.info(
            "部署 Agent agent_id=%s version_id=%s 到环境 env_id=%s tenant=%s",
            agent_id,
            version_id,
            env_id,
            tenant_id,
        )
        return deployment

    async def undeploy_agent(
        self, env_id: int, agent_id: int, *, tenant_id: str = "default"
    ) -> EnvironmentDeployment:
        """取消 Agent 在环境的部署

        Args:
            env_id: 环境 ID。
            agent_id: Agent 预设 ID。
            tenant_id: 租户 ID。

        Returns:
            更新后的 EnvironmentDeployment 对象。

        Raises:
            ValueError: 环境或部署记录不存在。
        """
        environment = await self.get_environment(env_id, tenant_id=tenant_id)
        if environment is None:
            raise ValueError(f"环境 {env_id} 不存在")

        deployment = await self._get_deployment_by_agent_env(
            env_id, agent_id, tenant_id=tenant_id
        )
        if deployment is None:
            raise ValueError(
                f"Agent {agent_id} 在环境 {env_id} 无部署记录"
            )
        if deployment.status == DEPLOY_STATUS_UNDEPLOYED:
            raise ValueError(
                f"Agent {agent_id} 在环境 {env_id} 已处于取消部署状态"
            )
        deployment.status = DEPLOY_STATUS_UNDEPLOYED
        deployment.undeployed_at = datetime.now(timezone.utc)
        await self.session.flush()
        await self.session.refresh(deployment)
        logger.info(
            "取消部署 Agent agent_id=%s 于环境 env_id=%s tenant=%s",
            agent_id,
            env_id,
            tenant_id,
        )
        return deployment

    async def get_deployments(
        self,
        *,
        env_id: Optional[int] = None,
        agent_id: Optional[int] = None,
        tenant_id: str = "default",
    ) -> List[Dict[str, Any]]:
        """查询部署记录列表 (按部署时间倒序)

        Args:
            env_id: 按环境过滤 (可选)。
            agent_id: 按 Agent 过滤 (可选)。
            tenant_id: 租户 ID。

        Returns:
            部署记录 dict 列表。
        """
        stmt = select(EnvironmentDeployment).where(
            EnvironmentDeployment.tenant_id == tenant_id
        )
        if env_id is not None:
            stmt = stmt.where(EnvironmentDeployment.environment_id == env_id)
        if agent_id is not None:
            stmt = stmt.where(EnvironmentDeployment.agent_id == agent_id)
        stmt = stmt.order_by(EnvironmentDeployment.deployed_at.desc())
        result = await self.session.execute(stmt)
        deployments = result.scalars().all()
        return [self._deployment_to_dict(d) for d in deployments]

    async def get_agent_deployment(
        self,
        agent_id: int,
        env_name: str,
        *,
        tenant_id: str = "default",
    ) -> Optional[EnvironmentDeployment]:
        """获取 Agent 在指定环境 (按名称) 的部署记录

        仅返回 deployed 状态的部署。

        Args:
            agent_id: Agent 预设 ID。
            env_name: 环境名称 (如 dev / staging / prod)。
            tenant_id: 租户 ID。

        Returns:
            EnvironmentDeployment 实体, 无则返回 None。
        """
        # 先按名称定位环境
        environment = await self._get_environment_by_name(env_name, tenant_id=tenant_id)
        if environment is None:
            return None
        # 查询该 Agent 在该环境的 deployed 部署
        return (
            await self.session.execute(
                select(EnvironmentDeployment).where(
                    EnvironmentDeployment.tenant_id == tenant_id,
                    EnvironmentDeployment.environment_id == environment.id,
                    EnvironmentDeployment.agent_id == agent_id,
                    EnvironmentDeployment.status == DEPLOY_STATUS_DEPLOYED,
                )
            )
        ).scalar_one_or_none()

    async def _get_deployment_by_agent_env(
        self, env_id: int, agent_id: int, *, tenant_id: str = "default"
    ) -> Optional[EnvironmentDeployment]:
        """获取 Agent 在环境的部署记录 (任意状态, 内部使用)

        同一 Agent 在同一环境只保留一条部署记录 (重新部署时更新)。
        """
        return (
            await self.session.execute(
                select(EnvironmentDeployment).where(
                    EnvironmentDeployment.tenant_id == tenant_id,
                    EnvironmentDeployment.environment_id == env_id,
                    EnvironmentDeployment.agent_id == agent_id,
                )
            )
        ).scalar_one_or_none()

    # ===================== 序列化 =====================

    @staticmethod
    def _environment_to_dict(e: Environment) -> Dict[str, Any]:
        """Environment → dict"""
        return {
            "id": e.id,
            "tenant_id": e.tenant_id,
            "name": e.name,
            "display_name": e.display_name,
            "description": e.description,
            "config": e.config,
            "variables": e.variables,
            "is_default": e.is_default,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "updated_at": e.updated_at.isoformat() if e.updated_at else None,
        }

    @staticmethod
    def _deployment_to_dict(d: EnvironmentDeployment) -> Dict[str, Any]:
        """EnvironmentDeployment → dict"""
        return {
            "id": d.id,
            "tenant_id": d.tenant_id,
            "environment_id": d.environment_id,
            "agent_id": d.agent_id,
            "version_id": d.version_id,
            "status": d.status,
            "config_snapshot": d.config_snapshot,
            "deployed_by": d.deployed_by,
            "deployed_at": d.deployed_at.isoformat() if d.deployed_at else None,
            "undeployed_at": d.undeployed_at.isoformat() if d.undeployed_at else None,
        }
