"""
generate_dataset.py 单元测试

覆盖：
- 数据集生成/加载的所有公开函数（generate_case / main，以及与 load_dataset 的集成）
- 每类 archetype 的生成逻辑（强制选中后校验输出一致性）
- 边界情况（空输入 count=0、无效参数 idx<=0、报告不含关键词）
- 生成的数据集格式与 Schema 一致性（字段集合、分数区间容差、关键词来源、input_id 格式等）

注：generate_dataset.py 为纯同步模块，无 async 函数，故本测试文件均为同步用例。
"""

import json
import random
from pathlib import Path

import pytest

from eval import generate_dataset as gd
from eval.generate_dataset import ARCHETYPES, PERIODS, generate_case, main

# scripts/generate_dataset.py（M1 补完）模块导入，别名避免与 eval 模块符号冲突
from scripts import generate_dataset as scripts_gd
from scripts.generate_dataset import (
    ARCHETYPES as SCRIPTS_ARCHETYPES,
    DEPARTMENTS,
    LEVELS,
    PERIODS as SCRIPTS_PERIODS,
    generate_case as scripts_generate_case,
    generate_dataset as scripts_generate_dataset,
)


# 用例对象应包含的字段集合(v1.1 起,dataset 为手写双输入格式,
# 新增 expected_employee_view_tone 字段,故 EXPECTED_CASE_KEYS 包含该字段)
EXPECTED_CASE_KEYS = {
    "employee_id",
    "period",
    "archetype",
    "raw_inputs",
    "expected_overall_score_range",
    "expected_contains",
    "expected_view_keys",
    "expected_employee_view_tone",
}
# 兼容旧格式生成器的最小字段集(不含 expected_employee_view_tone)
MINIMAL_CASE_KEYS = EXPECTED_CASE_KEYS - {"expected_employee_view_tone"}
# expected_view_keys 固定常量
EXPECTED_VIEW_KEYS = ["summary", "growth_areas", "next_week_focus"]


@pytest.fixture(autouse=True)
def reset_random_seed():
    """每个用例前重置随机种子，保证 generate_case 输出可复现。"""
    random.seed(42)


@pytest.fixture
def loaded_dataset():
    """加载已生成的 dataset.json，用于回归一致性校验。"""
    dataset_path = Path(gd.__file__).with_name("dataset.json")
    with open(dataset_path, "r", encoding="utf-8") as f:
        return json.load(f)


class TestArchetypesConfig:
    """ARCHETYPES / PERIODS 配置完整性"""

    EXPECTED_ARCHETYPES = {"star", "slacker", "bottleneck", "newcomer", "workaholic"}

    def test_all_archetypes_present(self):
        """应包含 5 类画像 archetype。"""
        assert set(ARCHETYPES.keys()) == self.EXPECTED_ARCHETYPES

    @pytest.mark.parametrize("archetype", list(ARCHETYPES.keys()))
    def test_archetype_structure(self, archetype):
        """每个 archetype 配置包含 score_range / keywords / reports 三段。"""
        info = ARCHETYPES[archetype]
        assert set(info.keys()) == {"score_range", "keywords", "reports"}
        low, high = info["score_range"]
        assert (
            0 <= low <= high <= 100
        ), f"{archetype} score_range 非法: {info['score_range']}"
        assert isinstance(info["keywords"], list) and info["keywords"]
        assert isinstance(info["reports"], list) and info["reports"]

    @pytest.mark.parametrize("archetype", list(ARCHETYPES.keys()))
    def test_score_range_width(self, archetype):
        """每个 archetype 的 score_range 宽度足以体现画像差异。"""
        low, high = ARCHETYPES[archetype]["score_range"]
        assert high - low >= 10

    def test_periods_length_and_format(self):
        """PERIODS 共 10 周，格式为 2026-W20 ~ 2026-W29。"""
        assert len(PERIODS) == 10
        assert PERIODS[0] == "2026-W20"
        assert PERIODS[-1] == "2026-W29"
        for p in PERIODS:
            assert p.startswith("2026-W")


