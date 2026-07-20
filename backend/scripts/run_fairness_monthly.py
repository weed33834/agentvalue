#!/usr/bin/env python3
"""
AgentValue-AI 公平性审计月报

按月（默认 4 周）聚合评估记录，按部门 / 职级 / 性别 / 办公地分组统计，
输出 JSON 报告与可读 markdown 摘要，供 HRBP 与公平性审计师月度复核。

重点检查：
    - 各组均值 / 标准差 / 中位数
    - 组间 max_gap 与风险告警
    - 4 周趋势（各组均值随周次变化）
    - 双线汇报员工是否被系统性压低 / 抬高

自带造数函数 generate_pilot_evaluations 生成 4 周模拟数据，
不依赖外部数据库，可独立跑通。

用法：
    cd backend
    python -m scripts.run_fairness_monthly --output data/pilot/
"""

import argparse
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# 复用既有公平性审计能力（部门级风险判定）
from scripts.fairness_audit import audit_fairness
from scripts._stats_utils import fmt_num, std

# 仓库根：backend/ 的上一级，用于定位 docs/
BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent
DEFAULT_DOCS_DIR = REPO_ROOT / "docs"

# 分组维度
DIMENSIONS = ["department", "level", "gender", "office"]
# 风险阈值：组间均值差超过该值视为潜在公平性风险
RISK_THRESHOLD = 5.0


def _median(values: list[float]) -> float:
    """中位数；空列表返回 0.0。"""
    return statistics.median(values) if values else 0.0


def group_stats(
    records: list[dict], dimension: str, threshold: float = RISK_THRESHOLD
) -> dict:
    """
    单维度分组统计：均值 / 标准差 / 中位数 / 最小 / 最大 / 样本数，
    并计算组间 max_gap 与风险标记。

    参数：
        records: 评估记录列表，每条含 overall_score 与 dimension 字段；
        dimension: 分组字段名（department/level/gender/office）；
        threshold: 组间均值差风险阈值。
    """
    buckets: dict[str, list[float]] = {}
    for record in records:
        key = str(record.get(dimension) or "unknown")
        score = record.get("overall_score")
        if score is None:
            continue
        buckets.setdefault(key, []).append(float(score))

    groups: dict[str, dict[str, Any]] = {}
    for key, scores in buckets.items():
        groups[key] = {
            "count": len(scores),
            "mean": round(statistics.mean(scores), 2),
            "std": round(std(scores), 2),
            "median": round(_median(scores), 2),
            "min": round(min(scores), 2),
            "max": round(max(scores), 2),
        }

    group_means = {k: v["mean"] for k, v in groups.items()}
    if len(group_means) >= 2:
        max_group = max(group_means, key=group_means.get)
        min_group = min(group_means, key=group_means.get)
        max_gap = round(group_means[max_group] - group_means[min_group], 2)
    else:
        max_group = next(iter(group_means), None)
        min_group = max_group
        max_gap = 0.0

    return {
        "dimension": dimension,
        "groups": groups,
        "max_gap": max_gap,
        "has_risk": max_gap > threshold,
        "max_group": max_group,
        "min_group": min_group,
        "threshold": threshold,
    }


def compute_trend(records: list[dict], dimension: str, week_labels: list[str]) -> dict:
    """
    4 周趋势：按周次 + 维度交叉聚合均值，返回 {group: [每周均值]}。
    缺失周次的组该周填 None，便于前端折线图对齐。
    """
    trend: dict[str, list[Optional[float]]] = {}
    for label in week_labels:
        week_records = [r for r in records if r.get("period") == label]
        stats = group_stats(week_records, dimension, threshold=RISK_THRESHOLD)
        for group, stat in stats["groups"].items():
            trend.setdefault(group, [None] * len(week_labels))
            idx = week_labels.index(label)
            trend[group][idx] = stat["mean"]
    return trend


def dual_reporting_focus(records: list[dict]) -> dict:
    """
    双线汇报员工专项检查：对比双线汇报与非双线汇报员工的均值差异，
    判断是否存在系统性压低 / 抬高。同时给出逐周对比。
    """
    dual = [r for r in records if r.get("dual_reporting") is True]
    non_dual = [r for r in records if r.get("dual_reporting") is not True]

    def _mean(rs):
        scores = [
            float(r["overall_score"]) for r in rs if r.get("overall_score") is not None
        ]
        return round(statistics.mean(scores), 2) if scores else 0.0

    dual_mean = _mean(dual)
    non_dual_mean = _mean(non_dual)
    gap = round(dual_mean - non_dual_mean, 2)

    week_labels = sorted({r.get("period", "") for r in records if r.get("period")})
    by_week = []
    for label in week_labels:
        dw = [r for r in dual if r.get("period") == label]
        nw = [r for r in non_dual if r.get("period") == label]
        by_week.append(
            {
                "period": label,
                "dual_reporting_mean": _mean(dw),
                "non_dual_reporting_mean": _mean(nw),
            }
        )

    # 系统性偏低：双线汇报均值比非双线低超过 3 分
    systematically_lower = gap <= -3.0 and len(dual) > 0
    systematically_higher = gap >= 3.0 and len(dual) > 0

    return {
        "dual_reporting_count": len(dual),
        "non_dual_reporting_count": len(non_dual),
        "dual_reporting_mean": dual_mean,
        "non_dual_reporting_mean": non_dual_mean,
        "gap": gap,
        "systematically_lower": systematically_lower,
        "systematically_higher": systematically_higher,
        "by_week": by_week,
    }


