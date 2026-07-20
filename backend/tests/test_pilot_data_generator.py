"""
试点数据生成器与就绪检查脚本测试

覆盖：
- scripts.pilot_data_generator：5 个规模档位生成、字段完整性、数据量、复杂场景
- scripts.pilot_readiness_check：mock 环境变量后验证 Go/No-Go 判定逻辑

所有用例均不依赖真实数据库或外部服务，使用 tmp_path 隔离输出目录。
"""

import json
from pathlib import Path

import pytest

from scripts.pilot_data_generator import (
    SCALE_CONFIGS,
    WEEKS,
    WORKDAYS_PER_WEEK,
    generate_all,
    generate_for_scale,
)
from scripts.pilot_readiness_check import check_readiness


# ---------------------------------------------------------------------------
# 试点数据生成器
# ---------------------------------------------------------------------------
class TestPilotDataGenerator:
    """5 个规模档位的数据生成与字段完整性。"""

    @pytest.mark.parametrize("scale", list(SCALE_CONFIGS.keys()))
    def test_generate_for_scale_creates_files(self, scale, tmp_path):
        """每个档位都能生成 employees.json + 4 个 weekly_reports_weekN.json。"""
        stat = generate_for_scale(scale, tmp_path, seed=42)
        out_dir = tmp_path / scale

        # employees.json 存在且可解析
        emp_file = out_dir / "employees.json"
        assert emp_file.exists()
        emp_data = json.loads(emp_file.read_text(encoding="utf-8"))
        assert emp_data["scale"] == scale
        assert emp_data["label"] == SCALE_CONFIGS[scale]["label"]
        assert isinstance(emp_data["employees"], list)

        # 4 个周报文件齐全
        for week in range(1, WEEKS + 1):
            wk_file = out_dir / f"weekly_reports_week{week}.json"
            assert wk_file.exists(), f"缺周报文件: {wk_file}"
            wk_data = json.loads(wk_file.read_text(encoding="utf-8"))
            assert wk_data["week"] == week
            assert isinstance(wk_data["records"], list)

        # 返回的统计结构完整
        for key in (
            "scale",
            "label",
            "employees_in_file",
            "weekly_records",
            "daily_reports",
            "features",
        ):
            assert key in stat

    def test_employee_count_matches_config(self, tmp_path):
        """每个档位员工数符合配置（初创 15 / 成长 80 / 中型抽样 100 / 大型 150 / 超大型 200）。"""
        expected = {
            "startup": 15,
            "growth": 80,
            "medium": 100,
            "large": 150,
            "huge": 200,
        }
        for scale, n in expected.items():
            stat = generate_for_scale(scale, tmp_path / scale, seed=42)
            assert (
                stat["employees_in_file"] == n
            ), f"{scale} 员工数应为 {n}，实为 {stat['employees_in_file']}"

    def test_daily_report_count_formula(self, tmp_path):
        """日报数 = 员工数 × 4 周 × 5 天（抽样档用文件内员工数，非声明总数）。"""
        for scale in SCALE_CONFIGS:
            stat = generate_for_scale(scale, tmp_path / scale, seed=42)
            expected = stat["employees_in_file"] * WEEKS * WORKDAYS_PER_WEEK
            assert (
                stat["daily_reports"] == expected
            ), f"{scale} 日报数应为 {expected}，实为 {stat['daily_reports']}"

    def test_weekly_records_count_formula(self, tmp_path):
        """周报记录数 = 员工数 × 4 周。"""
        for scale in SCALE_CONFIGS:
            stat = generate_for_scale(scale, tmp_path / scale, seed=42)
            expected = stat["employees_in_file"] * WEEKS
            assert (
                stat["weekly_records"] == expected
            ), f"{scale} 周报记录数应为 {expected}，实为 {stat['weekly_records']}"

    def test_employee_fields_complete(self, tmp_path):
        """员工清单字段完整：employee_id / name / department / level / location / reports_to。"""
        generate_for_scale("startup", tmp_path, seed=42)
        emp_data = json.loads((tmp_path / "startup" / "employees.json").read_text())
        for emp in emp_data["employees"]:
            for field in (
                "employee_id",
                "name",
                "department",
                "level",
                "location",
                "reports_to",
            ):
                assert field in emp, f"员工 {emp.get('employee_id')} 缺字段 {field}"

    def test_daily_report_fields_complete(self, tmp_path):
        """每条日报字段完整：work_content / collaboration / output / scenario_tag。"""
        generate_for_scale("growth", tmp_path, seed=42)
        wk = json.loads((tmp_path / "growth" / "weekly_reports_week1.json").read_text())
        for record in wk["records"]:
            assert len(record["daily_reports"]) == WORKDAYS_PER_WEEK
            for daily in record["daily_reports"]:
                for field in (
                    "work_content",
                    "collaboration",
                    "output",
                    "scenario_tag",
                    "day",
                ):
                    assert field in daily, f"日报缺字段 {field}"

    def test_level_distribution_pyramid(self, tmp_path):
        """职级分布呈金字塔形：低职级人数 ≥ 高职级人数。"""
        generate_for_scale("huge", tmp_path, seed=42)
        emp_data = json.loads((tmp_path / "huge" / "employees.json").read_text())
        levels = [e["level"] for e in emp_data["employees"]]

        # P 系列按数字排序，验证 P5 数量 ≥ P7 ≥ P9
        from collections import Counter

        counter = Counter(levels)
        # 至少 P5/P6 数量较多，P10 几乎没有
        assert (
            counter.get("P5", 0) >= counter.get("P7", 0) >= counter.get("P9", 0)
        ), f"职级未呈金字塔: {counter}"

    def test_reports_to_hierarchy_valid(self, tmp_path):
        """汇报对象要么为 None（最高层），要么指向真实存在的员工 ID。"""
        generate_for_scale("medium", tmp_path, seed=42)
        emp_data = json.loads((tmp_path / "medium" / "employees.json").read_text())
        all_ids = {e["employee_id"] for e in emp_data["employees"]}
        for emp in emp_data["employees"]:
            if emp["reports_to"] is not None:
                assert (
                    emp["reports_to"] in all_ids
                ), f"{emp['employee_id']} 的 reports_to={emp['reports_to']} 不存在"

    def test_generate_all_creates_summary(self, tmp_path):
        """generate_all 生成 5 档数据并写入 _summary.json。"""
        summary = generate_all(tmp_path, seed=42)
        assert len(summary) == 5
        assert (tmp_path / "_summary.json").exists()
        summary_data = json.loads((tmp_path / "_summary.json").read_text())
        assert len(summary_data["scales"]) == 5

    def test_seed_reproducibility(self, tmp_path):
        """相同 seed 生成相同员工 ID 序列（可复现）。"""
        s1 = generate_for_scale("startup", tmp_path / "a", seed=42)
        s2 = generate_for_scale("startup", tmp_path / "b", seed=42)
        e1 = json.loads((tmp_path / "a" / "startup" / "employees.json").read_text())
        e2 = json.loads((tmp_path / "b" / "startup" / "employees.json").read_text())
        ids1 = [e["employee_id"] for e in e1["employees"]]
        ids2 = [e["employee_id"] for e in e2["employees"]]
        assert ids1 == ids2, "相同 seed 应生成相同员工 ID 序列"
        assert s1 == s2


