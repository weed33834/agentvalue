"""
WS-2 实验对比 + RAGAS 生成质量指标单元测试

使用独立临时 SQLite 异步数据库 + Mock LLM, 覆盖:
- bootstrap 置信区间: 固定 seed 的可复现性 / 显著性判定 / 样本不足降级
- 逐样本分类: improved / regressed / unchanged / only_in_a / only_in_b 与回归优先排序
- metric_summary 聚合: 纯函数与落库后的 _compute_metric_summary
- faithfulness: mock LLM 下的比例计分, 以及 LLM / embedding 不可用时
  必须返回 score=None + status="unavailable" (禁止编造 0/1)
- compare_runs / get_regression_report 端到端 (真实建表落库)
"""

import random
import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.database import Base
from core.providers.base import ChatCompletion
from models.experiment_models import ExperimentRunItem
from services.dataset_service import DatasetService
from services.experiment_service import (
    DEFAULT_BOOTSTRAP_SEED,
    ExperimentService,
    bootstrap_delta_ci,
    build_verdict,
    classify_samples,
    compare_metric,
    summarize_metric_buckets,
)
from services.ragas_metrics_service import (
    METRIC_ANSWER_RELEVANCY,
    METRIC_FAITHFULNESS,
    MetricStatus,
    RagasMetricsService,
)


# ============================================================
# 公共 fixture / 测试替身
# ============================================================