class TestGenerateCase:
    """generate_case 单条用例生成——结构、格式与不变式

    注: generate_case 输出旧格式(无 expected_employee_view_tone),
    用 MINIMAL_CASE_KEYS 校验。
    """

    def test_case_has_all_required_keys(self):
        case = generate_case(1)
        assert set(case.keys()) == MINIMAL_CASE_KEYS

    def test_employee_id_offset(self):
        """employee_id = E(1000 + idx)。"""
        assert generate_case(1)["employee_id"] == "E1001"
        assert generate_case(100)["employee_id"] == "E1100"

    def test_period_in_periods(self):
        case = generate_case(1)
        assert case["period"] in PERIODS

    def test_archetype_valid(self):
        case = generate_case(1)
        assert case["archetype"] in ARCHETYPES

    def test_raw_inputs_structure(self):
        """raw_inputs 为单元素列表，input_id 按三位零填充。"""
        case = generate_case(1)
        assert len(case["raw_inputs"]) == 1
        item = case["raw_inputs"][0]
        assert item["input_id"] == "daily-001"
        assert item["type"] == "daily_report"
        assert isinstance(item["content"], str) and item["content"]

    def test_input_id_zero_padding(self):
        assert generate_case(1)["raw_inputs"][0]["input_id"] == "daily-001"
        assert generate_case(50)["raw_inputs"][0]["input_id"] == "daily-050"

    def test_expected_view_keys_constant(self):
        """expected_view_keys 为固定常量。"""
        assert generate_case(1)["expected_view_keys"] == EXPECTED_VIEW_KEYS

    def test_score_range_tolerance_is_five(self):
        """分数区间 = [score-5, score+5]，宽度恒为 10，中心落在 archetype score_range 内。"""
        case = generate_case(1)
        low, high = case["expected_overall_score_range"]
        assert high - low == 10
        info = ARCHETYPES[case["archetype"]]
        center = (low + high) / 2
        assert info["score_range"][0] <= center <= info["score_range"][1]

    def test_expected_contains_subset_of_present_keywords(self):
        """expected_contains 必然是报告中真实命中的关键词子集。"""
        case = generate_case(1)
        info = ARCHETYPES[case["archetype"]]
        report = case["raw_inputs"][0]["content"]
        present = {kw for kw in info["keywords"] if kw in report}
        for kw in case["expected_contains"]:
            assert kw in present, f"expected_contains 出现未命中关键词: {kw}"

    def test_expected_contains_at_most_two(self):
        """expected_contains 最多取 2 个关键词。"""
        for idx in range(1, 21):
            case = generate_case(idx)
            assert len(case["expected_contains"]) <= 2

    def test_report_comes_from_archetype_pool(self):
        case = generate_case(1)
        info = ARCHETYPES[case["archetype"]]
        assert case["raw_inputs"][0]["content"] in info["reports"]