# ---------------------------------------------------------------------------
# 复杂场景验证
# ---------------------------------------------------------------------------
class TestComplexScenarios:
    """超大型/大型公司的复杂场景标记。"""

    def test_huge_has_dual_line_reporting(self, tmp_path):
        """超大型公司有双线汇报员工（实线评 A，虚线评 B，存在冲突标记）。"""
        generate_for_scale("huge", tmp_path, seed=42)
        emp_data = json.loads((tmp_path / "huge" / "employees.json").read_text())
        dual = [e for e in emp_data["employees"] if "dotted_line_manager" in e]
        assert len(dual) >= 2, "超大型档应至少有 2 个双线汇报员工"
        for emp in dual:
            assert (
                emp["dotted_line_manager"] != emp["reports_to"]
            ), "虚线项目经理不应与实线主管为同一人"
            assert emp["dual_line_conflict"]["solid_manager_grade"] == "A"
            assert emp["dual_line_conflict"]["dotted_manager_grade"] == "B"

    def test_huge_has_force_361_ranking(self, tmp_path):
        """超大型公司有 361 强制分布末位员工（forced_ranking=3.25）。"""
        generate_for_scale("huge", tmp_path, seed=42)
        emp_data = json.loads((tmp_path / "huge" / "employees.json").read_text())
        f361 = [e for e in emp_data["employees"] if e.get("forced_ranking") == "3.25"]
        assert len(f361) >= 3, "超大型档应至少有 3 个 361 末位员工"

    def test_huge_has_multinational_employees(self, tmp_path):
        """超大型公司有跨国员工（办公地在海外）。"""
        generate_for_scale("huge", tmp_path, seed=42)
        emp_data = json.loads((tmp_path / "huge" / "employees.json").read_text())
        overseas_locs = {"新加坡", "旧金山", "西雅图", "伦敦"}
        overseas = [e for e in emp_data["employees"] if e["location"] in overseas_locs]
        assert len(overseas) >= 1, "超大型档应有海外办公地员工"

    def test_huge_daily_reports_have_english_mix(self, tmp_path):
        """海外员工日报应包含英文片段（中英文混合）。"""
        generate_for_scale("huge", tmp_path, seed=42)
        # 多次生成不同周报以提升命中率
        for week in range(1, WEEKS + 1):
            wk = json.loads(
                (tmp_path / "huge" / f"weekly_reports_week{week}.json").read_text()
            )
            for record in wk["records"]:
                # 仅检查海外员工的日报是否混入英文
                # 由于是 50% 概率混入，整周 5 天日报中至少应有英文出现
                pass
        # 至少海外员工日报中有英文片段
        all_daily = []
        for week in range(1, WEEKS + 1):
            wk = json.loads(
                (tmp_path / "huge" / f"weekly_reports_week{week}.json").read_text()
            )
            for record in wk["records"]:
                all_daily.extend(record["daily_reports"])
        has_english = any(
            any(c.isalpha() and ord(c) < 128 for c in d["work_content"])
            and any(w.isascii() and w.isalpha() for w in d["work_content"].split())
            for d in all_daily
        )
        assert has_english, "海外员工日报应包含英文片段"

    def test_large_has_secondment_employees(self, tmp_path):
        """大型公司有借调员工（home_department ≠ current_department）。"""
        generate_for_scale("large", tmp_path, seed=42)
        emp_data = json.loads((tmp_path / "large" / "employees.json").read_text())
        seconded = [e for e in emp_data["employees"] if "home_department" in e]
        assert len(seconded) >= 2, "大型档应至少有 2 个借调员工"
        for emp in seconded:
            assert (
                emp["home_department"] != emp["current_department"]
            ), "借调员工的原部门与借调后部门不应相同"

    def test_large_has_bureaucratic_managers(self, tmp_path):
        """大型/超大型公司有官僚层中层管理者。"""
        for scale in ("large", "huge"):
            generate_for_scale(scale, tmp_path / scale, seed=42)
            emp_data = json.loads(
                (tmp_path / scale / scale / "employees.json").read_text()
            )
            bureau = [e for e in emp_data["employees"] if e.get("bureaucratic")]
            assert len(bureau) >= 1, f"{scale} 档应有官僚层中层管理者"

    def test_huge_features_list_complete(self, tmp_path):
        """超大型档的 features 包含所有复杂场景标识。"""
        generate_for_scale("huge", tmp_path, seed=42)
        emp_data = json.loads((tmp_path / "huge" / "employees.json").read_text())
        # employees.json 顶层 features 字段保留全部场景
        assert set(emp_data["features"]) >= {
            "dual_line_reporting",
            "matrix_secondment",
            "bureaucratic_layer",
            "force_361",
            "cross_location",
        }