@pytest.fixture
async def db_session():
    """每个测试使用独立临时 SQLite 异步数据库"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    async with SessionLocal() as session:
        yield session
    await engine.dispose()
    Path(tmp.name).unlink(missing_ok=True)


class ScriptedLLM:
    """按 prompt 关键词返回预置 JSON 的 LLM 替身

    routes: [(关键词, 返回内容)]; 命中第一个包含该关键词的规则。
    未命中时抛异常, 用于验证"调用失败必须降级为 unavailable"。
    """

    def __init__(self, routes, fail_on_miss=True):
        self.routes = routes
        self.fail_on_miss = fail_on_miss
        self.prompts = []

    async def __call__(self, model_router, messages=None, **kwargs):
        prompt = messages[0].content if messages else ""
        self.prompts.append(prompt)
        for keyword, payload in self.routes:
            if keyword in prompt:
                return ChatCompletion(content=payload, model="mock-judge"), "L0"
        if self.fail_on_miss:
            raise RuntimeError(f"ScriptedLLM 未覆盖的 prompt: {prompt[:40]}")
        return ChatCompletion(content="", model="mock-judge"), "L0"


class ZeroVectorEmbedding:
    """模拟 core/embeddings.py 无 key 时返回全零向量的行为"""

    def __init__(self, dim=8):
        self.dim = dim
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        return [[0.0] * self.dim for _ in texts]


def _ragas(monkeypatch, routes, *, embedding_client=None, fail_on_miss=True):
    """构造注入了 ScriptedLLM 的 RagasMetricsService"""
    import services.ragas_metrics_service as mod

    fake = ScriptedLLM(routes, fail_on_miss=fail_on_miss)
    monkeypatch.setattr(mod, "call_llm_with_fallback", fake)
    service = RagasMetricsService(object(), embedding_client=embedding_client)
    return service, fake


async def _seed_run(session, *, name, items, tenant_id="default"):
    """建 dataset + experiment + run, 并按 {sample_id: {metric: score}} 落逐样本结果"""
    dataset = await DatasetService(session).create_dataset(
        f"ds-{name}", tenant_id=tenant_id
    )
    service = ExperimentService(session)
    experiment = await service.create_experiment(
        f"exp-{name}", dataset.id, tenant_id=tenant_id
    )
    run = await service.create_run(experiment.id, tenant_id=tenant_id, name=name)
    for sample_id, scores in items.items():
        session.add(
            ExperimentRunItem(
                run_id=run.id,
                experiment_id=experiment.id,
                tenant_id=tenant_id,
                sample_id=sample_id,
                input={"question": f"Q-{sample_id}"},
                scores=scores,
                status="success",
            )
        )
    await session.flush()
    return experiment, run


async def _seed_second_run(session, experiment_id, *, name, items, tenant_id="default"):
    """在既有实验下再建一个 run 并落样本结果 (两 run 共享 sample_id 才可配对)"""
    service = ExperimentService(session)
    run = await service.create_run(experiment_id, tenant_id=tenant_id, name=name)
    for sample_id, scores in items.items():
        session.add(
            ExperimentRunItem(
                run_id=run.id,
                experiment_id=experiment_id,
                tenant_id=tenant_id,
                sample_id=sample_id,
                input={"question": f"Q-{sample_id}"},
                scores=scores,
                status="success",
            )
        )
    await session.flush()
    return run


# ============================================================
# bootstrap 置信区间
# ============================================================


class TestBootstrapCI:
    """bootstrap_delta_ci: 纯标准库实现, 固定 seed 必须完全可复现"""

    def test_same_seed_reproducible(self):
        """同 seed + 同输入 => 逐位相同的置信区间 (CI 门禁可复现的前提)"""
        a = [0.5, 0.6, 0.55, 0.62, 0.48, 0.7]
        b = [0.7, 0.8, 0.75, 0.82, 0.68, 0.9]
        first = bootstrap_delta_ci(
            a, b, rng=random.Random(20260808), iterations=500, paired=True
        )
        second = bootstrap_delta_ci(
            a, b, rng=random.Random(20260808), iterations=500, paired=True
        )
        assert first == second
        assert first[2] == "paired_bootstrap"

    def test_different_seed_changes_ci(self):
        """不同 seed 的重采样结果不应恰好相同 (证明确实在随机重采样)"""
        a = [0.1, 0.9, 0.2, 0.8, 0.3, 0.7]
        b = [0.2, 0.95, 0.15, 0.85, 0.4, 0.6]
        s1 = bootstrap_delta_ci(a, b, rng=random.Random(1), iterations=500, paired=True)
        s2 = bootstrap_delta_ci(a, b, rng=random.Random(2), iterations=500, paired=True)
        assert (s1[0], s1[1]) != (s2[0], s2[1])

    def test_consistent_improvement_ci_excludes_zero(self):
        """每条样本都提升 0.2 => 配对 CI 完全落在正区间"""
        a = [0.4, 0.5, 0.45, 0.55, 0.6]
        b = [x + 0.2 for x in a]
        low, high, method = bootstrap_delta_ci(
            a, b, rng=random.Random(7), iterations=800, paired=True
        )
        assert method == "paired_bootstrap"
        assert low is not None and low > 0
        assert high == pytest.approx(0.2, abs=1e-6)

    def test_identical_values_ci_contains_zero(self):
        """两组完全相同 => CI 收缩到 0, 不得判为显著"""
        values = [0.3, 0.6, 0.9, 0.45]
        low, high, _ = bootstrap_delta_ci(
            values, list(values), rng=random.Random(3), iterations=400, paired=True
        )
        assert low == pytest.approx(0.0, abs=1e-9)
        assert high == pytest.approx(0.0, abs=1e-9)

    def test_unpaired_when_no_pairing(self):
        """未配对模式走独立重采样"""
        low, high, method = bootstrap_delta_ci(
            [0.1, 0.2, 0.3],
            [0.7, 0.8, 0.9],
            rng=random.Random(11),
            iterations=400,
            paired=False,
        )
        assert method == "unpaired_bootstrap"
        assert low is not None and high is not None and low <= high

    def test_insufficient_data_returns_none(self):
        """样本不足时返回 None 而不是编造区间"""
        assert bootstrap_delta_ci(
            [0.5], [0.6], rng=random.Random(0), iterations=100, paired=True
        ) == (None, None, "insufficient_data")
        assert bootstrap_delta_ci(
            [], [], rng=random.Random(0), iterations=100, paired=False
        ) == (None, None, "insufficient_data")


class TestCompareMetric:
    """compare_metric: 均值 / delta / winner / 显著性"""

    def test_significant_improvement(self):
        """B 全面优于 A 且配对差值稳定 => significant + winner=b"""
        scores_a = {f"s{i}": {"faithfulness": 0.5} for i in range(6)}
        scores_b = {f"s{i}": {"faithfulness": 0.8} for i in range(6)}
        result = compare_metric(
            metric="faithfulness",
            scores_a=scores_a,
            scores_b=scores_b,
            rng=random.Random(DEFAULT_BOOTSTRAP_SEED),
            iterations=500,
        )
        assert result.mean_a == pytest.approx(0.5)
        assert result.mean_b == pytest.approx(0.8)
        assert result.delta == pytest.approx(0.3)
        assert result.delta_pct == pytest.approx(60.0)
        assert result.winner == "b"
        assert result.n_paired == 6
        assert result.method == "paired_bootstrap"
        assert result.significant is True

    def test_tie_not_significant(self):
        """两组完全一致 => winner=tie 且不显著"""
        scores = {f"s{i}": {"answer_relevancy": 0.6} for i in range(5)}
        result = compare_metric(
            metric="answer_relevancy",
            scores_a=scores,
            scores_b={k: dict(v) for k, v in scores.items()},
            rng=random.Random(DEFAULT_BOOTSTRAP_SEED),
            iterations=300,
        )
        assert result.winner == "tie"
        assert result.delta == pytest.approx(0.0)
        assert result.significant is False

    def test_regression_winner_a(self):
        """B 明显变差 => winner=a"""
        scores_a = {f"s{i}": {"faithfulness": 0.9} for i in range(5)}
        scores_b = {f"s{i}": {"faithfulness": 0.4} for i in range(5)}
        result = compare_metric(
            metric="faithfulness",
            scores_a=scores_a,
            scores_b=scores_b,
            rng=random.Random(DEFAULT_BOOTSTRAP_SEED),
            iterations=300,
        )
        assert result.winner == "a"
        assert result.delta == pytest.approx(-0.5)
        assert result.significant is True

    def test_missing_metric_not_counted(self):
        """某侧完全没有该指标 => mean 为 None, winner=unknown, 不得当成 0"""
        result = compare_metric(
            metric="context_recall",
            scores_a={"s1": {"faithfulness": 0.5}},
            scores_b={"s1": {"faithfulness": 0.7}},
            rng=random.Random(1),
            iterations=100,
        )
        assert result.mean_a is None
        assert result.mean_b is None
        assert result.delta is None
        assert result.winner == "unknown"
        assert result.significant is False


# ============================================================
# 逐样本分类
# ============================================================


class TestClassifySamples:
    """classify_samples: 五类判定 + 回归优先排序"""

    def test_all_five_classifications(self):
        scores_a = {
            "up": {"faithfulness": 0.4},
            "down": {"faithfulness": 0.9},
            "same": {"faithfulness": 0.6},
            "gone": {"faithfulness": 0.5},
        }
        scores_b = {
            "up": {"faithfulness": 0.85},
            "down": {"faithfulness": 0.3},
            "same": {"faithfulness": 0.6},
            "new": {"faithfulness": 0.7},
        }
        result = {
            s.sample_id: s
            for s in classify_samples(scores_a=scores_a, scores_b=scores_b)
        }
        assert result["up"].classification == "improved"
        assert result["down"].classification == "regressed"
        assert result["same"].classification == "unchanged"
        assert result["gone"].classification == "only_in_a"
        assert result["new"].classification == "only_in_b"
        assert result["down"].delta == pytest.approx(-0.6)
        assert result["gone"].delta is None

    def test_sorted_worst_regression_first(self):
        """排序: 回归最严重排最前, delta 为 None 的排最后"""
        scores_a = {
            "mild": {"faithfulness": 0.6},
            "severe": {"faithfulness": 0.9},
            "better": {"faithfulness": 0.2},
            "orphan": {"faithfulness": 0.5},
        }
        scores_b = {
            "mild": {"faithfulness": 0.5},
            "severe": {"faithfulness": 0.1},
            "better": {"faithfulness": 0.8},
        }
        ordered = classify_samples(scores_a=scores_a, scores_b=scores_b)
        assert [s.sample_id for s in ordered] == [
            "severe",
            "mild",
            "better",
            "orphan",
        ]
        assert ordered[-1].delta is None

    def test_primary_metric_selects_single_dimension(self):
        """指定主指标时只按该指标比较, 而不是全指标均值"""
        scores_a = {"s1": {"faithfulness": 0.9, "answer_relevancy": 0.1}}
        scores_b = {"s1": {"faithfulness": 0.2, "answer_relevancy": 0.95}}

        by_faith = classify_samples(
            scores_a=scores_a, scores_b=scores_b, primary_metric="faithfulness"
        )[0]
        assert by_faith.classification == "regressed"
        assert by_faith.delta == pytest.approx(-0.7)

        by_mean = classify_samples(scores_a=scores_a, scores_b=scores_b)[0]
        assert by_mean.score_a == pytest.approx(0.5)
        assert by_mean.score_b == pytest.approx(0.575)
        assert by_mean.classification == "improved"

    def test_tolerance_treats_float_noise_as_unchanged(self):
        """浮点噪声不应被判成 improved/regressed"""
        result = classify_samples(
            scores_a={"s1": {"faithfulness": 0.5}},
            scores_b={"s1": {"faithfulness": 0.5 + 1e-9}},
        )[0]
        assert result.classification == "unchanged"

    def test_metric_deltas_none_when_one_side_missing(self):
        """单侧缺失的指标 delta 记 None, 不能补 0"""
        result = classify_samples(
            scores_a={"s1": {"faithfulness": 0.5}},
            scores_b={"s1": {"faithfulness": 0.6, "context_recall": 0.8}},
        )[0]
        assert result.metric_deltas["faithfulness"] == pytest.approx(0.1)
        assert result.metric_deltas["context_recall"] is None


class TestVerdict:
    """build_verdict: 结论文案覆盖显著变好 / 变差 / 无显著差异"""

    def test_reports_significant_regression(self):
        worse = compare_metric(
            metric="faithfulness",
            scores_a={f"s{i}": {"faithfulness": 0.9} for i in range(5)},
            scores_b={f"s{i}": {"faithfulness": 0.4} for i in range(5)},
            rng=random.Random(5),
            iterations=300,
        )
        text = build_verdict([worse], {"improved": 0, "regressed": 5, "unchanged": 0})
        assert "显著变差" in text
        assert "回归 5" in text

    def test_no_metrics(self):
        assert build_verdict([], {}) == "两次运行没有可比的指标数据"


# ============================================================
# metric_summary 聚合
# ============================================================


class TestMetricSummary:
    """summarize_metric_buckets + 落库后的 _compute_metric_summary"""

    def test_aggregation_fields(self):
        summary = summarize_metric_buckets(
            {"faithfulness": [0.2, 0.4, 0.9], "answer_relevancy": [0.5]}
        )
        assert summary["faithfulness"]["n"] == 3
        assert summary["faithfulness"]["mean"] == pytest.approx(0.5)
        assert summary["faithfulness"]["min"] == pytest.approx(0.2)
        assert summary["faithfulness"]["max"] == pytest.approx(0.9)
        assert summary["faithfulness"]["std"] == pytest.approx(0.360555, abs=1e-5)
        # n == 1 时样本标准差无定义, 约定为 0.0
        assert summary["answer_relevancy"]["std"] == 0.0
        assert summary["answer_relevancy"]["n"] == 1

    def test_empty_bucket_dropped(self):
        """没有任何真实得分的指标不进 summary (而不是写 0)"""
        assert summarize_metric_buckets({"faithfulness": []}) == {}

    async def test_compute_metric_summary_from_db(self, db_session):
        """从落库的 ExperimentRunItem 聚合, 不可用指标不出现在 summary 中"""
        _, run = await _seed_run(
            db_session,
            name="summary",
            items={
                "s1": {"faithfulness": 0.6, "answer_relevancy": 0.8},
                "s2": {"faithfulness": 1.0, "answer_relevancy": 0.6},
                # s3 的 faithfulness 不可用 => 该样本压根不写这个 key
                "s3": {"answer_relevancy": 0.7},
            },
        )
        summary = await ExperimentService(db_session)._compute_metric_summary(
            run.id, tenant_id="default"
        )
        assert summary["faithfulness"]["n"] == 2
        assert summary["faithfulness"]["mean"] == pytest.approx(0.8)
        assert summary["answer_relevancy"]["n"] == 3
        assert summary["answer_relevancy"]["mean"] == pytest.approx(0.7)
        assert "context_recall" not in summary


# ============================================================
# compare_runs / 回归报告 (端到端落库)
# ============================================================


class TestCompareRunsEndToEnd:
    """真实建表落库后的 compare_runs / get_regression_report"""

    async def test_compare_runs_full_result(self, db_session):
        experiment, run_a = await _seed_run(
            db_session,
            name="baseline",
            items={
                "s1": {"faithfulness": 0.5},
                "s2": {"faithfulness": 0.6},
                "s3": {"faithfulness": 0.7},
                "s4": {"faithfulness": 0.8},
                "only-a": {"faithfulness": 0.9},
            },
        )
        run_b = await _seed_second_run(
            db_session,
            experiment.id,
            name="candidate",
            items={
                "s1": {"faithfulness": 0.7},
                "s2": {"faithfulness": 0.8},
                "s3": {"faithfulness": 0.9},
                "s4": {"faithfulness": 1.0},
                "only-b": {"faithfulness": 0.4},
            },
        )
        result = await ExperimentService(db_session).compare_runs(
            run_a.id, run_b.id, bootstrap_iterations=400
        )

        assert result.counts["improved"] == 4
        assert result.counts["only_in_a"] == 1
        assert result.counts["only_in_b"] == 1
        assert result.counts["total"] == 6

        metric = next(m for m in result.metrics if m.metric == "faithfulness")
        assert metric.n_paired == 4
        assert metric.delta is not None and metric.delta > 0
        assert metric.winner == "b"
        assert metric.significant is True
        assert result.bootstrap["seed"] == DEFAULT_BOOTSTRAP_SEED
        assert "显著变好" in result.verdict

    async def test_compare_runs_is_deterministic(self, db_session):
        """同 seed 连跑两次 => 置信区间完全一致"""
        experiment, run_a = await _seed_run(
            db_session,
            name="det",
            items={f"s{i}": {"faithfulness": 0.3 + i * 0.1} for i in range(5)},
        )
        run_b = await _seed_second_run(
            db_session,
            experiment.id,
            name="det-b",
            items={f"s{i}": {"faithfulness": 0.35 + i * 0.1} for i in range(5)},
        )
        service = ExperimentService(db_session)
        first = await service.compare_runs(run_a.id, run_b.id, bootstrap_iterations=300)
        second = await service.compare_runs(
            run_a.id, run_b.id, bootstrap_iterations=300
        )
        assert [m.to_dict() for m in first.metrics] == [
            m.to_dict() for m in second.metrics
        ]

    async def test_regression_report_threshold(self, db_session):
        """回归报告只挑出跌幅超过阈值的样本"""
        experiment, run_a = await _seed_run(
            db_session,
            name="reg",
            items={
                "big-drop": {"faithfulness": 0.9},
                "small-drop": {"faithfulness": 0.6},
                "stable": {"faithfulness": 0.5},
            },
        )
        run_b = await _seed_second_run(
            db_session,
            experiment.id,
            name="reg-b",
            items={
                "big-drop": {"faithfulness": 0.3},
                "small-drop": {"faithfulness": 0.58},
                "stable": {"faithfulness": 0.5},
            },
        )
        report = await ExperimentService(db_session).get_regression_report(
            run_a.id, run_b.id, threshold=0.1
        )
        assert report["has_regression"] is True
        ids = [s["sample_id"] for s in report["regressed_samples"]]
        assert ids == ["big-drop"]
        assert report["counts"]["regressed"] == 2

    async def test_no_regression_when_all_improved(self, db_session):
        experiment, run_a = await _seed_run(
            db_session,
            name="clean",
            items={"s1": {"faithfulness": 0.5}, "s2": {"faithfulness": 0.6}},
        )
        run_b = await _seed_second_run(
            db_session,
            experiment.id,
            name="clean-b",
            items={"s1": {"faithfulness": 0.7}, "s2": {"faithfulness": 0.8}},
        )
        report = await ExperimentService(db_session).get_regression_report(
            run_a.id, run_b.id, threshold=0.05
        )
        assert report["has_regression"] is False
        assert report["regressed_samples"] == []

    async def test_compare_missing_run_raises(self, db_session):
        _, run_a = await _seed_run(
            db_session, name="solo", items={"s1": {"faithfulness": 0.5}}
        )
        with pytest.raises(ValueError, match="不存在"):
            await ExperimentService(db_session).compare_runs(run_a.id, 999999)


# ============================================================
# RAGAS faithfulness (mock LLM)
# ============================================================


class TestFaithfulness:
    """faithfulness = 被上下文支撑的原子声明比例"""

    async def test_all_claims_supported(self, monkeypatch):
        service, fake = _ragas(
            monkeypatch,
            [
                ("原子声明", '{"claims": ["小王重构了用户画像模块", "性能提升了40%"]}'),
                (
                    "事实核查员",
                    '{"verdicts": [{"index": 0, "verdict": 1, "reason": "上下文直述"},'
                    ' {"index": 1, "verdict": 1, "reason": "上下文直述"}]}',
                ),
            ],
        )
        result = await service.faithfulness(
            "小王重构了用户画像模块, 性能提升了40%。",
            ["小王主导用户画像模块重构", "重构后接口性能提升40%"],
        )
        assert result.status == MetricStatus.OK
        assert result.score == pytest.approx(1.0)
        assert result.details["supported"] == 2
        assert len(result.details["verdicts"]) == 2
        assert len(fake.prompts) == 2

    async def test_partial_support_ratio(self, monkeypatch):
        """4 条声明支撑 1 条 => 0.25"""
        service, _ = _ragas(
            monkeypatch,
            [
                ("原子声明", '{"claims": ["c1", "c2", "c3", "c4"]}'),
                (
                    "事实核查员",
                    '{"verdicts": ['
                    '{"index": 0, "verdict": 1, "reason": "有依据"},'
                    '{"index": 1, "verdict": 0, "reason": "未提及"},'
                    '{"index": 2, "verdict": 0, "reason": "矛盾"},'
                    '{"index": 3, "verdict": 0, "reason": "未提及"}]}',
                ),
            ],
        )
        result = await service.faithfulness("一段答案", ["一段上下文"])
        assert result.score == pytest.approx(0.25)
        assert result.status == MetricStatus.OK
        assert result.details["supported"] == 1

    async def test_markdown_fenced_json_is_parsed(self, monkeypatch):
        """LLM 用 ```json 包裹时仍要能解析, 而不是直接判不可用"""
        service, _ = _ragas(
            monkeypatch,
            [
                ("原子声明", '```json\n{"claims": ["c1", "c2"]}\n```'),
                (
                    "事实核查员",
                    '```json\n{"verdicts": ['
                    '{"index": 0, "verdict": 1, "reason": "ok"},'
                    '{"index": 1, "verdict": 0, "reason": "no"}]}\n```',
                ),
            ],
        )
        result = await service.faithfulness("答案", ["上下文"])
        assert result.status == MetricStatus.OK
        assert result.score == pytest.approx(0.5)

    async def test_llm_failure_is_unavailable_not_zero(self, monkeypatch):
        """LLM 调用失败必须 score=None + unavailable, 绝不能上报 0 分"""
        service, _ = _ragas(monkeypatch, [], fail_on_miss=True)
        result = await service.faithfulness("答案", ["上下文"])
        assert result.score is None
        assert result.status == MetricStatus.UNAVAILABLE
        assert "LLM 调用失败" in result.reason

    async def test_invalid_json_is_unavailable(self, monkeypatch):
        """非法 JSON 响应同样降级为 unavailable"""
        service, _ = _ragas(monkeypatch, [("原子声明", "抱歉, 我无法完成该任务")])
        result = await service.faithfulness("答案", ["上下文"])
        assert result.score is None
        assert result.status == MetricStatus.UNAVAILABLE

    async def test_empty_contexts_unavailable(self, monkeypatch):
        """无上下文时不调用 LLM, 直接判不可用"""
        service, fake = _ragas(monkeypatch, [])
        result = await service.faithfulness("答案", [])
        assert result.score is None
        assert result.status == MetricStatus.UNAVAILABLE
        assert "上下文为空" in result.reason
        assert fake.prompts == []

    async def test_empty_answer_unavailable(self, monkeypatch):
        service, _ = _ragas(monkeypatch, [])
        result = await service.faithfulness("   ", ["上下文"])
        assert result.score is None
        assert result.status == MetricStatus.UNAVAILABLE


