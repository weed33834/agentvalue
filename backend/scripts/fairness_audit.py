#!/usr/bin/env python3
"""
AgentValue-AI 公平性审计脚本

对一批评估结果按群体维度统计评分偏差，输出公平性报告。

维度选择优先级：
    1. department（部门）
    2. level（职级）—— 当记录无 department 字段时
    3. employee_id 首字母 —— 当 department 与 level 都缺失时的基线分组

指标：
    - 各群体 overall_score 的均值、标准差、最小/最大值、样本数
    - 群体间均值最大差异（max - min）
    - 偏差比（max_mean / min_mean）

阈值：
    群体间均值差 > threshold（默认 10 分）标记为「潜在公平性风险」。

用法：
    cd backend
    python -m scripts.fairness_audit --input records.json [--output report.json]
"""

import argparse
import json
import statistics
import sys
from typing import Any, Iterable

from scripts._stats_utils import std


def _pick_dimension(records: Iterable[dict]) -> str:
    """根据首条记录判断分组维度：department > level > employee_id 首字母。"""
    records = list(records)
    if not records:
        return "employee_id_initial"
    sample = records[0]
    if sample.get("department"):
        return "department"
    if sample.get("level"):
        return "level"
    return "employee_id_initial"


def _group_key(record: dict, dimension: str) -> str:
    """计算单条记录所属群体键。"""
    if dimension == "department":
        return str(record.get("department") or "unknown")
    if dimension == "level":
        return str(record.get("level") or "unknown")
    # employee_id 首字母基线，统一大写
    eid = str(record.get("employee_id") or "")
    return eid[:1].upper() or "unknown"


def audit_fairness(records: list[dict], threshold: float = 10.0) -> dict:
    """
    对评估记录列表做群体公平性审计。

    参数：
        records: 评估记录列表，每条至少含 overall_score；
                 推荐 employee_id / department / level 字段。
        threshold: 群体间均值差的风险阈值（默认 10 分）。

    返回：
        dict {
            groups: {群体名: {count, mean, std, min, max}},
            max_gap: 群体间均值最大差异（max-min），
            has_risk: 是否超过阈值,
            details: 详细信息（维度、各群体均值、偏差比等）
        }
    """
    # 空数据：返回空报告，不崩溃
    if not records:
        return {
            "groups": {},
            "max_gap": 0.0,
            "has_risk": False,
            "details": {
                "dimension": "employee_id_initial",
                "threshold": threshold,
                "group_means": {},
                "max_group": None,
                "min_group": None,
                "deviation_ratio": None,
            },
        }

    dimension = _pick_dimension(records)

    # 按群体聚合 overall_score
    buckets: dict[str, list[float]] = {}
    for record in records:
        key = _group_key(record, dimension)
        score = record.get("overall_score")
        if score is None:
            # 缺失分数的记录跳过，避免污染统计
            continue
        buckets.setdefault(key, []).append(float(score))

    # 计算各群体统计量
    groups: dict[str, dict[str, Any]] = {}
    for key, scores in buckets.items():
        groups[key] = {
            "count": len(scores),
            "mean": statistics.mean(scores),
            "std": std(scores),
            "min": min(scores),
            "max": max(scores),
        }

    # 群体间均值差异
    group_means = {k: v["mean"] for k, v in groups.items()}
    if len(group_means) >= 2:
        max_group = max(group_means, key=group_means.get)
        min_group = min(group_means, key=group_means.get)
        max_mean = group_means[max_group]
        min_mean = group_means[min_group]
        max_gap = max_mean - min_mean
        # 偏差比：max_mean / min_mean；min_mean 为 0 时置 None 避免除零
        deviation_ratio = (max_mean / min_mean) if min_mean > 0 else None
    else:
        # 单群体或无有效群体
        max_group = next(iter(group_means), None)
        min_group = max_group
        max_gap = 0.0
        deviation_ratio = 1.0 if group_means else None

    has_risk = max_gap > threshold

    return {
        "groups": groups,
        "max_gap": max_gap,
        "has_risk": has_risk,
        "details": {
            "dimension": dimension,
            "threshold": threshold,
            "group_means": group_means,
            "max_group": max_group,
            "min_group": min_group,
            "deviation_ratio": deviation_ratio,
        },
    }


# M4：小样本群体阈值，低于该值的交叉分组单独列出，不参与风险判定
SMALL_SAMPLE_THRESHOLD = 5


