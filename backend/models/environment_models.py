"""多环境管理数据模型

对标 Bisheng / Langfuse 的环境隔离能力:
- Environment: dev / staging / prod / custom 多环境配置隔离
  - config: 环境级配置覆盖 (如 database_url / redis_url / model_tier 等)
  - variables: 环境变量覆盖
  - is_default: 标记默认环境 (不允许删除)
- EnvironmentDeployment: Agent 版本在各环境的部署记录
  - 记录某版本部署到某环境的状态与配置快照

表: environments / environment_deployments
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class Environment(Base):
    """环境实体 (按 tenant + name 隔离)

    每个租户可创建多个环境 (dev / staging / prod / custom)。
    config 示例: {"database_url": "...", "redis_url": "...", "model_tier": "L2"}
    variables 示例: {"DEBUG": "false", "LOG_LEVEL": "INFO"}
    """

    __tablename__ = "environments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 多租户归属
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default="default"
    )
    # 环境名称 (dev / staging / prod / custom), 同一租户下唯一
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    # 展示名称 (前端展示, 如 "开发环境")
    display_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # 环境描述
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 环境级配置覆盖 (JSON, 如 database_url / redis_url / model_tier 等)
    config: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )
    # 环境变量覆盖 (JSON, 如 DEBUG / LOG_LEVEL 等)
    variables: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )
    # 是否默认环境 (默认环境不允许删除)
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=func.text("0")
    )
    # 创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 更新时间
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        # 同一租户下环境名称唯一
        UniqueConstraint("tenant_id", "name", name="uix_environment_tenant_name"),
        # 索引: tenant_id + name (按名称查询)
        Index("ix_environment_tenant_name", "tenant_id", "name"),
    )


class EnvironmentDeployment(Base):
    """环境部署记录 (Agent 版本部署到环境)

    记录某 Agent 版本在某环境的部署状态与配置快照。
    状态: deployed (已部署) / undeployed (已取消部署) / failed (部署失败)
    """

    __tablename__ = "environment_deployments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 多租户归属
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default="default"
    )
    # 关联的环境 ID
    environment_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("environments.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # 关联的 Agent 预设 ID (软关联 AgentPreset.id)
    agent_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("agent_presets.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # 关联的版本 ID (软关联 AgentVersion.id)
    version_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("agent_versions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # 部署状态: deployed / undeployed / failed
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="deployed", server_default="deployed"
    )
    # 配置快照 (部署时的 Agent / 版本配置快照, JSON)
    config_snapshot: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )
    # 部署人 (用户 ID)
    deployed_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # 部署时间
    deployed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 取消部署时间 (status → undeployed 时写入)
    undeployed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # 索引: tenant_id + environment_id (按环境列部署)
        Index("ix_env_deploy_tenant_env", "tenant_id", "environment_id"),
        # 索引: tenant_id + agent_id (按 Agent 列部署)
        Index("ix_env_deploy_tenant_agent", "tenant_id", "agent_id"),
    )
