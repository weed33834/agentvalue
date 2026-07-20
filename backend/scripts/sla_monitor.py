#!/usr/bin/env python3
"""
AgentValue-AI 申诉处理 SLA 监控

监控申诉从提交到响应/解决的时效，对照 72 小时响应 SLA，
输出达成率、超时清单与按部门 / 角色分组统计。

输入：申诉记录列表，字段含
    appeal_id / employee_id / evaluation_id / department / role /
    appeal_time(ISO) / resolved_time(ISO 或 null) / status(open|resolved)

自带造数函数 generate_appeals 生成 4 周模拟申诉（含按时与超时两种），
不依赖外部数据库，可独立跑通。

用法：
    cd backend
    python -m scripts.sla_monitor --weeks 4 --output data/pilot/
"""

import argparse
import json
import random
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from scripts._stats_utils import fmt_num

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent
DEFAULT_DOCS_DIR = REPO_ROOT / "docs"

# 申诉响应 SLA：72 小时
SLA_HOURS = 72


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """解析 ISO 时间字符串为带时区 datetime；None / 空串返回 None。"""
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    # 统一补 UTC 时区，便于跨时区相减
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def compute_sla(
    appeals: list[dict],
    sla_hours: float = SLA_HOURS,
    now: Optional[datetime] = None,
) -> dict:
    """
    计算申诉处理 SLA 达成情况。

    判定规则：
        - 已解决（resolved_time 存在）：处理时长 = resolved - appeal；
          时长 ≤ sla_hours 计 met，否则 breached。
        - 未解决（open）：若 (now - appeal) > sla_hours 计 breached（逾期未响应），
          否则 pending（仍在 SLA 窗口内，尚未违约）。
        - 正好等于 sla_hours 视为达成（≤）。

    返回：total / resolved / open / met / breached / pending /
          achievement_rate（met / (met+breached) * 100）/ breaches 清单。
    """
    if now is None:
        now = datetime.now(timezone.utc)
    else:
        # 保证 now 带时区
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

    total = len(appeals)
    resolved = open = met = breached = pending = 0
    breaches: list[dict] = []
    case_details: list[dict] = []

    for ap in appeals:
        appeal_time = _parse_dt(ap.get("appeal_time"))
        resolved_time = _parse_dt(ap.get("resolved_time"))
        status = ap.get("status", "open")
        hours: Optional[float] = None
        verdict: str

        if resolved_time is not None and appeal_time is not None:
            hours = round((resolved_time - appeal_time).total_seconds() / 3600.0, 2)
            resolved += 1
            if hours <= sla_hours:
                met += 1
                verdict = "met"
            else:
                breached += 1
                verdict = "breached"
        else:
            open += 1
            if appeal_time is not None:
                elapsed = round((now - appeal_time).total_seconds() / 3600.0, 2)
                hours = elapsed
                if elapsed > sla_hours:
                    breached += 1
                    verdict = "breached"
                else:
                    pending += 1
                    verdict = "pending"
            else:
                # 无 appeal_time 的脏数据，按违规记录但不计入分母
                breached += 1
                verdict = "breached"
                hours = None

        detail = {
            "appeal_id": ap.get("appeal_id"),
            "employee_id": ap.get("employee_id"),
            "evaluation_id": ap.get("evaluation_id"),
            "department": ap.get("department", "unknown"),
            "role": ap.get("role", "unknown"),
            "appeal_time": ap.get("appeal_time"),
            "resolved_time": ap.get("resolved_time"),
            "hours": hours,
            "status": status,
            "verdict": verdict,
        }
        case_details.append(detail)
        if verdict == "breached":
            breaches.append(detail)

    decided = met + breached
    achievement_rate = round(met / decided * 100.0, 2) if decided > 0 else 100.0

    return {
        "total": total,
        "resolved": resolved,
        "open": open,
        "met": met,
        "breached": breached,
        "pending": pending,
        "achievement_rate": achievement_rate,
        "sla_hours": sla_hours,
        "breaches": breaches,
        "case_details": case_details,
    }


def group_stats(sla_result: dict, dimension: str) -> dict:
    """按 dimension（department / role）分组统计达成率与超时数。"""
    groups: dict[str, dict[str, Any]] = {}
    for case in sla_result["case_details"]:
        key = str(case.get(dimension) or "unknown")
        g = groups.setdefault(
            key, {"total": 0, "met": 0, "breached": 0, "pending": 0, "hours": []}
        )
        g["total"] += 1
        g[case["verdict"]] = g.get(case["verdict"], 0) + 1
        if case["hours"] is not None:
            g["hours"].append(case["hours"])

    for key, g in groups.items():
        decided = g["met"] + g["breached"]
        g["achievement_rate"] = (
            round(g["met"] / decided * 100.0, 2) if decided > 0 else 100.0
        )
        g["avg_hours"] = round(statistics.mean(g["hours"]), 2) if g["hours"] else 0.0
        g["max_hours"] = round(max(g["hours"]), 2) if g["hours"] else 0.0
        del g["hours"]
    return {"dimension": dimension, "groups": groups}