def audit_fairness_cross(
    records: list[dict],
    threshold: float = 10.0,
    min_sample: int = SMALL_SAMPLE_THRESHOLD,
) -> dict:
    """
    M4：多维交叉公平性审计（部门 × 职级）。

    与 audit_fairness 的区别：
    - 按 department × level 交叉分组，暴露单维度无法发现的组合偏差
    - 样本数 < min_sample 的群体单独列入 small_samples，不参与 max_gap 风险判定，
      避免小样本噪声触发误报

    返回：
        dict {
            groups: 大样本群体统计（n>=min_sample）,
            small_samples: 小样本群体统计（n<min_sample）,
            max_gap: 大样本群体间均值最大差异,
            has_risk: 是否超过阈值,
            details: 详细信息（维度、各群体均值、偏差比等）
        }
    """
    if not records:
        return {
            "groups": {},
            "small_samples": {},
            "max_gap": 0.0,
            "has_risk": False,
            "details": {
                "dimensions": ["department", "level"],
                "threshold": threshold,
                "min_sample": min_sample,
                "group_means": {},
                "max_group": None,
                "min_group": None,
                "deviation_ratio": None,
            },
        }

    # 按 department × level 交叉分组
    buckets: dict[str, list[float]] = {}
    for record in records:
        dept = str(record.get("department") or "unknown")
        level = str(record.get("level") or "unknown")
        key = f"{dept}×{level}"
        score = record.get("overall_score")
        if score is None:
            continue
        buckets.setdefault(key, []).append(float(score))

    # 分离小样本群体，仅大样本群体参与风险判定
    groups: dict[str, dict[str, Any]] = {}
    small_samples: dict[str, dict[str, Any]] = {}
    for key, scores in buckets.items():
        stat = {
            "count": len(scores),
            "mean": statistics.mean(scores),
            "std": std(scores),
            "min": min(scores),
            "max": max(scores),
        }
        if len(scores) < min_sample:
            small_samples[key] = stat
        else:
            groups[key] = stat

    # 大样本群体间均值差异
    group_means = {k: v["mean"] for k, v in groups.items()}
    if len(group_means) >= 2:
        max_group = max(group_means, key=group_means.get)
        min_group = min(group_means, key=group_means.get)
        max_mean = group_means[max_group]
        min_mean = group_means[min_group]
        max_gap = max_mean - min_mean
        deviation_ratio = (max_mean / min_mean) if min_mean > 0 else None
    else:
        max_group = next(iter(group_means), None)
        min_group = max_group
        max_gap = 0.0
        deviation_ratio = 1.0 if group_means else None

    has_risk = max_gap > threshold

    return {
        "groups": groups,
        "small_samples": small_samples,
        "max_gap": max_gap,
        "has_risk": has_risk,
        "details": {
            "dimensions": ["department", "level"],
            "threshold": threshold,
            "min_sample": min_sample,
            "group_means": group_means,
            "max_group": max_group,
            "min_group": min_group,
            "deviation_ratio": deviation_ratio,
        },
    }


def print_report(report: dict) -> None:
    """以可读文本形式打印公平性报告。"""
    details = report["details"]
    print("=" * 60)
    print("公平性审计报告")
    print("=" * 60)
    print(f"分组维度: {details['dimension']}")
    print(f"风险阈值: 群体间均值差 > {details['threshold']}")
    print("-" * 60)
    print(f"{'群体':<20}{'样本数':>8}{'均值':>10}{'标准差':>10}{'最小':>8}{'最大':>8}")
    for name, stat in report["groups"].items():
        print(
            f"{name:<20}{stat['count']:>8}{stat['mean']:>10.2f}"
            f"{stat['std']:>10.2f}{stat['min']:>8.2f}{stat['max']:>8.2f}"
        )
    print("-" * 60)
    print(f"群体间均值最大差异 (max_gap): {report['max_gap']:.2f}")
    if details.get("deviation_ratio") is not None:
        print(f"偏差比 (max/min): {details['deviation_ratio']:.4f}")
    else:
        print("偏差比 (max/min): N/A")
    if report["has_risk"]:
        print("结论: ❌ 潜在公平性风险（群体间均值差超过阈值）")
    else:
        print("结论: ✅ 未超过阈值，暂无公平性风险告警")
    print("=" * 60)


def _load_records(path: str) -> list[dict]:
    """从 JSON 文件读取评估记录列表。"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        # 兼容 {"records": [...]} 或单条记录
        if "records" in data and isinstance(data["records"], list):
            return data["records"]
        return [data]
    if isinstance(data, list):
        return data
    raise ValueError(f"不支持的 JSON 结构: {type(data).__name__}")


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="AgentValue-AI 公平性审计")
    parser.add_argument("--input", required=True, help="评估记录 JSON 文件路径")
    parser.add_argument("--output", default=None, help="（可选）将报告写入该 JSON 文件")
    parser.add_argument(
        "--threshold",
        type=float,
        default=10.0,
        help="群体间均值差风险阈值，默认 10 分",
    )
    args = parser.parse_args(argv)

    records = _load_records(args.input)
    report = audit_fairness(records, threshold=args.threshold)
    print_report(report)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"报告已写入: {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
