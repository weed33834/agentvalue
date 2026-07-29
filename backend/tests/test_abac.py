"""
ABAC 权限引擎测试 (P1-7: 属性级访问控制)

覆盖:
- RBAC 向后兼容 (无策略回退 / casbin 未安装降级)
- ABAC 策略匹配 (角色策略 + 条件匹配: 部门隔离 / 主管团队 / 员工自评)
- 用户组权限继承 (组成员继承组策略)
- 策略优先级 (deny 优先于 allow / priority 字段决定强弱)
- 多租户隔离 (租户 A 策略不影响租户 B)
- require_permission FastAPI 依赖装饰器 (端到端)
- 用户组管理 API (CRUD / 成员管理 / 组策略)

运行:
    cd /workspace/agentvalue/backend && python -m pytest tests/test_abac.py -v
"""

import asyncio
import tempfile
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from auth.abac import (
    ABACEngine,
    CASBIN_AVAILABLE,
    DEFAULT_ROLE_PERMISSIONS,
    _rbac_check,
    check_permission,
    get_abac_engine,
    reset_abac_engine,
    require_permission,
)
from auth.rbac import Role
from models.models import DEFAULT_TENANT_ID, User
from models.policy import (
    DEFAULT_PRIORITY,
    EFFECT_ALLOW,
    EFFECT_DENY,
    SUBJECT_TYPE_GROUP,
    SUBJECT_TYPE_ROLE,
    SUBJECT_TYPE_USER,
    WILDCARD,
    Policy,
)
from models.user_group import UserGroup, UserGroupMember


# ============================================================
# Fixtures: 临时 SQLite + 全局 engine/AsyncSessionLocal 替换
# ============================================================


