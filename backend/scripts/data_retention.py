#!/usr/bin/env python3
"""
AgentValue-AI 数据留存策略自动化（Phase 9.3）

按 GDPR / 个保法要求自动归档与清理过期数据：
- 原始输入（RawInput）保留 2 年（730 天）
- 评估结果（Evaluation）保留 5 年（1825 天）
- 到期先归档（archived=True + archived_at），缓冲 30 天后再真删，
  避免误删与申诉期数据缺失。

CLI：
    python -m scripts.data_retention --dry-run   # 仅扫描，不执行
    python -m scripts.data_retention --execute   # 执行归档 + 清理
"""

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.database import AsyncSessionLocal
from models import Evaluation, RawInput

logger = logging.getLogger(__name__)


@dataclass
class ExpiredRecord:
    """单条过期记录描述，供归档与审计追溯使用。"""

    type: str  # raw_input / evaluation
    id: int  # 主键
    business_id: str  # input_id / evaluation_id，便于人读
    employee_id: str
    created_at: datetime
    expired_at: datetime  # created_at + 留存天数
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "id": self.id,
            "business_id": self.business_id,
            "employee_id": self.employee_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expired_at": self.expired_at.isoformat() if self.expired_at else None,
            "extra": self.extra,
        }


class RetentionPolicy:
    """数据留存策略执行器。所有写操作不 commit，由 run_retention_job 统一提交。"""

    def __init__(self, session: AsyncSession, settings=None):
        self.session = session
        self.settings = settings or get_settings()

    # ---- 周期配置 ----
    @property
    def raw_input_days(self) -> int:
        return self.settings.retention_raw_input_days

    @property
    def evaluation_days(self) -> int:
        return self.settings.retention_evaluation_days

    @property
    def archive_buffer_days(self) -> int:
        return self.settings.retention_archive_buffer_days

    # ---- 扫描 ----
    async def scan_expired(self, now: Optional[datetime] = None) -> List[ExpiredRecord]:
        """扫描未归档但已超过留存期的记录，返回待处理列表。"""
        now = now or datetime.now(timezone.utc)
        records: List[ExpiredRecord] = []

        # 原始输入：created_at + raw_input_days < now 且未归档
        raw_threshold = now - timedelta(days=self.raw_input_days)
        raw_rows = (
            (
                await self.session.execute(
                    select(RawInput)
                    .where(
                        RawInput.archived.is_(False),
                        RawInput.created_at < raw_threshold,
                    )
                    .order_by(RawInput.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        for r in raw_rows:
            records.append(
                ExpiredRecord(
                    type="raw_input",
                    id=r.id,
                    business_id=r.input_id,
                    employee_id=r.employee_id,
                    created_at=r.created_at,
                    expired_at=r.created_at + timedelta(days=self.raw_input_days),
                    extra={"period": r.period, "type": r.type},
                )
            )

        # 评估：created_at + evaluation_days < now 且未归档
        eval_threshold = now - timedelta(days=self.evaluation_days)
        eval_rows = (
            (
                await self.session.execute(
                    select(Evaluation)
                    .where(
                        Evaluation.archived.is_(False),
                        Evaluation.created_at < eval_threshold,
                    )
                    .order_by(Evaluation.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        for e in eval_rows:
            records.append(
                ExpiredRecord(
                    type="evaluation",
                    id=e.id,
                    business_id=e.evaluation_id,
                    employee_id=e.employee_id,
                    created_at=e.created_at,
                    expired_at=e.created_at + timedelta(days=self.evaluation_days),
                    extra={"period": e.period, "status": e.status},
                )
            )

        return records

    # ---- 归档 ----
    async def archive(self, records: List[ExpiredRecord]) -> int:
        """将待处理记录标记为归档（archived=True + archived_at=now），返回处理条数。"""
        if not records:
            return 0
        now = datetime.now(timezone.utc)
        raw_ids = [r.id for r in records if r.type == "raw_input"]
        eval_ids = [r.id for r in records if r.type == "evaluation"]

        if raw_ids:
            raw_rows = (
                (
                    await self.session.execute(
                        select(RawInput).where(RawInput.id.in_(raw_ids))
                    )
                )
                .scalars()
                .all()
            )
            for r in raw_rows:
                r.archived = True
                r.archived_at = now

        if eval_ids:
            eval_rows = (
                (
                    await self.session.execute(
                        select(Evaluation).where(Evaluation.id.in_(eval_ids))
                    )
                )
                .scalars()
                .all()
            )
            for e in eval_rows:
                e.archived = True
                e.archived_at = now

        return len(raw_ids) + len(eval_ids)

    # ---- 清理（真删）----
    async def purge(self, now: Optional[datetime] = None) -> Dict[str, int]:
        """真删除已归档超过缓冲期的记录，返回各类删除条数。

        仅删 archived=True 且 archived_at + archive_buffer_days < now 的记录，
        给申诉与误删留出缓冲窗口。
        """
        now = now or datetime.now(timezone.utc)
        purge_threshold = now - timedelta(days=self.archive_buffer_days)

        raw_result = await self.session.execute(
            delete(RawInput).where(
                RawInput.archived.is_(True),
                RawInput.archived_at.is_not(None),
                RawInput.archived_at < purge_threshold,
            )
        )
        eval_result = await self.session.execute(
            delete(Evaluation).where(
                Evaluation.archived.is_(True),
                Evaluation.archived_at.is_not(None),
                Evaluation.archived_at < purge_threshold,
            )
        )

        return {
            "raw_input": raw_result.rowcount or 0,
            "evaluation": eval_result.rowcount or 0,
        }

    # ---- 完整流程 ----
    async def run_retention_job(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """完整执行：扫描 → 归档 → 清理，返回执行摘要。"""
        now = now or datetime.now(timezone.utc)
        expired = await self.scan_expired(now=now)
        archived_count = await self.archive(expired)
        purged = await self.purge(now=now)
        return {
            "scanned_expired": len(expired),
            "archived": archived_count,
            "purged": purged,
            "records": [r.to_dict() for r in expired],
            "run_at": now.isoformat(),
        }


async def _run(dry_run: bool) -> int:
    """CLI 内部执行：dry_run 只扫描输出，execute 执行完整流程并提交。"""
    async with AsyncSessionLocal() as session:
        policy = RetentionPolicy(session)
        if dry_run:
            expired = await policy.scan_expired()
            print(f"[dry-run] 发现 {len(expired)} 条过期待归档记录：")
            for r in expired:
                print(
                    f"  - [{r.type}] {r.business_id} "
                    f"employee={r.employee_id} "
                    f"created={r.created_at.date()} "
                    f"expired={r.expired_at.date()}"
                )
            return 0
        summary = await policy.run_retention_job()
        await session.commit()
        print(
            f"[execute] 扫描过期 {summary['scanned_expired']} 条，"
            f"归档 {summary['archived']} 条，"
            f"清理 raw_input={summary['purged']['raw_input']} "
            f"evaluation={summary['purged']['evaluation']}"
        )
        return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AgentValue-AI 数据留存策略")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="仅扫描，不执行")
    group.add_argument("--execute", action="store_true", help="执行归档与清理")
    args = parser.parse_args(argv)
    return asyncio.run(_run(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
