"""
数据留存策略自动化测试（Phase 9.3）

覆盖 RetentionPolicy 的 scan_expired / archive / purge / run_retention_job，
使用独立临时 SQLite 异步数据库，不依赖真实库。
"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.database import Base
from models import Evaluation, RawInput, User  # 触发模型注册
from scripts.data_retention import RetentionPolicy


def _settings(raw=730, evaluation=1825, buffer=30):
    """构造可调留存周期配置，便于用小周期验证逻辑。"""
    return SimpleNamespace(
        retention_raw_input_days=raw,
        retention_evaluation_days=evaluation,
        retention_archive_buffer_days=buffer,
    )


@pytest.fixture
async def session():
    """每个测试使用独立临时 SQLite 异步数据库。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_url = f"sqlite+aiosqlite:///{tmp.name}"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    async with SessionLocal() as s:
        yield s
    await engine.dispose()
    Path(tmp.name).unlink(missing_ok=True)


def _old_dt(days_ago: int) -> datetime:
    """构造 days_ago 天前的 UTC 时间。"""
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


async def _add_user(s: AsyncSession, user_id: str = "E1001") -> None:
    """插入用户以满足外键约束。"""
    s.add(User(user_id=user_id, name=user_id, role="employee"))
    await s.flush()


async def _add_raw(
    s: AsyncSession, input_id: str, days_ago: int, employee_id: str = "E1001"
) -> RawInput:
    raw = RawInput(
        input_id=input_id,
        employee_id=employee_id,
        period="2024-W01",
        type="daily_report",
        content="历史日报",
        attachments=[],
        created_at=_old_dt(days_ago),
    )
    s.add(raw)
    await s.flush()
    return raw


async def _add_eval(
    s: AsyncSession, evaluation_id: str, days_ago: int, employee_id: str = "E1001"
) -> Evaluation:
    ev = Evaluation(
        evaluation_id=evaluation_id,
        employee_id=employee_id,
        period="2024-W01",
        overall_score=80.0,
        employee_view={"summary": "ok"},
        manager_view={"harsh_assessment": "ok"},
        audit={"model_tier": "L2"},
        status="approved",
        created_at=_old_dt(days_ago),
    )
    s.add(ev)
    await s.flush()
    return ev


# ---------------- scan_expired ----------------


class TestScanExpired:
    async def test_unexpired_not_returned(self, session):
        """未过期记录不应返回。"""
        await _add_user(session)
        await _add_raw(session, "in-fresh", days_ago=10)
        await _add_eval(session, "ev-fresh", days_ago=100)
        policy = RetentionPolicy(session, settings=_settings())
        records = await policy.scan_expired()
        assert records == []

    async def test_expired_returned(self, session):
        """超过留存期的记录应返回。"""
        await _add_user(session)
        await _add_raw(session, "in-old", days_ago=800)  # 800 > 730
        await _add_eval(session, "ev-old", days_ago=2000)  # 2000 > 1825
        policy = RetentionPolicy(session, settings=_settings())
        records = await policy.scan_expired()
        types = {r.type for r in records}
        assert types == {"raw_input", "evaluation"}
        biz_ids = {r.business_id for r in records}
        assert "in-old" in biz_ids
        assert "ev-old" in biz_ids

    async def test_different_period_per_type(self, session):
        """原始输入 730 天、评估 1825 天，边界值分别判定。"""
        await _add_user(session)
        # raw: 725 天未过期，735 天过期
        await _add_raw(session, "in-ok", days_ago=725)
        await _add_raw(session, "in-expired", days_ago=735)
        # eval: 1800 天未过期，1830 天过期
        await _add_eval(session, "ev-ok", days_ago=1800)
        await _add_eval(session, "ev-expired", days_ago=1830)

        policy = RetentionPolicy(session, settings=_settings())
        records = await policy.scan_expired()
        biz_ids = {r.business_id for r in records}
        assert "in-ok" not in biz_ids
        assert "in-expired" in biz_ids
        assert "ev-ok" not in biz_ids
        assert "ev-expired" in biz_ids

    async def test_archived_excluded_from_scan(self, session):
        """已归档记录不应再次进入待处理列表。"""
        await _add_user(session)
        raw = await _add_raw(session, "in-archived", days_ago=800)
        raw.archived = True
        raw.archived_at = _old_dt(5)
        await session.flush()

        policy = RetentionPolicy(session, settings=_settings())
        records = await policy.scan_expired()
        assert all(r.business_id != "in-archived" for r in records)

    async def test_expired_record_carries_metadata(self, session):
        """过期记录应携带类型、ID、过期日期等元信息。"""
        await _add_user(session)
        await _add_raw(session, "in-meta", days_ago=800)
        policy = RetentionPolicy(session, settings=_settings())
        records = await policy.scan_expired()
        assert len(records) == 1
        r = records[0]
        assert r.type == "raw_input"
        assert r.id is not None
        assert r.business_id == "in-meta"
        assert r.employee_id == "E1001"
        assert r.created_at is not None
        assert r.expired_at is not None
        d = r.to_dict()
        assert d["type"] == "raw_input"
        assert d["created_at"] and d["expired_at"]


# ---------------- archive ----------------