def generate_pilot_evaluations(weeks: int = 4, seed: int = 20260615) -> list[dict]:
    """
    造数函数：生成 weeks 周模拟评估记录，含部门 / 职级 / 性别 / 办公地 /
    双线汇报标记。刻意埋入两类试点发现的偏置：
        1) 研发部均值系统性偏低约 5-6 分（主管评分偏严）；
        2) 双线汇报员工均值偏低约 4 分（双线反馈冲突压低综合分）。
    使用固定 seed 保证可复现。
    """
    import random

    rng = random.Random(seed)

    departments = ["Engineering", "Sales", "Product", "Operations", "HR"]
    levels = ["L1", "L2", "L3", "L4"]
    genders = ["M", "F"]
    offices = ["Beijing", "Shanghai", "Shenzhen", "Singapore", "Remote"]

    # 部门基础分基准（Engineering 偏低，HR 偏高，模拟评分尺度差异）
    dept_base = {
        "Engineering": 72,
        "Sales": 78,
        "Product": 77,
        "Operations": 79,
        "HR": 81,
    }
    # 职级越高基准分略高
    level_delta = {"L1": -3, "L2": 0, "L3": 3, "L4": 5}

    # 基准周（ISO 周从 W25 起）
    base_week = 25
    records: list[dict] = []

    # 固定员工池，跨周保持画像一致
    employee_pool = []
    for i in range(120):
        emp = {
            "employee_id": f"E{i + 1:04d}",
            "department": rng.choice(departments),
            "level": rng.choices(levels, weights=[3, 5, 3, 1])[0],
            "gender": rng.choices(genders, weights=[6, 5])[0],
            "office": rng.choice(offices),
            # 约 22% 员工为双线汇报
            "dual_reporting": rng.random() < 0.22,
        }
        employee_pool.append(emp)

    for w in range(weeks):
        period = f"2026-W{base_week + w}"
        # 每周轻度漂移：研发部持续走低，其余部门小幅波动
        week_drift = rng.uniform(-1.5, 1.5)
        for emp in employee_pool:
            base = dept_base[emp["department"]] + level_delta[emp["level"]]
            # 研发部逐周累计下行（连续偏低触发趋势告警）
            if emp["department"] == "Engineering":
                base -= w * 1.2
            base += week_drift + rng.uniform(-4, 4)
            # 双线汇报压低
            if emp["dual_reporting"]:
                base -= 4.0
            score = max(40.0, min(98.0, round(base, 1)))
            records.append(
                {
                    "employee_id": emp["employee_id"],
                    "period": period,
                    "week_index": w,
                    "department": emp["department"],
                    "level": emp["level"],
                    "gender": emp["gender"],
                    "office": emp["office"],
                    "dual_reporting": emp["dual_reporting"],
                    "overall_score": score,
                }
            )
    return records


def generate_monthly_report(
    records: Optional[list[dict]] = None, weeks: int = 4
) -> dict:
    """
    生成完整月报 dict。records 为空时调用造数函数生成 weeks 周数据。

    返回结构包含：周次、各维度分组统计、4 周趋势、双线汇报专项、整体指标。
    """
    if records is None or not records:
        records = generate_pilot_evaluations(weeks=weeks)

    week_labels = sorted({r["period"] for r in records if r.get("period")})
    all_scores = [
        float(r["overall_score"]) for r in records if r.get("overall_score") is not None
    ]

    by_dimension: dict[str, dict] = {}
    for dim in DIMENSIONS:
        by_dimension[dim] = group_stats(records, dim, threshold=RISK_THRESHOLD)

    trend: dict[str, dict] = {}
    for dim in DIMENSIONS:
        trend[dim] = compute_trend(records, dim, week_labels)

    # 部门维度同时复用既有 audit_fairness 做交叉校验（保留单一职责脚本的可追溯性）
    dept_audit = audit_fairness(records, threshold=RISK_THRESHOLD)

    dual_focus = dual_reporting_focus(records)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "weeks": week_labels,
        "dimensions": DIMENSIONS,
        "total_evaluations": len(records),
        "overall": {
            "mean": round(statistics.mean(all_scores), 2) if all_scores else 0.0,
            "std": round(std(all_scores), 2),
            "median": round(_median(all_scores), 2),
            "min": round(min(all_scores), 2) if all_scores else 0.0,
            "max": round(max(all_scores), 2) if all_scores else 0.0,
        },
        "by_dimension": by_dimension,
        "trend": trend,
        "dual_reporting_focus": dual_focus,
        "department_audit_crosscheck": {
            "max_gap": dept_audit["max_gap"],
            "has_risk": dept_audit["has_risk"],
            "dimension": dept_audit["details"]["dimension"],
        },
    }


