"""
ABAC (属性级访问控制) 权限引擎 (P1-7)

设计目标:
- 基于 RBAC + ABAC 混合模型,保留现有 RBAC 角色检查 (向后兼容),
  增加属性级策略: subject(用户/角色/组) + resource(资源属性) + action + condition。
- 使用 casbin 库做主体层次 (角色/组继承) 与资源通配匹配,不重复造轮子。
- 策略存储在数据库表 (models.policy.Policy),支持运行时动态管理。
- 优雅降级: casbin 未安装时,所有 ABAC 检查自动回退到纯 RBAC (只检查角色)。

鉴权决策流程:
1. admin 角色恒为超级用户 → 直接放行。
2. casbin 未安装 → 回退 RBAC (DEFAULT_ROLE_PERMISSIONS 角色-权限映射)。
3. 当前租户无任何 ABAC 策略 → 回退 RBAC (向后兼容,零策略零侵入)。
4. 当前租户有 ABAC 策略 → 进入 ABAC 评估:
   a. 用 casbin RoleManager 解析主体层次 (user → role / group / self),
      用 casbin key_match 做资源通配匹配,筛出结构匹配的策略。
   b. 对结构匹配的策略逐条评估 condition (属性级约束),
      过滤掉条件不满足的策略。
   c. 在条件满足的策略中按 priority 排序 (越小越优),
      同优先级 deny 优先于 allow (deny-override),取最高优先级策略的 effect。
   d. 无任何策略匹配 → 默认拒绝 (ABAC 显式授权语义)。

condition 表达式 (JSON dict, 语义为 AND):
- key 形如 "subject.<attr>" 或 "resource.<attr>":
    subject 属性: id / role / department / manager_id / tenant_id
    resource 属性: id / type / owner_id / department / tenant_id
- value 为字面量 (直接相等比较) 或引用串 (以 "subject." / "resource." 开头,
  表示取对应属性值做相等比较)。
- 特殊 key "subject.manages": value 为 resource 属性引用 (如 "resource.owner_id"),
  语义为 "subject.id 是该资源所有者的直属主管" (查 DB users.manager_id 校验)。

示例:
- HR 只能审计本部门员工:
  {"subject.department": "resource.department"}
- manager 只能查看自己团队的评估:
  {"subject.manages": "resource.owner_id"}
- 员工只能查看自己的评估:
  {"subject.id": "resource.owner_id"}
- 仅限 Engineering 部门资源:
  {"resource.department": "Engineering"}
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select

from auth.rbac import Role, get_current_user_id, get_current_user_role
from core.database import AsyncSessionLocal
from core.tenant_context import get_current_tenant
from models.models import DEFAULT_TENANT_ID, User
from models.policy import (
    EFFECT_ALLOW,
    EFFECT_DENY,
    SUBJECT_TYPE_GROUP,
    SUBJECT_TYPE_ROLE,
    SUBJECT_TYPE_USER,
    WILDCARD,
    Policy,
)
from models.user_group import UserGroupMember

logger = logging.getLogger(__name__)

# ============================================================
# casbin 可选依赖检测 (未安装时全局降级为纯 RBAC)
# ============================================================

try:
    import casbin  # noqa: F401
    from casbin.model import Model
    from casbin.util import key_match

    CASBIN_AVAILABLE = True
except ImportError:  # casbin 未安装 → 优雅降级为纯 RBAC
    CASBIN_AVAILABLE = False
    Model = None  # type: ignore
    key_match = None  # type: ignore


# ============================================================
# RBAC 回退: 角色 → 权限 映射 (向后兼容)
# ============================================================

# 默认角色权限映射: casbin 未安装或租户无 ABAC 策略时使用。
# 通配 "evaluation:*" 表示匹配该前缀下所有动作; admin 的 "*" 表示全权限。
DEFAULT_ROLE_PERMISSIONS: Dict[Role, set] = {
    Role.ADMIN: {"*"},
    Role.HR: {
        "evaluation:read",
        "evaluation:write",
        "evaluation:approve",
        "evaluation:delete",
        "audit:read",
        "user:read",
        "user:write",
    },
    Role.MANAGER: {
        "evaluation:read",
        "evaluation:write",
        "evaluation:approve",
        "audit:read",
    },
    Role.EMPLOYEE: {"evaluation:read"},
}


def _rbac_check(role: Role, action: str) -> bool:
    """纯 RBAC 权限检查 (casbin 未安装或无 ABAC 策略时回退使用)。

    匹配规则:
    - admin ("*") → 恒为 True。
    - action 精确命中角色权限集 → True。
    - action 命中权限集中的前缀通配 (如 "evaluation:*" 匹配 "evaluation:read") → True。
    - 否则 False。
    """
    perms = DEFAULT_ROLE_PERMISSIONS.get(role, set())
    if WILDCARD in perms:
        return True
    if action in perms:
        return True
    for p in perms:
        # 前缀通配: "evaluation:*" 匹配 "evaluation:read" / "evaluation:write" 等
        if p.endswith(":*") and action.startswith(p[:-1]):
            return True
    return False


# ============================================================
# casbin 模型定义 (RBAC + 资源/动作通配, deny-override 效果)
# ============================================================

# 注意: casbin-python 的角色定义段名为 [role_definition] (不是 [role_manager])。
# 模型仅用于结构匹配 (主体层次 + 资源通配 + 动作通配),
# 最终 allow/deny 决策由本引擎结合 condition 与 priority 在 Python 层完成,
# 以支持 casbin 原生不易表达的属性级条件与优先级。
_ABAC_MODEL_TEXT = """
[request_definition]
r = sub, obj, act