class TestArchetypeGeneration:
    """每类 archetype 的生成逻辑——通过 monkeypatch 强制选中后逐项校验"""

    @pytest.mark.parametrize("archetype", list(ARCHETYPES.keys()))
    def test_force_archetype_generation(self, monkeypatch, archetype):
        """强制选中指定 archetype + 首条报告，校验输出各字段一致性。"""
        info = ARCHETYPES[archetype]
        report = info["reports"][0]

        # 控制 random.choice 调用序列：archetype -> period -> report
        choice_seq = [archetype, PERIODS[0], report]
        monkeypatch.setattr(gd.random, "choice", lambda seq: choice_seq.pop(0))
        # randint 返回 score_range 下界，使分数可预测
        monkeypatch.setattr(gd.random, "randint", lambda a, b: a)
        # sample 取前 k 个，保持顺序确定
        monkeypatch.setattr(
            gd.random, "sample", lambda population, k: list(population)[:k]
        )

        case = generate_case(7)

        assert case["archetype"] == archetype
        assert case["raw_inputs"][0]["content"] == report

        # 分数区间中心 = score_range 下界
        low, high = case["expected_overall_score_range"]
        assert low == info["score_range"][0] - 5
        assert high == info["score_range"][0] + 5

        # expected_contains = 报告中真实命中关键词的前 min(2, n) 个
        present = [kw for kw in info["keywords"] if kw in report]
        assert case["expected_contains"] == present[: min(2, len(present))]

    @pytest.mark.parametrize("archetype", list(ARCHETYPES.keys()))
    def test_report_without_keywords_yields_empty_contains(
        self, monkeypatch, archetype
    ):
        """报告不含任何关键词时，expected_contains 应为空（避免 Mock/LLM 无法命中）。"""
        info = ARCHETYPES[archetype]
        blank_report = "本周期无任何显著信号词，仅做常规例行记录。"
        # 确认该报告确实不命中当前 archetype 的任何关键词
        assert not any(kw in blank_report for kw in info["keywords"])

        choice_seq = [archetype, PERIODS[0], blank_report]
        monkeypatch.setattr(gd.random, "choice", lambda seq: choice_seq.pop(0))
        monkeypatch.setattr(gd.random, "randint", lambda a, b: a)
        monkeypatch.setattr(
            gd.random, "sample", lambda population, k: list(population)[:k]
        )

        case = generate_case(1)
        assert case["expected_contains"] == []

    @pytest.mark.parametrize("archetype", list(ARCHETYPES.keys()))
    def test_force_archetype_with_upper_score(self, monkeypatch, archetype):
        """randint 取上界时，分数区间中心 = score_range 上界。"""
        info = ARCHETYPES[archetype]
        report = info["reports"][0]

        choice_seq = [archetype, PERIODS[0], report]
        monkeypatch.setattr(gd.random, "choice", lambda seq: choice_seq.pop(0))
        monkeypatch.setattr(gd.random, "randint", lambda a, b: b)
        monkeypatch.setattr(
            gd.random, "sample", lambda population, k: list(population)[:k]
        )

        case = generate_case(3)
        low, high = case["expected_overall_score_range"]
        assert low == info["score_range"][1] - 5
        assert high == info["score_range"][1] + 5


class TestEdgeCases:
    """边界情况：空输入、无效参数"""

    def test_generate_case_idx_zero(self):
        """idx=0 时 employee_id 退化为 E1000，input_id=daily-000。"""
        case = generate_case(0)
        assert case["employee_id"] == "E1000"
        assert case["raw_inputs"][0]["input_id"] == "daily-000"
        assert set(case.keys()) == MINIMAL_CASE_KEYS

    def test_generate_case_negative_idx(self):
        """idx=-1 不抛异常，employee_id 为 E999。"""
        case = generate_case(-1)
        assert case["employee_id"] == "E999"
        assert set(case.keys()) == MINIMAL_CASE_KEYS

    def test_generate_case_large_idx(self):
        """大 idx 不抛异常，input_id 超过三位时自然扩展。"""
        case = generate_case(9999)
        assert case["employee_id"] == "E10999"
        assert case["raw_inputs"][0]["input_id"] == "daily-9999"

    def test_main_count_zero(self, monkeypatch, tmp_path):
        """count=0 生成空数据集。"""
        monkeypatch.setattr(gd, "__file__", str(tmp_path / "generate_dataset.py"))
        main(count=0)

        out = tmp_path / "dataset.json"
        assert out.exists()
        assert json.loads(out.read_text(encoding="utf-8")) == []


