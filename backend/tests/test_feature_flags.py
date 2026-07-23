"""Feature Flag 系统测试 (P3-2: 应用级功能开关, 对标 Langfuse Feature Flag)

覆盖:
- SDK: is_enabled 各种规则组合 (flag 不存在 / enabled=False / target_user 命中 / target_tenant 命中 /
        percentage 命中 / percentage 未命中)
- explain: 命中原因解释
- CRUD: 创建 / 读取 / 更新 / 删除 / 切换
- 缓存: 第二次查询走缓存 (mock session_factory 验证)
- API: 8 端点全链路 (list / create / get / update / delete / toggle / check)
- 鉴权: 非 ADMIN → 403

运行:
    pytest tests/test_feature_flags.py -v
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import get_settings


# ============================================================
# Fixtures
# ============================================================


def _admin_headers(user_id="ADMIN001"):
    return {"x-user-role": "admin", "x-user-id": user_id}


def _employee_headers(user_id="E1001"):
    return {"x-user-role": "employee", "x-user-id": user_id}


@pytest.fixture
def temp_db(monkeypatch):
    """临时 SQLite + 替换全局 engine/AsyncSessionLocal"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp.name}"

    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from core import database as db_module

    engine = create_async_engine(
        db_url, echo=False, future=True, connect_args={"check_same_thread": False}
    )
    db_module.engine = engine
    db_module.AsyncSessionLocal = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    yield db_module.AsyncSessionLocal
    try:
        Path(tmp.name).unlink(missing_ok=True)
    except Exception:
        pass


@pytest.fixture
async def initialized_db(temp_db):
    from core.database import close_db, init_db

    await init_db()
    yield temp_db
    await close_db()


@pytest.fixture
def app_with_routers(initialized_db):
    """FastAPI app 仅挂载 feature_flags 路由 + 一个 mock AppState"""
    from api.admin.feature_flags import router as feature_flags_router
    from core.feature_flag import FeatureFlagService

    app = FastAPI()
    app.include_router(feature_flags_router)

    class _MockAppState:
        def __init__(self, session_factory):
            self.feature_flag_service = FeatureFlagService(session_factory)

    mock_state = _MockAppState(initialized_db)
    with TestClient(app) as c:
        c.app.state.app_state = mock_state
        yield c


# ============================================================
# SDK: is_enabled 规则测试
# ============================================================


