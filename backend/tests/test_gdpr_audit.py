"""
GDPR / 个保法合规审计脚本测试（Phase 9.3）

覆盖 export_employee_data / delete_employee_data / query_employee_data /
generate_compliance_report，使用独立临时 SQLite 异步数据库。
"""

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.database import Base
from models import AuditLog, Evaluation, Feedback, Memory, RawInput, User
from scripts.gdpr_audit import (
    delete_employee_data,
    export_employee_data,
    generate_compliance_report,
    query_employee_data,
)


def _settings(raw=730, evaluation=1825, buffer=30):
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


async def _seed_employee(s: AsyncSession, employee_id: str = "E1001") -> None:
    """为一个员工写入各类数据：用户/原始输入/评估/反馈/记忆/审计日志。"""
    s.add(
        User(
            user_id=employee_id,
            name="张三",
            email=f"{employee_id}@example.com",
            role="employee",
            department="Engineering",
        )
    )
    s.add(
        RawInput(
            input_id=f"in-{employee_id}",
            employee_id=employee_id,
            period="2026-W25",
            type="daily_report",
            content="完成了登录模块",
            attachments=[],
        )
    )
    s.add(
        Evaluation(
            evaluation_id=f"ev-{employee_id}",
            employee_id=employee_id,
            period="2026-W25",
            overall_score=85.0,
            employee_view={"summary": "表现稳定"},
            manager_view={"harsh_assessment": "稳定"},
            audit={"model_tier": "L2"},
            status="approved",
        )
    )
    s.add(
        Feedback(
            feedback_id=f"fb-{employee_id}",
            evaluation_id=f"ev-{employee_id}",
            employee_id=employee_id,
            type="feedback",
            content="希望能更多挑战性任务",
        )
    )
    s.add(
        Memory(
            employee_id=employee_id,
            period="2026-W25",
            content="本周表现稳定",
            payload={"period": "2026-W25"},
        )
    )
    s.add(
        AuditLog(
            log_id=f"log-{employee_id}",
            actor_id=employee_id,
            action="view_manager_view",
            employee_id=employee_id,
            evaluation_id=f"ev-{employee_id}",
            details={"note": "查看"},
        )
    )
    await s.flush()


# ---------------- export ----------------


class TestExportEmployeeData:
    async def test_export_contains_all_record_types(self, session):
        """导出应包含用户/原始输入/评估/反馈/记忆/审计日志全部数据。"""
        await _seed_employee(session)
        data = await export_employee_data("E1001", session)

        assert data["employee_id"] == "E1001"
        assert data["exported_at"]
        assert data["user"]["user_id"] == "E1001"
        assert data["user"]["name"] == "张三"
        assert len(data["raw_inputs"]) == 1
        assert data["raw_inputs"][0]["input_id"] == "in-E1001"
        assert len(data["evaluations"]) == 1
        assert data["evaluations"][0]["evaluation_id"] == "ev-E1001"
        assert data["evaluations"][0]["overall_score"] == 85.0
        assert len(data["feedback"]) == 1
        assert len(data["memories"]) == 1
        assert len(data["audit_logs"]) == 1

    async def test_export_excludes_archived(self, session):
        """已软删除（归档）的数据不应导出。"""
        await _seed_employee(session)
        # 手动将评估标记为归档（模拟软删除）
        ev = (
            await session.execute(
                select(Evaluation).where(Evaluation.evaluation_id == "ev-E1001")
            )
        ).scalar_one()
        ev.archived = True
        ev.archived_at = datetime.now(timezone.utc)
        await session.flush()

        data = await export_employee_data("E1001", session)
        assert data["evaluations"] == []
        # 原始输入未归档，仍应导出
        assert len(data["raw_inputs"]) == 1

    async def test_export_nonexistent_employee_returns_empty(self, session):
        """导出不存在的员工应返回空数据集合（user=None）。"""
        data = await export_employee_data("NOPE", session)
        assert data["user"] is None
        assert data["raw_inputs"] == []
        assert data["evaluations"] == []


# ---------------- delete ----------------


