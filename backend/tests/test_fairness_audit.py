"""
公平性审计脚本测试

覆盖 scripts.fairness_audit.audit_fairness 与命令行入口，
使用合成数据，不依赖真实数据库。
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.fairness_audit import audit_fairness, audit_fairness_cross, main

# backend 根目录（用于以 `python -m` 方式运行命令行入口）
BACKEND_DIR = Path(__file__).resolve().parent.parent


def _records_by_department():
    """构造按部门分组的合成评估记录。"""
    return [
        {
            "employee_id": "E001",
            "department": "Engineering",
            "level": "L2",
            "overall_score": 80,
        },
        {
            "employee_id": "E002",
            "department": "Engineering",
            "level": "L2",
            "overall_score": 82,
        },
        {
            "employee_id": "E003",
            "department": "Engineering",
            "level": "L3",
            "overall_score": 84,
        },
        {
            "employee_id": "E004",
            "department": "Sales",
            "level": "L2",
            "overall_score": 70,
        },
        {
            "employee_id": "E005",
            "department": "Sales",
            "level": "L2",
            "overall_score": 72,
        },
        {
            "employee_id": "E006",
            "department": "Sales",
            "level": "L3",
            "overall_score": 74,
        },
    ]


class TestAuditFairness:
    """audit_fairness 核心统计逻辑。"""

    def test_multi_group_stats_correct(self):
        """多群体分组、均值/标准差/最大差异计算正确。"""
        report = audit_fairness(_records_by_department(), threshold=10.0)

        # 维度应选 department（首条记录含 department）
        assert report["details"]["dimension"] == "department"

        groups = report["groups"]
        assert set(groups.keys()) == {"Engineering", "Sales"}

        eng = groups["Engineering"]
        sal = groups["Sales"]

        # Engineering: [80,82,84] -> mean=82, 样本标准差=2.0, min=80, max=84
        assert eng["count"] == 3
        assert eng["mean"] == pytest.approx(82.0)
        assert eng["std"] == pytest.approx(2.0)
        assert eng["min"] == pytest.approx(80.0)
        assert eng["max"] == pytest.approx(84.0)

        # Sales: [70,72,74] -> mean=72, 样本标准差=2.0
        assert sal["count"] == 3
        assert sal["mean"] == pytest.approx(72.0)
        assert sal["std"] == pytest.approx(2.0)

        # max_gap = 82 - 72 = 10
        assert report["max_gap"] == pytest.approx(10.0)
        # 阈值 10，> 10 才告警，故 10 不告警
        assert report["has_risk"] is False

        # 偏差比 = 82 / 72
        assert report["details"]["deviation_ratio"] == pytest.approx(82.0 / 72.0)
        assert report["details"]["max_group"] == "Engineering"
        assert report["details"]["min_group"] == "Sales"

    def test_risk_when_gap_exceeds_threshold(self):
        """群体间均值差超阈值 -> has_risk=True。"""
        records = [
            {"employee_id": "E001", "department": "Engineering", "overall_score": 85},
            {"employee_id": "E002", "department": "Engineering", "overall_score": 87},
            {"employee_id": "E003", "department": "Engineering", "overall_score": 89},
            {"employee_id": "E004", "department": "Sales", "overall_score": 70},
            {"employee_id": "E005", "department": "Sales", "overall_score": 72},
            {"employee_id": "E006", "department": "Sales", "overall_score": 74},
        ]
        # Engineering mean=87, Sales mean=72, gap=15 > 10
        report = audit_fairness(records, threshold=10.0)
        assert report["max_gap"] == pytest.approx(15.0)
        assert report["has_risk"] is True

    def test_no_risk_when_gap_within_threshold(self):
        """群体间均值差在阈值内 -> has_risk=False。"""
        records = [
            {"employee_id": "E001", "department": "Engineering", "overall_score": 80},
            {"employee_id": "E002", "department": "Engineering", "overall_score": 81},
            {"employee_id": "E003", "department": "Sales", "overall_score": 78},
            {"employee_id": "E004", "department": "Sales", "overall_score": 79},
        ]
        # Engineering mean=80.5, Sales mean=78.5, gap=2.0 < 10
        report = audit_fairness(records, threshold=10.0)
        assert report["max_gap"] == pytest.approx(2.0)
        assert report["has_risk"] is False

    def test_empty_records_returns_empty_report(self):
        """空数据列表 -> 返回空报告不崩溃。"""
        report = audit_fairness([], threshold=10.0)
        assert report["groups"] == {}
        assert report["max_gap"] == 0.0
        assert report["has_risk"] is False
        assert report["details"]["group_means"] == {}
        assert report["details"]["deviation_ratio"] is None

    def test_single_group_no_gap(self):
        """单群体 -> max_gap=0, has_risk=False。"""
        records = [
            {"employee_id": "E001", "department": "Engineering", "overall_score": 80},
            {"employee_id": "E002", "department": "Engineering", "overall_score": 90},
        ]
        report = audit_fairness(records, threshold=10.0)
        assert len(report["groups"]) == 1
        assert "Engineering" in report["groups"]
        assert report["max_gap"] == 0.0
        assert report["has_risk"] is False
        # 单群体偏差比为 1.0
        assert report["details"]["deviation_ratio"] == pytest.approx(1.0)

    def test_fallback_to_level_dimension(self):
        """无 department 字段时按 level 分组。"""
        records = [
            {"employee_id": "E001", "level": "L2", "overall_score": 80},
            {"employee_id": "E002", "level": "L3", "overall_score": 90},
        ]
        report = audit_fairness(records, threshold=10.0)
        assert report["details"]["dimension"] == "level"
        assert set(report["groups"].keys()) == {"L2", "L3"}
        assert report["max_gap"] == pytest.approx(10.0)

    def test_fallback_to_employee_id_initial(self):
        """无 department 与 level 时按 employee_id 首字母做基线分组。"""
        records = [
            {"employee_id": "A001", "overall_score": 80},
            {"employee_id": "B001", "overall_score": 95},
        ]
        report = audit_fairness(records, threshold=10.0)
        assert report["details"]["dimension"] == "employee_id_initial"
        assert set(report["groups"].keys()) == {"A", "B"}
        assert report["max_gap"] == pytest.approx(15.0)
        assert report["has_risk"] is True


class TestCliEntry:
    """命令行入口 python -m scripts.fairness_audit。"""

    def test_cli_runs_and_writes_report(self, tmp_path):
        """命令行入口可跑通：读取 JSON、打印报告、写出 report.json。"""
        records = _records_by_department()
        input_path = tmp_path / "records.json"
        input_path.write_text(json.dumps(records), encoding="utf-8")
        report_path = tmp_path / "report.json"

        # 以 `python -m scripts.fairness_audit` 真实命令行方式运行
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.fairness_audit",
                "--input",
                str(input_path),
                "--output",
                str(report_path),
            ],
            cwd=str(BACKEND_DIR),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"CLI 异常: {proc.stderr}"
        # 报告文件已写出
        assert report_path.exists()
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert "groups" in report
        assert "max_gap" in report
        assert "has_risk" in report
        # 标准输出包含报告标题
        assert "公平性审计报告" in proc.stdout

    def test_main_with_argv(self, tmp_path, capsys):
        """直接调用 main(argv) 也可跑通。"""
        records = _records_by_department()
        input_path = tmp_path / "records.json"
        input_path.write_text(json.dumps(records), encoding="utf-8")
        report_path = tmp_path / "report.json"

        ret = main(["--input", str(input_path), "--output", str(report_path)])
        assert ret == 0
        assert report_path.exists()
        captured = capsys.readouterr()
        assert "公平性审计报告" in captured.out


# ---------------- M4：多维交叉公平性审计 ----------------


class TestAuditFairnessCross:
    """audit_fairness_cross 部门×职级交叉分组与小样本过滤。"""

    def _records_cross(self):
        """构造部门×职级交叉分组数据：两个大样本群体 + 一个小样本群体。"""
        records = []
        # Engineering×L2: 6 条，均值 85
        for i in range(6):
            records.append(
                {
                    "employee_id": f"ENG2-{i}",
                    "department": "Engineering",
                    "level": "L2",
                    "overall_score": 85,
                }
            )
        # Sales×L2: 6 条，均值 70
        for i in range(6):
            records.append(
                {
                    "employee_id": f"SAL2-{i}",
                    "department": "Sales",
                    "level": "L2",
                    "overall_score": 70,
                }
            )
        # Engineering×L3: 2 条（小样本），均值 95
        for i in range(2):
            records.append(
                {
                    "employee_id": f"ENG3-{i}",
                    "department": "Engineering",
                    "level": "L3",
                    "overall_score": 95,
                }
            )
        return records

    def test_cross_groups_by_department_x_level(self):
        """交叉分组应按 department×level 聚合，键含 × 分隔符"""
        report = audit_fairness_cross(self._records_cross(), threshold=10.0)
        assert report["details"]["dimensions"] == ["department", "level"]
        # 大样本群体应含 Engineering×L2 与 Sales×L2
        assert "Engineering×L2" in report["groups"]
        assert "Sales×L2" in report["groups"]
        # 小样本群体单独列出
        assert "Engineering×L3" in report["small_samples"]
        assert report["small_samples"]["Engineering×L3"]["count"] == 2

    def test_small_samples_excluded_from_risk(self):
        """小样本群体不参与 max_gap 风险判定"""
        report = audit_fairness_cross(self._records_cross(), threshold=10.0)
        # 仅大样本群体间比较：85 vs 70 = gap 15
        assert report["max_gap"] == pytest.approx(15.0)
        assert report["has_risk"] is True
        # 小样本群体均值为 95，若纳入会扩大 gap，但被排除
        assert "Engineering×L3" not in report["details"]["group_means"]

    def test_custom_min_sample_threshold(self):
        """min_sample=2 时原小样本群体进入大样本组，参与风险判定"""
        report = audit_fairness_cross(
            self._records_cross(), threshold=10.0, min_sample=2
        )
        # min_sample=2，Engineering×L3（count=2）不再属于小样本
        assert "Engineering×L3" not in report["small_samples"]
        assert "Engineering×L3" in report["groups"]
        # 三群体：85 vs 70 vs 95，max=95, min=70, gap=25
        assert report["max_gap"] == pytest.approx(25.0)

    def test_cross_empty_records(self):
        """空数据返回空报告不崩溃"""
        report = audit_fairness_cross([], threshold=10.0)
        assert report["groups"] == {}
        assert report["small_samples"] == {}
        assert report["max_gap"] == 0.0
        assert report["has_risk"] is False

    def test_cross_single_large_group_no_risk(self):
        """仅一个大样本群体时 max_gap=0，无风险"""
        records = [
            {
                "employee_id": f"E{i}",
                "department": "Eng",
                "level": "L2",
                "overall_score": 80,
            }
            for i in range(6)
        ]
        report = audit_fairness_cross(records, threshold=10.0)
        assert len(report["groups"]) == 1
        assert report["max_gap"] == 0.0
        assert report["has_risk"] is False

    def test_cross_all_small_samples(self):
        """全部群体均为小样本时，groups 为空，max_gap=0"""
        records = [
            {
                "employee_id": "E1",
                "department": "Eng",
                "level": "L2",
                "overall_score": 80,
            },
            {
                "employee_id": "E2",
                "department": "Sales",
                "level": "L3",
                "overall_score": 90,
            },
        ]
        report = audit_fairness_cross(records, threshold=10.0)
        assert report["groups"] == {}
        assert len(report["small_samples"]) == 2
        assert report["max_gap"] == 0.0
        assert report["has_risk"] is False

    def test_cross_missing_dimensions_uses_unknown(self):
        """缺少 department/level 时归入 unknown×unknown 分组"""
        records = [{"employee_id": f"E{i}", "overall_score": 80} for i in range(6)]
        report = audit_fairness_cross(records, threshold=10.0)
        assert "unknown×unknown" in report["groups"]
        assert report["groups"]["unknown×unknown"]["count"] == 6