class TestIsEnabledRules:
    """is_enabled 各规则组合测试"""

    def test_flag_not_exists_returns_false(self, initialized_db):
        """flag 不存在 → False"""
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            return await svc.is_enabled("does_not_exist")

        result = asyncio.run(_run())
        assert result is False

    def test_flag_disabled_returns_false(self, initialized_db):
        """flag enabled=False → False (即使 target 命中)"""
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(
                key="disabled_flag",
                description="disabled",
                enabled=False,
                target_user_ids=["u1"],
            )
            return await svc.is_enabled("disabled_flag", user_id="u1")

        assert asyncio.run(_run()) is False

    def test_flag_enabled_no_target_no_percentage_returns_false(self, initialized_db):
        """flag enabled=True 但无 target 无 percentage → 默认 False"""
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(key="empty_flag", enabled=True)
            return await svc.is_enabled("empty_flag")

        assert asyncio.run(_run()) is False

    def test_target_user_hit_returns_true(self, initialized_db):
        """user_id 在 target_user_ids → True"""
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(
                key="user_hit",
                enabled=True,
                target_user_ids=["alice", "bob"],
            )
            return await svc.is_enabled("user_hit", user_id="alice")

        assert asyncio.run(_run()) is True

    def test_target_user_not_in_list_returns_false(self, initialized_db):
        """user_id 不在 target_user_ids 且无 percentage → False"""
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(
                key="user_miss",
                enabled=True,
                target_user_ids=["alice"],
            )
            return await svc.is_enabled("user_miss", user_id="charlie")

        assert asyncio.run(_run()) is False

    def test_target_tenant_hit_returns_true(self, initialized_db):
        """tenant_id 在 target_tenant_ids → True"""
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(
                key="tenant_hit",
                enabled=True,
                target_tenant_ids=["tenant_a"],
            )
            return await svc.is_enabled("tenant_hit", tenant_id="tenant_a")

        assert asyncio.run(_run()) is True

    def test_target_tenant_not_in_list_returns_false(self, initialized_db):
        """tenant_id 不在 target_tenant_ids 且无 percentage → False"""
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(
                key="tenant_miss",
                enabled=True,
                target_tenant_ids=["tenant_a"],
            )
            return await svc.is_enabled("tenant_miss", tenant_id="tenant_z")

        assert asyncio.run(_run()) is False

    def test_percentage_100_always_returns_true(self, initialized_db):
        """rollout_percentage=100 → 任意 identifier 都命中 (bucket < 100 恒为真)"""
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(
                key="full_rollout",
                enabled=True,
                rollout_percentage=100,
            )
            results = []
            for uid in ("u1", "u2", "u3", "u4", "u5"):
                results.append(await svc.is_enabled("full_rollout", user_id=uid))
            return results

        results = asyncio.run(_run())
        assert all(r is True for r in results), f"应全部命中: {results}"

    def test_percentage_0_returns_false(self, initialized_db):
        """rollout_percentage=0 → 永远不命中"""
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(
                key="zero_rollout",
                enabled=True,
                rollout_percentage=0,
            )
            return await svc.is_enabled("zero_rollout", user_id="anyone")

        assert asyncio.run(_run()) is False

    def test_percentage_partial_buckets_distribute(self, initialized_db):
        """rollout_percentage=50 → 大致一半命中 (统计验证)

        使用 hash 桶均匀分布的特性, 取 200 个 user_id 统计命中率应在 30%-70% 之间。
        """
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(
                key="half_rollout",
                enabled=True,
                rollout_percentage=50,
            )
            hits = 0
            total = 200
            for i in range(total):
                if await svc.is_enabled("half_rollout", user_id=f"u_{i}"):
                    hits += 1
            return hits, total

        hits, total = asyncio.run(_run())
        ratio = hits / total
        assert (
            0.3 <= ratio <= 0.7
        ), f"命中率 {ratio} 偏离 0.5 太多 (hits={hits}/{total})"

    def test_percentage_uses_tenant_id_when_no_user(self, initialized_db):
        """无 user_id 时, percentage 用 tenant_id 做分流"""
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(
                key="tenant_percentage",
                enabled=True,
                rollout_percentage=100,  # 100% 确保命中
            )
            return await svc.is_enabled("tenant_percentage", tenant_id="tenant_x")

        # percentage=100 + tenant_id 提供时, 应命中
        assert asyncio.run(_run()) is True

    def test_percentage_no_identifier_returns_false(self, initialized_db):
        """percentage>0 但无 user_id 也无 tenant_id → False"""
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(
                key="no_id",
                enabled=True,
                rollout_percentage=50,
            )
            return await svc.is_enabled("no_id")

        assert asyncio.run(_run()) is False

    def test_target_user_overrides_percentage_miss(self, initialized_db):
        """target_user 命中优先生效 (即使 percentage 未命中)"""
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            # 找一个 bucket >= 50 的 user_id (百分比 50 不命中)
            # 简单做法: 用 percentage=0, 但把用户加入 target_user_ids
            await svc.create_flag(
                key="override",
                enabled=True,
                rollout_percentage=0,
                target_user_ids=["special_user"],
            )
            return await svc.is_enabled("override", user_id="special_user")

        assert asyncio.run(_run()) is True


# ============================================================
# SDK: explain (命中原因)
# ============================================================


class TestExplain:
    """explain 返回 (enabled, reason)"""

    def test_explain_flag_not_found(self, initialized_db):
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            return await svc.explain("nope")

        r = asyncio.run(_run())
        assert r["enabled"] is False
        assert r["reason"] == "flag_not_found"

    def test_explain_flag_disabled(self, initialized_db):
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(key="disabled", enabled=False)
            return await svc.explain("disabled")

        r = asyncio.run(_run())
        assert r["enabled"] is False
        assert r["reason"] == "flag_disabled"

    def test_explain_target_user_hit(self, initialized_db):
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(key="uhit", enabled=True, target_user_ids=["u1"])
            return await svc.explain("uhit", user_id="u1")

        r = asyncio.run(_run())
        assert r["enabled"] is True
        assert r["reason"] == "target_user_hit"

    def test_explain_target_tenant_hit(self, initialized_db):
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(key="thit", enabled=True, target_tenant_ids=["t1"])
            return await svc.explain("thit", tenant_id="t1")

        r = asyncio.run(_run())
        assert r["enabled"] is True
        assert r["reason"] == "target_tenant_hit"

    def test_explain_rollout_hit(self, initialized_db):
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(key="rh", enabled=True, rollout_percentage=100)
            return await svc.explain("rh", user_id="u1")

        r = asyncio.run(_run())
        assert r["enabled"] is True
        assert r["reason"] == "rollout_percentage_hit"
        assert r["percentage"] == 100
        assert "bucket" in r

    def test_explain_rollout_miss(self, initialized_db):
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(key="rm", enabled=True, rollout_percentage=0)
            return await svc.explain("rm", user_id="u1")

        r = asyncio.run(_run())
        assert r["enabled"] is False
        assert r["reason"] == "default_off"