class TestDeleteEmployeeData:
    async def test_delete_marks_records_archived(self, session):
        """软删除应将原始输入与评估标记为 archived 并写入 archived_at。"""
        await _seed_employee(session)
        result = await delete_employee_data("E1001", session, settings=_settings())

        assert result["employee_id"] == "E1001"
        assert result["raw_inputs_marked"] == 1
        assert result["evaluations_marked"] == 1
        assert result["buffer_days"] == 30

        raw = (
            await session.execute(
                select(RawInput).where(RawInput.input_id == "in-E1001")
            )
        ).scalar_one()
        assert raw.archived is True
        assert raw.archived_at is not None

        ev = (
            await session.execute(
                select(Evaluation).where(Evaluation.evaluation_id == "ev-E1001")
            )
        ).scalar_one()
        assert ev.archived is True
        assert ev.archived_at is not None

    async def test_delete_writes_audit_log(self, session):
        """软删除应写入 gdpr_deletion_requested 审计日志，便于追溯。"""
        await _seed_employee(session)
        await delete_employee_data("E1001", session, settings=_settings())
        await session.flush()

        logs = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == "gdpr_deletion_requested")
                )
            )
            .scalars()
            .all()
        )
        assert len(logs) == 1
        log = logs[0]
        assert log.employee_id == "E1001"
        assert log.details["raw_inputs_marked"] == 1
        assert log.details["evaluations_marked"] == 1
        assert log.details["buffer_days"] == 30

    async def test_delete_idempotent_second_call_no_marks(self, session):
        """第二次调用：已全部归档，无新增标记，但仍写审计日志。"""
        await _seed_employee(session)
        await delete_employee_data("E1001", session, settings=_settings())
        await session.flush()

        result = await delete_employee_data("E1001", session, settings=_settings())
        assert result["raw_inputs_marked"] == 0
        assert result["evaluations_marked"] == 0


# ---------------- query ----------------


class TestQueryEmployeeData:
    async def test_query_returns_summary_counts(self, session):
        """查询应返回各类数据计数摘要与最近评估。"""
        await _seed_employee(session)
        data = await query_employee_data("E1001", session)

        assert data["employee_id"] == "E1001"
        assert data["user"]["name"] == "张三"
        summary = data["summary"]
        assert summary["raw_inputs"] == 1
        assert summary["evaluations"] == 1
        assert summary["feedback"] == 1
        assert summary["memories"] == 1
        assert summary["audit_logs"] == 1
        assert data["latest_evaluation"]["evaluation_id"] == "ev-E1001"
        assert data["latest_evaluation"]["overall_score"] == 85.0

    async def test_query_excludes_archived_from_counts(self, session):
        """查询摘要不计入已软删除（归档）的数据。"""
        await _seed_employee(session)
        ev = (
            await session.execute(
                select(Evaluation).where(Evaluation.evaluation_id == "ev-E1001")
            )
        ).scalar_one()
        ev.archived = True
        ev.archived_at = datetime.now(timezone.utc)
        await session.flush()

        data = await query_employee_data("E1001", session)
        assert data["summary"]["evaluations"] == 0
        assert data["latest_evaluation"] is None
        # 原始输入未归档仍计数
        assert data["summary"]["raw_inputs"] == 1

    async def test_query_nonexistent_returns_zeros(self, session):
        """查询不存在员工应返回空摘要。"""
        data = await query_employee_data("NOPE", session)
        assert data["user"] is None
        assert data["summary"]["raw_inputs"] == 0
        assert data["latest_evaluation"] is None


# ---------------- generate_compliance_report ----------------


class TestGenerateComplianceReport:
    async def test_report_generates_markdown_file(self, session, tmp_path):
        """报告应生成 markdown 文件并返回内容。"""
        await _seed_employee(session)
        out = tmp_path / "compliance-report.md"
        content = await generate_compliance_report(
            session, output_path=out, settings=_settings()
        )

        assert out.exists()
        assert content == out.read_text(encoding="utf-8")
        assert "AgentValue-AI 合规审计报告" in content
        assert "数据分类清单" in content
        assert "raw_inputs" in content
        assert "evaluations" in content

    async def test_report_includes_data_classification(self, session, tmp_path):
        """报告应包含数据分类清单与各类数据总量。"""
        await _seed_employee(session)
        out = tmp_path / "report.md"
        content = await generate_compliance_report(
            session, output_path=out, settings=_settings()
        )
        # 单条原始输入与评估应反映在分类清单中
        assert "| 员工原始输入 | raw_inputs | 1 |" in content
        assert "| 评估结果 | evaluations | 1 |" in content

    async def test_report_includes_audit_log_stats(self, session, tmp_path):
        """报告应包含访问日志统计。"""
        await _seed_employee(session)
        out = tmp_path / "report.md"
        content = await generate_compliance_report(
            session, output_path=out, settings=_settings()
        )
        assert "访问日志统计" in content
        # 种子数据写入了一条 view_manager_view 日志
        assert "view_manager_view" in content

    async def test_report_records_gdpr_deletion_requests(self, session, tmp_path):
        """报告应记录数据主体请求（GDPR 删除日志）。"""
        await _seed_employee(session)
        await delete_employee_data("E1001", session, settings=_settings())
        await session.flush()

        out = tmp_path / "report.md"
        content = await generate_compliance_report(
            session, output_path=out, settings=_settings()
        )
        assert "数据主体请求记录" in content
        assert "gdpr_deletion_requested" in content

    async def test_report_empty_db_no_crash(self, session, tmp_path):
        """空数据库生成报告不应崩溃。"""
        out = tmp_path / "empty.md"
        content = await generate_compliance_report(
            session, output_path=out, settings=_settings()
        )
        assert "AgentValue-AI 合规审计报告" in content
        assert "暂无审计日志记录" in content or "审计日志总量：**0**" in content
