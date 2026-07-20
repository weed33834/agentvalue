"""脚本统计工具共享模块。

抽取 fairness_audit / run_fairness_monthly / sla_monitor 中重复的
样本标准差与浮点格式化实现，避免多处复制粘贴导致行为漂移。
"""

import statistics


def std(values: list[float]) -> float:
    """样本标准差（n-1 分母）；样本数 < 2 时返回 0.0。"""
    if len(values) < 2:
        return 0.0
    return statistics.stdev(values)


def fmt_num(n: float) -> str:
    """统一浮点格式化为两位小数字符串；None 返回 'N/A'。"""
    if n is None:
        return "N/A"
    return f"{n:.2f}"
