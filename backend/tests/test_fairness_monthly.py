"""
公平性审计月报脚本测试。

覆盖 generate_monthly_report 报告结构、group_stats 统计正确性、
4 周趋势聚合、双线汇报员工标记，以及 markdown 渲染关键节。
使用合成数据，不依赖真实数据库。
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.run_fairness_monthly import (
    compute_trend,
    dual_reporting_focus,
    generate_monthly_report,
    generate_pilot_evaluations,
    group_stats,
    main,
    render_markdown,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _records_two_departments():
    """构造两部门合成记录，用于统计正确性验证。"""
    return [
        {
            "employee_id": "E001",
            "period": "2026-W25",
            "department": "Engineering",
            "level": "L2",
            "gender": "M",
            "office": "Beijing",
            "dual_reporting": False,
            "overall_score": 80,
        },
        {
            "employee_id": "E002",
            "period": "2026-W25",
            "department": "Engineering",
            "level": "L3",
            "gender": "F",
            "office": "Shanghai",
            "dual_reporting": False,
            "overall_score": 84,
        },
        {
            "employee_id": "E003",
            "period": "2026-W25",
            "department": "Sales",
            "level": "L2",
            "gender": "M",
            "office": "Beijing",
            "dual_reporting": False,
            "overall_score": 70,
        },
        {
            "employee_id": "E004",
            "period": "2026-W25",
            "department": "Sales",
            "level": "L3",
            "gender": "F",
            "office": "Shenzhen",
            "dual_reporting": False,
            "overall_score": 74,
        },
    ]


class TestGroupStats:
    """单维度分组统计正确性。"""

    def test_stats_correct(self):
        stats = group_stats(_records_two_departments(), "department")
        eng = stats["groups"]["Engineering"]
        sal = stats["groups"]["Sales"]
        # Engineering: [80, 84] -> mean=82, median=82, std=2.83(n-1)
        assert eng["count"] == 2
        assert eng["mean"] == 82.0
        assert eng["median"] == 82.0
        assert eng["std"] == pytest.approx(2.83, abs=0.01)
        # Sales: [70, 74] -> mean=72
        assert sal["mean"] == 72.0
        # max_gap = 82 - 72 = 10 > 5 阈值 -> 有风险
        assert stats["max_gap"] == 10.0
        assert stats["has_risk"] is True
        assert stats["max_group"] == "Engineering"
        assert stats["min_group"] == "Sales"

    def test_empty_records(self):
        stats = group_stats([], "department")
        assert stats["groups"] == {}
        assert stats["max_gap"] == 0.0
        assert stats["has_risk"] is False


class TestComputeTrend:
    """4 周趋势聚合正确性。"""

    def test_trend_has_one_entry_per_week(self):
        records = []
        for w in range(4):
            period = f"2026-W{25 + w}"
            records.append(
                {
                    "employee_id": "E001",
                    "period": period,
                    "department": "Engineering",
                    "level": "L2",
                    "gender": "M",
                    "office": "Beijing",
                    "dual_reporting": False,
                    "overall_score": 70 + w,
                }
            )
            records.append(
                {
                    "employee_id": "E002",
                    "period": period,
                    "department": "Sales",
                    "level": "L2",
                    "gender": "F",
                    "office": "Shanghai",
                    "dual_reporting": False,
                    "overall_score": 80 + w,
                }
            )
        week_labels = [f"2026-W{25 + w}" for w in range(4)]
        trend = compute_trend(records, "department", week_labels)
        # 每组应有 4 个周次值
        assert trend["Engineering"] == [70.0, 71.0, 72.0, 73.0]
        assert trend["Sales"] == [80.0, 81.0, 82.0, 83.0]


class TestDualReportingFocus:
    """双线汇报员工标记。"""

    def test_systematically_lower_detected(self):
        """双线汇报员工评分系统性偏低 → systematically_lower=True。"""
        records = []
        # 5 名非双线汇报，高分
        for i in range(5):
            records.append(
                {
                    "employee_id": f"E{i}",
                    "period": "2026-W25",
                    "department": "Engineering",
                    "level": "L2",
                    "gender": "M",
                    "office": "Beijing",
                    "dual_reporting": False,
                    "overall_score": 85,
                }
            )
        # 5 名双线汇报，低 8 分
        for i in range(5, 10):
            records.append(
                {
                    "employee_id": f"E{i}",
                    "period": "2026-W25",
                    "department": "Engineering",
                    "level": "L2",
                    "gender": "F",
                    "office": "Beijing",
                    "dual_reporting": True,
                    "overall_score": 77,
                }
            )
        focus = dual_reporting_focus(records)
        assert focus["dual_reporting_count"] == 5
        assert focus["non_dual_reporting_count"] == 5
        assert focus["gap"] == pytest.approx(-8.0, abs=0.01)
        assert focus["systematically_lower"] is True
        assert focus["systematically_higher"] is False

    def test_no_bias_when_balanced(self):
        """双线与非双线均值接近 → 无系统性偏置。"""
        records = []
        for i in range(5):
            records.append(
                {
                    "employee_id": f"E{i}",
                    "period": "2026-W25",
                    "department": "X",
                    "level": "L2",
                    "gender": "M",
                    "office": "X",
                    "dual_reporting": False,
                    "overall_score": 80,
                }
            )
            records.append(
                {
                    "employee_id": f"E{i + 10}",
                    "period": "2026-W25",
                    "department": "X",
                    "level": "L2",
                    "gender": "F",
                    "office": "X",
                    "dual_reporting": True,
                    "overall_score": 80,
                }
            )
        focus = dual_reporting_focus(records)
        assert focus["gap"] == pytest.approx(0.0, abs=0.01)
        assert focus["systematically_lower"] is False
        assert focus["systematically_higher"] is False


class TestGenerateMonthlyReport:
    """月报生成结构完整性。"""

    def test_report_structure_complete(self):
        report = generate_monthly_report(weeks=4)
        for key in (
            "generated_at",
            "weeks",
            "dimensions",
            "total_evaluations",
            "overall",
            "by_dimension",
            "trend",
            "dual_reporting_focus",
        ):
            assert key in report, f"报告缺少 {key}"
        assert report["total_evaluations"] > 0
        assert len(report["weeks"]) == 4
        # 四个维度都应有分组统计
        for dim in report["dimensions"]:
            assert dim in report["by_dimension"]
            assert "groups" in report["by_dimension"][dim]
            assert "max_gap" in report["by_dimension"][dim]
        # 趋势每周一个值
        for dim in report["dimensions"]:
            for group, means in report["trend"][dim].items():
                assert len(means) == 4

    def test_report_with_custom_records(self):
        """传入自定义 records 时不调用造数，直接统计。"""
        records = _records_two_departments()
        report = generate_monthly_report(records=records)
        assert report["total_evaluations"] == 4
        assert report["by_dimension"]["department"]["max_gap"] == 10.0

    def test_generated_data_is_deterministic(self):
        """同 seed 造数结果一致。"""
        a = generate_pilot_evaluations(weeks=4, seed=20260615)
        b = generate_pilot_evaluations(weeks=4, seed=20260615)
        assert a == b
        # 双线汇报员工存在
        assert any(r["dual_reporting"] for r in a)


class TestRenderMarkdown:
    """markdown 渲染含关键节。"""

    def test_markdown_contains_key_sections(self):
        report = generate_monthly_report(weeks=4)
        md = render_markdown(report)
        # 关键节标题
        assert "公平性审计月报" in md
        assert "整体指标" in md
        assert "4 周趋势" in md
        assert "双线汇报员工专项检查" in md
        assert "结论与建议" in md
        # 含数据：周次与样本数
        assert "2026-W25" in md
        assert str(report["total_evaluations"]) in md

    def test_markdown_with_dual_reporting_finding(self):
        """当双线汇报被系统性压低时，markdown 含偏低告警。"""
        records = []
        for i in range(6):
            records.append(
                {
                    "employee_id": f"E{i}",
                    "period": "2026-W25",
                    "department": "Engineering",
                    "level": "L2",
                    "gender": "M",
                    "office": "Beijing",
                    "dual_reporting": False,
                    "overall_score": 85,
                }
            )
        for i in range(6, 12):
            records.append(
                {
                    "employee_id": f"E{i}",
                    "period": "2026-W25",
                    "department": "Engineering",
                    "level": "L2",
                    "gender": "F",
                    "office": "Beijing",
                    "dual_reporting": True,
                    "overall_score": 75,
                }
            )
        report = generate_monthly_report(records=records)
        md = render_markdown(report)
        assert "系统性偏低" in md


class TestCliEntry:
    """命令行入口 python -m scripts.run_fairness_monthly。"""

    def test_cli_runs_and_writes_reports(self, tmp_path):
        md_dir = tmp_path / "md"
        ret = main(
            [
                "--weeks",
                "2",
                "--output",
                str(tmp_path),
                "--markdown-dir",
                str(md_dir),
            ]
        )
        assert ret == 0
        assert (tmp_path / "fairness-monthly-report.json").exists()
        assert (md_dir / "fairness-monthly.md").exists()
        report = json.loads(
            (tmp_path / "fairness-monthly-report.json").read_text(encoding="utf-8")
        )
        assert "by_dimension" in report
        assert "dual_reporting_focus" in report

    def test_cli_subprocess_invocation(self, tmp_path):
        """以真实 python -m 子进程方式运行，退出码 0。"""
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.run_fairness_monthly",
                "--weeks",
                "2",
                "--output",
                str(tmp_path),
                "--markdown-dir",
                str(tmp_path),
            ],
            cwd=str(BACKEND_DIR),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"CLI 异常: {proc.stderr}"
        assert (tmp_path / "fairness-monthly-report.json").exists()