class TestZeroVectorDegradation:
    """core/embeddings.py 无 key 时返回零向量, 必须判不可用而非相似度 0"""

    async def test_answer_relevancy_zero_vector_unavailable(self, monkeypatch):
        embedding = ZeroVectorEmbedding()
        service, _ = _ragas(
            monkeypatch,
            [("提问生成器", '{"questions": ["q1", "q2", "q3"], "noncommittal": 0}')],
            embedding_client=embedding,
        )
        result = await service.answer_relevancy("原问题是什么?", "这是答案")
        assert result.metric == METRIC_ANSWER_RELEVANCY
        assert result.score is None
        assert result.status == MetricStatus.UNAVAILABLE
        assert "全零向量" in result.reason
        assert embedding.calls == 1


class TestEvaluateSample:
    """evaluate_sample: 并发编排 + 不可用指标不进 scores"""

    async def test_only_available_metrics_enter_scores(self, monkeypatch):
        service, _ = _ragas(
            monkeypatch,
            [
                ("原子声明", '{"claims": ["c1", "c2"]}'),
                (
                    "事实核查员",
                    '{"verdicts": [{"index": 0, "verdict": 1, "reason": "ok"},'
                    ' {"index": 1, "verdict": 1, "reason": "ok"}]}',
                ),
            ],
            embedding_client=ZeroVectorEmbedding(),
            fail_on_miss=False,
        )
        result = await service.evaluate_sample(
            question="问题",
            answer="答案",
            contexts=["上下文"],
            metrics=[METRIC_FAITHFULNESS, METRIC_ANSWER_RELEVANCY],
            sample_id="s1",
        )
        assert result.sample_id == "s1"
        assert result.scores[METRIC_FAITHFULNESS] == pytest.approx(1.0)
        # embedding 不可用 => 该指标不得出现在 scores 中
        assert METRIC_ANSWER_RELEVANCY not in result.scores
        assert (
            result.metrics[METRIC_ANSWER_RELEVANCY].status == MetricStatus.UNAVAILABLE
        )

    async def test_unknown_metric_reported_unavailable(self, monkeypatch):
        service, _ = _ragas(monkeypatch, [], fail_on_miss=False)
        result = await service.evaluate_sample(
            question="问题", answer="答案", metrics=["not_a_metric"]
        )
        assert result.metrics["not_a_metric"].status == MetricStatus.UNAVAILABLE
        assert "未知指标" in result.metrics["not_a_metric"].reason

    async def test_applicable_metrics_filters_by_inputs(self):
        """缺 contexts / ground_truth 时不应尝试计算对应指标"""
        only_question = RagasMetricsService.applicable_metrics(
            contexts=[], ground_truth=None
        )
        assert only_question == [METRIC_ANSWER_RELEVANCY]

        with_all = RagasMetricsService.applicable_metrics(
            contexts=["c"], ground_truth="gt"
        )
        assert METRIC_FAITHFULNESS in with_all
        assert "context_recall" in with_all
        assert "answer_correctness" in with_all