@pytest.fixture
def temp_db(monkeypatch):
    """临时 SQLite + 替换 core.database 全局 engine / AsyncSessionLocal。

    同时替换 auth.abac.AsyncSessionLocal 并重置 ABAC 引擎单例,
    确保 get_abac_engine() 使用临时 DB。
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp.name}"

    from core import database as db_module

    engine = create_async_engine(
        db_url, echo=False, future=True, connect_args={"check_same_thread": False}
    )
    new_session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    db_module.engine = engine
    db_module.AsyncSessionLocal = new_session_factory
    # 同步替换 auth.abac 持有的 AsyncSessionLocal 引用
    monkeypatch.setattr("auth.abac.AsyncSessionLocal", new_session_factory)
    reset_abac_engine()
    yield new_session_factory
    try:
        Path(tmp.name).unlink(missing_ok=True)
    except Exception:
        pass


@pytest.fixture
async def initialized_db(temp_db):
    """初始化数据库表 (Base.metadata.create_all 创建全部表)"""
    from core.database import close_db, init_db

    await init_db()
    yield temp_db
    await close_db()


@pytest.fixture
def engine(initialized_db):
    """获取绑定临时 DB 的 ABAC 引擎单例"""
    return get_abac_engine()


# ============================================================
# 数据播种辅助
# ============================================================


async def _seed_user(
    session_factory,
    user_id: str,
    role: str,
    tenant_id: str = DEFAULT_TENANT_ID,
    department: str = None,
    manager_id: str = None,
):
    """播种一个 User 行"""
    async with session_factory() as session:
        user = User(
            user_id=user_id,
            name=user_id,
            role=role,
            tenant_id=tenant_id,
            department=department,
            manager_id=manager_id,
        )
        session.add(user)
        await session.commit()


async def _seed_group(session_factory, group_id: str, tenant_id: str = DEFAULT_TENANT_ID):
    """播种一个 UserGroup"""
    async with session_factory() as session:
        grp = UserGroup(group_id=group_id, name=group_id, tenant_id=tenant_id)
        session.add(grp)
        await session.commit()


async def _add_member(
    session_factory, user_id: str, group_id: str, tenant_id: str = DEFAULT_TENANT_ID
):
    """播种组成员关系"""
    async with session_factory() as session:
        m = UserGroupMember(
            user_id=user_id, group_id=group_id, tenant_id=tenant_id
        )
        session.add(m)
        await session.commit()


async def _seed_policy(
    session_factory,
    subject_type: str,
    subject_id: str,
    resource_type: str,
    action: str,
    effect: str = EFFECT_ALLOW,
    resource_id: str = WILDCARD,
    condition: dict = None,
    priority: int = DEFAULT_PRIORITY,
    tenant_id: str = DEFAULT_TENANT_ID,
):
    """播种一条 ABAC 策略"""
    import uuid

    async with session_factory() as session:
        p = Policy(
            policy_id=f"pol_{uuid.uuid4().hex[:16]}",
            subject_type=subject_type,
            subject_id=subject_id,
            resource_type=resource_type,
            resource_id=resource_id,
            action=action,
            effect=effect,
            condition=condition,
            priority=priority,
            tenant_id=tenant_id,
        )
        session.add(p)
        await session.commit()


# ============================================================
# 1. RBAC 向后兼容
# ============================================================


class TestRBACBackwardCompat:
    """RBAC 向后兼容: casbin 可用但无策略时回退 RBAC; casbin 不可用时纯 RBAC"""

    def test_default_role_permissions_covers_all_roles(self):
        """默认角色权限映射覆盖 4 种角色"""
        assert set(DEFAULT_ROLE_PERMISSIONS.keys()) == {
            Role.EMPLOYEE,
            Role.MANAGER,
            Role.HR,
            Role.ADMIN,
        }

    def test_rbac_check_admin_allows_everything(self):
        """admin 通配 '*' → 任意动作放行"""
        assert _rbac_check(Role.ADMIN, "evaluation:read") is True
        assert _rbac_check(Role.ADMIN, "anything:weird") is True

    def test_rbac_check_employee_can_read_cannot_approve(self):
        """employee 可 evaluation:read, 不可 evaluation:approve"""
        assert _rbac_check(Role.EMPLOYEE, "evaluation:read") is True
        assert _rbac_check(Role.EMPLOYEE, "evaluation:approve") is False

    def test_no_policy_falls_back_to_rbac(self, engine, initialized_db):
        """租户无任何 ABAC 策略 → 回退 RBAC (向后兼容, 零策略零侵入)"""

        async def _run():
            # employee 可读
            ok_read = await engine.check(
                "u-emp", Role.EMPLOYEE, DEFAULT_TENANT_ID, "evaluation:read"
            )
            # employee 不可审批
            ok_approve = await engine.check(
                "u-emp", Role.EMPLOYEE, DEFAULT_TENANT_ID, "evaluation:approve"
            )
            return ok_read, ok_approve

        ok_read, ok_approve = asyncio.run(_run())
        assert ok_read is True
        assert ok_approve is False

    def test_casbin_unavailable_degrades_to_rbac(
        self, engine, initialized_db, monkeypatch
    ):
        """casbin 标记为不可用 → 所有检查降级为纯 RBAC"""

        async def _run():
            # 即便有策略, casbin 不可用也应直接走 RBAC
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_ROLE,
                "employee",
                "evaluation",
                "evaluation:approve",
                EFFECT_ALLOW,
            )
            ok_read = await engine.check(
                "u-emp", Role.EMPLOYEE, DEFAULT_TENANT_ID, "evaluation:read"
            )
            ok_approve = await engine.check(
                "u-emp", Role.EMPLOYEE, DEFAULT_TENANT_ID, "evaluation:approve"
            )
            return ok_read, ok_approve

        monkeypatch.setattr("auth.abac.CASBIN_AVAILABLE", False)
        ok_read, ok_approve = asyncio.run(_run())
        # 纯 RBAC: read 放行, approve 拒绝 (策略被忽略)
        assert ok_read is True
        assert ok_approve is False

    def test_admin_always_allowed_even_with_deny_policy(self, engine, initialized_db):
        """admin 超级用户: 即便有 deny 策略也恒放行"""

        async def _run():
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_ROLE,
                "*",
                "evaluation",
                "evaluation:read",
                EFFECT_DENY,
            )
            return await engine.check(
                "u-admin", Role.ADMIN, DEFAULT_TENANT_ID, "evaluation:read"
            )

        assert asyncio.run(_run()) is True

    def test_existing_rbac_role_check_still_works(self):
        """现有 RBAC (require_role / can_access) 函数签名未改, 仍可正常使用"""
        from auth.rbac import Role, can_access, require_role

        assert can_access(Role.HR, "audit") is True
        assert can_access(Role.EMPLOYEE, "audit") is False
        # require_role 返回可调用 checker (签名未变)
        checker = require_role(Role.ADMIN)
        assert callable(checker)


# ============================================================
# 2. ABAC 策略匹配
# ============================================================


class TestABACPolicyMatching:
    """ABAC 策略匹配: 角色/用户主体 + 资源通配 + 动作 + 属性条件"""

    def test_role_policy_allows_matching_action(self, engine, initialized_db):
        """role:hr 策略 allow audit:read → hr 用户可读 audit"""

        async def _run():
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_ROLE,
                "hr",
                "audit_log",
                "audit:read",
                EFFECT_ALLOW,
            )
            return await engine.check(
                "u-hr", Role.HR, DEFAULT_TENANT_ID, "audit:read", "audit_log", "*"
            )

        assert asyncio.run(_run()) is True

    def test_role_policy_denies_non_matching_action(self, engine, initialized_db):
        """role:hr 策略 allow audit:read → hr 用户不可 audit:write (默认拒绝)"""

        async def _run():
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_ROLE,
                "hr",
                "audit_log",
                "audit:read",
                EFFECT_ALLOW,
            )
            return await engine.check(
                "u-hr", Role.HR, DEFAULT_TENANT_ID, "audit:write", "audit_log", "*"
            )

        assert asyncio.run(_run()) is False

    def test_role_policy_does_not_apply_to_other_role(self, engine, initialized_db):
        """role:hr 策略不适用于 employee (主体不匹配 → 默认拒绝)"""

        async def _run():
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_ROLE,
                "hr",
                "audit_log",
                "audit:read",
                EFFECT_ALLOW,
            )
            return await engine.check(
                "u-emp", Role.EMPLOYEE, DEFAULT_TENANT_ID, "audit:read", "audit_log", "*"
            )

        assert asyncio.run(_run()) is False

    def test_wildcard_subject_matches_all(self, engine, initialized_db):
        """subject_id="*" 通配所有主体 → employee 也命中"""

        async def _run():
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_ROLE,
                "*",
                "report",
                "report:read",
                EFFECT_ALLOW,
            )
            return await engine.check(
                "u-emp", Role.EMPLOYEE, DEFAULT_TENANT_ID, "report:read", "report", "*"
            )

        assert asyncio.run(_run()) is True

    def test_resource_wildcard_matches_specific_id(self, engine, initialized_db):
        """resource_id="*" 通配 → 匹配具体资源 ID"""

        async def _run():
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_ROLE,
                "hr",
                "evaluation",
                "evaluation:read",
                EFFECT_ALLOW,
                resource_id=WILDCARD,
            )
            return await engine.check(
                "u-hr",
                Role.HR,
                DEFAULT_TENANT_ID,
                "evaluation:read",
                "evaluation",
                "eval-001",
            )

        assert asyncio.run(_run()) is True

    def test_condition_hr_same_department(self, engine, initialized_db):
        """HR 只能审计本部门员工 (subject.department == resource.department)"""

        async def _run():
            await _seed_user(initialized_db, "u-hr", "hr", department="Engineering")
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_ROLE,
                "hr",
                "evaluation",
                "evaluation:read",
                EFFECT_ALLOW,
                condition={"subject.department": "resource.department"},
            )
            # 同部门 → 放行
            ok_same = await check_permission(
                "u-hr",
                Role.HR,
                DEFAULT_TENANT_ID,
                "evaluation:read",
                "evaluation",
                "eval-1",
                resource_attrs={"owner_id": "e1", "department": "Engineering"},
            )
            # 不同部门 → 拒绝
            ok_diff = await check_permission(
                "u-hr",
                Role.HR,
                DEFAULT_TENANT_ID,
                "evaluation:read",
                "evaluation",
                "eval-2",
                resource_attrs={"owner_id": "e2", "department": "Sales"},
            )
            return ok_same, ok_diff

        ok_same, ok_diff = asyncio.run(_run())
        assert ok_same is True
        assert ok_diff is False

    def test_condition_manager_own_team(self, engine, initialized_db):
        """manager 只能查看自己团队的评估 (subject.manages == resource.owner_id)"""

        async def _run():
            # manager M, 员工 e1 (manager_id=M), 员工 e2 (manager_id=其他)
            await _seed_user(initialized_db, "M1", "manager", department="Engineering")
            await _seed_user(
                initialized_db,
                "e1",
                "employee",
                department="Engineering",
                manager_id="M1",
            )
            await _seed_user(
                initialized_db,
                "e2",
                "employee",
                department="Engineering",
                manager_id="OTHER",
            )
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_ROLE,
                "manager",
                "evaluation",
                "evaluation:read",
                EFFECT_ALLOW,
                condition={"subject.manages": "resource.owner_id"},
            )
            # M1 查看下属 e1 的评估 → 放行
            ok_own = await check_permission(
                "M1",
                Role.MANAGER,
                DEFAULT_TENANT_ID,
                "evaluation:read",
                "evaluation",
                "eval-e1",
                resource_attrs={"owner_id": "e1"},
            )
            # M1 查看非下属 e2 的评估 → 拒绝
            ok_other = await check_permission(
                "M1",
                Role.MANAGER,
                DEFAULT_TENANT_ID,
                "evaluation:read",
                "evaluation",
                "eval-e2",
                resource_attrs={"owner_id": "e2"},
            )
            return ok_own, ok_other

        ok_own, ok_other = asyncio.run(_run())
        assert ok_own is True
        assert ok_other is False

    def test_condition_employee_self_only(self, engine, initialized_db):
        """员工只能查看自己的评估 (subject.id == resource.owner_id)"""

        async def _run():
            await _seed_user(initialized_db, "e1", "employee")
            await _seed_user(initialized_db, "e2", "employee")
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_ROLE,
                "employee",
                "evaluation",
                "evaluation:read",
                EFFECT_ALLOW,
                condition={"subject.id": "resource.owner_id"},
            )
            # e1 查看自己的 → 放行
            ok_self = await check_permission(
                "e1",
                Role.EMPLOYEE,
                DEFAULT_TENANT_ID,
                "evaluation:read",
                "evaluation",
                "eval-e1",
                resource_attrs={"owner_id": "e1"},
            )
            # e1 查看 e2 的 → 拒绝
            ok_other = await check_permission(
                "e1",
                Role.EMPLOYEE,
                DEFAULT_TENANT_ID,
                "evaluation:read",
                "evaluation",
                "eval-e2",
                resource_attrs={"owner_id": "e2"},
            )
            return ok_self, ok_other

        ok_self, ok_other = asyncio.run(_run())
        assert ok_self is True
        assert ok_other is False

    def test_user_subject_policy(self, engine, initialized_db):
        """subject_type=user → 仅指定用户命中"""

        async def _run():
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_USER,
                "u-special",
                "report",
                "report:read",
                EFFECT_ALLOW,
            )
            ok_owner = await engine.check(
                "u-special",
                Role.EMPLOYEE,
                DEFAULT_TENANT_ID,
                "report:read",
                "report",
                "*",
            )
            ok_other = await engine.check(
                "u-other",
                Role.EMPLOYEE,
                DEFAULT_TENANT_ID,
                "report:read",
                "report",
                "*",
            )
            return ok_owner, ok_other

        ok_owner, ok_other = asyncio.run(_run())
        assert ok_owner is True
        assert ok_other is False


# ============================================================
# 3. 用户组权限继承
# ============================================================


class TestGroupInheritance:
    """用户组权限继承: 组成员自动继承组绑定的 allow 策略"""

    def test_group_member_inherits_allow(self, engine, initialized_db):
        """组成员继承组的 allow 策略; 非成员不继承"""

        async def _run():
            await _seed_user(initialized_db, "g-mem", "employee")
            await _seed_user(initialized_db, "g-non", "employee")
            await _seed_group(initialized_db, "eng-team")
            await _add_member(initialized_db, "g-mem", "eng-team")
            # 组策略: eng-team 可审批评估 (employee 默认不可)
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_GROUP,
                "eng-team",
                "evaluation",
                "evaluation:approve",
                EFFECT_ALLOW,
            )
            ok_mem = await engine.check(
                "g-mem",
                Role.EMPLOYEE,
                DEFAULT_TENANT_ID,
                "evaluation:approve",
                "evaluation",
                "*",
            )
            ok_non = await engine.check(
                "g-non",
                Role.EMPLOYEE,
                DEFAULT_TENANT_ID,
                "evaluation:approve",
                "evaluation",
                "*",
            )
            return ok_mem, ok_non

        ok_mem, ok_non = asyncio.run(_run())
        assert ok_mem is True
        assert ok_non is False

    def test_user_in_multiple_groups(self, engine, initialized_db):
        """用户属于多个组 → 任一组的 allow 策略生效"""

        async def _run():
            await _seed_user(initialized_db, "u-multi", "employee")
            await _seed_group(initialized_db, "grp-a")
            await _seed_group(initialized_db, "grp-b")
            await _add_member(initialized_db, "u-multi", "grp-a")
            await _add_member(initialized_db, "u-multi", "grp-b")
            # grp-a 允许 action-a, grp-b 允许 action-b
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_GROUP,
                "grp-a",
                "res",
                "res:actionA",
                EFFECT_ALLOW,
            )
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_GROUP,
                "grp-b",
                "res",
                "res:actionB",
                EFFECT_ALLOW,
            )
            ok_a = await engine.check(
                "u-multi", Role.EMPLOYEE, DEFAULT_TENANT_ID, "res:actionA", "res", "*"
            )
            ok_b = await engine.check(
                "u-multi", Role.EMPLOYEE, DEFAULT_TENANT_ID, "res:actionB", "res", "*"
            )
            return ok_a, ok_b

        ok_a, ok_b = asyncio.run(_run())
        assert ok_a is True
        assert ok_b is True


# ============================================================
# 4. 策略优先级 (deny 优先于 allow)
# ============================================================


class TestPolicyPriority:
    """策略优先级: priority 越小越高; 同优先级 deny 优先于 allow (deny-override)"""

    def test_deny_overrides_allow_same_priority(self, engine, initialized_db):
        """同优先级下 deny 优先于 allow (核心: deny 优先于 allow)"""

        async def _run():
            await _seed_user(initialized_db, "u-hr", "hr")
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_ROLE,
                "hr",
                "evaluation",
                "evaluation:delete",
                EFFECT_ALLOW,
                priority=10,
            )
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_ROLE,
                "hr",
                "evaluation",
                "evaluation:delete",
                EFFECT_DENY,
                priority=10,
            )
            return await engine.check(
                "u-hr",
                Role.HR,
                DEFAULT_TENANT_ID,
                "evaluation:delete",
                "evaluation",
                "*",
            )

        assert asyncio.run(_run()) is False  # deny 胜

    def test_higher_priority_deny_beats_lower_priority_allow(
        self, engine, initialized_db
    ):
        """deny 优先级更高 (priority 更小) → deny 胜"""

        async def _run():
            await _seed_user(initialized_db, "u-hr", "hr")
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_ROLE,
                "hr",
                "evaluation",
                "evaluation:delete",
                EFFECT_ALLOW,
                priority=20,
            )
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_ROLE,
                "hr",
                "evaluation",
                "evaluation:delete",
                EFFECT_DENY,
                priority=5,
            )
            return await engine.check(
                "u-hr",
                Role.HR,
                DEFAULT_TENANT_ID,
                "evaluation:delete",
                "evaluation",
                "*",
            )

        assert asyncio.run(_run()) is False

    def test_higher_priority_allow_beats_lower_priority_deny(
        self, engine, initialized_db
    ):
        """allow 优先级更高 (priority 更小) → allow 胜 (priority 字段决定强弱)"""

        async def _run():
            await _seed_user(initialized_db, "u-hr", "hr")
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_ROLE,
                "hr",
                "evaluation",
                "evaluation:delete",
                EFFECT_ALLOW,
                priority=5,
            )
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_ROLE,
                "hr",
                "evaluation",
                "evaluation:delete",
                EFFECT_DENY,
                priority=20,
            )
            return await engine.check(
                "u-hr",
                Role.HR,
                DEFAULT_TENANT_ID,
                "evaluation:delete",
                "evaluation",
                "*",
            )

        assert asyncio.run(_run()) is True

    def test_group_deny_overrides_role_allow(self, engine, initialized_db):
        """组级 deny 优先于角色级 allow (同优先级): 细粒度拒绝胜出"""

        async def _run():
            await _seed_user(initialized_db, "u-hr", "hr")
            await _seed_group(initialized_db, "restricted")
            await _add_member(initialized_db, "u-hr", "restricted")
            # 角色级 allow
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_ROLE,
                "hr",
                "evaluation",
                "evaluation:read",
                EFFECT_ALLOW,
            )
            # 组级 deny (同优先级)
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_GROUP,
                "restricted",
                "evaluation",
                "evaluation:read",
                EFFECT_DENY,
            )
            return await engine.check(
                "u-hr",
                Role.HR,
                DEFAULT_TENANT_ID,
                "evaluation:read",
                "evaluation",
                "*",
            )

        assert asyncio.run(_run()) is False


# ============================================================
# 5. 多租户隔离
# ============================================================


class TestTenantIsolation:
    """多租户隔离: 租户 A 的策略不影响租户 B"""

    def test_policy_only_applies_to_its_tenant(self, engine, initialized_db):
        """tA 的策略不授予 tB 用户权限"""

        async def _run():
            TENANT_A = "tenant-a"
            TENANT_B = "tenant-b"
            await _seed_user(initialized_db, "uA", "employee", tenant_id=TENANT_A)
            await _seed_user(initialized_db, "uB", "employee", tenant_id=TENANT_B)
            # 仅在 tA 创建策略: employee 可 report:read (employee 默认不可)
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_ROLE,
                "employee",
                "report",
                "report:read",
                EFFECT_ALLOW,
                tenant_id=TENANT_A,
            )
            # tA 用户 → ABAC 命中 → 放行
            ok_a = await engine.check(
                "uA", Role.EMPLOYEE, TENANT_A, "report:read", "report", "*"
            )
            # tB 用户 → tB 无策略 → RBAC 回退 → employee 不可 report:read → 拒绝
            ok_b = await engine.check(
                "uB", Role.EMPLOYEE, TENANT_B, "report:read", "report", "*"
            )
            return ok_a, ok_b

        ok_a, ok_b = asyncio.run(_run())
        assert ok_a is True
        assert ok_b is False

    def test_group_membership_isolated_per_tenant(self, engine, initialized_db):
        """组成员关系按租户隔离: tA 的组成员不继承 tB 的组策略"""

        async def _run():
            TENANT_A = "tenant-a"
            TENANT_B = "tenant-b"
            # 同一 user_id 在两个租户各建一行 (user_id 按租户隔离)
            await _seed_user(initialized_db, "uX", "employee", tenant_id=TENANT_A)
            await _seed_user(initialized_db, "uX", "employee", tenant_id=TENANT_B)
            await _seed_group(initialized_db, "grp", tenant_id=TENANT_A)
            await _seed_group(initialized_db, "grp", tenant_id=TENANT_B)
            # 仅在 tA 将 uX 加入 grp
            await _add_member(initialized_db, "uX", "grp", tenant_id=TENANT_A)
            # 两个租户都创建组策略
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_GROUP,
                "grp",
                "report",
                "report:read",
                EFFECT_ALLOW,
                tenant_id=TENANT_A,
            )
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_GROUP,
                "grp",
                "report",
                "report:read",
                EFFECT_ALLOW,
                tenant_id=TENANT_B,
            )
            # tA: uX 是 grp 成员 → 放行
            ok_a = await engine.check(
                "uX", Role.EMPLOYEE, TENANT_A, "report:read", "report", "*"
            )
            # tB: uX 不是 grp 成员 (成员关系仅 tA) → 拒绝
            ok_b = await engine.check(
                "uX", Role.EMPLOYEE, TENANT_B, "report:read", "report", "*"
            )
            return ok_a, ok_b

        ok_a, ok_b = asyncio.run(_run())
        assert ok_a is True
        assert ok_b is False


# ============================================================
# 6. require_permission FastAPI 依赖装饰器 (端到端)
# ============================================================


@pytest.fixture
def api_client(initialized_db):
    """挂载 require_permission 保护路由 + user_groups 路由的最小 FastAPI app"""
    from api.admin.user_groups import router as user_groups_router

    app = FastAPI()

    @app.get("/protected/evaluations/{evaluation_id}")
    async def read_eval(
        evaluation_id: str,
        role: Role = Depends(
            require_permission("evaluation:read", resource_type="evaluation")
        ),
    ):
        return {"evaluation_id": evaluation_id, "role": role.value}

    app.include_router(user_groups_router)
    with TestClient(app) as c:
        yield c


class TestRequirePermissionDecorator:
    """require_permission 装饰器端到端测试"""

    def test_admin_passes(self, api_client, initialized_db):
        """admin 恒放行 (即便无策略)"""
        resp = api_client.get(
            "/protected/evaluations/eval-1",
            headers={"x-user-role": "admin", "x-user-id": "u-admin"},
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

    def test_employee_forbidden_without_policy(self, api_client, initialized_db):
        """无 ABAC 策略 → RBAC 回退: employee 可 evaluation:read → 放行"""

        async def _run():
            return None

        asyncio.run(_run())
        resp = api_client.get(
            "/protected/evaluations/eval-1",
            headers={"x-user-role": "employee", "x-user-id": "u-emp"},
        )
        # 无策略 → RBAC 回退 → employee 可 evaluation:read → 200
        assert resp.status_code == 200

    def test_employee_denied_when_abac_policy_no_match(
        self, api_client, initialized_db
    ):
        """租户有 ABAC 策略但 employee 不匹配 → 403"""

        async def _run():
            # 创建一条 hr 专属策略, 触发 ABAC 模式 (租户有策略)
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_ROLE,
                "hr",
                "evaluation",
                "evaluation:read",
                EFFECT_ALLOW,
            )

        asyncio.run(_run())
        resp = api_client.get(
            "/protected/evaluations/eval-1",
            headers={"x-user-role": "employee", "x-user-id": "u-emp"},
        )
        # ABAC 模式: employee 无匹配策略 → 默认拒绝 → 403
        assert resp.status_code == 403

    def test_hr_allowed_when_abac_policy_matches(self, api_client, initialized_db):
        """hr 命中 ABAC 策略 → 200"""

        async def _run():
            await _seed_policy(
                initialized_db,
                SUBJECT_TYPE_ROLE,
                "hr",
                "evaluation",
                "evaluation:read",
                EFFECT_ALLOW,
            )

        asyncio.run(_run())
        resp = api_client.get(
            "/protected/evaluations/eval-1",
            headers={"x-user-role": "hr", "x-user-id": "u-hr"},
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "hr"


# ============================================================
# 7. 用户组管理 API
# ============================================================


def _admin_headers(user_id="ADMIN001"):
    return {"x-user-role": "admin", "x-user-id": user_id}


def _employee_headers(user_id="E1001"):
    return {"x-user-role": "employee", "x-user-id": user_id}


class TestUserGroupsAPI:
    """用户组管理 API 全链路: CRUD / 成员 / 组策略 / 鉴权"""

    def test_employee_forbidden(self, api_client):
        """非 admin/hr → 403 (router 级 dependencies)"""
        resp = api_client.get(
            "/api/v1/admin/user-groups", headers=_employee_headers()
        )
        assert resp.status_code == 403

    def test_group_crud(self, api_client):
        """用户组 CRUD: 创建 / 详情 / 更新 / 删除"""
        # 创建
        resp = api_client.post(
            "/api/v1/admin/user-groups",
            json={"group_id": "eng-team", "name": "Engineering Team"},
            headers=_admin_headers(),
        )
        assert resp.status_code == 201
        assert resp.json()["group_id"] == "eng-team"

        # 列表
        resp = api_client.get(
            "/api/v1/admin/user-groups", headers=_admin_headers()
        )
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

        # 详情
        resp = api_client.get(
            "/api/v1/admin/user-groups/eng-team", headers=_admin_headers()
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Engineering Team"

        # 更新
        resp = api_client.put(
            "/api/v1/admin/user-groups/eng-team",
            json={"name": "Eng", "description": "eng group"},
            headers=_admin_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Eng"
        assert resp.json()["description"] == "eng group"

        # 删除
        resp = api_client.delete(
            "/api/v1/admin/user-groups/eng-team", headers=_admin_headers()
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    def test_duplicate_group_id_conflict(self, api_client):
        """同租户 group_id 重复 → 409"""
        api_client.post(
            "/api/v1/admin/user-groups",
            json={"group_id": "dup", "name": "DUP"},
            headers=_admin_headers(),
        )
        resp = api_client.post(
            "/api/v1/admin/user-groups",
            json={"group_id": "dup", "name": "DUP2"},
            headers=_admin_headers(),
        )
        assert resp.status_code == 409

    def test_member_management(self, api_client):
        """成员管理: 添加 / 列表 / 移除"""
        api_client.post(
            "/api/v1/admin/user-groups",
            json={"group_id": "m-team", "name": "M Team"},
            headers=_admin_headers(),
        )
        # 添加成员 (批量)
        resp = api_client.post(
            "/api/v1/admin/user-groups/m-team/members",
            json={"user_ids": ["u1", "u2", "u1"]},  # u1 重复应跳过
            headers=_admin_headers(),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["added_count"] == 2  # u1, u2
        assert body["skipped_count"] == 1  # 第二个 u1

        # 列表成员
        resp = api_client.get(
            "/api/v1/admin/user-groups/m-team/members", headers=_admin_headers()
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 2

        # 移除成员
        resp = api_client.delete(
            "/api/v1/admin/user-groups/members/u1?group_id=m-team",
            headers=_admin_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["removed"] is True

        # 再次移除 → 404
        resp = api_client.delete(
            "/api/v1/admin/user-groups/members/u1?group_id=m-team",
            headers=_admin_headers(),
        )
        assert resp.status_code == 404

    def test_group_policy_management(self, api_client):
        """组策略管理: 创建 / 列表 / 删除"""
        api_client.post(
            "/api/v1/admin/user-groups",
            json={"group_id": "p-team", "name": "P Team"},
            headers=_admin_headers(),
        )
        # 创建组策略
        resp = api_client.post(
            "/api/v1/admin/user-groups/p-team/policies",
            json={
                "resource_type": "evaluation",
                "resource_id": "*",
                "action": "evaluation:read",
                "effect": "allow",
                "priority": 0,
            },
            headers=_admin_headers(),
        )
        assert resp.status_code == 201
        policy_id = resp.json()["policy_id"]
        assert resp.json()["subject_type"] == "group"
        assert resp.json()["subject_id"] == "p-team"

        # 列表组策略
        resp = api_client.get(
            "/api/v1/admin/user-groups/p-team/policies", headers=_admin_headers()
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

        # 删除组策略
        resp = api_client.delete(
            f"/api/v1/admin/user-groups/p-team/policies/{policy_id}",
            headers=_admin_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    def test_invalid_effect_rejected(self, api_client):
        """effect 取值非法 → 422"""
        api_client.post(
            "/api/v1/admin/user-groups",
            json={"group_id": "e-team", "name": "E Team"},
            headers=_admin_headers(),
        )
        resp = api_client.post(
            "/api/v1/admin/user-groups/e-team/policies",
            json={
                "resource_type": "evaluation",
                "action": "evaluation:read",
                "effect": "maybe",
            },
            headers=_admin_headers(),
        )
        assert resp.status_code == 422

    def test_delete_group_cascades_policies_and_members(self, api_client, initialized_db):
        """删除组 → 成员关联 + 组策略一并清理"""
        api_client.post(
            "/api/v1/admin/user-groups",
            json={"group_id": "c-team", "name": "C Team"},
            headers=_admin_headers(),
        )
        api_client.post(
            "/api/v1/admin/user-groups/c-team/members",
            json={"user_ids": ["cu1"]},
            headers=_admin_headers(),
        )
        api_client.post(
            "/api/v1/admin/user-groups/c-team/policies",
            json={"resource_type": "evaluation", "action": "evaluation:read"},
            headers=_admin_headers(),
        )
        # 删除组
        resp = api_client.delete(
            "/api/v1/admin/user-groups/c-team", headers=_admin_headers()
        )
        assert resp.status_code == 200

        # 验证成员与策略已清理 (DB 直查)
        async def _verify():
            from sqlalchemy import select

            async with initialized_db() as session:
                members = (
                    await session.execute(
                        select(UserGroupMember).where(
                            UserGroupMember.group_id == "c-team"
                        )
                    )
                ).scalars().all()
                policies = (
                    await session.execute(
                        select(Policy).where(
                            Policy.subject_type == SUBJECT_TYPE_GROUP,
                            Policy.subject_id == "c-team",
                        )
                    )
                ).scalars().all()
            return len(members), len(policies)

        n_members, n_policies = asyncio.run(_verify())
        assert n_members == 0
        assert n_policies == 0