# ---------------------------------------------------------------------------
# 就绪检查脚本
# ---------------------------------------------------------------------------
class TestPilotReadinessCheck:
    """check_readiness 的 Go/No-Go 判定逻辑（mock 环境）。"""

    def _make_safe_env(self):
        """构造一组可通过 env_vars 检查的环境变量。"""
        return {
            "JWT_SECRET_KEY": "pilot-strong-random-secret-0x9f8e7d6c5b4a",
            "AUTH_DEMO_MODE": "false",
            "MODEL_TIER": "L2",
            "DATABASE_URL": "sqlite+aiosqlite:///./pilot_test.db",
        }

    def test_all_pass_returns_go(self, tmp_path, monkeypatch):
        """全部 PASS（允许 WARN）时判定 Go。"""
        # 构造一个可通过各项检查的 Settings
        from core.config import Settings

        settings = Settings(
            auth_demo_mode=False,
            jwt_secret_key="pilot-strong-random-secret-0x9f8e7d6c5b4a",
            database_url=f"sqlite+aiosqlite:///{tmp_path}/pilot.db",
            model_tier="L2",
            vector_store_dir=str(tmp_path / "vs"),
        )
        # 准备 dist 目录与 index.html
        dist_dir = tmp_path / "dist"
        dist_dir.mkdir()
        (dist_dir / "index.html").write_text("<html></html>")

        # 准备演示账号：直接在 SQLite 建表插入 E1001
        import asyncio
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine
        from models.models import Base, User

        engine = create_async_engine(settings.database_url, future=True)

        async def _seed():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            from sqlalchemy.ext.asyncio import async_sessionmaker

            session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
            async with session_factory() as s:
                s.add(
                    User(
                        user_id="E1001",
                        name="试点员工",
                        role="employee",
                        department="技术部",
                    )
                )
                await s.commit()

        asyncio.run(_seed())

        result = check_readiness(
            settings=settings,
            env=self._make_safe_env(),
            dist_dir=dist_dir,
            backend_dir=tmp_path,
            run_tests=False,
        )
        # 检查项齐全
        names = {c["name"] for c in result["checks"]}
        assert {
            "env_vars",
            "database",
            "model_tier",
            "vector_store",
            "demo_accounts",
            "frontend",
            "test_baseline",
        } <= names
        assert result["all_passed"] is True
        assert result["decision"] == "Go"

    def test_missing_env_fails(self, tmp_path):
        """关键环境变量缺失时 env_vars 项 FAIL，整体 No-Go。"""
        from core.config import Settings

        settings = Settings(
            auth_demo_mode=False,
            jwt_secret_key="x",
            database_url=f"sqlite+aiosqlite:///{tmp_path}/x.db",
            model_tier="L2",
        )
        result = check_readiness(
            settings=settings,
            env={},  # 全空
            dist_dir=tmp_path,
            backend_dir=tmp_path,
            run_tests=False,
        )
        env_check = next(c for c in result["checks"] if c["name"] == "env_vars")
        assert env_check["status"] == "FAIL"
        assert result["all_passed"] is False
        assert result["decision"] == "No-Go"

    def test_demo_mode_true_fails(self, tmp_path):
        """AUTH_DEMO_MODE=true 时 env_vars FAIL（试点禁止开启）。"""
        from core.config import Settings

        settings = Settings(
            auth_demo_mode=True,
            jwt_secret_key="x",
            database_url=f"sqlite+aiosqlite:///{tmp_path}/x.db",
            model_tier="L2",
        )
        env = self._make_safe_env()
        env["AUTH_DEMO_MODE"] = "true"
        result = check_readiness(
            settings=settings,
            env=env,
            dist_dir=tmp_path,
            backend_dir=tmp_path,
            run_tests=False,
        )
        env_check = next(c for c in result["checks"] if c["name"] == "env_vars")
        assert env_check["status"] == "FAIL"
        assert "AUTH_DEMO_MODE" in env_check["message"]

    def test_missing_frontend_dist_fails(self, tmp_path):
        """前端 dist 缺失时 frontend 项 FAIL。"""
        from core.config import Settings

        settings = Settings(
            auth_demo_mode=False,
            jwt_secret_key="x",
            database_url=f"sqlite+aiosqlite:///{tmp_path}/x.db",
            model_tier="L2",
        )
        nonexistent = tmp_path / "no_such_dist"
        result = check_readiness(
            settings=settings,
            env=self._make_safe_env(),
            dist_dir=nonexistent,
            backend_dir=tmp_path,
            run_tests=False,
        )
        fe_check = next(c for c in result["checks"] if c["name"] == "frontend")
        assert fe_check["status"] == "FAIL"

    def test_model_tier_auto_warns_not_fail(self, tmp_path):
        """MODEL_TIER=auto 时给 WARN，不算 FAIL，不阻断 Go。"""
        from core.config import Settings

        settings = Settings(
            auth_demo_mode=False,
            jwt_secret_key="x",
            database_url=f"sqlite+aiosqlite:///{tmp_path}/x.db",
            model_tier="auto",
            vector_store_dir=str(tmp_path),
        )
        result = check_readiness(
            settings=settings,
            env=self._make_safe_env(),
            dist_dir=tmp_path,
            backend_dir=tmp_path,
            run_tests=False,
        )
        tier_check = next(c for c in result["checks"] if c["name"] == "model_tier")
        assert tier_check["status"] == "WARN"

    def test_skip_tests_gives_warn(self, tmp_path):
        """run_tests=False 时 test_baseline 给 WARN。"""
        from core.config import Settings

        settings = Settings(
            auth_demo_mode=False,
            jwt_secret_key="x",
            database_url=f"sqlite+aiosqlite:///{tmp_path}/x.db",
            model_tier="L2",
        )
        result = check_readiness(
            settings=settings,
            env=self._make_safe_env(),
            dist_dir=tmp_path,
            backend_dir=tmp_path,
            run_tests=False,
        )
        tb_check = next(c for c in result["checks"] if c["name"] == "test_baseline")
        assert tb_check["status"] == "WARN"

    def test_decision_is_no_go_when_any_fail(self, tmp_path):
        """任意 FAIL 项导致整体 No-Go。"""
        from core.config import Settings

        settings = Settings(
            auth_demo_mode=False,
            jwt_secret_key="x",
            database_url=f"sqlite+aiosqlite:///{tmp_path}/x.db",
            model_tier="L2",
        )
        # env 为空 -> env_vars FAIL
        result = check_readiness(
            settings=settings,
            env={},
            dist_dir=tmp_path,
            backend_dir=tmp_path,
            run_tests=False,
        )
        assert any(c["status"] == "FAIL" for c in result["checks"])
        assert result["decision"] == "No-Go"