class TestMain:
    """main() 端到端生成——重定向 __file__ 避免覆盖生产 dataset.json"""

    @pytest.mark.parametrize("count", [1, 5, 50])
    def test_main_writes_dataset_file(self, monkeypatch, tmp_path, count):
        """main(count) 写入 count 条用例，结构完整(旧格式,无 expected_employee_view_tone)。"""
        monkeypatch.setattr(gd, "__file__", str(tmp_path / "generate_dataset.py"))
        main(count=count)

        out = tmp_path / "dataset.json"
        assert out.exists()
        data = json.loads(out.read_text(encoding="utf-8"))
        assert len(data) == count
        for case in data:
            assert set(case.keys()) == MINIMAL_CASE_KEYS

    def test_main_default_count_is_fifty(self, monkeypatch, tmp_path):
        """main() 默认生成 50 条。"""
        monkeypatch.setattr(gd, "__file__", str(tmp_path / "generate_dataset.py"))
        main()

        out = tmp_path / "dataset.json"
        data = json.loads(out.read_text(encoding="utf-8"))
        assert len(data) == 50

    def test_main_output_loadable_by_load_dataset(self, monkeypatch, tmp_path):
        """main() 生成的文件能被 evaluate.load_dataset 正确加载（生成→加载集成）。"""
        monkeypatch.setattr(gd, "__file__", str(tmp_path / "generate_dataset.py"))
        main(count=5)

        from eval.evaluate import load_dataset

        loaded = load_dataset(str(tmp_path / "dataset.json"))
        assert len(loaded) == 5
        assert all("employee_id" in c for c in loaded)

    def test_main_does_not_clobber_production_dataset(self, monkeypatch, tmp_path):
        """重定向后调用 main() 不应修改生产 dataset.json 内容。"""
        # 在 monkeypatch 前解析真实生产文件路径
        prod_path = Path(gd.__file__).with_name("dataset.json")
        before = prod_path.read_bytes()

        monkeypatch.setattr(gd, "__file__", str(tmp_path / "generate_dataset.py"))
        main(count=3)

        after = prod_path.read_bytes()
        assert before == after, "main() 意外覆盖了生产 dataset.json"
        assert (tmp_path / "dataset.json").exists()