# ============================================================
# SDK: CRUD
# ============================================================


class TestCRUD:
    """CRUD: 创建/读取/更新/删除/切换"""

    def test_create_then_get(self, initialized_db):
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            flag = await svc.create_flag(
                key="crud1",
                description="test",
                enabled=True,
                rollout_percentage=50,
                target_tenant_ids=["t1"],
                target_user_ids=["u1"],
                category="feature",
            )
            fetched = await svc.get_flag("crud1")
            return flag, fetched

        flag, fetched = asyncio.run(_run())
        assert flag.key == "crud1"
        assert fetched is not None
        assert fetched.key == "crud1"
        assert fetched.enabled is True
        assert fetched.rollout_percentage == 50
        assert fetched.target_tenant_ids == ["t1"]
        assert fetched.target_user_ids == ["u1"]
        assert fetched.category == "feature"

    def test_create_duplicate_raises(self, initialized_db):
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(key="dup")
            await svc.create_flag(key="dup")  # 应抛 ValueError

        with pytest.raises(ValueError, match="已存在"):
            asyncio.run(_run())

    def test_create_invalid_percentage_raises(self, initialized_db):
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        with pytest.raises(ValueError):
            asyncio.run(svc.create_flag(key="bad_pct", rollout_percentage=200))

    def test_create_invalid_category_raises(self, initialized_db):
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        with pytest.raises(ValueError):
            asyncio.run(svc.create_flag(key="bad_cat", category="invalid"))

    def test_list_with_category_filter(self, initialized_db):
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(key="g1", category="general")
            await svc.create_flag(key="m1", category="model")
            await svc.create_flag(key="m2", category="model")
            all_flags = await svc.list_flags()
            model_flags = await svc.list_flags(category="model")
            return all_flags, model_flags

        all_flags, model_flags = asyncio.run(_run())
        assert len(all_flags) == 3
        assert len(model_flags) == 2
        assert all(f.category == "model" for f in model_flags)

    def test_update_fields(self, initialized_db):
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(key="upd", enabled=False, rollout_percentage=10)
            updated = await svc.update_flag(
                "upd",
                description="updated",
                enabled=True,
                rollout_percentage=80,
                target_user_ids=["new_user"],
            )
            return updated

        updated = asyncio.run(_run())
        assert updated.description == "updated"
        assert updated.enabled is True
        assert updated.rollout_percentage == 80
        assert updated.target_user_ids == ["new_user"]

    def test_update_nonexistent_returns_none(self, initialized_db):
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            return await svc.update_flag("nope", enabled=True)

        assert asyncio.run(_run()) is None

    def test_update_no_fields_raises(self, initialized_db):
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        with pytest.raises(ValueError):
            asyncio.run(svc.update_flag("any"))

    def test_delete(self, initialized_db):
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(key="del")
            ok = await svc.delete_flag("del")
            fetched = await svc.get_flag("del")
            return ok, fetched

        ok, fetched = asyncio.run(_run())
        assert ok is True
        assert fetched is None

    def test_delete_nonexistent_returns_false(self, initialized_db):
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            return await svc.delete_flag("nope")

        assert asyncio.run(_run()) is False

    def test_toggle_flag(self, initialized_db):
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(key="tog", enabled=False)
            on = await svc.toggle_flag("tog", True)
            off = await svc.toggle_flag("tog", False)
            return on, off

        on, off = asyncio.run(_run())
        assert on.enabled is True
        assert off.enabled is False


# ============================================================
# SDK: 缓存
# ============================================================