class TestArchive:
    async def test_archive_marks_archived_and_archived_at(self, session):
        """归档应将 archived 置 True 并写入 archived_at。"""
        await _add_user(session)
        await _add_raw(session, "in-1", days_ago=800)
        await _add_eval(session, "ev-1", days_ago=2000)
        policy = RetentionPolicy(session, settings=_settings())
        records = await policy.scan_expired()

        count = await policy.archive(records)
        assert count == 2

        raw = (
            await session.execute(select(RawInput).where(RawInput.input_id == "in-1"))
        ).scalar_one()
        assert raw.archived is True
        assert raw.archived_at is not None

        ev = (
            await session.execute(
                select(Evaluation).where(Evaluation.evaluation_id == "ev-1")
            )
        ).scalar_one()
        assert ev.archived is True
        assert ev.archived_at is not None

    async def test_archive_empty_list_noop(self, session):
        """空列表归档应返回 0 且不报错。"""
        policy = RetentionPolicy(session, settings=_settings())
        count = await policy.archive([])
        assert count == 0


# ---------------- purge ----------------


class TestPurge:
    async def test_purge_only_deletes_archived_beyond_buffer(self, session):
        """仅删除归档超过缓冲期（30 天）的记录。"""
        await _add_user(session)

        # 归档 31 天前 → 应被删
        old_raw = await _add_raw(session, "in-purge", days_ago=800)
        old_raw.archived = True
        old_raw.archived_at = _old_dt(31)

        # 归档 10 天前 → 不应被删（仍在缓冲期）
        recent_raw = await _add_raw(session, "in-buffer", days_ago=800)
        recent_raw.archived = True
        recent_raw.archived_at = _old_dt(10)

        # 未归档 → 不应被删
        await _add_raw(session, "in-active", days_ago=10)
        await session.flush()

        policy = RetentionPolicy(session, settings=_settings(buffer=30))
        purged = await policy.purge()
        assert purged["raw_input"] == 1

        ids = {
            r.input_id
            for r in (await session.execute(select(RawInput))).scalars().all()
        }
        assert "in-purge" not in ids
        assert "in-buffer" in ids
        assert "in-active" in ids

    async def test_purge_evaluations_beyond_buffer(self, session):
        """评估归档超过缓冲期也应被删。"""
        await _add_user(session)
        old_ev = await _add_eval(session, "ev-purge", days_ago=2000)
        old_ev.archived = True
        old_ev.archived_at = _old_dt(40)
        await session.flush()

        policy = RetentionPolicy(session, settings=_settings(buffer=30))
        purged = await policy.purge()
        assert purged["evaluation"] == 1

        ids = {
            e.evaluation_id
            for e in (await session.execute(select(Evaluation))).scalars().all()
        }
        assert "ev-purge" not in ids

    async def test_purge_keeps_recently_archived(self, session):
        """归档未超缓冲期的记录保留。"""
        await _add_user(session)
        ev = await _add_eval(session, "ev-keep", days_ago=2000)
        ev.archived = True
        ev.archived_at = _old_dt(5)
        await session.flush()

        policy = RetentionPolicy(session, settings=_settings(buffer=30))
        purged = await policy.purge()
        assert purged["evaluation"] == 0

        ids = {
            e.evaluation_id
            for e in (await session.execute(select(Evaluation))).scalars().all()
        }
        assert "ev-keep" in ids


# ---------------- run_retention_job ----------------


class TestRunRetentionJob:
    async def test_full_flow_archives_expired(self, session):
        """完整流程：过期未归档记录被归档（缓冲期内不真删）。"""
        await _add_user(session)
        await _add_raw(session, "in-flow", days_ago=800)
        await _add_eval(session, "ev-flow", days_ago=2000)

        policy = RetentionPolicy(session, settings=_settings())
        summary = await policy.run_retention_job()

        assert summary["scanned_expired"] == 2
        assert summary["archived"] == 2
        # 刚归档，未超缓冲期，不应被真删
        assert summary["purged"]["raw_input"] == 0
        assert summary["purged"]["evaluation"] == 0

        raw = (
            await session.execute(
                select(RawInput).where(RawInput.input_id == "in-flow")
            )
        ).scalar_one()
        assert raw.archived is True

    async def test_full_flow_purges_old_archived(self, session):
        """完整流程：已归档超过缓冲期的记录被真删。"""
        await _add_user(session)
        # 预置一条已归档 40 天的评估，应被真删
        old_ev = await _add_eval(session, "ev-stale", days_ago=2000)
        old_ev.archived = True
        old_ev.archived_at = _old_dt(40)
        await session.flush()

        policy = RetentionPolicy(session, settings=_settings(buffer=30))
        summary = await policy.run_retention_job()

        # 该记录已归档，不在 scan_expired（scan 只看未归档），故 scanned=0
        assert summary["scanned_expired"] == 0
        assert summary["purged"]["evaluation"] == 1

        ids = {
            e.evaluation_id
            for e in (await session.execute(select(Evaluation))).scalars().all()
        }
        assert "ev-stale" not in ids

    async def test_run_at_present_in_summary(self, session):
        """执行摘要应包含 run_at 时间。"""
        policy = RetentionPolicy(session, settings=_settings())
        summary = await policy.run_retention_job()
        assert summary["run_at"]
        assert isinstance(summary["records"], list)