class TestDatasetConsistency:
    """手写 dataset.json(v1.1 起:15 条双输入,E2001-E2015)的格式与一致性回归校验。

    v1.1 dataset 由人工撰写以覆盖更真实的双输入场景(每 archetype 3 条,共 5 类 15 条),
    分数区间宽度放宽(不再固定 10),关键词源拓宽到 raw_inputs 全部内容
    (不再限于 ARCHETYPES[archetype]["keywords"] 池),并新增 expected_employee_view_tone
    字段标注三视图语气校验锚点。
    """

    def test_dataset_length(self, loaded_dataset):
        """v1.1 dataset 为 15 条手写用例(5 archetype × 3 条)。"""
        assert len(loaded_dataset) == 15

    def test_dataset_case_keys(self, loaded_dataset):
        """每条用例必须包含 v1.1 全字段集合。"""
        for case in loaded_dataset:
            assert (
                set(case.keys()) == EXPECTED_CASE_KEYS
            ), f"{case.get('employee_id')} 字段不匹配: {set(case.keys()) ^ EXPECTED_CASE_KEYS}"

    def test_dataset_employee_ids_unique_and_sequential(self, loaded_dataset):
        """employee_id 唯一且为 E2001-E2015。"""
        ids = [c["employee_id"] for c in loaded_dataset]
        assert len(ids) == len(set(ids))
        assert ids == [f"E{2000 + i}" for i in range(1, 16)]

    def test_dataset_archetypes_valid(self, loaded_dataset):
        """每条 archetype 必须在 ARCHETYPES 配置内。"""
        for case in loaded_dataset:
            assert case["archetype"] in ARCHETYPES

    def test_dataset_periods_valid(self, loaded_dataset):
        """period 必须在 PERIODS 配置内。"""
        for case in loaded_dataset:
            assert case["period"] in PERIODS

    def test_dataset_view_keys_constant(self, loaded_dataset):
        """expected_view_keys 固定常量。"""
        for case in loaded_dataset:
            assert case["expected_view_keys"] == EXPECTED_VIEW_KEYS

    def test_dataset_score_range_tolerance(self, loaded_dataset):
        """分数区间宽度 ≥10,中心落在对应 archetype score_range 内或紧邻(±5)。"""
        for case in loaded_dataset:
            low, high = case["expected_overall_score_range"]
            assert high > low, f"{case['employee_id']} 区间非法: {low, high}"
            assert high - low >= 10, f"{case['employee_id']} 区间宽度 {high - low} < 10"
            info = ARCHETYPES[case["archetype"]]
            center = (low + high) / 2
            # 允许 ±5 容差,手写用例分数中心可能与 archetype score_range 边界略有偏移
            assert info["score_range"][0] - 5 <= center <= info["score_range"][1] + 5, (
                f"{case['employee_id']} 分数中心 {center} 超出 "
                f"{case['archetype']} score_range {info['score_range']} 容差"
            )

    def test_dataset_expected_contains_source(self, loaded_dataset):
        """expected_contains 必须在 raw_inputs 全部 content 中真实命中。

        v1.1 dataset 的关键词不再限于 ARCHETYPES[archetype]["keywords"] 池,
        而是源自手写周报内容(如"重构""主导""团队"等业务关键词)。
        """
        for case in loaded_dataset:
            assert (
                len(case["expected_contains"]) <= 3
            ), f"{case['employee_id']} expected_contains 超过 3 个"
            full_text = " ".join(item["content"] for item in case["raw_inputs"])
            for kw in case["expected_contains"]:
                assert (
                    kw in full_text
                ), f"{case['employee_id']} 关键词 {kw} 未在 raw_inputs 中出现"

    def test_dataset_raw_inputs_format(self, loaded_dataset):
        """raw_inputs 为 2 项,每项含 input_id/type/content。

        v1.1 dataset 第 1 项固定为 daily_report,第 2 项为辅助输入
        (code_review / peer_feedback / incident / 1:1_record 等)。
        """
        for case in loaded_dataset:
            assert (
                len(case["raw_inputs"]) == 2
            ), f"{case['employee_id']} raw_inputs 非双输入: {len(case['raw_inputs'])}"
            for item in case["raw_inputs"]:
                assert "input_id" in item
                assert "type" in item
                assert "content" in item
                assert isinstance(item["content"], str) and item["content"]
            # 第 1 项固定 daily_report
            assert case["raw_inputs"][0]["type"] == "daily_report"

    def test_dataset_all_archetypes_covered(self, loaded_dataset):
        """15 条用例应覆盖全部 5 类 archetype(每类 3 条)。"""
        from collections import Counter

        counter = Counter(c["archetype"] for c in loaded_dataset)
        assert set(counter.keys()) == set(ARCHETYPES.keys())
        for archetype, count in counter.items():
            assert count == 3, f"archetype {archetype} 用例数 {count} != 3"

    def test_dataset_employee_view_tone_field(self, loaded_dataset):
        """v1.1 新增 expected_employee_view_tone 字段,标注三视图语气校验锚点。"""
        for case in loaded_dataset:
            tone = case["expected_employee_view_tone"]
            assert (
                isinstance(tone, str) and len(tone) >= 5
            ), f"{case['employee_id']} expected_employee_view_tone 过短或缺失"


# ---
# 以下为 scripts/generate_dataset.py（M1 补完）模块测试
# 与上方 eval/generate_dataset.py 测试共存，互不影响。
# ---


