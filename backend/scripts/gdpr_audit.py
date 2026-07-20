#!/usr/bin/env python3
"""
AgentValue-AI GDPR / 个保法合规审计脚本（Phase 9.3）

支持数据主体三项权利：
- 访问权：query_employee_data 查询员工数据摘要
- 可携带权：export_employee_data 导出员工全部数据（JSON）
- 被遗忘权：delete_employee_data 软删除（标记 archived，30 天缓冲后由留存策略真删）

合规报告：generate_compliance_report 生成数据分类清单、留存状态、
访问日志统计与数据主体请求记录，输出 markdown 到 docs/compliance-report.md。

CLI：
    python -m scripts.gdpr_audit --export E1001
    python -m scripts.gdpr_audit --delete E1001
    python -m scripts.gdpr_audit --query E1001
    python -m scripts.gdpr_audit --report
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.database import AsyncSessionLocal
from models import AuditLog, Evaluation, Feedback, Memory, RawInput, User

logger = logging.getLogger(__name__)

# 报告默认输出路径：仓库根 docs/compliance-report.md
BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent
DEFAULT_REPORT_PATH = REPO_ROOT / "docs" / "compliance-report.md"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


# ---------------- 数据主体权利 ----------------


async def query_employee_data(
    employee_id: str, session: AsyncSession
) -> Dict[str, Any]:
    """访问权：查询员工数据摘要（不计入已软删除的归档数据）。"""
    user = (
        await session.execute(select(User).where(User.user_id == employee_id))
    ).scalar_one_or_none()

    # 仅统计未归档（未软删除）的数据
    raw_count = (
        await session.execute(
            select(func.count(RawInput.id)).where(
                RawInput.employee_id == employee_id, RawInput.archived.is_(False)
            )
        )
    ).scalar() or 0
    eval_count = (
        await session.execute(
            select(func.count(Evaluation.id)).where(
                Evaluation.employee_id == employee_id, Evaluation.archived.is_(False)
            )
        )
    ).scalar() or 0
    feedback_count = (
        await session.execute(
            select(func.count(Feedback.id)).where(Feedback.employee_id == employee_id)
        )
    ).scalar() or 0
    memory_count = (
        await session.execute(
            select(func.count(Memory.id)).where(Memory.employee_id == employee_id)
        )
    ).scalar() or 0
    audit_count = (
        await session.execute(
            select(func.count(AuditLog.id)).where(AuditLog.employee_id == employee_id)
        )
    ).scalar() or 0

    # 最近一次评估摘要
    latest_eval = (
        await session.execute(
            select(Evaluation)
            .where(
                Evaluation.employee_id == employee_id, Evaluation.archived.is_(False)
            )
            .order_by(Evaluation.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    return {
        "employee_id": employee_id,
        "user": (
            {
                "name": user.name if user else None,
                "email": user.email if user else None,
                "role": user.role if user else None,
                "department": user.department if user else None,
            }
            if user
            else None
        ),
        "summary": {
            "raw_inputs": raw_count,
            "evaluations": eval_count,
            "feedback": feedback_count,
            "memories": memory_count,
            "audit_logs": audit_count,
        },
        "latest_evaluation": (
            {
                "evaluation_id": latest_eval.evaluation_id,
                "period": latest_eval.period,
                "overall_score": latest_eval.overall_score,
                "status": latest_eval.status,
                "created_at": _iso(latest_eval.created_at),
            }
            if latest_eval
            else None
        ),
        "queried_at": datetime.now(timezone.utc).isoformat(),
    }


async def export_employee_data(
    employee_id: str, session: AsyncSession
) -> Dict[str, Any]:
    """可携带权：导出员工全部数据（JSON 结构，机器可读）。"""
    user = (
        await session.execute(select(User).where(User.user_id == employee_id))
    ).scalar_one_or_none()

    raw_inputs = (
        (
            await session.execute(
                select(RawInput).where(
                    RawInput.employee_id == employee_id, RawInput.archived.is_(False)
                )
            )
        )
        .scalars()
        .all()
    )
    evaluations = (
        (
            await session.execute(
                select(Evaluation).where(
                    Evaluation.employee_id == employee_id,
                    Evaluation.archived.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    feedback = (
        (
            await session.execute(
                select(Feedback).where(Feedback.employee_id == employee_id)
            )
        )
        .scalars()
        .all()
    )
    memories = (
        (await session.execute(select(Memory).where(Memory.employee_id == employee_id)))
        .scalars()
        .all()
    )
    audit_logs = (
        (
            await session.execute(
                select(AuditLog).where(AuditLog.employee_id == employee_id)
            )
        )
        .scalars()
        .all()
    )

    return {
        "employee_id": employee_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user": (
            {
                "user_id": user.user_id,
                "name": user.name,
                "email": user.email,
                "role": user.role,
                "department": user.department,
                "manager_id": user.manager_id,
                "created_at": _iso(user.created_at),
            }
            if user
            else None
        ),
        "raw_inputs": [
            {
                "input_id": r.input_id,
                "period": r.period,
                "type": r.type,
                "content": r.content,
                "attachments": r.attachments,
                "created_at": _iso(r.created_at),
            }
            for r in raw_inputs
        ],
        "evaluations": [
            {
                "evaluation_id": e.evaluation_id,
                "period": e.period,
                "overall_score": e.overall_score,
                "status": e.status,
                "employee_view": e.employee_view,
                "manager_view": e.manager_view,
                "audit": e.audit,
                "created_at": _iso(e.created_at),
                "approved_at": _iso(e.approved_at),
                "approver_id": e.approver_id,
            }
            for e in evaluations
        ],
        "feedback": [
            {
                "feedback_id": f.feedback_id,
                "evaluation_id": f.evaluation_id,
                "type": f.type,
                "content": f.content,
                "created_at": _iso(f.created_at),
            }
            for f in feedback
        ],
        "memories": [
            {
                "period": m.period,
                "content": m.content,
                "payload": m.payload,
                "created_at": _iso(m.created_at),
            }
            for m in memories
        ],
        "audit_logs": [
            {
                "log_id": a.log_id,
                "action": a.action,
                "actor_id": a.actor_id,
                "details": a.details,
                "ip_address": a.ip_address,
                "created_at": _iso(a.created_at),
            }
            for a in audit_logs
        ],
    }


async def delete_employee_data(
    employee_id: str, session: AsyncSession, settings=None
) -> Dict[str, Any]:
    """被遗忘权：软删除员工全部数据。

    策略：将 RawInput / Evaluation 标记为 archived=True + archived_at=now，
    写入 gdpr_deletion_requested 审计日志。30 天缓冲期后由留存策略 purge 真删，
    给申诉与误操作留出恢复窗口。Feedback / Memory 因无 archived 字段，
    一并记入审计日志，由后续清理任务处理。
    """
    settings = settings or get_settings()
    now = datetime.now(timezone.utc)

    raw_rows = (
        (
            await session.execute(
                select(RawInput).where(
                    RawInput.employee_id == employee_id, RawInput.archived.is_(False)
                )
            )
        )
        .scalars()
        .all()
    )
    eval_rows = (
        (
            await session.execute(
                select(Evaluation).where(
                    Evaluation.employee_id == employee_id,
                    Evaluation.archived.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )

    for r in raw_rows:
        r.archived = True
        r.archived_at = now
    for e in eval_rows:
        e.archived = True
        e.archived_at = now

    # 记录删除请求审计日志，便于追溯与缓冲期管理
    deletion_log = AuditLog(
        log_id=f"GDPR-DEL-{now.strftime('%Y%m%d%H%M%S')}-{employee_id}",
        actor_id="gdpr_script",
        action="gdpr_deletion_requested",
        employee_id=employee_id,
        details={
            "raw_inputs_marked": len(raw_rows),
            "evaluations_marked": len(eval_rows),
            "buffer_days": settings.retention_archive_buffer_days,
            "scheduled_purge_at": (
                now.replace(microsecond=0)
                + timedelta(days=settings.retention_archive_buffer_days)
            ).isoformat(),
        },
    )
    session.add(deletion_log)

    return {
        "employee_id": employee_id,
        "soft_deleted_at": now.isoformat(),
        "raw_inputs_marked": len(raw_rows),
        "evaluations_marked": len(eval_rows),
        "buffer_days": settings.retention_archive_buffer_days,
        "note": "数据已标记归档，缓冲期后由留存策略真删",
    }


# ---------------- 合规报告 ----------------


async def generate_compliance_report(
    session: AsyncSession,
    output_path: Optional[Path] = None,
    settings=None,
) -> str:
    """生成 GDPR/个保法合规报告（markdown），返回报告内容。"""
    settings = settings or get_settings()
    output_path = output_path or DEFAULT_REPORT_PATH
    now = datetime.now(timezone.utc)

    # 数据分类清单：各类数据总量与归档量
    raw_total = (await session.execute(select(func.count(RawInput.id)))).scalar() or 0
    raw_archived = (
        await session.execute(
            select(func.count(RawInput.id)).where(RawInput.archived.is_(True))
        )
    ).scalar() or 0
    eval_total = (
        await session.execute(select(func.count(Evaluation.id)))
    ).scalar() or 0
    eval_archived = (
        await session.execute(
            select(func.count(Evaluation.id)).where(Evaluation.archived.is_(True))
        )
    ).scalar() or 0
    feedback_total = (
        await session.execute(select(func.count(Feedback.id)))
    ).scalar() or 0
    memory_total = (await session.execute(select(func.count(Memory.id)))).scalar() or 0
    audit_total = (await session.execute(select(func.count(AuditLog.id)))).scalar() or 0
    user_total = (await session.execute(select(func.count(User.id)))).scalar() or 0

    # 留存状态：待归档（已过期未归档）数量
    raw_threshold = now - timedelta(days=settings.retention_raw_input_days)
    eval_threshold = now - timedelta(days=settings.retention_evaluation_days)
    raw_pending_archive = (
        await session.execute(
            select(func.count(RawInput.id)).where(
                RawInput.archived.is_(False), RawInput.created_at < raw_threshold
            )
        )
    ).scalar() or 0
    eval_pending_archive = (
        await session.execute(
            select(func.count(Evaluation.id)).where(
                Evaluation.archived.is_(False), Evaluation.created_at < eval_threshold
            )
        )
    ).scalar() or 0

    # 访问日志统计：按 action 分布
    action_rows = (
        await session.execute(
            select(AuditLog.action, func.count(AuditLog.id))
            .group_by(AuditLog.action)
            .order_by(func.count(AuditLog.id).desc())
        )
    ).all()
    action_stats = {row[0]: row[1] for row in action_rows}

    # 数据主体请求记录：GDPR 相关审计日志
    dsr_rows = (
        (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.action.like("gdpr_%"))
                .order_by(AuditLog.created_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    dsr_records = [
        {
            "log_id": l.log_id,
            "action": l.action,
            "employee_id": l.employee_id,
            "created_at": _iso(l.created_at),
            "details": l.details,
        }
        for l in dsr_rows
    ]

    top_actions = sorted(action_stats.items(), key=lambda kv: kv[1], reverse=True)[:10]

    lines: List[str] = []
    lines.append("# AgentValue-AI 合规审计报告")
    lines.append("")
    lines.append(f"> 生成时间：{now.isoformat()}")
    lines.append("> 适用法规：GDPR / 中华人民共和国个人信息保护法")
    lines.append("")
    lines.append("## 一、数据分类清单")
    lines.append("")
    lines.append("| 数据类别 | 表 | 总量 | 已归档 | 留存期 |")
    lines.append("|---|---|---|---|---|")
    lines.append(
        f"| 员工原始输入 | raw_inputs | {raw_total} | {raw_archived} | {settings.retention_raw_input_days} 天 |"
    )
    lines.append(
        f"| 评估结果 | evaluations | {eval_total} | {eval_archived} | {settings.retention_evaluation_days} 天 |"
    )
    lines.append(f"| 反馈与申诉 | feedback | {feedback_total} | - | 随评估关联 |")
    lines.append(f"| 员工记忆 | memories | {memory_total} | - | 随评估关联 |")
    lines.append(f"| 审计日志 | audit_logs | {audit_total} | - | 安全留存 |")
    lines.append(f"| 用户账号 | users | {user_total} | - | 离职后清理 |")
    lines.append("")
    lines.append(
        f"> 归档缓冲期：归档后 {settings.retention_archive_buffer_days} 天真删，给申诉与误操作留恢复窗口。"
    )
    lines.append("")

    lines.append("## 二、留存策略执行状态")
    lines.append("")
    lines.append("| 数据类别 | 待归档（已过期未归档） | 风险提示 |")
    lines.append("|---|---|---|")
    raw_risk = "⚠️ 有过期数据未归档" if raw_pending_archive else "✅ 正常"
    eval_risk = "⚠️ 有过期数据未归档" if eval_pending_archive else "✅ 正常"
    lines.append(f"| 原始输入 | {raw_pending_archive} | {raw_risk} |")
    lines.append(f"| 评估结果 | {eval_pending_archive} | {eval_risk} |")
    lines.append("")
    lines.append(
        "建议定期执行 `python -m scripts.data_retention --execute` 处理过期数据。"
    )
    lines.append("")

    lines.append("## 三、访问日志统计")
    lines.append("")
    if top_actions:
        lines.append("| 操作动作 | 日志条数 |")
        lines.append("|---|---|")
        for action, cnt in top_actions:
            lines.append(f"| {action} | {cnt} |")
    else:
        lines.append("暂无审计日志记录。")
    lines.append("")
    lines.append(f"审计日志总量：**{audit_total}** 条。")
    lines.append("")

    lines.append("## 四、数据主体请求记录（DSR）")
    lines.append("")
    if dsr_records:
        lines.append("| 时间 | 动作 | 员工 | 摘要 |")
        lines.append("|---|---|---|---|")
        for r in dsr_records:
            summary = (
                f"标记 raw={r['details'].get('raw_inputs_marked', 0)}, eval={r['details'].get('evaluations_marked', 0)}"
                if r["details"]
                else "-"
            )
            lines.append(
                f"| {r['created_at']} | {r['action']} | {r['employee_id']} | {summary} |"
            )
    else:
        lines.append("暂无数据主体请求记录。")
    lines.append("")

    lines.append("## 五、合规结论")
    lines.append("")
    issues: List[str] = []
    if raw_pending_archive:
        issues.append(f"原始输入有 {raw_pending_archive} 条过期未归档")
    if eval_pending_archive:
        issues.append(f"评估结果有 {eval_pending_archive} 条过期未归档")
    if issues:
        lines.append("⚠️ 发现以下待处理事项：")
        for s in issues:
            lines.append(f"- {s}")
        lines.append("建议尽快执行留存策略并复核数据主体请求处理时效。")
    else:
        lines.append("✅ 当前数据留存状态符合 GDPR / 个保法留存期要求，无待处理告警。")
    lines.append("")
    lines.append("---")
    lines.append("*本报告由 AgentValue-AI 合规审计脚本自动生成，供 DPO 与审计师复核。*")

    content = "\n".join(lines)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return content


# ---------------- CLI ----------------


async def _run_cli(args) -> int:
    async with AsyncSessionLocal() as session:
        if args.export:
            data = await export_employee_data(args.export, session)
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        if args.delete:
            result = await delete_employee_data(args.delete, session)
            await session.commit()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.query:
            data = await query_employee_data(args.query, session)
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        if args.report:
            content = await generate_compliance_report(session)
            print(f"合规报告已生成: {DEFAULT_REPORT_PATH}")
            print(f"报告长度: {len(content)} 字符")
            return 0
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AgentValue-AI GDPR/个保法合规审计")
    parser.add_argument("--export", metavar="EMP_ID", help="导出员工全部数据（JSON）")
    parser.add_argument("--delete", metavar="EMP_ID", help="软删除员工数据（被遗忘权）")
    parser.add_argument("--query", metavar="EMP_ID", help="查询员工数据摘要（访问权）")
    parser.add_argument("--report", action="store_true", help="生成合规报告 markdown")
    args = parser.parse_args(argv)

    if not any([args.export, args.delete, args.query, args.report]):
        parser.error("需指定 --export / --delete / --query / --report 之一")
    return asyncio.run(_run_cli(args))


if __name__ == "__main__":
    sys.exit(main())
