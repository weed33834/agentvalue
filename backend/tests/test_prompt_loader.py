"""
PromptLoader 单元测试：覆盖加载、版本管理、渲染与异常分支。
"""

import pytest

from agent.prompt_loader import PromptLoader


@pytest.fixture
def loader():
    """使用仓库自带 prompts 目录的加载器。"""
    return PromptLoader()


def test_load_returns_prompt_text(loader):
    text = loader.load("daily_evaluation")
    assert "AgentValue-AI" in text or "员工" in text


def test_load_missing_raises(loader):
    with pytest.raises(FileNotFoundError):
        loader.load("not-a-real-prompt")


def test_version_extracted_from_header(loader):
    # v1.1 上线后，daily_evaluation.md 头部标注 v1.1
    assert loader.version("daily_evaluation") == "v1.1"


def test_current_version_constant_matches_header(loader):
    # PromptLoader.CURRENT_VERSION 与文件头部版本保持一致
    assert loader.CURRENT_VERSION == "v1.1"
    assert loader.version("daily_evaluation") == loader.CURRENT_VERSION


def test_version_unknown_when_no_header(tmp_path):
    # 构造一个无版本头的临时 prompt 文件
    (tmp_path / "no_version.md").write_text(
        "# 只有标题\n\n无版本信息。", encoding="utf-8"
    )
    loader = PromptLoader(prompts_dir=tmp_path)
    assert loader.version("no_version") == "unknown"


def test_list_versions_returns_sorted(loader):
    # v1.1 归档后，versions/ 目录下含 v0.1、v0.2、v1.0、v1.1 四个快照
    assert loader.list_versions("daily_evaluation") == ["v0.1", "v0.2", "v1.0", "v1.1"]


def test_list_versions_empty_when_no_versions_dir(tmp_path):
    loader = PromptLoader(prompts_dir=tmp_path)
    assert loader.list_versions("daily_evaluation") == []


def test_list_versions_filters_by_name(tmp_path):
    versions_dir = tmp_path / "versions"
    versions_dir.mkdir()
    (versions_dir / "daily_evaluation_v0.1.md").write_text("a", encoding="utf-8")
    (versions_dir / "daily_evaluation_v0.3.md").write_text("b", encoding="utf-8")
    (versions_dir / "other_v0.2.md").write_text("c", encoding="utf-8")
    loader = PromptLoader(prompts_dir=tmp_path)
    assert loader.list_versions("daily_evaluation") == ["v0.1", "v0.3"]
    assert loader.list_versions("other") == ["v0.2"]


def test_load_version_with_v_prefix(loader):
    text = loader.load_version("daily_evaluation", "v0.1")
    assert len(text) > 0


def test_load_version_without_v_prefix(loader):
    text = loader.load_version("daily_evaluation", "0.1")
    assert len(text) > 0


def test_load_version_missing_raises_with_available(loader):
    with pytest.raises(FileNotFoundError) as exc:
        loader.load_version("daily_evaluation", "v9.9")
    assert "v0.1" in str(exc.value)


def test_render_replaces_known_placeholders(loader):
    # daily_evaluation 模板使用 {raw_inputs}/{employee_history}/{company_kb}
    rendered = loader.render(
        "daily_evaluation",
        raw_inputs=[{"day": "周一", "content": "完成 A"}],
        employee_history=[{"period": "2026-W24", "score": 80}],
        company_kb=[{"rule": "执行力优先"}],
        employee_id="EMP001",
        period="2026-W25",
    )
    assert "完成 A" in rendered
    assert "执行力优先" in rendered
    assert "2026-W24" in rendered


def test_render_substitutes_employee_id_and_period():
    # 用合成模板验证 {employee_id}/{period} 占位符替换
    loader = PromptLoader()
    result = loader._render_template(
        "id={employee_id}, period={period}",
        raw_inputs=[],
        employee_id="EMP001",
        period="2026-W25",
    )
    assert result == "id=EMP001, period=2026-W25"


def test_render_preserves_unknown_braces(loader):
    # 模板里若含未知 {foo} 占位符，应原样保留
    template = "inputs={raw_inputs}, keep={foo}"
    result = loader._render_template(template, raw_inputs=[{"x": 1}])
    assert "{foo}" in result
    assert '"x": 1' in result