[policy_definition]
p = sub, obj, act, eft

[role_definition]
g = _, _

[policy_effect]
e = some(where (p.eft == allow)) && !some(where (p.eft == deny))

[matchers]
m = (g(r.sub, p.sub) || p.sub == "*") && keyMatch(r.obj, p.obj) && keyMatch(r.act, p.act)
"""


def _build_enforcer():
    """构建内存 casbin Enforcer (从模型文本加载, 策略稍后动态注入)。

    每次鉴权都构建新 Enforcer,因为策略与主体关系按请求动态加载;
    casbin Enforcer 是纯内存对象,构建开销极低 (微秒级)。
    """
    model = Model()
    model.load_model_from_text(_ABAC_MODEL_TEXT)
    return casbin.Enforcer(model)


def _subject_token(subject_type: str, subject_id: str) -> str:
    """将 (subject_type, subject_id) 规范化为 casbin 主体标识。

    - role  → "role:<role_value>" (如 "role:hr")
    - group → "group:<group_id>"
    - user  → "user:<user_id>"
    - subject_id == "*" → 直接返回 "*" (通配所有主体)
    """
    if subject_id == WILDCARD:
        return WILDCARD
    if subject_type == SUBJECT_TYPE_ROLE:
        return f"role:{subject_id}"
    if subject_type == SUBJECT_TYPE_GROUP:
        return f"group:{subject_id}"
    if subject_type == SUBJECT_TYPE_USER:
        return f"user:{subject_id}"
    # 兜底: 未知类型按原始值处理
    return subject_id


def _resource_key(resource_type: str, resource_id: str) -> str:
    """构造资源匹配键 "resource_type:resource_id"。

    resource_id 为 "*" 时返回 "resource_type:*" (通配该类型所有资源)。
    """
    rid = resource_id if resource_id else WILDCARD
    return f"{resource_type}:{rid}"


# ============================================================
# ABAC 权限引擎
# ============================================================


class ABACEngine:
    """ABAC 权限引擎 (RBAC + ABAC 混合, 数据库策略存储, casbin 结构匹配)

    使用方式:
        engine = get_abac_engine()           # 模块级单例
        ok = await engine.check(user_id, role, tenant_id,
                                "evaluation:read",
                                resource_type="evaluation",
                                resource_id="eval-001",
                                resource_attrs={"owner_id": "u2", "department": "Eng"})
    """

    def __init__(self, session_factory=None):
        # session_factory 为 async_sessionmaker,用于打开 DB 会话加载策略/用户属性
        self._session_factory = session_factory or AsyncSessionLocal
        # 为保证动态管理即时生效,默认每次鉴权都从 DB 加载 (策略量小,开销可接受)。

    # ---------- 公共入口 ----------

    async def check(
        self,
        user_id: str,
        role: Role,
        tenant_id: str,
        action: str,
        resource_type: str = WILDCARD,
        resource_id: str = WILDCARD,
        subject_attrs: Optional[Dict[str, Any]] = None,
        resource_attrs: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """鉴权: 当前用户能否对指定资源执行指定动作。

        Args:
            user_id: 当前用户 ID。
            role:    当前用户角色 (Role 枚举)。
            tenant_id: 当前租户 ID (策略按租户隔离)。
            action:  动作 (如 "evaluation:read")。
            resource_type: 资源类型 (如 "evaluation")。
            resource_id:   资源 ID, "*" 表示通配。
            subject_attrs: 主体属性 (department/manager_id 等);未提供时从 DB 加载。
            resource_attrs: 资源属性 (owner_id/department 等);未提供时仅含 id/type。

        Returns:
            True 允许, False 拒绝。
        """
        # 1. admin 超级用户: 恒放行 (与现有 RBAC 行为一致)
        if role == Role.ADMIN:
            return True

        # 2. casbin 未安装 → 回退纯 RBAC
        if not CASBIN_AVAILABLE:
            return _rbac_check(role, action)

        tenant_id = tenant_id or DEFAULT_TENANT_ID

        # 3. 加载当前租户全部 ABAC 策略 + 当前用户的组成员关系
        policies, group_ids = await self._load_policies_and_groups(
            user_id, tenant_id
        )

        # 4. 租户无任何 ABAC 策略 → 回退 RBAC (零策略零侵入,向后兼容)
        if not policies:
            return _rbac_check(role, action)

        # 5. 预取主体属性 (若调用方未注入),供 condition 评估
        if subject_attrs is None:
            subject_attrs = await self._load_subject_attrs(user_id, tenant_id)

        # 6. 预取 subject.manages 条件所需的 owner → 是否为主管 映射
        resource_attrs = dict(resource_attrs or {})
        resource_attrs.setdefault("id", resource_id)
        resource_attrs.setdefault("type", resource_type)
        owner_id = resource_attrs.get("owner_id")
        if owner_id:
            owner_manager = await self._load_user_manager_id(owner_id, tenant_id)
            cache = subject_attrs.setdefault("_manages_cache", {})
            cache[owner_id] = (owner_manager == user_id)

        # 7. ABAC 评估: 结构匹配 → 条件过滤 → 优先级 + deny-override
        return self._evaluate(
            user_id=user_id,
            role=role,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            policies=policies,
            group_ids=group_ids,
            subject_attrs=subject_attrs,
            resource_attrs=resource_attrs,
        )

    # ---------- 数据加载 ----------

    async def _load_policies_and_groups(
        self, user_id: str, tenant_id: str
    ) -> Tuple[List[Policy], List[str]]:
        """从 DB 加载: 当前租户全部 ABAC 策略 + 当前用户的组成员 group_id 列表。

        策略按 priority 升序排列,便于后续评估时直接取最高优先级。
        """
        async with self._session_factory() as session:
            # 当前租户全部策略 (按 priority 升序)
            stmt = (
                select(Policy)
                .where(Policy.tenant_id == tenant_id)
                .order_by(Policy.priority.asc())
            )
            result = await session.execute(stmt)
            policies: List[Policy] = list(result.scalars().all())

            # 当前用户的组成员关系 (同租户)
            if user_id:
                g_stmt = select(UserGroupMember.group_id).where(
                    UserGroupMember.tenant_id == tenant_id,
                    UserGroupMember.user_id == user_id,
                )
                g_result = await session.execute(g_stmt)
                group_ids: List[str] = [row[0] for row in g_result.all()]
            else:
                group_ids = []

        return policies, group_ids

    async def _load_subject_attrs(
        self, user_id: str, tenant_id: str
    ) -> Dict[str, Any]:
        """从 DB 加载主体属性 (id/role/department/manager_id/tenant_id)。

        用于 condition 评估。用户不存在时返回最小属性集 (id 已知)。
        """
        attrs: Dict[str, Any] = {"id": user_id, "tenant_id": tenant_id}
        if not user_id:
            return attrs
        async with self._session_factory() as session:
            stmt = select(User).where(
                User.tenant_id == tenant_id, User.user_id == user_id
            )
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()
        if user is not None:
            attrs["role"] = user.role
            attrs["department"] = user.department
            attrs["manager_id"] = user.manager_id
        return attrs

    async def _load_user_manager_id(
        self, user_id: str, tenant_id: str
    ) -> Optional[str]:
        """查某用户的直属主管 ID (用于 subject.manages 条件校验)。

        用户不存在或未配置主管时返回 None。
        """
        if not user_id:
            return None
        async with self._session_factory() as session:
            stmt = select(User.manager_id).where(
                User.tenant_id == tenant_id, User.user_id == user_id
            )
            result = await session.execute(stmt)
            row = result.first()
        return row[0] if row else None

    # ---------- 评估核心 (纯同步, casbin 与条件比较均无 IO) ----------

    def _evaluate(
        self,
        user_id: str,
        role: Role,
        action: str,
        resource_type: str,
        resource_id: str,
        policies: List[Policy],
        group_ids: List[str],
        subject_attrs: Dict[str, Any],
        resource_attrs: Dict[str, Any],
    ) -> bool:
        """ABAC 评估主逻辑 (由 check 在完成异步预取后调用)。

        步骤:
        a. 构建 casbin Enforcer,注入当前用户的主体关系 (role/group/self)。
        b. 遍历策略,用 casbin RoleManager + key_match 做结构匹配。
        c. 对结构匹配的策略评估 condition (属性级)。
        d. 在条件满足的策略中按 priority + deny-override 决定最终效果。
        """
        # a. 构建 Enforcer 并注入当前用户主体关系
        enforcer = _build_enforcer()
        # user → role:<role_value> (用户 RBAC 角色)
        enforcer.add_grouping_policy(user_id, f"role:{role.value}")
        # user → group:<group_id> (用户组权限继承)
        for gid in group_ids:
            enforcer.add_grouping_policy(user_id, f"group:{gid}")
        # user → user:<user_id> (自身, 支持 user 类型策略)
        enforcer.add_grouping_policy(user_id, f"user:{user_id}")

        rm = enforcer.get_role_manager()
        res_key = _resource_key(resource_type, resource_id)

        # b + c. 结构匹配 + 条件过滤
        satisfied: List[Policy] = []
        for p in policies:
            sub_token = _subject_token(p.subject_type, p.subject_id)
            p_obj_key = _resource_key(p.resource_type, p.resource_id)
            if not self._struct_matches(
                rm, user_id, sub_token, res_key, p_obj_key, p.action, action
            ):
                continue
            if not self._condition_satisfied(
                p.condition, subject_attrs, resource_attrs
            ):
                continue
            satisfied.append(p)

        # 无任何策略匹配 → 默认拒绝 (ABAC 显式授权语义)
        if not satisfied:
            return False

        # d. priority + deny-override:
        #    policies 已按 priority 升序加载,此处再按 (priority, deny优先) 排序后取首条。
        #    deny 优先于 allow: 同 priority 下 deny 排前 (effect==deny → 0, allow → 1)。
        satisfied.sort(key=lambda p: (p.priority, 0 if p.effect == EFFECT_DENY else 1))
        return satisfied[0].effect == EFFECT_ALLOW

    def _struct_matches(
        self,
        rm,
        user_id: str,
        sub_token: str,
        res_key: str,
        p_obj_key: str,
        p_action: str,
        action: str,
    ) -> bool:
        """结构匹配: 主体层次 + 资源通配 + 动作通配。

        复用 casbin RoleManager.has_link 解析主体继承链 (role/group/user),
        复用 casbin key_match 做资源与动作的通配匹配,不重复造轮子。

        注意: 使用 key_match (而非 key_match2),因为 key_match2 会将 ":"
        视为 RESTful 路径参数分隔符 (如 ":id" → 匹配任意非斜杠串),
        导致 "audit:read" 错误匹配 "audit:write"。key_match 仅将 "*" 视为通配,
        其他字符按字面量匹配,符合 "type:action" 格式的语义。

        - 主体: "*" 通配; RoleManager.has_link (覆盖 role/group/user 继承链);
          或主体标识即当前用户 (兼容直接 user_id 写法)。
        - 资源: key_match (支持 "evaluation:*" 通配 "evaluation:eval-1")。
        - 动作: key_match (支持 "evaluation:*" 通配 "evaluation:read")。
        """
        # 主体匹配
        if sub_token == WILDCARD:
            sub_ok = True
        elif rm.has_link(user_id, sub_token):
            sub_ok = True
        elif sub_token in (user_id, f"user:{user_id}"):
            sub_ok = True
        else:
            sub_ok = False
        if not sub_ok:
            return False
        # 资源匹配
        if not key_match(res_key, p_obj_key):
            return False
        # 动作匹配
        if not key_match(action, p_action):
            return False
        return True

    def _condition_satisfied(
        self,
        condition: Optional[dict],
        subject_attrs: Optional[Dict[str, Any]],
        resource_attrs: Optional[Dict[str, Any]],
    ) -> bool:
        """评估 condition (JSON dict, AND 语义)。

        condition 为 None / 空 → 恒满足 (无条件策略)。
        每个 kv: key 为属性路径 (subject.X / resource.X / 特殊 subject.manages),
        value 为字面量或引用串;两侧取值后做相等比较。
        所有 kv 均满足才返回 True。

        若 condition 引用了未被提供的属性 (None),视为该条件无法满足 → False,
        以保证 "需属性才能判定" 的策略在属性缺失时不会误放行。
        """
        if not condition:
            return True
        subject_attrs = subject_attrs or {}
        resource_attrs = resource_attrs or {}

        for key, expected in condition.items():
            # 特殊条件: subject.manages → 主体是否为资源所有者的主管
            # 预取结果由 check 注入到 subject_attrs["_manages_cache"][owner_id]
            if key == "subject.manages":
                owner_ref = expected  # 引用 resource 属性 (如 "resource.owner_id")
                owner_id = self._resolve_value(
                    owner_ref, subject_attrs, resource_attrs
                )
                if owner_id is None:
                    return False
                cache = subject_attrs.get("_manages_cache") or {}
                if not cache.get(owner_id):
                    return False
                continue

            actual = self._resolve_attr(key, subject_attrs, resource_attrs)
            expected_val = self._resolve_value(
                expected, subject_attrs, resource_attrs
            )
            if actual is None or expected_val is None:
                return False
            if str(actual) != str(expected_val):
                return False
        return True

    def _resolve_attr(
        self,
        key: str,
        subject_attrs: Dict[str, Any],
        resource_attrs: Dict[str, Any],
    ) -> Any:
        """解析属性路径 key (subject.X / resource.X) 为实际值。"""
        if not isinstance(key, str) or "." not in key:
            return None
        prefix, _, attr = key.partition(".")
        if prefix == "subject":
            return subject_attrs.get(attr)
        if prefix == "resource":
            return resource_attrs.get(attr)
        return None

    def _resolve_value(
        self,
        value: Any,
        subject_attrs: Dict[str, Any],
        resource_attrs: Dict[str, Any],
    ) -> Any:
        """解析 condition 值: 引用串 (subject.X/resource.X) → 取属性值; 否则字面量。"""
        if isinstance(value, str) and "." in value:
            prefix, _, attr = value.partition(".")
            if prefix == "subject":
                return subject_attrs.get(attr)
            if prefix == "resource":
                return resource_attrs.get(attr)
        return value


# ============================================================
# 模块级单例
# ============================================================

_abac_engine: Optional[ABACEngine] = None


def get_abac_engine() -> ABACEngine:
    """获取 ABAC 引擎单例 (懒加载, 复用全局 AsyncSessionLocal)。

    casbin 未安装时单例仍可用,check 内部自动回退 RBAC。
    """
    global _abac_engine
    if _abac_engine is None:
        _abac_engine = ABACEngine()
    return _abac_engine


def reset_abac_engine() -> None:
    """重置单例 (测试用: 切换 session_factory 后重建)"""
    global _abac_engine
    _abac_engine = None


# ============================================================
# FastAPI Depends 装饰器: require_permission
# ============================================================


def require_permission(
    action: str,
    resource_type: str = WILDCARD,
    resource_id: Optional[str] = None,
    resource_attrs: Optional[Dict[str, Any]] = None,
):
    """FastAPI 依赖工厂: 要求当前用户对指定资源具备指定动作权限。

    用法:
        @router.get("/evaluations/{evaluation_id}")
        async def get_eval(
            evaluation_id: str,
            role: Role = Depends(require_permission(
                "evaluation:read",
                resource_type="evaluation",
            )),
        ):
            ...

    Args:
        action: 动作 (如 "evaluation:read")。
        resource_type: 资源类型; 未指定时取 "*"。
        resource_id: 资源 ID; 为 None 时尝试从路径参数提取
            (优先 "{resource_type}_id", 回退 "id"); 均无则 "*"。
        resource_attrs: 静态资源属性 (字面量 dict); 用于无 DB 资源加载的场景。
            动态资源属性建议在路由内调用 check_permission() 时传入。

    降级:
        - casbin 未安装 → 回退 RBAC (仅检查角色)。
        - 当前租户无 ABAC 策略 → 回退 RBAC。
    """
    # 闭包捕获静态参数,运行时从 Depends 解析身份与租户
    _static_resource_attrs = resource_attrs

    async def checker(
        request: Request,
        role: Role = Depends(get_current_user_role),
        user_id: str = Depends(get_current_user_id),
    ):
        tenant_id = get_current_tenant()

        # 解析 resource_id: 显式 > 路径参数 > 通配
        rid = resource_id
        if rid is None:
            rid = (
                request.path_params.get(f"{resource_type}_id")
                or request.path_params.get("id")
                or WILDCARD
            )

        engine = get_abac_engine()
        # require_permission 装饰器层无法低成本加载具体资源属性,
        # 仅做结构级 + 主体属性级鉴权;资源属性级鉴权由路由内显式调用
        # check_permission() 完成。此处透传静态属性 + 资源 id/type。
        r_attrs = dict(_static_resource_attrs or {})
        r_attrs.setdefault("id", rid)
        r_attrs.setdefault("type", resource_type)

        ok = await engine.check(
            user_id=user_id,
            role=role,
            tenant_id=tenant_id,
            action=action,
            resource_type=resource_type,
            resource_id=rid,
            resource_attrs=r_attrs,
        )
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="权限不足",
            )
        return role

    return checker


# ============================================================
# 便捷: 路由内精细鉴权 (自动预取主体属性 + manager 关系缓存)
# ============================================================


async def check_permission(
    user_id: str,
    role: Role,
    tenant_id: str,
    action: str,
    resource_type: str = WILDCARD,
    resource_id: str = WILDCARD,
    resource_attrs: Optional[Dict[str, Any]] = None,
) -> bool:
    """引擎鉴权的便捷异步包装: 自动预取主体属性 + subject.manages 缓存。

    路由内精细鉴权 (已知资源属性) 推荐使用本函数,例如:
        ok = await check_permission(user_id, role, tenant_id,
                                    "evaluation:read",
                                    "evaluation", eval_id,
                                    resource_attrs={"owner_id": emp_id,
                                                    "department": emp_dept})
    """
    engine = get_abac_engine()
    # admin 短路
    if role == Role.ADMIN:
        return True
    if not CASBIN_AVAILABLE:
        return _rbac_check(role, action)

    tenant_id = tenant_id or DEFAULT_TENANT_ID
    # 预取主体属性 (department/manager_id 等,供 condition 评估)
    subject_attrs = await engine._load_subject_attrs(user_id, tenant_id)

    # 预取 subject.manages 条件所需的 owner → 是否为主管 映射
    resource_attrs = dict(resource_attrs or {})
    resource_attrs.setdefault("id", resource_id)
    resource_attrs.setdefault("type", resource_type)

    owner_id = resource_attrs.get("owner_id")
    if owner_id:
        owner_manager = await engine._load_user_manager_id(owner_id, tenant_id)
        cache = subject_attrs.setdefault("_manages_cache", {})
        cache[owner_id] = owner_manager == user_id

    return await engine.check(
        user_id=user_id,
        role=role,
        tenant_id=tenant_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        subject_attrs=subject_attrs,
        resource_attrs=resource_attrs,
    )
