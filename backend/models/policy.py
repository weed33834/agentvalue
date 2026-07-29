"""
访问策略数据模型 (P1-7: ABAC 属性级访问控制)

策略四元组 + 扩展字段:
    subject  (谁)      → subject_type + subject_id
    resource (对什么)  → resource_type + resource_id (支持 "*" 通配)
    action   (做什么)  → action (如 "evaluation:read")
    effect   (允许/拒绝) → effect ("allow" / "deny")

扩展:
- condition: JSON 条件表达式,支持属性级约束 (如 "HR 只能审计本部门员工"),
  满足时策略才生效;为空表示无条件 (纯结构匹配)。
- priority:  策略优先级 (整数,越小优先级越高);同优先级下 deny 优先于 allow
  (deny-override 语义),用于实现 "deny 优先于 allow" 安全策略。
- tenant_id: 多租户隔离,策略仅在同租户内生效。

策略存储方式:
- 使用数据库表 (而非 casbin 文件),支持运行时动态增删改查,
  无需重启服务即可调整权限。ABAC 引擎在每次鉴权时从 DB 加载策略并喂给 casbin。
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base

# ====== 枚举常量 (用字符串而非 Enum,便于跨 DB 兼容与策略 JSON 序列化) ======

# subject_type: 策略主体类型
SUBJECT_TYPE_USER = "user"
SUBJECT_TYPE_ROLE = "role"
SUBJECT_TYPE_GROUP = "group"
SUBJECT_TYPES = (SUBJECT_TYPE_USER, SUBJECT_TYPE_ROLE, SUBJECT_TYPE_GROUP)

# effect: 策略效果
EFFECT_ALLOW = "allow"
EFFECT_DENY = "deny"
EFFECTS = (EFFECT_ALLOW, EFFECT_DENY)

# 通配符: resource_id 为 "*" 时匹配该 resource_type 下所有资源
WILDCARD = "*"

# 默认优先级: 0 表示最高优先级 (未指定时使用)
DEFAULT_PRIORITY = 0


def _now_utc() -> datetime:
    """UTC 当前时间 (与 models.models.now_utc 行为一致)"""
    return datetime.now(timezone.utc)


class Policy(Base):
    """访问控制策略实体 (ABAC + RBAC 混合,数据库存储,动态管理)

    示例策略:
    1. HR 只能审计本部门员工:
       subject_type="role", subject_id="hr",
       resource_type="evaluation", resource_id="*",
       action="evaluation:read", effect="allow",
       condition={"subject.department": "resource.department"}

    2. manager 只能查看自己团队的评估:
       subject_type="role", subject_id="manager",
       resource_type="evaluation", resource_id="*",
       action="evaluation:read", effect="allow",
       condition={"subject.manages": "resource.owner_id"}

    3. 员工只能查看自己的评估:
       subject_type="role", subject_id="employee",
       resource_type="evaluation", resource_id="*",
       action="evaluation:read", effect="allow",
       condition={"subject.id": "resource.owner_id"}

    4. 禁止任何非 admin 删除评估 (deny 优先):
       subject_type="role", subject_id="*",
       resource_type="evaluation", resource_id="*",
       action="evaluation:delete", effect="deny", priority=-1
    """

    __tablename__ = "abac_policies"

    # 自增主键
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # 业务主键: 策略 ID (便于审计日志引用)
    policy_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    # 主体类型: user / role / group
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # 主体标识: user_id / role 值 / group_id;"*" 配合 subject_type 表示所有主体
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # 资源类型: evaluation / user / audit_log 等
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # 资源标识: 具体 ID 或 "*" (通配,匹配该类型下所有资源)
    resource_id: Mapped[str] = mapped_column(
        String(128), nullable=False, default=WILDCARD
    )
    # 动作: "资源类型:操作" 形式 (如 evaluation:read / evaluation:write)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    # 效果: allow / deny
    effect: Mapped[str] = mapped_column(String(16), nullable=False, default=EFFECT_ALLOW)
    # 条件: JSON 表达式 (为空表示无条件);见模块 docstring 示例
    condition: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 优先级: 整数,越小优先级越高 (deny-override: 同优先级 deny 优先)
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_PRIORITY
    )
    # 多租户隔离: 策略仅在同租户内生效
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # 描述 (可选,便于管理员理解策略意图)
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
        # effect 取值约束
        CheckConstraint(
            f"effect IN ({', '.join(repr(e) for e in EFFECTS)})",
            name="ck_abac_policy_effect_valid",
        ),
        # subject_type 取值约束
        CheckConstraint(
            f"subject_type IN ({', '.join(repr(s) for s in SUBJECT_TYPES)})",
            name="ck_abac_policy_subject_type_valid",
        ),
        # 按租户 + 资源类型 + 动作索引: 鉴权查询常用过滤维度
        Index(
            "ix_abac_policy_tenant_resource_action",
            "tenant_id",
            "resource_type",
            "action",
        ),
        # 按租户 + 主体索引: 查某用户/组/角色的策略
        Index(
            "ix_abac_policy_tenant_subject",
            "tenant_id",
            "subject_type",
            "subject_id",
        ),
    )