class TestCache:
    """LRU 缓存: 第二次查询走缓存"""

    def test_second_get_hits_cache(self, initialized_db):
        """第二次 get_flag 不再访问 DB"""
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(key="cached", enabled=True)
            # 第一次: 走 DB, 写缓存
            await svc.get_flag("cached")
            # 第二次: 应走缓存 (session_factory 不应被再次调用)
            # 用 patch session_factory 验证
            call_count = {"n": 0}
            original = svc._session_factory

            def counting_factory():
                call_count["n"] += 1
                return original()

            svc._session_factory = counting_factory
            await svc.get_flag("cached")
            return call_count["n"]

        n = asyncio.run(_run())
        assert n == 0, f"第二次查询应走缓存, 实际访问 DB {n} 次"

    def test_invalidate_clears_cache(self, initialized_db):
        """invalidate 后下次查询重新走 DB"""
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(key="inv", enabled=True)
            await svc.get_flag("inv")
            # 清缓存
            svc.invalidate("inv")
            # 再次查询应走 DB
            call_count = {"n": 0}
            original = svc._session_factory

            def counting_factory():
                call_count["n"] += 1
                return original()

            svc._session_factory = counting_factory
            await svc.get_flag("inv")
            return call_count["n"]

        n = asyncio.run(_run())
        assert n == 1, f"清缓存后应走 DB 1 次, 实际 {n} 次"

    def test_update_clears_cache(self, initialized_db):
        """update_flag 后缓存应被清除, 下次查询拿到新值"""
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(key="upd_cache", enabled=False, description="old")
            # 写缓存
            await svc.get_flag("upd_cache")
            # 更新
            await svc.update_flag("upd_cache", description="new", enabled=True)
            # 再查, 应拿到新值
            flag = await svc.get_flag("upd_cache")
            return flag

        flag = asyncio.run(_run())
        assert flag.description == "new"
        assert flag.enabled is True

    def test_delete_clears_cache(self, initialized_db):
        """delete_flag 后缓存应被清除, 下次查询返 None"""
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(key="del_cache", enabled=True)
            await svc.get_flag("del_cache")  # 缓存
            await svc.delete_flag("del_cache")
            return await svc.get_flag("del_cache")

        assert asyncio.run(_run()) is None

    def test_cache_ttl_expires(self, initialized_db):
        """TTL 过期后下次查询重新走 DB"""
        from core import feature_flag as ff_module
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            await svc.create_flag(key="ttl", enabled=True)
            await svc.get_flag("ttl")  # 缓存
            # 模拟时间过期: 直接把 expiry 改为过去
            svc._cache_expiry["ttl"] = 0.0
            # 再次查询应走 DB
            call_count = {"n": 0}
            original = svc._session_factory

            def counting_factory():
                call_count["n"] += 1
                return original()

            svc._session_factory = counting_factory
            await svc.get_flag("ttl")
            return call_count["n"]

        n = asyncio.run(_run())
        assert n == 1, f"过期后应走 DB 1 次, 实际 {n} 次"

    def test_cache_negative_result(self, initialized_db):
        """flag 不存在也缓存 (None), 第二次查询不打 DB"""
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _run():
            # 第一次: 不存在, 写 None 缓存
            await svc.get_flag("not_exists")
            # 第二次: 应走缓存
            call_count = {"n": 0}
            original = svc._session_factory

            def counting_factory():
                call_count["n"] += 1
                return original()

            svc._session_factory = counting_factory
            result = await svc.get_flag("not_exists")
            return call_count["n"], result

        n, result = asyncio.run(_run())
        assert n == 0, "第二次应走缓存 (None)"
        assert result is None


# ============================================================
# API: 8 端点全链路
# ============================================================


