"""
LLM-as-Judge 评估框架单元测试
覆盖 MockJudge 的三个 judge 方法、judge_all 聚合、LLMJudge 解析容错，
以及命令行入口 python -m eval.llm_judge。
"""

import json

import pytest

from eval.evaluate import build_mock_evaluation
from eval.llm_judge import LLMJudge, MockJudge


def _raw_inputs():
    """构造与 positive mock 评估证据一致的原始输入"""
    return [
        {
            "input_id": "daily-001",
            "type": "daily_report",
            "content": "本周主导完成用户画像模块重构，性能提升40%，并辅导两名新人完成Code Review。",
        }
    ]


class TestJudgeEvidence:
    """judge_evidence：证据是否真实引用自 raw_inputs"""

    @pytest.mark.asyncio
    async def test_high_score_when_evidence_sourced(self):
        """证据真实存在于 raw_inputs → 高分"""
        eval_result = {
            "employee_view": {
                "growth_areas": [
                    {
                        "dimension": "技术交付",
                        "evidence": ["主导完成用户画像模块重构，性能提升40%"],
                    },
                    {
                        "dimension": "团队协作",
                        "evidence": ["辅导两名新人完成Code Review"],
                    },
                ]
            }
        }
        judge = MockJudge(None)
        result = await judge.judge_evidence(eval_result, _raw_inputs())
        assert result["score"] == 100
        assert "溯源" in result["reason"]

    @pytest.mark.asyncio
    async def test_low_score_when_evidence_missing(self):
        """证据不存在于 raw_inputs → 低分"""
        eval_result = {
            "employee_view": {
                "growth_areas": [
                    {
                        "dimension": "技术交付",
                        "evidence": ["虚构的业绩：完成月球基地建设"],
                    },
                ]
            }
        }
        judge = MockJudge(None)
        result = await judge.judge_evidence(eval_result, _raw_inputs())
        assert result["score"] < 30
        assert "编造" in result["reason"] or "找到依据" in result["reason"]

    @pytest.mark.asyncio
    async def test_no_evidence_returns_low(self):
        """无任何证据引用 → 低分"""
        judge = MockJudge(None)
        result = await judge.judge_evidence(
            {"employee_view": {"growth_areas": []}}, _raw_inputs()
        )
        assert result["score"] <= 30

    @pytest.mark.asyncio
    async def test_partial_evidence_medium(self):
        """部分证据可溯源 → 中等分数"""
        eval_result = {
            "employee_view": {
                "growth_areas": [
                    {
                        "dimension": "a",
                        "evidence": ["主导完成用户画像模块重构，性能提升40%"],
                    },
                    {"dimension": "b", "evidence": ["虚构内容：完成火星开发"]},
                ]
            }
        }
        judge = MockJudge(None)
        result = await judge.judge_evidence(eval_result, _raw_inputs())
        assert 0 < result["score"] < 100


class TestJudgeToneSeparation:
    """judge_tone_separation：员工视图建设性 + 管理视图尖锐"""

    @pytest.mark.asyncio
    async def test_high_score_when_constructive_and_sharp(self):
        """员工视图建设性 + 管理视图有 risk_flags → 高分"""
        eval_result = {
            "employee_view": {
                "summary": "本周期整体表现优秀，建议继续保持技术影响力。",
                "growth_areas": [],
            },
            "manager_view": {
                "harsh_assessment": "该员工交付质量与主动性均高于团队平均水平，建议纳入晋升观察名单。",
                "risk_flags": [
                    {
                        "level": "high",
                        "category": "交付风险",
                        "description": "需关注长期高负荷",
                    }
                ],
            },
        }
        judge = MockJudge(None)
        result = await judge.judge_tone_separation(eval_result)
        # 无负面词 +50 + risk_flags +30 + harsh +20 = 100
        assert result["score"] == 100

    @pytest.mark.asyncio
    async def test_low_score_when_negative_and_no_risk(self):
        """员工视图有负面词 + 管理视图无 risk_flags → 低分"""
        eval_result = {
            "employee_view": {"summary": "本周表现很差，做事拖沓，态度消极。"},
            "manager_view": {"harsh_assessment": "", "risk_flags": []},
        }
        judge = MockJudge(None)
        result = await judge.judge_tone_separation(eval_result)
        # 负面词 → 0 + 无 risk_flags → 0 + 无 harsh → 0
        assert result["score"] <= 20

    @pytest.mark.asyncio
    async def test_partial_tone(self):
        """员工视图建设性但管理视图缺 risk_flags → 中等"""
        eval_result = {
            "employee_view": {"summary": "整体表现稳定，继续保持。"},
            "manager_view": {"harsh_assessment": "建议持续观察。", "risk_flags": []},
        }
        judge = MockJudge(None)
        result = await judge.judge_tone_separation(eval_result)
        # 无负面 +50 + 无 risk +0 + harsh +20 = 70
        assert result["score"] == 70


