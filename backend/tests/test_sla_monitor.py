"""
申诉处理 SLA 监控脚本测试。

覆盖 compute_sla 的达成/超时判定、72 小时边界、空列表与全超时场景，
以及 group_stats 分组与 generate_sla_report 报告结构完整性。
使用合成数据，不依赖真实数据库。
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.sla_monitor import compute_sla, generate_sla_report, group_stats, main

# backend 根目录（用于以 python -m 方式运行命令行入口）
BACKEND_DIR = Path(__file__).resolve().parent.parent

NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)


def _appeal(
    appeal_time,
    resolved_time=None,
    status="resolved",
    department="Engineering",
    role="IC",
    appeal_id="AP-1",
    employee_id="E0001",
    evaluation_id="EV-1",
):
    """构造一条申诉记录。"""
    return {
        "appeal_id": appeal_id,
        "employee_id": employee_id,
        "evaluation_id": evaluation_id,
        "department": department,
        "role": role,
        "appeal_time": (
            appeal_time.isoformat()
            if isinstance(appeal_time, datetime)
            else appeal_time
        ),
        "resolved_time": (
            resolved_time.isoformat()
            if isinstance(resolved_time, datetime)
            else resolved_time
        ),
        "status": status,
    }


class TestComputeSla:
    """compute_sla 核心达成/超时判定。"""

    def test_met_within_72h(self):
        """已解决且处理时长 <72h → 达成。"""
        appeal_time = datetime(2026, 6, 20, 9, 0, 0, tzinfo=timezone.utc)
        resolved_time = appeal_time + timedelta(hours=48)
        result = compute_sla([_appeal(appeal_time, resolved_time)], now=NOW)
        assert result["met"] == 1
        assert result["breached"] == 0
        assert result["achievement_rate"] == 100.0

    def test_breached_over_72h(self):
        """已解决但处理时长 >72h → 超时。"""
        appeal_time = datetime(2026, 6, 20, 9, 0, 0, tzinfo=timezone.utc)
        resolved_time = appeal_time + timedelta(hours=100)
        result = compute_sla([_appeal(appeal_time, resolved_time)], now=NOW)
        assert result["met"] == 0
        assert result["breached"] == 1
        assert result["achievement_rate"] == 0.0
        assert len(result["breaches"]) == 1

    def test_boundary_exactly_72h_is_met(self):
        """正好 72 小时 → 视为达成（≤）。"""
        appeal_time = datetime(2026, 6, 20, 9, 0, 0, tzinfo=timezone.utc)
        resolved_time = appeal_time + timedelta(hours=72)
        result = compute_sla([_appeal(appeal_time, resolved_time)], now=NOW)
        assert result["met"] == 1
        assert result["breached"] == 0
        assert result["case_details"][0]["hours"] == pytest.approx(72.0)

    def test_empty_list(self):
        """空申诉列表 → total 0，达成率 100（无已决分母）。"""
        result = compute_sla([], now=NOW)
        assert result["total"] == 0
        assert result["met"] == 0
        assert result["breached"] == 0
        assert result["achievement_rate"] == 100.0
        assert result["breaches"] == []

    def test_all_breached(self):
        """全部超时 → 达成率 0。"""
        appeals = []
        for i in range(3):
            appeal_time = datetime(2026, 6, 15, 9, 0, 0, tzinfo=timezone.utc)
            resolved_time = appeal_time + timedelta(hours=120 + i * 10)
            appeals.append(
                _appeal(
                    appeal_time,
                    resolved_time,
                    appeal_id=f"AP-{i}",
                    employee_id=f"E{i}",
                    evaluation_id=f"EV-{i}",
                )
            )
        result = compute_sla(appeals, now=NOW)
        assert result["met"] == 0
        assert result["breached"] == 3
        assert result["achievement_rate"] == 0.0
        assert len(result["breaches"]) == 3

    def test_open_overdue_is_breached(self):
        """未解决且 now-appeal >72h → 逾期计入超时。"""
        appeal_time = NOW - timedelta(hours=100)  # 100 小时前提交，仍未解决
        result = compute_sla([_appeal(appeal_time, None, status="open")], now=NOW)
        assert result["open"] == 1
        assert result["breached"] == 1
        assert result["pending"] == 0

    def test_open_within_window_is_pending(self):
        """未解决且 now-appeal ≤72h → 仍在 SLA 窗口内（pending）。"""
        appeal_time = NOW - timedelta(hours=20)
        result = compute_sla([_appeal(appeal_time, None, status="open")], now=NOW)
        assert result["open"] == 1
        assert result["pending"] == 1
        assert result["breached"] == 0

    def test_mixed_achievement_rate(self):
        """混合：2 达成 + 1 超时 → 达成率 66.67。"""
        appeal_time = datetime(2026, 6, 20, 9, 0, 0, tzinfo=timezone.utc)
        appeals = [
            _appeal(
                appeal_time,
                appeal_time + timedelta(hours=40),
                appeal_id="AP-1",
                employee_id="E1",
                evaluation_id="EV-1",
            ),
            _appeal(
                appeal_time,
                appeal_time + timedelta(hours=60),
                appeal_id="AP-2",
                employee_id="E2",
                evaluation_id="EV-2",
            ),
            _appeal(
                appeal_time,
                appeal_time + timedelta(hours=90),
                appeal_id="AP-3",
                employee_id="E3",
                evaluation_id="EV-3",
            ),
        ]
        result = compute_sla(appeals, now=NOW)
        assert result["met"] == 2
        assert result["breached"] == 1
        assert result["achievement_rate"] == pytest.approx(66.67, abs=0.01)


class TestGroupStats:
    """分组统计正确性。"""

    def test_group_by_department(self):
        appeal_time = datetime(2026, 6, 20, 9, 0, 0, tzinfo=timezone.utc)
        appeals = [
            _appeal(
                appeal_time,
                appeal_time + timedelta(hours=40),
                department="Engineering",
                appeal_id="AP-1",
                employee_id="E1",
                evaluation_id="EV-1",
            ),
            _appeal(
                appeal_time,
                appeal_time + timedelta(hours=50),
                department="Engineering",
                appeal_id="AP-2",
                employee_id="E2",
                evaluation_id="EV-2",
            ),
            _appeal(
                appeal_time,
                appeal_time + timedelta(hours=100),
                department="Sales",
                appeal_id="AP-3",
                employee_id="E3",
                evaluation_id="EV-3",
            ),
        ]
        result = compute_sla(appeals, now=NOW)
        stats = group_stats(result, "department")
        eng = stats["groups"]["Engineering"]
        sal = stats["groups"]["Sales"]
        assert eng["total"] == 2
        assert eng["met"] == 2
        assert eng["breached"] == 0
        assert eng["achievement_rate"] == 100.0
        assert sal["total"] == 1
        assert sal["breached"] == 1
        assert sal["achievement_rate"] == 0.0

    def test_group_by_role(self):
        appeal_time = datetime(2026, 6, 20, 9, 0, 0, tzinfo=timezone.utc)
        appeals = [
            _appeal(
                appeal_time,
                appeal_time + timedelta(hours=40),
                role="IC",
                appeal_id="AP-1",
                employee_id="E1",
                evaluation_id="EV-1",
            ),
            _appeal(
                appeal_time,
                appeal_time + timedelta(hours=100),
                role="Manager",
                appeal_id="AP-2",
                employee_id="E2",
                evaluation_id="EV-2",
            ),
        ]
        result = compute_sla(appeals, now=NOW)
        stats = group_stats(result, "role")
        assert stats["groups"]["IC"]["met"] == 1
        assert stats["groups"]["Manager"]["breached"] == 1


class TestGenerateSlaReport:
    """报告结构完整性。"""

    def test_report_has_required_keys(self):
        appeal_time = datetime(2026, 6, 20, 9, 0, 0, tzinfo=timezone.utc)
        appeals = [
            _appeal(
                appeal_time,
                appeal_time + timedelta(hours=40),
                appeal_id="AP-1",
                employee_id="E1",
                evaluation_id="EV-1",
                department="Engineering",
            ),
        ]
        report = generate_sla_report(appeals=appeals, now=NOW)
        for key in (
            "generated_at",
            "weeks",
            "sla_hours",
            "total_appeals",
            "summary",
            "by_department",
            "by_role",
            "by_week",
            "breaches",
        ):
            assert key in report, f"报告缺少 {key}"
        assert report["sla_hours"] == 72
        assert report["summary"]["met"] == 1
        assert report["summary"]["achievement_rate"] == 100.0

    def test_report_with_generated_appeals(self):
        """用造数函数生成 4 周数据，报告结构完整且非空。"""
        report = generate_sla_report(weeks=4)
        assert report["total_appeals"] > 0
        assert len(report["weeks"]) == 4
        assert "by_department" in report and report["by_department"]["groups"]
        assert "by_role" in report
        assert isinstance(report["breaches"], list)


class TestCliEntry:
    """命令行入口 python -m scripts.sla_monitor。"""

    def test_cli_runs_and_writes_reports(self, tmp_path):
        """CLI 跑通：写出 sla-report.json 与 sla-monthly.md。"""
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
        assert (tmp_path / "sla-report.json").exists()
        assert (md_dir / "sla-monthly.md").exists()
        report = json.loads((tmp_path / "sla-report.json").read_text(encoding="utf-8"))
        assert "summary" in report
        assert "achievement_rate" in report["summary"]
        md = (md_dir / "sla-monthly.md").read_text(encoding="utf-8")
        assert "SLA 月报" in md
        assert "72 小时" in md

    def test_cli_subprocess_invocation(self, tmp_path):
        """以真实 python -m 子进程方式运行，退出码 0。"""
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.sla_monitor",
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
        assert (tmp_path / "sla-report.json").exists()