class TestScriptsArchetypesConfig:
    """scripts 模块 ARCHETYPES / DEPARTMENTS / LEVELS / PERIODS 配置完整性"""

    EXPECTED_ARCHETYPES = {"star", "steady", "slacker", "newhire", "bottleneck"}

    def test_all_five_archetypes_present(self):
        """应包含任务要求的 5 类画像 star/steady/slacker/newhire/bottleneck。"""
        assert set(SCRIPTS_ARCHETYPES.keys()) == self.EXPECTED_ARCHETYPES

    @pytest.mark.parametrize("archetype", list(SCRIPTS_ARCHETYPES.keys()))
    def test_archetype_structure(self, archetype):
        """每个 archetype 配置包含 score_range / keywords / reports 三段。"""
        info = SCRIPTS_ARCHETYPES[archetype]
        assert set(info.keys()) == {"score_range", "keywords", "reports"}
        low, high = info["score_range"]
        assert 0 <= low <= high <= 100
        assert isinstance(info["keywords"], list) and info["keywords"]
        assert isinstance(info["reports"], list) and info["reports"]

    @pytest.mark.parametrize("archetype", list(SCRIPTS_ARCHETYPES.keys()))
    def test_archetype_score_range_within_bounds(self, archetype):
        """每个 archetype 的 score_range ±5 必须落在 [0,100]，保证区间宽度恒为 10。"""
        low, high = SCRIPTS_ARCHETYPES[archetype]["score_range"]
        assert low - 5 >= 0
        assert high + 5 <= 100

    def test_departments_config(self):
        """DEPARTMENTS 多部门配置存在且非空。"""
        assert isinstance(DEPARTMENTS, list) and len(DEPARTMENTS) >= 2

    def test_levels_config(self):
        """LEVELS 多职级配置存在且非空。"""
        assert isinstance(LEVELS, list) and len(LEVELS) >= 2

    def test_periods_config(self):
        """PERIODS 为 2026-W20 ~ 2026-W29。"""
        assert SCRIPTS_PERIODS == [f"2026-W{i:02d}" for i in range(20, 30)]


class TestScriptsGenerateDataset:
    """scripts.generate_dataset 批量生成——数量、字段、画像覆盖、ID 唯一、分数合理"""

    # 任务要求每条用例必须包含的字段（允许额外字段 department/level）
    REQUIRED_CASE_KEYS = {
        "employee_id",
        "period",
        "archetype",
        "raw_inputs",
        "expected_overall_score_range",
        "expected_contains",
        "expected_view_keys",
    }
    EXPECTED_VIEW_KEYS = ["summary", "growth_areas", "next_week_focus"]

    def test_generate_dataset_default_count(self):
        """generate_dataset() 默认生成 50 条。"""
        cases = scripts_generate_dataset()
        assert len(cases) == 50

    @pytest.mark.parametrize("count", [0, 1, 5, 30, 100])
    def test_generate_dataset_custom_count(self, count):
        """指定 count 生成对应数量。"""
        assert len(scripts_generate_dataset(count=count)) == count

    def test_generate_dataset_negative_count_raises(self):
        """负数 count 抛 ValueError。"""
        with pytest.raises(ValueError):
            scripts_generate_dataset(count=-1)

    def test_generate_dataset_returns_list(self):
        """返回类型为 list。"""
        assert isinstance(scripts_generate_dataset(count=5), list)

    def test_case_has_all_required_keys(self):
        """每条用例包含全部必需字段（允许额外字段 department/level）。"""
        cases = scripts_generate_dataset(count=10)
        for case in cases:
            assert self.REQUIRED_CASE_KEYS.issubset(
                set(case.keys())
            ), f"缺失字段: {self.REQUIRED_CASE_KEYS - set(case.keys())}"

    def test_case_extra_fields_department_level(self):
        """用例额外携带 department/level，体现多部门多职级支持。"""
        cases = scripts_generate_dataset(count=10)
        for case in cases:
            assert "department" in case
            assert "level" in case
            assert case["department"] in DEPARTMENTS
            assert case["level"] in LEVELS

    def test_all_five_archetypes_covered(self):
        """count=50 时 5 类画像全部出现（轮转抽样保证覆盖）。"""
        cases = scripts_generate_dataset(count=50)
        seen = {c["archetype"] for c in cases}
        assert seen == {"star", "steady", "slacker", "newhire", "bottleneck"}

    def test_all_archetypes_covered_min_count(self):
        """count=5 时刚好每类出现一次（轮转）。"""
        cases = scripts_generate_dataset(count=5)
        seen = {c["archetype"] for c in cases}
        assert seen == {"star", "steady", "slacker", "newhire", "bottleneck"}

    def test_employee_ids_unique(self):
        """50 条用例 employee_id 全部唯一。"""
        cases = scripts_generate_dataset(count=50)
        ids = [c["employee_id"] for c in cases]
        assert len(ids) == len(set(ids))

    def test_employee_ids_sequential(self):
        """employee_id 按 E1001..E1050 顺序生成。"""
        cases = scripts_generate_dataset(count=50)
        assert [c["employee_id"] for c in cases] == [
            f"E{1000 + i}" for i in range(1, 51)
        ]

    def test_score_range_reasonable(self):
        """分数区间合理：宽度恒为 10、落在 [0,100]、中心落在对应 archetype 档位内。"""
        cases = scripts_generate_dataset(count=50)
        for case in cases:
            low, high = case["expected_overall_score_range"]
            assert 0 <= low <= high <= 100
            assert high - low == 10
            info = SCRIPTS_ARCHETYPES[case["archetype"]]
            center = (low + high) / 2
            assert info["score_range"][0] <= center <= info["score_range"][1]

    def test_expected_contains_subset_of_report(self):
        """expected_contains 必然是报告中真实命中的关键词子集。"""
        cases = scripts_generate_dataset(count=50)
        for case in cases:
            assert len(case["expected_contains"]) <= 2
            info = SCRIPTS_ARCHETYPES[case["archetype"]]
            report = case["raw_inputs"][0]["content"]
            present = {kw for kw in info["keywords"] if kw in report}
            for kw in case["expected_contains"]:
                assert (
                    kw in present
                ), f"{case['employee_id']} 关键词 {kw} 未在报告中出现"

    def test_expected_view_keys_constant(self):
        """expected_view_keys 为固定常量。"""
        cases = scripts_generate_dataset(count=5)
        for case in cases:
            assert case["expected_view_keys"] == self.EXPECTED_VIEW_KEYS

    def test_period_in_periods(self):
        """period 取自 PERIODS 配置。"""
        cases = scripts_generate_dataset(count=20)
        for case in cases:
            assert case["period"] in SCRIPTS_PERIODS

    def test_raw_inputs_structure(self):
        """raw_inputs 为单元素列表，input_id 三位零填充。"""
        cases = scripts_generate_dataset(count=5)
        for i, case in enumerate(cases, start=1):
            assert len(case["raw_inputs"]) == 1
            item = case["raw_inputs"][0]
            assert item["type"] == "daily_report"
            assert isinstance(item["content"], str) and item["content"]

    def test_departments_varied(self):
        """50 条用例覆盖多个部门（多部门多样性）。"""
        cases = scripts_generate_dataset(count=50)
        deps = {c["department"] for c in cases}
        assert len(deps) >= 2

    def test_levels_varied(self):
        """50 条用例覆盖多个职级（多职级多样性）。"""
        cases = scripts_generate_dataset(count=50)
        levels = {c["level"] for c in cases}
        assert len(levels) >= 2