def generate_appeals(weeks: int = 4, seed: int = 20260616) -> list[dict]:
    """
    造数函数：生成 weeks 周模拟申诉记录，含按时解决与超时两种，
    并保留少量 open（含逾期未响应）。固定 seed 保证可复现。
    """
    rng = random.Random(seed)
    departments = ["Engineering", "Sales", "Product", "Operations", "HR"]
    roles = ["IC", "Manager"]

    # 生成窗口起点（UTC），让数据落在「过去」便于 open 判定
    window_start = datetime(2026, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
    appeals: list[dict] = []
    base_week = 25

    for w in range(weeks):
        period = f"2026-W{base_week + w}"
        # 每周 14-20 条申诉
        n = rng.randint(14, 20)
        for i in range(n):
            appeal_id = f"AP-{period}-W{w}-{i + 1:03d}"
            emp_id = f"E{rng.randint(1, 120):04d}"
            eval_id = f"EV-{period}-{emp_id}"
            dept = rng.choice(departments)
            role = rng.choices(roles, weights=[8, 2])[0]
            # 申诉时间落在该周内
            appeal_time = window_start + timedelta(
                days=w * 7 + rng.randint(0, 6),
                hours=rng.randint(0, 23),
                minutes=rng.randint(0, 59),
            )
            roll = rng.random()
            if roll < 0.65:
                # 按时解决：24-70 小时内
                delay_hours = rng.uniform(8, 70)
                resolved_time = appeal_time + timedelta(hours=delay_hours)
                status = "resolved"
            elif roll < 0.85:
                # 超时解决：80-160 小时
                delay_hours = rng.uniform(80, 160)
                resolved_time = appeal_time + timedelta(hours=delay_hours)
                status = "resolved"
            else:
                # 未解决：open（部分已逾期，部分仍在窗口内）
                resolved_time = None
                status = "open"
            appeals.append(
                {
                    "appeal_id": appeal_id,
                    "employee_id": emp_id,
                    "evaluation_id": eval_id,
                    "department": dept,
                    "role": role,
                    "appeal_time": appeal_time.isoformat(),
                    "resolved_time": (
                        resolved_time.isoformat() if resolved_time else None
                    ),
                    "status": status,
                    "period": period,
                }
            )
    return appeals


def generate_sla_report(
    appeals: Optional[list[dict]] = None, weeks: int = 4, now: Optional[datetime] = None
) -> dict:
    """
    生成完整 SLA 报告 dict。appeals 为空时调用造数函数生成 weeks 周数据。
    """
    if appeals is None or not appeals:
        appeals = generate_appeals(weeks=weeks)

    sla_result = compute_sla(appeals, sla_hours=SLA_HOURS, now=now)

    # 按周统计趋势
    week_labels = sorted({a.get("period", "") for a in appeals if a.get("period")})
    by_week: list[dict] = []
    for label in week_labels:
        week_appeals = [a for a in appeals if a.get("period") == label]
        wr = compute_sla(week_appeals, sla_hours=SLA_HOURS, now=now)
        by_week.append(
            {
                "period": label,
                "total": wr["total"],
                "met": wr["met"],
                "breached": wr["breached"],
                "achievement_rate": wr["achievement_rate"],
            }
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "weeks": week_labels,
        "sla_hours": SLA_HOURS,
        "total_appeals": sla_result["total"],
        "summary": {
            "resolved": sla_result["resolved"],
            "open": sla_result["open"],
            "met": sla_result["met"],
            "breached": sla_result["breached"],
            "pending": sla_result["pending"],
            "achievement_rate": sla_result["achievement_rate"],
        },
        "by_department": group_stats(sla_result, "department"),
        "by_role": group_stats(sla_result, "role"),
        "by_week": by_week,
        "breaches": sla_result["breaches"],
    }


def render_markdown(report: dict) -> str:
    """渲染 SLA 月报 markdown，含达成率、超时清单、分组统计与建议。"""
    lines: list[str] = []
    s = report["summary"]
    lines.append("# AgentValue-AI 申诉处理 SLA 月报")
    lines.append("")
    lines.append(f"> 生成时间：{report['generated_at']}")
    lines.append(f"> 覆盖周次：{', '.join(report['weeks'])}")
    lines.append(f"> SLA 标准：{report['sla_hours']} 小时内响应/解决")
    lines.append(f"> 申诉总数：{report['total_appeals']} 条")
    lines.append("")
    lines.append("## 一、整体达成情况")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 已解决 | {s['resolved']} |")
    lines.append(f"| 未解决（open） | {s['open']} |")
    lines.append(f"| 达成（≤{report['sla_hours']}h） | {s['met']} |")
    lines.append(f"| 超时 | {s['breached']} |")
    lines.append(f"| 仍在 SLA 窗口内（pending） | {s['pending']} |")
    lines.append(f"| **SLA 达成率** | **{fmt_num(s['achievement_rate'])}%** |")
    lines.append("")
    rate = s["achievement_rate"]
    if rate >= 95:
        verdict = "✅ 达成率 ≥95%，符合试点退出标准。"
    elif rate >= 85:
        verdict = "⚠️ 达成率 85%-95%，接近临界，需关注超时根因。"
    else:
        verdict = "❌ 达成率 <85%，未达试点退出标准（≤3 个工作日），须立即整改。"
    lines.append(f"**判断**：{verdict}")
    lines.append("")

    # 按周趋势
    lines.append("## 二、逐周趋势")
    lines.append("")
    lines.append("| 周次 | 申诉数 | 达成 | 超时 | 达成率 |")
    lines.append("|---|---|---|---|---|")
    for row in report["by_week"]:
        lines.append(
            f"| {row['period']} | {row['total']} | {row['met']} | {row['breached']} | {fmt_num(row['achievement_rate'])}% |"
        )
    lines.append("")

    # 分组统计
    for idx, (title, key) in enumerate(
        [("按部门", "by_department"), ("按角色", "by_role")], start=3
    ):
        stat = report[key]
        lines.append(f"## {idx}、{title}分组统计")
        lines.append("")
        dim = stat["dimension"]
        lines.append(
            f"| {dim} | 申诉数 | 达成 | 超时 | 达成率 | 平均处理时长(h) | 最长时长(h) |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        sorted_groups = sorted(
            stat["groups"].items(), key=lambda kv: kv[1]["achievement_rate"]
        )
        for name, g in sorted_groups:
            lines.append(
                f"| {name} | {g['total']} | {g['met']} | {g['breached']} "
                f"| {fmt_num(g['achievement_rate'])}% | {fmt_num(g['avg_hours'])} | {fmt_num(g['max_hours'])} |"
            )
        lines.append("")

    # 超时清单
    lines.append("## 五、超时清单（Top 10）")
    lines.append("")
    breaches = sorted(
        report["breaches"],
        key=lambda b: b["hours"] if b["hours"] is not None else -1,
        reverse=True,
    )
    if not breaches:
        lines.append("本月无超时申诉。")
    else:
        lines.append(
            "| appeal_id | 员工 | 部门 | 角色 | 申诉时间 | 处理时长(h) | 状态 |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        for b in breaches[:10]:
            lines.append(
                f"| {b['appeal_id']} | {b['employee_id']} | {b['department']} | {b['role']} "
                f"| {b['appeal_time']} | {fmt_num(b['hours'])} | {b['status']} |"
            )
    lines.append("")

    # 结论与建议
    lines.append("## 六、结论与建议")
    lines.append("")
    lines.append(
        f"1. 本月 SLA 达成率 {fmt_num(rate)}%（目标 ≥95% 对应「≤3 个工作日」退出标准）。"
    )
    # 找达成率最低的部门
    dept_groups = report["by_department"]["groups"]
    if dept_groups:
        worst_dept = min(dept_groups.items(), key=lambda kv: kv[1]["achievement_rate"])
        lines.append(
            f"2. 达成率最低的部门为 {worst_dept[0]}（{fmt_num(worst_dept[1]['achievement_rate'])}%），"
            "建议核查该部门主管审批队列与 HRBP 人力配置。"
        )
    if s["breached"] > 0:
        lines.append(
            f"3. 共 {s['breached']} 条超时申诉，建议对超时 Top 3 做根因复盘"
            "（主管未及时响应 / HRBP 人力不足 / 系统未自动升级）。"
        )
    lines.append(
        "4. 建议为 open 且临近 72h 的申诉配置自动提醒与升级机制，避免从 pending 滑入 breached。"
    )
    lines.append("")
    lines.append("---")
    lines.append(
        "*本报告由 AgentValue-AI SLA 监控脚本自动生成，达成率供 SRE 与 HRBP 复核。*"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """命令行入口：生成 SLA 报告并写出 JSON + markdown。"""
    parser = argparse.ArgumentParser(description="AgentValue-AI 申诉处理 SLA 监控")
    parser.add_argument("--weeks", type=int, default=4, help="聚合周数，默认 4")
    parser.add_argument(
        "--output", default="data/pilot/", help="JSON 报告输出目录，默认 data/pilot/"
    )
    parser.add_argument(
        "--markdown-dir",
        default=str(DEFAULT_DOCS_DIR),
        help="markdown 摘要输出目录，默认 <repo>/docs/",
    )
    parser.add_argument(
        "--seed", type=int, default=20260616, help="造数随机种子，默认 20260616"
    )
    args = parser.parse_args(argv)

    appeals = generate_appeals(weeks=args.weeks, seed=args.seed)
    report = generate_sla_report(appeals=appeals, weeks=args.weeks)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "sla-report.json"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"JSON 报告已写入: {json_path}")

    md_dir = Path(args.markdown_dir)
    md_dir.mkdir(parents=True, exist_ok=True)
    md_path = md_dir / "sla-monthly.md"
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(f"Markdown 报告已写入: {md_path}")

    s = report["summary"]
    print(f"\n申诉总数: {report['total_appeals']}")
    print(f"  达成: {s['met']} | 超时: {s['breached']} | pending: {s['pending']}")
    print(f"  SLA 达成率: {s['achievement_rate']:.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
