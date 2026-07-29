"""
用户组数据模型 (P1-7: ABAC 属性级访问控制)

用途:
- 用户组是 ABAC 策略的 subject 之一 (subject_type="group"),
  支持将权限策略绑定到组,组内成员自动继承组权限。
- 一个用户可同时属于多个组 (多对多关系),权限取并集 (allow) / deny 优先。

表结构:
- user_groups:        用户组主表 (group_id 全局唯一,按 tenant_id 隔离)
- user_group_members: 组成员关联表 (user_id + group_id + tenant_id 联合唯一)

设计要点:
- group_id 为业务主键 (字符串,便于策略引用如 "group:eng-team"),
  id 为自增主键 (内部索引/外键使用,避免长字符串索引开销)。
- tenant_id 冗余存储在成员表,便于按租户直接聚合成员列表,无需 join 组表。
- 软删除组时,成员关联应级联清理 (通过应用层 ensure,不依赖 DB 外键级联,
  兼容 SQLite 默认不开启 PRAGMA foreign_keys 的现状)。
"""

from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


def _now_utc() -> datetime:
    """UTC 当前时间 (与 models.models.now_utc 行为一致)"""
    return datetime.now(timezone.utc)


class UserGroup(Base):
    """用户组实体 (按 tenant_id 隔离,group_id 全局唯一便于策略引用)"""

    __tablename__ = "user_groups"

    # 自增主键 (内部索引/外键使用)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # 业务主键: 组标识 (如 "eng-team"),ABAC 策略引用为 "group:<group_id>"
    # 不设全局 unique: 多租户场景下不同租户可使用相同 group_id,
    # 由 (tenant_id, group_id) 复合唯一约束保证租户内唯一。
    group_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False
    )
    # 组名称 (展示用,同租户内建议唯一但不强制)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 多租户归属: 组按租户隔离,策略引用时自动限定同租户
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # 描述 (可选,便于管理员理解组用途)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    # 创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )
    # 更新时间
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, onupdate=_now_utc
    )

    __table_args__ = (
        # 同租户内 group_id 唯一 (group_id 本身已全局唯一,此约束额外保证租户语义)
        UniqueConstraint("tenant_id", "group_id", name="uix_tenant_user_group_id"),
        Index("ix_user_group_tenant_name", "tenant_id", "name"),
    )


class UserGroupMember(Base):
    """用户组成员关联 (多对多,支持一个用户属于多个组)

    权限继承语义:
    - 用户加入组后,该组绑定的所有 allow 策略对用户生效。
    - deny 策略始终优先于 allow (deny-override),组权限不会覆盖显式 deny。
    """

    __tablename__ = "user_group_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # 成员用户 ID (对应 users.user_id,不建外键以兼容跨表软删场景)
    user_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # 所属组 ID (对应 user_groups.group_id; 不建外键因 group_id 非全局唯一,
    # 级联清理由应用层 delete_group 函数显式处理,兼容 SQLite 默认不开启外键)
    group_id: Mapped[str] = mapped_column(
        String(64),
        index=True,
        nullable=False,
    )
    # 租户冗余字段 (便于按租户直接聚合成员列表,无需 join 组表)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # 加入时间
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )

    __table_args__ = (
        # 同租户内同一用户不可重复加入同一组
        UniqueConstraint(
            "tenant_id", "user_id", "group_id", name="uix_tenant_user_group_member"
        ),
        Index("ix_user_group_member_tenant_group", "tenant_id", "group_id"),
    )