class TestScriptsGenerateCase:
    """scripts.generate_case 单条生成逻辑"""

    def test_case_idx_one(self):
        """idx=1 时 employee_id=E1001。"""
        case = scripts_generate_case(1)
        assert case["employee_id"] == "E1001"

    def test_case_idx_zero(self):
        """idx=0 时 employee_id=E1000，archetype 为列表首项。"""
        case = scripts_generate_case(0)
        assert case["employee_id"] == "E1000"
        assert case["archetype"] == "star"

    def test_case_idx_large(self):
        """大 idx 不抛异常。"""
        case = scripts_generate_case(9999)
        assert case["employee_id"] == "E10999"

    def test_case_archetype_rotation(self):
        """archetype 按 idx 轮转，前 5 条刚好覆盖 5 类画像。"""
        cases = [scripts_generate_case(i) for i in range(1, 6)]
        archetypes = [c["archetype"] for c in cases]
        assert sorted(archetypes) == sorted(
            ["star", "steady", "slacker", "newhire", "bottleneck"]
        )


class TestScriptsDeterminism:
    """可复现性：固定种子下 generate_dataset 输出稳定"""

    def test_generate_dataset_deterministic(self):
        """重置种子后两次 generate_dataset(50) 输出完全一致。"""
        random.seed(42)
        first = scripts_generate_dataset(count=50)
        random.seed(42)
        second = scripts_generate_dataset(count=50)
        assert first == second