def render_markdown(report: dict) -> str:
    """
    将月报渲染为可读 markdown，写得像真实审计师：有数据、有判断、有建议。
    """
    lines: list[str] = []
    lines.append("# AgentValue-AI 公平性审计月报")
    lines.append("")
    lines.append(f"> 生成时间：{report['generated_at']}")
    lines.append(f"> 覆盖周次：{', '.join(report['weeks'])}")
    lines.append(f"> 评估总样本数：{report['total_evaluations']} 条")
    lines.append("")
    lines.append("## 一、整体指标")
    overall = report["overall"]
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 均值 | {fmt_num(overall['mean'])} |")
    lines.append(f"| 标准差 | {fmt_num(overall['std'])} |")
    lines.append(f"| 中位数 | {fmt_num(overall['median'])} |")
    lines.append(f"| 最小值 | {fmt_num(overall['min'])} |")
    lines.append(f"| 最大值 | {fmt_num(overall['max'])} |")
    lines.append("")

    # 各维度分组统计
    lines.append("## 二、分组统计与组间差异")
    lines.append("")
    for dim in report["dimensions"]:
        stat = report["by_dimension"][dim]
        lines.append(f"### 2.{report['dimensions'].index(dim) + 1} 按 {dim} 分组")
        lines.append("")
        lines.append(
            f"组间均值最大差异 max_gap = **{fmt_num(stat['max_gap'])}**（阈值 {fmt_num(stat['threshold'])}）"
            f"→ {'⚠️ 超阈值，存在潜在公平性风险' if stat['has_risk'] else '✅ 未超阈值'}。"
        )
        lines.append("")
        lines.append(f"| {dim} | 样本数 | 均值 | 标准差 | 中位数 | 最小 | 最大 |")
        lines.append("|---|---|---|---|---|---|---|")
        # 按均值降序排列，便于一眼看出高低调
        sorted_groups = sorted(
            stat["groups"].items(), key=lambda kv: kv[1]["mean"], reverse=True
        )
        for name, g in sorted_groups:
            lines.append(
                f"| {name} | {g['count']} | {fmt_num(g['mean'])} | {fmt_num(g['std'])} "
                f"| {fmt_num(g['median'])} | {fmt_num(g['min'])} | {fmt_num(g['max'])} |"
            )
        lines.append("")
        if stat["has_risk"]:
            lines.append(
                f"**判断**：{stat['max_group']} 均值最高，{stat['min_group']} 均值最低，"
                f"差距 {fmt_num(stat['max_gap'])} 分已超阈值，建议核查 {stat['min_group']} 侧"
                f"的评分尺度与证据充分性。"
            )
            lines.append("")

    # 趋势
    lines.append("## 三、4 周趋势")
    lines.append("")
    for dim in report["dimensions"]:
        trend = report["trend"][dim]
        lines.append(f"### {dim} 各组周均值变化")
        lines.append("")
        header = "| " + dim + " | " + " | ".join(report["weeks"]) + " |"
        sep = "|---" * (len(report["weeks"]) + 1) + "|"
        lines.append(header)
        lines.append(sep)
        for group, means in trend.items():
            row = "| " + group + " | " + " | ".join(fmt_num(m) for m in means) + " |"
            lines.append(row)
        lines.append("")

    # 趋势判断：研发部连续 3 周低于全公司均值 5 分以上
    eng_trend = report["trend"]["department"].get("Engineering", [])
    overall_mean = report["overall"]["mean"]
    eng_below_streak = 0
    for m in eng_trend:
        if m is not None and (overall_mean - m) >= 5.0:
            eng_below_streak += 1
        else:
            eng_below_streak = 0
    if eng_below_streak >= 3:
        lines.append(
            f"**趋势告警**：研发部均值连续 {eng_below_streak} 周低于全公司均值 5 分以上，"
            "建议检查该部门主管评分标准是否过严，必要时做评分校准会。"
        )
        lines.append("")

    # 双线汇报专项
    lines.append("## 四、双线汇报员工专项检查")
    lines.append("")
    dr = report["dual_reporting_focus"]
    lines.append(
        f"- 双线汇报员工样本数：{dr['dual_reporting_count']}，非双线汇报样本数：{dr['non_dual_reporting_count']}"
    )
    lines.append(
        f"- 双线汇报员工均值：{fmt_num(dr['dual_reporting_mean'])}，"
        f"非双线汇报均值：{fmt_num(dr['non_dual_reporting_mean'])}，"
        f"差值（双线 - 非双线）：**{fmt_num(dr['gap'])}**"
    )
    if dr["systematically_lower"]:
        lines.append(
            "- ⚠️ **双线汇报员工评分系统性偏低**，疑似双线反馈冲突导致综合分被压低，"
            "建议在 Prompt v1.0 双线汇报综合判断逻辑上线后复测。"
        )
    elif dr["systematically_higher"]:
        lines.append("- ⚠️ 双线汇报员工评分系统性偏高，需复核是否存在「取高」倾向。")
    else:
        lines.append("- ✅ 双线汇报与非双线汇报均值差异在容忍区间，未发现系统性偏置。")
    lines.append("")
    lines.append("逐周对比：")
    lines.append("")
    lines.append("| 周次 | 双线汇报均值 | 非双线汇报均值 | 差值 |")
    lines.append("|---|---|---|---|")
    for row in dr["by_week"]:
        diff = round(row["dual_reporting_mean"] - row["non_dual_reporting_mean"], 2)
        lines.append(
            f"| {row['period']} | {fmt_num(row['dual_reporting_mean'])} "
            f"| {fmt_num(row['non_dual_reporting_mean'])} | {fmt_num(diff)} |"
        )
    lines.append("")

    # 结论与建议
    lines.append("## 五、结论与建议")
    lines.append("")
    risk_dims = [
        d for d in report["dimensions"] if report["by_dimension"][d]["has_risk"]
    ]
    if risk_dims:
        lines.append(f"1. 以下维度组间差异超阈值，需重点关注：{', '.join(risk_dims)}。")
    else:
        lines.append("1. 各维度组间差异均在阈值内，本月未触发公平性风险告警。")
    if eng_below_streak >= 3:
        lines.append(
            "2. 研发部评分连续偏低，建议组织评分校准会，统一主管对「执行力/交付确定性」的打分尺度。"
        )
    if dr["systematically_lower"]:
        lines.append(
            "3. 双线汇报员工被系统性压低，待 Prompt v1.0 双线汇报综合判断逻辑经真实模型验证后切换上线，"
            "并持续观察该群体均值是否回升至非双线水平 ±3 分内。"
        )
    lines.append(
        "4. 建议下月对 max_gap 最大的维度做 10% 人工抽检，复核证据引用准确率与评分一致性。"
    )
    lines.append("")
    lines.append("---")
    lines.append(
        "*本报告由 AgentValue-AI 公平性审计月报脚本自动生成，结论供 HRBP 与审计师复核，不直接产生人事决策。*"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """命令行入口：生成月报并写出 JSON + markdown。"""
    parser = argparse.ArgumentParser(description="AgentValue-AI 公平性审计月报")
    parser.add_argument(
        "--output",
        default="data/pilot/",
        help="JSON 报告输出目录，默认 data/pilot/",
    )
    parser.add_argument(
        "--weeks",
        type=int,
        default=4,
        help="聚合周数，默认 4",
    )
    parser.add_argument(
        "--markdown-dir",
        default=str(DEFAULT_DOCS_DIR),
        help="markdown 摘要输出目录，默认 <repo>/docs/",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20260615,
        help="造数随机种子，默认 20260615",
    )
    args = parser.parse_args(argv)

    records = generate_pilot_evaluations(weeks=args.weeks, seed=args.seed)
    report = generate_monthly_report(records=records, weeks=args.weeks)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "fairness-monthly-report.json"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"JSON 月报已写入: {json_path}")

    md_dir = Path(args.markdown_dir)
    md_dir.mkdir(parents=True, exist_ok=True)
    md_path = md_dir / "fairness-monthly.md"
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(f"Markdown 月报已写入: {md_path}")

    # 控制台简要摘要
    print(f"\n评估总样本: {report['total_evaluations']}")
    for dim in report["dimensions"]:
        s = report["by_dimension"][dim]
        flag = "⚠️" if s["has_risk"] else "✅"
        print(
            f"  {flag} {dim}: max_gap={s['max_gap']:.2f} ({s['max_group']} vs {s['min_group']})"
        )
    dr = report["dual_reporting_focus"]
    dr_flag = (
        "⚠️ 偏低"
        if dr["systematically_lower"]
        else ("⚠️ 偏高" if dr["systematically_higher"] else "✅ 正常")
    )
    print(f"  双线汇报专项: gap={dr['gap']:.2f} {dr_flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