class TestJudgeHallucination:
    """judge_hallucination：所有结论有证据支撑则高分"""

    @pytest.mark.asyncio
    async def test_high_score_when_all_supported(self):
        """所有维度结论都有据可查 → 高分"""
        eval_result = {
            "employee_view": {
                "growth_areas": [
                    {
                        "dimension": "技术交付",
                        "evidence": ["主导完成用户画像模块重构，性能提升40%"],
                    },
                    {
                        "dimension": "团队协作",
                        "evidence": ["辅导两名新人完成Code Review"],
                    },
                ]
            }
        }
        judge = MockJudge(None)
        result = await judge.judge_hallucination(eval_result, _raw_inputs())
        assert result["score"] == 100
        assert "支撑" in result["reason"]

    @pytest.mark.asyncio
    async def test_low_score_when_conclusion_without_evidence(self):
        """有结论无证据 → 低分"""
        eval_result = {
            "employee_view": {
                "growth_areas": [
                    {"dimension": "技术交付", "evidence": []},
                    {"dimension": "团队协作", "evidence": []},
                ]
            }
        }
        judge = MockJudge(None)
        result = await judge.judge_hallucination(eval_result, _raw_inputs())
        assert result["score"] < 30

    @pytest.mark.asyncio
    async def test_low_score_when_evidence_unsourced(self):
        """证据存在但不可溯源 → 低分"""
        eval_result = {
            "employee_view": {
                "growth_areas": [
                    {
                        "dimension": "技术交付",
                        "evidence": ["虚构业绩：完成月球基地建设"],
                    },
                ]
            }
        }
        judge = MockJudge(None)
        result = await judge.judge_hallucination(eval_result, _raw_inputs())
        assert result["score"] < 30

    @pytest.mark.asyncio
    async def test_no_growth_areas_low(self):
        """缺少 growth_areas → 低分"""
        judge = MockJudge(None)
        result = await judge.judge_hallucination({"employee_view": {}}, _raw_inputs())
        assert result["score"] <= 30


class TestJudgeAll:
    """judge_all：聚合三个维度"""

    @pytest.mark.asyncio
    async def test_structure_and_overall_score_range(self):
        """返回结构完整，overall_score 在 0-100"""
        eval_result = build_mock_evaluation(
            score=88, tone="positive", employee_id="E1", period="W1", keywords=["主导"]
        )
        judge = MockJudge(None)
        result = await judge.judge_all(eval_result, _raw_inputs())
        assert set(result.keys()) == {
            "evidence",
            "tone",
            "hallucination",
            "overall_score",
        }
        for dim in ("evidence", "tone", "hallucination"):
            assert "score" in result[dim]
            assert "reason" in result[dim]
            assert 0 <= result[dim]["score"] <= 100
        assert 0 <= result["overall_score"] <= 100
        # overall_score 应为三维度均值
        expected = round(
            (
                result["evidence"]["score"]
                + result["tone"]["score"]
                + result["hallucination"]["score"]
            )
            / 3,
            2,
        )
        assert result["overall_score"] == expected


class TestLLMJudgeParseScore:
    """LLMJudge._parse_score 静态解析（不调真实 LLM）"""

    def test_parse_valid_json(self):
        result = LLMJudge._parse_score(
            '{"score": 85, "reason": "证据充分"}', "evidence"
        )
        assert result["score"] == 85
        assert result["reason"] == "证据充分"

    def test_parse_clamps_range(self):
        result = LLMJudge._parse_score('{"score": 150, "reason": "x"}', "tone")
        assert result["score"] == 100

    def test_parse_invalid_json(self):
        result = LLMJudge._parse_score("not json", "hallucination")
        assert result["score"] == 0
        assert "解析失败" in result["reason"]

    def test_llmjudge_requires_provider(self):
        """LLMJudge 无 provider 时调用 judge 方法应报错"""
        judge = LLMJudge(None)
        with pytest.raises(RuntimeError):
            import asyncio

            asyncio.get_event_loop().run_until_complete(
                judge.judge_evidence({"employee_view": {}}, [])
            )


class TestCLI:
    """命令行入口 python -m eval.llm_judge"""

    @pytest.mark.asyncio
    async def test_mock_cli_runs(self, monkeypatch, tmp_path, capsys):
        """--mock 模式跑通数据集并输出打分"""
        dataset = [
            {
                "employee_id": "E9001",
                "period": "2026-W26",
                "archetype": "star",
                "raw_inputs": [
                    {
                        "input_id": "daily-cli",
                        "type": "daily_report",
                        "content": "本周主导完成用户画像模块重构，性能提升40%，并辅导两名新人完成Code Review。",
                    }
                ],
            }
        ]
        dataset_path = tmp_path / "dataset.json"
        dataset_path.write_text(
            json.dumps(dataset, ensure_ascii=False), encoding="utf-8"
        )

        monkeypatch.setattr(
            "sys.argv", ["llm_judge", "--dataset", str(dataset_path), "--mock"]
        )
        from eval.llm_judge import main

        rc = await main()
        out = capsys.readouterr().out
        assert rc == 0
        assert "加载了 1 条用例" in out
        assert "Judge 平均分" in out
        # 单条用例应打印 OK 与打分
        assert "[OK] E9001" in out

    @pytest.mark.asyncio
    async def test_mock_cli_writes_output(self, monkeypatch, tmp_path, capsys):
        """--output 写入报告文件且结构完整"""
        dataset = [
            {
                "employee_id": "E9002",
                "period": "2026-W26",
                "archetype": "workaholic",
                "raw_inputs": [
                    {
                        "input_id": "daily-cli2",
                        "type": "daily_report",
                        "content": "本周独立完成全部指派任务，加班较多，但跨团队协作沟通偏少。",
                    }
                ],
            }
        ]
        dataset_path = tmp_path / "dataset.json"
        dataset_path.write_text(
            json.dumps(dataset, ensure_ascii=False), encoding="utf-8"
        )
        out_path = tmp_path / "report.json"

        monkeypatch.setattr(
            "sys.argv",
            [
                "llm_judge",
                "--dataset",
                str(dataset_path),
                "--mock",
                "--output",
                str(out_path),
            ],
        )
        from eval.llm_judge import main

        rc = await main()
        assert rc == 0
        report = json.loads(out_path.read_text(encoding="utf-8"))
        assert report["total"] == 1
        assert report["valid"] == 1
        assert report["results"][0]["judge"] is not None
        assert "overall_score" in report["results"][0]["judge"]
        assert "average_judge_score" in report