def test_render_defaults_empty_collections(loader):
    rendered = loader.render(
        "daily_evaluation",
        raw_inputs=[{"d": "周一"}],
        employee_history=None,
        company_kb=None,
    )
    assert "[]" in rendered  # None -> 空数组


def test_render_version_replaces_placeholders(loader):
    rendered = loader.render_version(
        "daily_evaluation",
        "v0.1",
        raw_inputs=[{"day": "周一", "task": "修复 bug"}],
        employee_id="EMP002",
        period="2026-W26",
    )
    assert "修复 bug" in rendered


# ---------------- v1.1 新增能力测试 ----------------


def test_v1_1_archive_loadable(loader):
    # v1.1 快照可在 versions/ 目录下被加载
    text = loader.load_version("daily_evaluation", "v1.1")
    assert len(text) > 0
    # 头部应标注 v1.1
    head_lines = text.splitlines()[:10]
    assert any("v1.1" in line for line in head_lines)


def test_v1_1_archive_matches_current(loader):
    # 当前生效 Prompt 与 v1.1 快照内容一致（仅文件位置不同）
    current_text = loader.load("daily_evaluation")
    archived_text = loader.load_version("daily_evaluation", "v1.1")
    assert current_text == archived_text


def test_v1_1_includes_few_shot_examples(loader):
    # v1.1 必须包含 3 个端到端 few-shot 示例（star/slacker/workaholic）
    text = loader.load("daily_evaluation")
    assert "## 完整示例" in text
    assert "示例 A — Star 员工" in text
    assert "示例 B — Slacker 员工" in text
    assert "示例 C — Workaholic 员工" in text
    # 每个 few-shot 输出必须包含 audit.evidence_sources（v1.0 → v1.1 强化）
    assert "evidence_sources" in text


def test_v1_1_includes_chain_of_thought_guidance(loader):
    # v1.1 必须在输出质量控制规则中包含 chain-of-thought 引导
    text = loader.load("daily_evaluation")
    assert "chain-of-thought" in text
    assert "推理过程不直接输出到最终 JSON 中" in text


def test_v1_1_audit_prompt_version_field(loader):
    # v1.1 Schema 中 audit.prompt_version 默认值标注为 v1.1
    text = loader.load("daily_evaluation")
    assert '"prompt_version": "v1.1"' in text


def test_v1_1_distinct_from_v1_0(loader):
    # v1.1 与 v1.0 是两个不同的版本快照
    v1_0 = loader.load_version("daily_evaluation", "v1.0")
    v1_1 = loader.load_version("daily_evaluation", "v1.1")
    # v1.0 头部为 v1.0，v1.1 头部为 v1.1
    assert "**版本：** v1.0" in v1_0
    assert "**版本：** v1.1" in v1_1
    # v1.1 比 v1.0 多出 few-shot 段与 chain-of-thought 规则
    assert "## 完整示例" in v1_1
    assert "## 完整示例" not in v1_0
    assert "chain-of-thought" in v1_1
    assert "chain-of-thought" not in v1_0


def test_v1_1_render_keeps_few_shot_section(loader):
    # 渲染后的 Prompt 仍应保留 few-shot 段，方便 LLM 学习
    rendered = loader.render(
        "daily_evaluation",
        raw_inputs=[{"input_id": "d1", "content": "本周完成 A"}],
        employee_id="E_TEST",
        period="2026-W27",
    )
    assert "## 完整示例" in rendered
    assert "示例 A — Star 员工" in rendered


# ---------------- 数据集扩充校验 ----------------


def test_dataset_archetype_variety():
    # 扩充后每个 archetype 至少 3 条用例
    from eval.evaluate import load_dataset

    dataset = load_dataset()
    archetypes = {}
    for case in dataset:
        a = case.get("archetype", "unknown")
        archetypes.setdefault(a, 0)
        archetypes[a] += 1
    for archetype, count in archetypes.items():
        assert count >= 3, f"archetype {archetype} 用例数 {count} < 3"


def test_dataset_includes_employee_view_tone_field():
    # 扩充后每条用例新增 expected_employee_view_tone 字段
    from eval.evaluate import load_dataset

    dataset = load_dataset()
    assert len(dataset) >= 15
    for case in dataset:
        assert (
            "expected_employee_view_tone" in case
        ), f"{case['employee_id']} 缺少 expected_employee_view_tone"
        assert (
            "建设性" in case["expected_employee_view_tone"]
            or "肯定" in case["expected_employee_view_tone"]
        )