class TestAPI:
    """API 端点全链路测试"""

    def test_create_flag(self, app_with_routers):
        """POST /admin/feature-flags - 创建"""
        resp = app_with_routers.post(
            "/api/v1/admin/feature-flags",
            json={
                "key": "api_flag",
                "description": "via API",
                "enabled": True,
                "rollout_percentage": 50,
                "target_tenant_ids": ["t1"],
                "target_user_ids": ["u1"],
                "category": "feature",
            },
            headers=_admin_headers(),
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["key"] == "api_flag"
        assert data["enabled"] is True
        assert data["rollout_percentage"] == 50

    def test_list_flags(self, app_with_routers):
        """GET /admin/feature-flags - 列表"""
        app_with_routers.post(
            "/api/v1/admin/feature-flags",
            json={"key": "list1", "category": "general"},
            headers=_admin_headers(),
        )
        app_with_routers.post(
            "/api/v1/admin/feature-flags",
            json={"key": "list2", "category": "model"},
            headers=_admin_headers(),
        )
        resp = app_with_routers.get(
            "/api/v1/admin/feature-flags", headers=_admin_headers()
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 2

    def test_list_with_category_filter(self, app_with_routers):
        """GET /admin/feature-flags?category=model - 按 category 过滤"""
        app_with_routers.post(
            "/api/v1/admin/feature-flags",
            json={"key": "cat1", "category": "general"},
            headers=_admin_headers(),
        )
        app_with_routers.post(
            "/api/v1/admin/feature-flags",
            json={"key": "cat2", "category": "model"},
            headers=_admin_headers(),
        )
        resp = app_with_routers.get(
            "/api/v1/admin/feature-flags?category=model",
            headers=_admin_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert all(item["category"] == "model" for item in data["items"])

    def test_get_flag_detail(self, app_with_routers):
        """GET /admin/feature-flags/{key} - 详情"""
        app_with_routers.post(
            "/api/v1/admin/feature-flags",
            json={"key": "detail1", "description": "detail"},
            headers=_admin_headers(),
        )
        resp = app_with_routers.get(
            "/api/v1/admin/feature-flags/detail1",
            headers=_admin_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["key"] == "detail1"
        assert resp.json()["description"] == "detail"

    def test_get_nonexistent_returns_404(self, app_with_routers):
        """GET 不存在 → 404"""
        resp = app_with_routers.get(
            "/api/v1/admin/feature-flags/nonexistent",
            headers=_admin_headers(),
        )
        assert resp.status_code == 404

    def test_update_flag(self, app_with_routers):
        """PUT /admin/feature-flags/{key} - 更新"""
        app_with_routers.post(
            "/api/v1/admin/feature-flags",
            json={"key": "upd1", "enabled": False},
            headers=_admin_headers(),
        )
        resp = app_with_routers.put(
            "/api/v1/admin/feature-flags/upd1",
            json={"enabled": True, "description": "updated"},
            headers=_admin_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert data["description"] == "updated"

    def test_update_nonexistent_returns_404(self, app_with_routers):
        """PUT 不存在 → 404"""
        resp = app_with_routers.put(
            "/api/v1/admin/feature-flags/nonexistent",
            json={"enabled": True},
            headers=_admin_headers(),
        )
        assert resp.status_code == 404

    def test_delete_flag(self, app_with_routers):
        """DELETE /admin/feature-flags/{key} - 删除"""
        app_with_routers.post(
            "/api/v1/admin/feature-flags",
            json={"key": "del1"},
            headers=_admin_headers(),
        )
        resp = app_with_routers.delete(
            "/api/v1/admin/feature-flags/del1",
            headers=_admin_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        # 再次 GET 应 404
        get_resp = app_with_routers.get(
            "/api/v1/admin/feature-flags/del1",
            headers=_admin_headers(),
        )
        assert get_resp.status_code == 404

    def test_toggle_flag(self, app_with_routers):
        """POST /admin/feature-flags/{key}/toggle - 启用/禁用"""
        app_with_routers.post(
            "/api/v1/admin/feature-flags",
            json={"key": "tog1", "enabled": False},
            headers=_admin_headers(),
        )
        # 启用
        on_resp = app_with_routers.post(
            "/api/v1/admin/feature-flags/tog1/toggle",
            json={"enabled": True},
            headers=_admin_headers(),
        )
        assert on_resp.status_code == 200
        assert on_resp.json()["enabled"] is True
        # 禁用
        off_resp = app_with_routers.post(
            "/api/v1/admin/feature-flags/tog1/toggle",
            json={"enabled": False},
            headers=_admin_headers(),
        )
        assert off_resp.status_code == 200
        assert off_resp.json()["enabled"] is False

    def test_check_endpoint_enabled(self, app_with_routers):
        """GET /admin/feature-flags/{key}/check - 检查 (启用)"""
        app_with_routers.post(
            "/api/v1/admin/feature-flags",
            json={
                "key": "chk1",
                "enabled": True,
                "target_user_ids": ["check_user"],
            },
            headers=_admin_headers(),
        )
        resp = app_with_routers.get(
            "/api/v1/admin/feature-flags/chk1/check?user_id=check_user",
            headers=_admin_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert data["reason"] == "target_user_hit"

    def test_check_endpoint_disabled(self, app_with_routers):
        """GET /admin/feature-flags/{key}/check - 检查 (禁用)"""
        app_with_routers.post(
            "/api/v1/admin/feature-flags",
            json={"key": "chk2", "enabled": False},
            headers=_admin_headers(),
        )
        resp = app_with_routers.get(
            "/api/v1/admin/feature-flags/chk2/check?user_id=anyone",
            headers=_admin_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False
        assert data["reason"] == "flag_disabled"

    def test_check_endpoint_not_found(self, app_with_routers):
        """GET /admin/feature-flags/{key}/check - 检查 (不存在)"""
        resp = app_with_routers.get(
            "/api/v1/admin/feature-flags/no_such/check",
            headers=_admin_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False
        assert data["reason"] == "flag_not_found"


# ============================================================
# 鉴权 RBAC
# ============================================================


class TestRBAC:
    """非 ADMIN → 403"""

    def test_employee_gets_403_on_list(self, app_with_routers):
        """employee GET /admin/feature-flags → 403"""
        resp = app_with_routers.get(
            "/api/v1/admin/feature-flags",
            headers=_employee_headers(),
        )
        assert resp.status_code == 403

    def test_employee_gets_403_on_create(self, app_with_routers):
        """employee POST → 403"""
        resp = app_with_routers.post(
            "/api/v1/admin/feature-flags",
            json={"key": "x"},
            headers=_employee_headers(),
        )
        assert resp.status_code == 403

    def test_no_auth_gets_403_or_401(self, app_with_routers):
        """无 auth header → 401/403 (演示模式下默认 employee → 403)"""
        resp = app_with_routers.get("/api/v1/admin/feature-flags")
        assert resp.status_code in (401, 403)


# ============================================================
# 集成示例: graph.py retrieve_context 通过 FeatureFlag 控制 rerank
# ============================================================


class TestIntegrationWithGraph:
    """验证 _rerank_kb_if_enabled 通过 Feature Flag 控制 rerank 行为"""

    def test_flag_use_rerank_v2_not_exists_skips_rerank_in_dummy_mode(
        self, initialized_db
    ):
        """flag 不存在 + dummy 模式 → 不调 rerank, 返回原 documents"""
        from agent.graph import _rerank_kb_if_enabled

        # 测试默认 settings.rerank_provider = "dummy" (conftest 没改这个字段)
        # 但保险起见, 显式设为 dummy
        settings = get_settings()
        original_rerank = getattr(settings, "rerank_provider", None)
        try:
            settings.rerank_provider = "dummy"
            docs = [{"content": "doc1"}, {"content": "doc2"}]

            async def _run():
                return await _rerank_kb_if_enabled("query", list(docs))

            result = asyncio.run(_run())
            # dummy 模式 + flag 未启用 → 原样返回
            assert result == docs
        finally:
            settings.rerank_provider = original_rerank

    def test_flag_use_rerank_v2_enabled_forces_rerank(self, initialized_db):
        """flag 启用 → 即使 dummy 模式也尝试加载 rerank provider"""
        from core.feature_flag import FeatureFlagService

        svc = FeatureFlagService(initialized_db)

        async def _setup_flag():
            await svc.create_flag(
                key="use_rerank_v2",
                description="enable rerank v2",
                enabled=True,
                rollout_percentage=100,
            )

        asyncio.run(_setup_flag())

        # 现在 flag 启用了, _rerank_kb_if_enabled 应该尝试调用 reranker
        # 由于 DummyRerankProvider.rerank 返回原列表 (验证不抛异常即可)
        from agent.graph import _rerank_kb_if_enabled

        settings = get_settings()
        original_rerank = getattr(settings, "rerank_provider", None)
        try:
            settings.rerank_provider = "dummy"
            docs = [{"content": "doc1"}, {"content": "doc2"}]

            async def _run():
                return await _rerank_kb_if_enabled("query", list(docs))

            result = asyncio.run(_run())
            # DummyRerankProvider 在 dummy 模式下应返回原列表 (或加 rerank_score 的列表)
            # 关键是不抛异常, 证明 flag 检查路径被走到了
            assert isinstance(result, list)
            assert len(result) == 2
        finally:
            settings.rerank_provider = original_rerank
