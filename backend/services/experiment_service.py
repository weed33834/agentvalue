"""实验对比服务 (WS-2 评估体系升级)

对标 Braintrust Experiments / LangSmith Experiments, 补齐 v2.3 审计发现的空白:
`agent_version_service.compare_versions` 与 `prompts.diff_versions` 只能 diff **配置**,
无法 diff **结果**。本服务让两次 run 按 sample_id 对齐, 回答
"Run B 相比 Run A 在这 12 条样本上差了 4%, 且该差异是否统计显著"。

三块能力:
1. CRUD:              Experiment / ExperimentRun 增删改查
2. execute_run:       拉数据集条目 -> 逐样本评测 -> 落 ExperimentRunItem ->
                      增量更新进度 -> 收尾算 metric_summary
3. compare_runs:      逐指标 delta + bootstrap 95% 置信区间 + 显著性,
                      逐样本 improved/regressed/unchanged/only_in_a/only_in_b

依赖约束: bootstrap 重采样只用标准库 `random`(requirements.txt 无 scipy/numpy,
不引入重依赖); 统计量用标准库 `statistics`。

事务边界: CRUD 方法只 flush 不 commit(由路由层控制);
`execute_run` 是长任务, 需要增量可见, 因此内部自行 commit。
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import random
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import AsyncSessionLocal
from core.llm_call import call_llm_with_fallback
from core.providers.base import ChatMessage
from core.tenant_context import tenant_scope
from models.experiment_models import (
    EXPERIMENT_STATUSES,
    EXPERIMENT_TASK_TYPES,
    Experiment,
    ExperimentRun,
    ExperimentRunItem,
    ITEM_STATUS_FAILED,
    ITEM_STATUS_SUCCESS,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_PENDING,
    RUN_STATUS_RUNNING,
    RUN_STATUSES,
)
from services.dataset_service import DatasetService
from services.llm_judge_service import (
    DEFAULT_JUDGE_PROMPT_TEMPLATE,
    LLMJudgeService,
)
from services.ragas_metrics_service import (
    ALL_METRICS as RAGAS_METRICS,
    MetricStatus,
    RagasMetricsService,
)

logger = logging.getLogger(__name__)

# 默认逐样本评测并发上限
DEFAULT_RUN_CONCURRENCY = 4

# 数据集条目分页拉取大小
DATASET_PAGE_SIZE = 100

# bootstrap 重采样次数
DEFAULT_BOOTSTRAP_ITERATIONS = 1000

# bootstrap 随机种子 (固定种子 => 同样输入产出同样置信区间, 便于 CI 复现)
DEFAULT_BOOTSTRAP_SEED = 20260808

# 置信水平
DEFAULT_CONFIDENCE = 0.95

# 逐样本 improved/regressed 判定容差 (浮点噪声视为 unchanged)
SAMPLE_DELTA_TOLERANCE = 1e-6

# CI 回归门禁默认阈值 (得分下降超过该值即视为回归)
DEFAULT_REGRESSION_THRESHOLD = 0.05

# judge 维度分数量纲 (LLM Judge 返回 0-100, 统一归一化到 0-1 与 RAGAS 对齐)
JUDGE_SCORE_SCALE = 100.0


# ============================================================
# 对比结果结构
# ============================================================


@dataclass
class MetricComparison:
    """单个指标在两次 run 之间的对比"""

    metric: str
    mean_a: Optional[float]
    mean_b: Optional[float]
    delta: Optional[float]
    delta_pct: Optional[float]
    winner: str
    n_a: int
    n_b: int
    n_paired: int
    ci_low: Optional[float]
    ci_high: Optional[float]
    significant: bool
    method: str

    def to_dict(self) -> Dict[str, Any]:
        """序列化"""
        return {
            "metric": self.metric,
            "mean_a": self.mean_a,
            "mean_b": self.mean_b,
            "delta": self.delta,
            "delta_pct": self.delta_pct,
            "winner": self.winner,
            "n_a": self.n_a,
            "n_b": self.n_b,
            "n_paired": self.n_paired,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "significant": self.significant,
            "method": self.method,
        }


@dataclass
class SampleComparison:
    """单条样本在两次 run 之间的对比"""

    sample_id: str
    classification: str
    delta: Optional[float]
    score_a: Optional[float]
    score_b: Optional[float]
    metric_deltas: Dict[str, Optional[float]] = field(default_factory=dict)
    input: Any = None

    def to_dict(self) -> Dict[str, Any]:
        """序列化"""
        return {
            "sample_id": self.sample_id,
            "classification": self.classification,
            "delta": self.delta,
            "score_a": self.score_a,
            "score_b": self.score_b,
            "metric_deltas": self.metric_deltas,
            "input": self.input,
        }


@dataclass
class ComparisonResult:
    """Run A vs Run B 的完整对比结果"""

    run_a: Dict[str, Any]
    run_b: Dict[str, Any]
    metrics: List[MetricComparison] = field(default_factory=list)
    samples: List[SampleComparison] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)
    verdict: str = ""
    bootstrap: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """序列化"""
        return {
            "run_a": self.run_a,
            "run_b": self.run_b,
            "metrics": [m.to_dict() for m in self.metrics],
            "samples": [s.to_dict() for s in self.samples],
            "counts": self.counts,
            "verdict": self.verdict,
            "bootstrap": self.bootstrap,
        }


# ============================================================
# 服务
# ============================================================


class ExperimentService:
    """实验 / 运行 / 对比服务"""

    def __init__(self, session: AsyncSession):
        self.session = session

    # ===================== Experiment CRUD =====================

    async def create_experiment(
        self,
        name: str,
        dataset_id: int,
        *,
        tenant_id: str = "default",
        description: Optional[str] = None,
        task_type: str = "rag",
        metrics: Optional[List[str]] = None,
        config: Optional[Dict[str, Any]] = None,
        created_by: Optional[str] = None,
    ) -> Experiment:
        """创建实验

        Args:
            name: 实验名称。
            dataset_id: 关联的评测数据集 ID。
            tenant_id: 租户 ID。
            description: 描述。
            task_type: rag / agent / prompt / judge。
            metrics: 参与评测的指标名列表。
            config: 实验级配置。
            created_by: 创建人。

        Returns:
            创建的 Experiment。

        Raises:
            ValueError: 名称为空 / 任务类型非法 / 数据集不存在。
        """
        if not name or not name.strip():
            raise ValueError("实验名称不能为空")
        if task_type not in EXPERIMENT_TASK_TYPES:
            raise ValueError(
                f"无效的任务类型: {task_type}, 可选: {sorted(EXPERIMENT_TASK_TYPES)}"
            )

        dataset = await DatasetService(self.session).get_dataset(
            dataset_id, tenant_id=tenant_id
        )
        if dataset is None:
            raise ValueError(f"数据集 {dataset_id} 不存在")

        entity = Experiment(
            tenant_id=tenant_id,
            name=name.strip(),
            description=description,
            dataset_id=dataset_id,
            task_type=task_type,
            metrics=metrics or list(RAGAS_METRICS),
            config=config or {},
            status="active",
            created_by=created_by,
        )
        self.session.add(entity)
        await self.session.flush()
        logger.info("创建实验: %s (数据集 %s, 租户 %s)", name, dataset_id, tenant_id)
        return entity

    async def get_experiment(
        self, experiment_id: int, *, tenant_id: str = "default"
    ) -> Optional[Experiment]:
        """获取实验详情"""
        return (
            await self.session.execute(
                select(Experiment).where(
                    Experiment.id == experiment_id,
                    Experiment.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()

    async def list_experiments(
        self,
        *,
        tenant_id: str = "default",
        status: Optional[str] = None,
        task_type: Optional[str] = None,
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """分页查询实验列表"""
        base = (
            select(Experiment)
            .where(Experiment.tenant_id == tenant_id)
            .order_by(Experiment.created_at.desc())
        )
        if status:
            base = base.where(Experiment.status == status)
        if task_type:
            base = base.where(Experiment.task_type == task_type)

        total = (
            await self.session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar() or 0

        rows = (
            (await self.session.execute(base.offset((page - 1) * size).limit(size)))
            .scalars()
            .all()
        )
        return {
            "items": [self._experiment_to_dict(e) for e in rows],
            "total": total,
            "page": page,
            "size": size,
        }

    async def update_experiment(
        self,
        experiment_id: int,
        *,
        tenant_id: str = "default",
        name: Optional[str] = None,
        description: Optional[str] = None,
        metrics: Optional[List[str]] = None,
        config: Optional[Dict[str, Any]] = None,
        status: Optional[str] = None,
    ) -> Optional[Experiment]:
        """更新实验"""
        entity = await self.get_experiment(experiment_id, tenant_id=tenant_id)
        if entity is None:
            return None

        if name is not None:
            if not name.strip():
                raise ValueError("实验名称不能为空")
            entity.name = name.strip()
        if description is not None:
            entity.description = description
        if metrics is not None:
            entity.metrics = metrics
        if config is not None:
            entity.config = config
        if status is not None:
            if status not in EXPERIMENT_STATUSES:
                raise ValueError(
                    f"无效的实验状态: {status}, 可选: {sorted(EXPERIMENT_STATUSES)}"
                )
            entity.status = status

        await self.session.flush()
        return entity

    async def delete_experiment(
        self, experiment_id: int, *, tenant_id: str = "default"
    ) -> bool:
        """删除实验 (级联删除其下的 run 与逐样本结果)"""
        entity = await self.get_experiment(experiment_id, tenant_id=tenant_id)
        if entity is None:
            return False

        runs = (
            (
                await self.session.execute(
                    select(ExperimentRun).where(
                        ExperimentRun.experiment_id == experiment_id,
                        ExperimentRun.tenant_id == tenant_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for run in runs:
            await self._delete_run_items(run.id, tenant_id=tenant_id)
            await self.session.delete(run)

        await self.session.delete(entity)
        await self.session.flush()
        logger.info("删除实验 id=%s (含 %d 次运行)", experiment_id, len(runs))
        return True

    # ===================== ExperimentRun CRUD =====================

    async def create_run(
        self,
        experiment_id: int,
        *,
        tenant_id: str = "default",
        name: Optional[str] = None,
        variant: Optional[Dict[str, Any]] = None,
    ) -> ExperimentRun:
        """创建实验运行 (初始 pending, 由 execute_run 推进)

        Raises:
            ValueError: 实验不存在。
        """
        experiment = await self.get_experiment(experiment_id, tenant_id=tenant_id)
        if experiment is None:
            raise ValueError(f"实验 {experiment_id} 不存在")

        run = ExperimentRun(
            experiment_id=experiment_id,
            tenant_id=tenant_id,
            name=(name or "").strip() or f"run-{int(time.time())}",
            variant=variant or {},
            status=RUN_STATUS_PENDING,
        )
        self.session.add(run)
        await self.session.flush()
        logger.info("创建实验运行 %s (实验 %s)", run.id, experiment_id)
        return run

    async def get_run(
        self, run_id: int, *, tenant_id: str = "default"
    ) -> Optional[ExperimentRun]:
        """获取运行详情"""
        return (
            await self.session.execute(
                select(ExperimentRun).where(
                    ExperimentRun.id == run_id,
                    ExperimentRun.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()

    async def list_runs(
        self,
        experiment_id: int,
        *,
        tenant_id: str = "default",
        status: Optional[str] = None,
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """分页查询某实验下的运行列表"""
        base = (
            select(ExperimentRun)
            .where(
                ExperimentRun.experiment_id == experiment_id,
                ExperimentRun.tenant_id == tenant_id,
            )
            .order_by(ExperimentRun.created_at.desc())
        )
        if status:
            base = base.where(ExperimentRun.status == status)

        total = (
            await self.session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar() or 0

        rows = (
            (await self.session.execute(base.offset((page - 1) * size).limit(size)))
            .scalars()
            .all()
        )
        return {
            "items": [self._run_to_dict(r) for r in rows],
            "total": total,
            "page": page,
            "size": size,
        }

    async def list_run_items(
        self,
        run_id: int,
        *,
        tenant_id: str = "default",
        status: Optional[str] = None,
        metric: Optional[str] = None,
        min_score: Optional[float] = None,
        max_score: Optional[float] = None,
        page: int = 1,
        size: int = 50,
    ) -> Dict[str, Any]:
        """分页查询逐样本结果, 支持按状态与得分区间过滤

        得分过滤在 Python 侧做: scores 是 JSON 列, SQLite / PG 的 JSON 路径
        查询语法不一致, 且单次 run 的样本量在可接受范围内。
        """
        base = (
            select(ExperimentRunItem)
            .where(
                ExperimentRunItem.run_id == run_id,
                ExperimentRunItem.tenant_id == tenant_id,
            )
            .order_by(ExperimentRunItem.id.asc())
        )
        if status:
            base = base.where(ExperimentRunItem.status == status)

        rows = (await self.session.execute(base)).scalars().all()

        if min_score is not None or max_score is not None:
            filtered = []
            for row in rows:
                value = self._composite_score(row.scores or {}, metric=metric)
                if value is None:
                    continue
                if min_score is not None and value < min_score:
                    continue
                if max_score is not None and value > max_score:
                    continue
                filtered.append(row)
            rows = filtered

        total = len(rows)
        offset = (page - 1) * size
        page_rows = rows[offset : offset + size]
        return {
            "items": [self._item_to_dict(i) for i in page_rows],
            "total": total,
            "page": page,
            "size": size,
        }

    async def cancel_run(self, run_id: int, *, tenant_id: str = "default") -> bool:
        """取消运行 (pending / running -> cancelled)

        执行循环每条样本前会重读状态, 因此取消最迟在下一条样本处生效。
        """
        run = await self.get_run(run_id, tenant_id=tenant_id)
        if run is None:
            return False
        if run.status not in (RUN_STATUS_PENDING, RUN_STATUS_RUNNING):
            return False
        run.status = RUN_STATUS_CANCELLED
        run.completed_at = datetime.now(timezone.utc)
        await self.session.flush()
        logger.info("取消实验运行 %s", run_id)
        return True

    async def delete_run(self, run_id: int, *, tenant_id: str = "default") -> bool:
        """删除运行及其逐样本结果"""
        run = await self.get_run(run_id, tenant_id=tenant_id)
        if run is None:
            return False
        await self._delete_run_items(run_id, tenant_id=tenant_id)
        await self.session.delete(run)
        await self.session.flush()
        return True

    async def _delete_run_items(self, run_id: int, *, tenant_id: str) -> int:
        """删除某 run 的全部逐样本结果"""
        items = (
            (
                await self.session.execute(
                    select(ExperimentRunItem).where(
                        ExperimentRunItem.run_id == run_id,
                        ExperimentRunItem.tenant_id == tenant_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for item in items:
            await self.session.delete(item)
        return len(items)

    # ===================== 运行执行 =====================

    async def execute_run(
        self,
        run_id: int,
        *,
        tenant_id: str = "default",
        model_router: Any = None,
        concurrency: Optional[int] = None,
    ) -> Dict[str, Any]:
        """执行实验运行: 逐样本评测 -> 落库 -> 增量更新进度 -> 汇总

        流程:
        1. 校验 run / experiment, 置 running;
        2. 通过 DatasetService.list_items 分页拉全部数据集条目;
        3. 有界并发逐条评测(每条独立 try/except, 单条失败只标记该条 failed);
        4. 每完成一条即写 ExperimentRunItem 并更新 progress(供 UI 轮询);
        5. 全部结束后计算 metric_summary。

        Args:
            run_id: 运行 ID。
            tenant_id: 租户 ID。
            model_router: ModelRouter 实例; 为 None 时无法调用 LLM,
                所有样本会被标记为 failed 并给出明确原因(不编造得分)。
            concurrency: 并发上限, 缺省读 experiment.config.concurrency。

        Returns:
            {"run_id", "status", "total_items", "completed_items",
             "failed_items", "metric_summary"}
        """
        run = await self.get_run(run_id, tenant_id=tenant_id)
        if run is None:
            raise ValueError(f"实验运行 {run_id} 不存在")
        experiment = await self.get_experiment(run.experiment_id, tenant_id=tenant_id)
        if experiment is None:
            raise ValueError(f"实验 {run.experiment_id} 不存在")

        started = time.monotonic()
        run.status = RUN_STATUS_RUNNING
        run.started_at = datetime.now(timezone.utc)
        run.error = None
        await self.session.commit()

        try:
            items = await self._load_dataset_items(
                experiment.dataset_id, tenant_id=tenant_id
            )
            run.total_items = len(items)
            run.completed_items = 0
            run.failed_items = 0
            run.progress = 0 if items else 100
            await self.session.commit()

            metrics = list(experiment.metrics or RAGAS_METRICS)
            limit = concurrency or int(
                (experiment.config or {}).get("concurrency", DEFAULT_RUN_CONCURRENCY)
            )
            sem = asyncio.Semaphore(max(1, limit))
            ragas = RagasMetricsService(model_router) if model_router else None
            progress_lock = asyncio.Lock()

            async def _evaluate(index: int, raw: Dict[str, Any]) -> ExperimentRunItem:
                async with sem:
                    return await self._evaluate_item(
                        raw,
                        run=run,
                        experiment=experiment,
                        metrics=metrics,
                        ragas=ragas,
                        model_router=model_router,
                        tenant_id=tenant_id,
                        fallback_sample_id=str(index),
                    )

            pending: List[Dict[str, Any]] = list(items)
            cancelled = False
            for offset in range(0, len(pending), max(1, limit)):
                # 每批开始前重读状态, 支持取消
                await self.session.refresh(run)
                if run.status == RUN_STATUS_CANCELLED:
                    cancelled = True
                    break

                batch = pending[offset : offset + max(1, limit)]
                results = await asyncio.gather(
                    *(_evaluate(offset + i, raw) for i, raw in enumerate(batch)),
                    return_exceptions=True,
                )
                async with progress_lock:
                    for i, outcome in enumerate(results):
                        if isinstance(outcome, BaseException):
                            # gather 兜底: 理论上 _evaluate_item 内部已吞掉异常
                            logger.warning("样本评测异常: %s", outcome)
                            entity = ExperimentRunItem(
                                run_id=run.id,
                                experiment_id=experiment.id,
                                tenant_id=tenant_id,
                                sample_id=str(offset + i),
                                scores={},
                                status=ITEM_STATUS_FAILED,
                                error=str(outcome),
                            )
                        else:
                            entity = outcome
                        self.session.add(entity)
                        run.completed_items += 1
                        if entity.status == ITEM_STATUS_FAILED:
                            run.failed_items += 1
                        run.total_cost = round(
                            (run.total_cost or 0.0) + (entity.cost or 0.0), 6
                        )
                        run.total_tokens = (run.total_tokens or 0) + (
                            entity.tokens or 0
                        )
                    run.progress = (
                        int(run.completed_items / run.total_items * 100)
                        if run.total_items
                        else 100
                    )
                    await self.session.commit()

            summary = await self._compute_metric_summary(run.id, tenant_id=tenant_id)
            run.metric_summary = summary
            run.duration_ms = int((time.monotonic() - started) * 1000)
            run.completed_at = datetime.now(timezone.utc)
            if cancelled:
                run.status = RUN_STATUS_CANCELLED
            else:
                run.status = RUN_STATUS_COMPLETED
                run.progress = 100
            await self.session.commit()

            logger.info(
                "实验运行 %s 结束: status=%s %d/%d (失败 %d)",
                run_id,
                run.status,
                run.completed_items,
                run.total_items,
                run.failed_items,
            )
            return {
                "run_id": run_id,
                "status": run.status,
                "total_items": run.total_items,
                "completed_items": run.completed_items,
                "failed_items": run.failed_items,
                "metric_summary": summary,
            }

        except Exception as e:  # noqa: BLE001 - 运行级失败需落库
            logger.error("实验运行 %s 执行失败: %s", run_id, e, exc_info=True)
            await self.session.rollback()
            run = await self.get_run(run_id, tenant_id=tenant_id)
            if run is not None:
                run.status = RUN_STATUS_FAILED
                run.error = str(e)
                run.duration_ms = int((time.monotonic() - started) * 1000)
                run.completed_at = datetime.now(timezone.utc)
                await self.session.commit()
            raise

    async def _load_dataset_items(
        self, dataset_id: int, *, tenant_id: str
    ) -> List[Dict[str, Any]]:
        """通过 DatasetService 分页拉取数据集全部条目 (复用既有列表 API)"""
        dataset_service = DatasetService(self.session)
        collected: List[Dict[str, Any]] = []
        page = 1
        while True:
            payload = await dataset_service.list_items(
                dataset_id,
                tenant_id=tenant_id,
                page=page,
                size=DATASET_PAGE_SIZE,
            )
            batch = payload.get("items") or []
            collected.extend(batch)
            if len(collected) >= int(payload.get("total") or 0) or not batch:
                break
            page += 1
        return collected

    async def _evaluate_item(
        self,
        raw: Dict[str, Any],
        *,
        run: ExperimentRun,
        experiment: Experiment,
        metrics: List[str],
        ragas: Optional[RagasMetricsService],
        model_router: Any,
        tenant_id: str,
        fallback_sample_id: str,
    ) -> ExperimentRunItem:
        """评测单条样本, 返回待落库的 ExperimentRunItem (自身不抛异常)"""
        started = time.monotonic()
        sample_id = str(raw.get("id") or fallback_sample_id)
        input_data = raw.get("input") or {}
        expected = raw.get("expected_output")
        metadata = raw.get("metadata") or {}

        question = LLMJudgeService._extract_text(input_data)
        ground_truth = LLMJudgeService._extract_text(expected) if expected else ""
        contexts = _extract_contexts(input_data, metadata)

        item = ExperimentRunItem(
            run_id=run.id,
            experiment_id=experiment.id,
            tenant_id=tenant_id,
            sample_id=sample_id,
            dataset_item_id=raw.get("id") if isinstance(raw.get("id"), int) else None,
            input=input_data if isinstance(input_data, dict) else {"text": question},
            expected=expected if isinstance(expected, dict) else {"text": ground_truth},
            contexts=contexts,
            scores={},
            status=ITEM_STATUS_SUCCESS,
        )

        if model_router is None:
            item.status = ITEM_STATUS_FAILED
            item.error = "未提供 model_router, 无法调用 LLM 生成/评分"
            item.latency_ms = int((time.monotonic() - started) * 1000)
            return item

        # 1. 产出被测答案: 样本已带答案则直接复用, 否则按 variant 调 LLM 生成
        answer, tokens, gen_error = await self._produce_answer(
            question=question,
            input_data=input_data,
            metadata=metadata,
            variant=run.variant or {},
            model_router=model_router,
        )
        item.tokens = tokens
        if gen_error:
            item.status = ITEM_STATUS_FAILED
            item.error = gen_error
            item.latency_ms = int((time.monotonic() - started) * 1000)
            return item
        item.actual = {"text": answer}

        # 2. 打分: RAGAS 指标走 ragas_metrics_service, 其余维度走 LLM Judge
        ragas_names = [m for m in metrics if m in RAGAS_METRICS]
        judge_names = [m for m in metrics if m not in RAGAS_METRICS]
        scores: Dict[str, float] = {}
        metric_details: Dict[str, Any] = {}
        errors: List[str] = []

        if ragas_names and ragas is not None:
            try:
                result = await ragas.evaluate_sample(
                    question=question,
                    answer=answer,
                    contexts=contexts,
                    ground_truth=ground_truth or None,
                    metrics=ragas_names,
                    sample_id=sample_id,
                )
                for name, metric_result in result.metrics.items():
                    metric_details[name] = {
                        "status": metric_result.status,
                        "reason": metric_result.reason,
                    }
                    if (
                        metric_result.score is not None
                        and metric_result.status != MetricStatus.UNAVAILABLE
                    ):
                        scores[name] = metric_result.score
                    else:
                        errors.append(f"{name}: {metric_result.reason}")
            except Exception as e:  # noqa: BLE001 - 单条打分失败不影响整批
                errors.append(f"RAGAS 打分失败: {e}")

        if judge_names:
            judged, judge_error = await self._judge_dimensions(
                question=question,
                answer=answer,
                ground_truth=ground_truth,
                dimensions=judge_names,
                model_router=model_router,
            )
            scores.update(judged)
            if judge_error:
                errors.append(judge_error)

        item.scores = scores
        item.latency_ms = int((time.monotonic() - started) * 1000)
        if not scores:
            item.status = ITEM_STATUS_FAILED
            item.error = "; ".join(errors) or "所有指标均不可用"
        elif errors:
            # 部分指标不可用: 结果仍可用, 但把原因留痕, 不用 0 填充缺失指标
            item.error = "; ".join(errors)[:2000]
        if metric_details:
            item.actual = {**(item.actual or {}), "metric_details": metric_details}
        return item

    async def _produce_answer(
        self,
        *,
        question: str,
        input_data: Any,
        metadata: Dict[str, Any],
        variant: Dict[str, Any],
        model_router: Any,
    ) -> Tuple[str, int, str]:
        """获取被测答案

        优先复用样本里已有的答案(离线打分场景), 否则按 variant 里的
        system_prompt / prompt_template 调 LLM 现场生成(在线对比场景)。

        Returns:
            (answer, tokens, error) —— error 非空表示生成失败。
        """
        for source in (input_data, metadata):
            if isinstance(source, dict):
                for key in ("answer", "output", "actual", "response"):
                    value = source.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip(), 0, ""

        system_prompt = (
            variant.get("system_prompt")
            or variant.get("prompt")
            or "你是一个专业的 AI 助手, 请根据输入生成准确、有据可依的回答。"
        )
        try:
            completion, _tier = await call_llm_with_fallback(
                model_router,
                messages=[
                    ChatMessage(role="system", content=str(system_prompt)),
                    ChatMessage(role="user", content=question),
                ],
                response_format={"type": "text"},
            )
        except Exception as e:  # noqa: BLE001
            return "", 0, f"生成答案失败: {e}"

        usage = getattr(completion, "usage", None) or {}
        tokens = int(usage.get("total_tokens") or 0) if isinstance(usage, dict) else 0
        return (completion.content or "").strip(), tokens, ""

    async def _judge_dimensions(
        self,
        *,
        question: str,
        answer: str,
        ground_truth: str,
        dimensions: List[str],
        model_router: Any,
    ) -> Tuple[Dict[str, float], str]:
        """用 LLM Judge 给非 RAGAS 维度打分

        直接复用 llm_judge_service 的提示词模板与响应解析器, 保证与
        LLM-as-a-Judge 任务链路口径一致; 分数从 0-100 归一化到 0-1。

        Returns:
            ({dimension: score_0_1}, error) —— 失败时返回 ({}, 原因)。
        """
        prompt = DEFAULT_JUDGE_PROMPT_TEMPLATE.format(
            input=question,
            expected_output=ground_truth or "无",
            output=answer,
            metrics="\n".join(f"- {d}" for d in dimensions),
        )
        try:
            completion, _tier = await call_llm_with_fallback(
                model_router, messages=[ChatMessage(role="system", content=prompt)]
            )
        except Exception as e:  # noqa: BLE001
            return {}, f"LLM Judge 调用失败: {e}"

        raw_scores, _feedback = LLMJudgeService._parse_judge_response(
            completion.content or "", dimensions
        )
        out: Dict[str, float] = {}
        for dim in dimensions:
            value = raw_scores.get(dim)
            if isinstance(value, (int, float)):
                out[dim] = round(
                    max(0.0, min(1.0, float(value) / JUDGE_SCORE_SCALE)), 6
                )
        if not out:
            return {}, "LLM Judge 未返回任何可用维度分数"
        return out, ""

    async def _compute_metric_summary(
        self, run_id: int, *, tenant_id: str
    ) -> Dict[str, Any]:
        """按指标聚合 {mean, std, min, max, n}

        只统计真实写入的得分; 某指标全程不可用时该指标不出现在 summary 中。
        """
        rows = (
            (
                await self.session.execute(
                    select(ExperimentRunItem).where(
                        ExperimentRunItem.run_id == run_id,
                        ExperimentRunItem.tenant_id == tenant_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        buckets: Dict[str, List[float]] = {}
        for row in rows:
            for name, value in (row.scores or {}).items():
                if isinstance(value, (int, float)):
                    buckets.setdefault(name, []).append(float(value))
        return summarize_metric_buckets(buckets)

    # ===================== Run 对比 (核心能力) =====================

    async def compare_runs(
        self,
        run_a_id: int,
        run_b_id: int,
        *,
        tenant_id: str = "default",
        bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
        seed: int = DEFAULT_BOOTSTRAP_SEED,
        confidence: float = DEFAULT_CONFIDENCE,
        primary_metric: Optional[str] = None,
        max_samples: Optional[int] = None,
    ) -> ComparisonResult:
        """对比两次运行: 逐指标 delta + bootstrap 置信区间 + 逐样本回归清单

        指标层:
        - mean_a / mean_b / delta(=b-a) / delta_pct / winner
        - bootstrap 95% 置信区间: 有配对样本时对**配对差值**重采样(方差更小),
          否则对两组独立重采样后取均值差; CI 不含 0 即判 significant
        - 只用标准库 random, 固定 seed 保证同输入同结果(CI 可复现)

        样本层:
        - 按 sample_id 内连接, 分类为 improved / regressed / unchanged /
          only_in_a / only_in_b, 按"回归最严重"排序(delta 升序)

        Args:
            run_a_id: 基线 run。
            run_b_id: 候选 run。
            tenant_id: 租户 ID。
            bootstrap_iterations: 重采样次数。
            seed: 随机种子。
            confidence: 置信水平 (0-1)。
            primary_metric: 逐样本对比用的主指标; 缺省用共同指标的均值。
            max_samples: 逐样本清单最多返回条数 (None 表示全部)。

        Returns:
            ComparisonResult。

        Raises:
            ValueError: 任一 run 不存在。
        """
        run_a = await self.get_run(run_a_id, tenant_id=tenant_id)
        run_b = await self.get_run(run_b_id, tenant_id=tenant_id)
        if run_a is None:
            raise ValueError(f"实验运行 {run_a_id} 不存在")
        if run_b is None:
            raise ValueError(f"实验运行 {run_b_id} 不存在")

        items_a = await self._fetch_items(run_a_id, tenant_id=tenant_id)
        items_b = await self._fetch_items(run_b_id, tenant_id=tenant_id)

        scores_a = {i.sample_id: dict(i.scores or {}) for i in items_a}
        scores_b = {i.sample_id: dict(i.scores or {}) for i in items_b}
        inputs = {i.sample_id: i.input for i in items_a}
        inputs.update(
            {i.sample_id: i.input for i in items_b if i.sample_id not in inputs}
        )

        metric_names = sorted(
            {m for s in scores_a.values() for m in s}
            | {m for s in scores_b.values() for m in s}
        )

        rng = random.Random(seed)
        metric_comparisons = [
            compare_metric(
                metric=name,
                scores_a=scores_a,
                scores_b=scores_b,
                rng=rng,
                iterations=bootstrap_iterations,
                confidence=confidence,
            )
            for name in metric_names
        ]

        samples = classify_samples(
            scores_a=scores_a,
            scores_b=scores_b,
            primary_metric=primary_metric,
            inputs=inputs,
        )
        counts = {
            "improved": 0,
            "regressed": 0,
            "unchanged": 0,
            "only_in_a": 0,
            "only_in_b": 0,
        }
        for s in samples:
            counts[s.classification] = counts.get(s.classification, 0) + 1
        counts["total"] = len(samples)

        limited = samples if max_samples is None else samples[:max_samples]

        return ComparisonResult(
            run_a=self._run_to_dict(run_a),
            run_b=self._run_to_dict(run_b),
            metrics=metric_comparisons,
            samples=limited,
            counts=counts,
            verdict=build_verdict(metric_comparisons, counts),
            bootstrap={
                "iterations": bootstrap_iterations,
                "seed": seed,
                "confidence": confidence,
                "implementation": "stdlib random (无 scipy/numpy 依赖)",
            },
        )

    async def get_regression_report(
        self,
        run_a_id: int,
        run_b_id: int,
        threshold: float = DEFAULT_REGRESSION_THRESHOLD,
        *,
        tenant_id: str = "default",
        primary_metric: Optional[str] = None,
    ) -> Dict[str, Any]:
        """CI 门禁用回归报告: 挑出下降幅度超过 threshold 的样本与指标

        Args:
            run_a_id: 基线 run。
            run_b_id: 候选 run。
            threshold: 回归判定阈值 (正数, 单位与得分一致, 0-1 量纲)。
            tenant_id: 租户 ID。
            primary_metric: 逐样本主指标。

        Returns:
            {"has_regression", "threshold", "regressed_samples",
             "regressed_metrics", "counts", "verdict"}
        """
        threshold = abs(float(threshold))
        comparison = await self.compare_runs(
            run_a_id,
            run_b_id,
            tenant_id=tenant_id,
            primary_metric=primary_metric,
        )

        regressed_samples = [
            s.to_dict()
            for s in comparison.samples
            if s.classification == "regressed"
            and s.delta is not None
            and -s.delta > threshold
        ]
        regressed_metrics = [
            m.to_dict()
            for m in comparison.metrics
            if m.delta is not None and -m.delta > threshold
        ]

        has_regression = bool(regressed_samples or regressed_metrics)
        return {
            "run_a_id": run_a_id,
            "run_b_id": run_b_id,
            "threshold": threshold,
            "has_regression": has_regression,
            "regressed_samples": regressed_samples,
            "regressed_metrics": regressed_metrics,
            "counts": comparison.counts,
            "verdict": (
                f"检测到 {len(regressed_samples)} 条样本 / "
                f"{len(regressed_metrics)} 个指标回归超过阈值 {threshold}"
                if has_regression
                else f"未检测到超过阈值 {threshold} 的回归"
            ),
        }

    async def _fetch_items(
        self, run_id: int, *, tenant_id: str
    ) -> List[ExperimentRunItem]:
        """取某 run 的全部逐样本结果"""
        return list(
            (
                await self.session.execute(
                    select(ExperimentRunItem)
                    .where(
                        ExperimentRunItem.run_id == run_id,
                        ExperimentRunItem.tenant_id == tenant_id,
                    )
                    .order_by(ExperimentRunItem.id.asc())
                )
            )
            .scalars()
            .all()
        )

    # ===================== 导出 =====================

    async def export_run(
        self, run_id: int, format: str = "json", *, tenant_id: str = "default"
    ) -> str:
        """导出某 run 的逐样本结果 (json / csv)

        Raises:
            ValueError: run 不存在或格式不支持。
        """
        run = await self.get_run(run_id, tenant_id=tenant_id)
        if run is None:
            raise ValueError(f"实验运行 {run_id} 不存在")
        items = await self._fetch_items(run_id, tenant_id=tenant_id)

        fmt = format.lower()
        if fmt == "json":
            return json.dumps(
                {
                    "run": self._run_to_dict(run),
                    "items": [self._item_to_dict(i) for i in items],
                },
                ensure_ascii=False,
                indent=2,
            )
        if fmt == "csv":
            metric_names = sorted(
                {m for i in items for m in (i.scores or {}) if isinstance(m, str)}
            )
            output = io.StringIO()
            writer = csv.DictWriter(
                output,
                fieldnames=[
                    "sample_id",
                    "status",
                    "latency_ms",
                    "cost",
                    "tokens",
                    "input",
                    "expected",
                    "actual",
                    "error",
                    *metric_names,
                ],
            )
            writer.writeheader()
            for i in items:
                row = {
                    "sample_id": i.sample_id,
                    "status": i.status,
                    "latency_ms": i.latency_ms,
                    "cost": i.cost,
                    "tokens": i.tokens,
                    "input": json.dumps(i.input, ensure_ascii=False),
                    "expected": json.dumps(i.expected, ensure_ascii=False),
                    "actual": json.dumps(i.actual, ensure_ascii=False),
                    "error": i.error or "",
                }
                for name in metric_names:
                    row[name] = (i.scores or {}).get(name, "")
                writer.writerow(row)
            return output.getvalue()
        raise ValueError(f"不支持的导出格式: {format}, 可选: json / csv")

    # ===================== 序列化辅助 =====================

    @staticmethod
    def _composite_score(
        scores: Dict[str, Any], *, metric: Optional[str] = None
    ) -> Optional[float]:
        """逐样本综合得分: 指定 metric 时取该指标, 否则取全部指标均值"""
        if metric:
            value = scores.get(metric)
            return float(value) if isinstance(value, (int, float)) else None
        values = [float(v) for v in scores.values() if isinstance(v, (int, float))]
        return round(sum(values) / len(values), 6) if values else None

    @staticmethod
    def _experiment_to_dict(e: Experiment) -> Dict[str, Any]:
        """Experiment -> dict"""
        return {
            "id": e.id,
            "tenant_id": e.tenant_id,
            "name": e.name,
            "description": e.description,
            "dataset_id": e.dataset_id,
            "task_type": e.task_type,
            "metrics": e.metrics,
            "config": e.config,
            "status": e.status,
            "created_by": e.created_by,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "updated_at": e.updated_at.isoformat() if e.updated_at else None,
        }

    @staticmethod
    def _run_to_dict(r: ExperimentRun) -> Dict[str, Any]:
        """ExperimentRun -> dict"""
        return {
            "id": r.id,
            "experiment_id": r.experiment_id,
            "tenant_id": r.tenant_id,
            "name": r.name,
            "variant": r.variant,
            "status": r.status,
            "progress": r.progress,
            "total_items": r.total_items,
            "completed_items": r.completed_items,
            "failed_items": r.failed_items,
            "metric_summary": r.metric_summary,
            "total_cost": r.total_cost,
            "total_tokens": r.total_tokens,
            "duration_ms": r.duration_ms,
            "error": r.error,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        }

    @staticmethod
    def _item_to_dict(i: ExperimentRunItem) -> Dict[str, Any]:
        """ExperimentRunItem -> dict"""
        return {
            "id": i.id,
            "run_id": i.run_id,
            "experiment_id": i.experiment_id,
            "tenant_id": i.tenant_id,
            "sample_id": i.sample_id,
            "dataset_item_id": i.dataset_item_id,
            "input": i.input,
            "expected": i.expected,
            "actual": i.actual,
            "contexts": i.contexts,
            "scores": i.scores,
            "latency_ms": i.latency_ms,
            "cost": i.cost,
            "tokens": i.tokens,
            "status": i.status,
            "error": i.error,
            "created_at": i.created_at.isoformat() if i.created_at else None,
        }


# ============================================================
# 后台执行入口
# ============================================================


async def execute_run_background(
    run_id: int,
    *,
    tenant_id: str = "default",
    model_router: Any = None,
) -> None:
    """后台执行实验运行 (供 FastAPI BackgroundTasks 调用)

    使用独立数据库会话 + 显式租户上下文, 与 llm_judge_service 后台任务一致。
    刻意不使用裸 `asyncio.create_task` —— v3 审计已把那类"重启即丢"的任务列为缺陷,
    这里由 BackgroundTasks 托管 (arq 队列当前只注册了 run_evaluation_task 一种函数,
    尚未提供通用任务分发, 故不走 arq)。
    """
    with tenant_scope(tenant_id):
        async with AsyncSessionLocal() as session:
            service = ExperimentService(session)
            try:
                await service.execute_run(
                    run_id, tenant_id=tenant_id, model_router=model_router
                )
            except Exception:
                logger.error("后台执行实验运行 %s 失败", run_id, exc_info=True)


# ============================================================
# 纯函数: 统计 / bootstrap / 分类 (无 IO, 便于单测)
# ============================================================


def summarize_metric_buckets(
    buckets: Dict[str, List[float]],
) -> Dict[str, Dict[str, Any]]:
    """把 {metric: [scores]} 聚合成 {metric: {mean,std,min,max,n}}

    std 用样本标准差 (n-1); n == 1 时为 0.0。
    """
    summary: Dict[str, Dict[str, Any]] = {}
    for name, values in buckets.items():
        if not values:
            continue
        summary[name] = {
            "mean": round(statistics.fmean(values), 6),
            "std": round(statistics.stdev(values), 6) if len(values) > 1 else 0.0,
            "min": round(min(values), 6),
            "max": round(max(values), 6),
            "n": len(values),
        }
    return summary


def bootstrap_delta_ci(
    values_a: Sequence[float],
    values_b: Sequence[float],
    *,
    rng: random.Random,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    confidence: float = DEFAULT_CONFIDENCE,
    paired: bool = False,
) -> Tuple[Optional[float], Optional[float], str]:
    """bootstrap 重采样估计 mean(b) - mean(a) 的置信区间

    纯标准库实现 (requirements.txt 无 scipy/numpy, 且不允许新增重依赖):
    - paired=True: 对配对差值 d_i = b_i - a_i 有放回重采样, 每轮取均值
    - paired=False: 对两组分别有放回重采样, 每轮取均值之差
    最后取重采样分布的 [(1-c)/2, 1-(1-c)/2] 分位数。

    Args:
        values_a: 基线组得分。
        values_b: 候选组得分。
        rng: 已 seed 的 random.Random, 决定结果可复现。
        iterations: 重采样次数。
        confidence: 置信水平。
        paired: 是否按配对差值重采样。

    Returns:
        (ci_low, ci_high, method); 样本不足时返回 (None, None, "insufficient_data")。
    """
    if iterations <= 0:
        return None, None, "insufficient_data"

    if paired:
        if len(values_a) != len(values_b) or len(values_a) < 2:
            return None, None, "insufficient_data"
        deltas = [b - a for a, b in zip(values_a, values_b)]
        n = len(deltas)
        means: List[float] = []
        for _ in range(iterations):
            total = 0.0
            for _ in range(n):
                total += deltas[rng.randrange(n)]
            means.append(total / n)
        method = "paired_bootstrap"
    else:
        if len(values_a) < 2 or len(values_b) < 2:
            return None, None, "insufficient_data"
        na, nb = len(values_a), len(values_b)
        means = []
        for _ in range(iterations):
            sa = 0.0
            for _ in range(na):
                sa += values_a[rng.randrange(na)]
            sb = 0.0
            for _ in range(nb):
                sb += values_b[rng.randrange(nb)]
            means.append(sb / nb - sa / na)
        method = "unpaired_bootstrap"

    means.sort()
    alpha = (1.0 - confidence) / 2.0
    low = _percentile(means, alpha)
    high = _percentile(means, 1.0 - alpha)
    return round(low, 6), round(high, 6), method


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """线性插值分位数 (输入必须已排序)"""
    if not sorted_values:
        raise ValueError("分位数计算输入为空")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    q = max(0.0, min(1.0, q))
    pos = q * (len(sorted_values) - 1)
    lower = int(pos)
    upper = min(lower + 1, len(sorted_values) - 1)
    frac = pos - lower
    return float(sorted_values[lower]) * (1 - frac) + float(sorted_values[upper]) * frac


def compare_metric(
    *,
    metric: str,
    scores_a: Dict[str, Dict[str, Any]],
    scores_b: Dict[str, Dict[str, Any]],
    rng: random.Random,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    confidence: float = DEFAULT_CONFIDENCE,
) -> MetricComparison:
    """计算单个指标的两 run 对比 (含 bootstrap CI 与显著性)

    所有指标按"越高越好"处理 (RAGAS 与归一化后的 judge 维度均满足)。
    """
    values_a = [
        float(s[metric])
        for s in scores_a.values()
        if isinstance(s.get(metric), (int, float))
    ]
    values_b = [
        float(s[metric])
        for s in scores_b.values()
        if isinstance(s.get(metric), (int, float))
    ]

    paired_a: List[float] = []
    paired_b: List[float] = []
    for sample_id, sa in scores_a.items():
        sb = scores_b.get(sample_id)
        if sb is None:
            continue
        va, vb = sa.get(metric), sb.get(metric)
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            paired_a.append(float(va))
            paired_b.append(float(vb))

    mean_a = round(statistics.fmean(values_a), 6) if values_a else None
    mean_b = round(statistics.fmean(values_b), 6) if values_b else None
    delta = (
        round(mean_b - mean_a, 6) if mean_a is not None and mean_b is not None else None
    )
    delta_pct = (
        round(delta / abs(mean_a) * 100, 4)
        if delta is not None and mean_a not in (None, 0)
        else None
    )

    if delta is None:
        winner = "unknown"
    elif delta > SAMPLE_DELTA_TOLERANCE:
        winner = "b"
    elif delta < -SAMPLE_DELTA_TOLERANCE:
        winner = "a"
    else:
        winner = "tie"

    if len(paired_a) >= 2:
        ci_low, ci_high, method = bootstrap_delta_ci(
            paired_a,
            paired_b,
            rng=rng,
            iterations=iterations,
            confidence=confidence,
            paired=True,
        )
    else:
        ci_low, ci_high, method = bootstrap_delta_ci(
            values_a,
            values_b,
            rng=rng,
            iterations=iterations,
            confidence=confidence,
            paired=False,
        )

    significant = (
        ci_low is not None and ci_high is not None and (ci_low > 0.0 or ci_high < 0.0)
    )

    return MetricComparison(
        metric=metric,
        mean_a=mean_a,
        mean_b=mean_b,
        delta=delta,
        delta_pct=delta_pct,
        winner=winner,
        n_a=len(values_a),
        n_b=len(values_b),
        n_paired=len(paired_a),
        ci_low=ci_low,
        ci_high=ci_high,
        significant=significant,
        method=method,
    )


def classify_samples(
    *,
    scores_a: Dict[str, Dict[str, Any]],
    scores_b: Dict[str, Dict[str, Any]],
    primary_metric: Optional[str] = None,
    inputs: Optional[Dict[str, Any]] = None,
    tolerance: float = SAMPLE_DELTA_TOLERANCE,
) -> List[SampleComparison]:
    """按 sample_id 对齐两次 run, 逐样本分类

    分类:
    - improved:   delta > tolerance
    - regressed:  delta < -tolerance
    - unchanged:  |delta| <= tolerance (含两侧都无可用得分的情况)
    - only_in_a:  仅 run A 有该样本
    - only_in_b:  仅 run B 有该样本

    排序: 回归最严重的排最前 (delta 升序), 其后是 unchanged / improved,
    only_in_* 排在最后, 便于 UI 与 CI 直接取 top-N 回归。
    """
    inputs = inputs or {}
    out: List[SampleComparison] = []

    for sample_id in sorted(set(scores_a) | set(scores_b)):
        sa = scores_a.get(sample_id)
        sb = scores_b.get(sample_id)
        score_a = _composite(sa, primary_metric) if sa is not None else None
        score_b = _composite(sb, primary_metric) if sb is not None else None

        if sa is not None and sb is None:
            classification, delta = "only_in_a", None
        elif sa is None and sb is not None:
            classification, delta = "only_in_b", None
        elif score_a is None or score_b is None:
            classification, delta = "unchanged", None
        else:
            delta = round(score_b - score_a, 6)
            if delta > tolerance:
                classification = "improved"
            elif delta < -tolerance:
                classification = "regressed"
            else:
                classification = "unchanged"

        metric_deltas: Dict[str, Optional[float]] = {}
        for name in sorted(set(sa or {}) | set(sb or {})):
            va = (sa or {}).get(name)
            vb = (sb or {}).get(name)
            if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
                metric_deltas[name] = round(float(vb) - float(va), 6)
            else:
                metric_deltas[name] = None

        out.append(
            SampleComparison(
                sample_id=sample_id,
                classification=classification,
                delta=delta,
                score_a=score_a,
                score_b=score_b,
                metric_deltas=metric_deltas,
                input=inputs.get(sample_id),
            )
        )

    # delta 为 None 的(only_in_* / 无可比得分)排到末尾, 其余按 delta 升序
    out.sort(key=lambda s: (s.delta is None, s.delta if s.delta is not None else 0.0))
    return out


def _composite(
    scores: Optional[Dict[str, Any]], primary_metric: Optional[str]
) -> Optional[float]:
    """逐样本综合得分 (指定主指标则取之, 否则取全部指标均值)"""
    if not scores:
        return None
    if primary_metric:
        value = scores.get(primary_metric)
        return round(float(value), 6) if isinstance(value, (int, float)) else None
    values = [float(v) for v in scores.values() if isinstance(v, (int, float))]
    return round(sum(values) / len(values), 6) if values else None


def build_verdict(metrics: List[MetricComparison], counts: Dict[str, int]) -> str:
    """生成整体结论字符串"""
    if not metrics:
        return "两次运行没有可比的指标数据"

    sig_better = [m.metric for m in metrics if m.significant and m.winner == "b"]
    sig_worse = [m.metric for m in metrics if m.significant and m.winner == "a"]
    parts: List[str] = []

    if sig_worse:
        parts.append(f"Run B 在 {', '.join(sig_worse)} 上显著变差")
    if sig_better:
        parts.append(f"Run B 在 {', '.join(sig_better)} 上显著变好")
    if not sig_better and not sig_worse:
        parts.append("所有指标的差异均未达到 95% 置信水平下的统计显著性")

    parts.append(
        f"逐样本: 改善 {counts.get('improved', 0)}, 回归 {counts.get('regressed', 0)}, "
        f"持平 {counts.get('unchanged', 0)}, "
        f"仅 A {counts.get('only_in_a', 0)}, 仅 B {counts.get('only_in_b', 0)}"
    )
    return "; ".join(parts)


def _extract_contexts(input_data: Any, metadata: Dict[str, Any]) -> List[str]:
    """从 dataset item 的 input / metadata 中抽取检索上下文列表"""
    for source in (input_data, metadata):
        if not isinstance(source, dict):
            continue
        for key in ("contexts", "context", "retrieved_contexts", "documents"):
            value = source.get(key)
            if isinstance(value, list) and value:
                out: List[str] = []
                for entry in value:
                    if isinstance(entry, str) and entry.strip():
                        out.append(entry.strip())
                    elif isinstance(entry, dict):
                        text = str(
                            entry.get("content")
                            or entry.get("text")
                            or entry.get("page_content")
                            or ""
                        ).strip()
                        if text:
                            out.append(text)
                if out:
                    return out
            if isinstance(value, str) and value.strip():
                return [value.strip()]
    return []


__all__ = [
    "ComparisonResult",
    "DEFAULT_BOOTSTRAP_ITERATIONS",
    "DEFAULT_BOOTSTRAP_SEED",
    "DEFAULT_REGRESSION_THRESHOLD",
    "ExperimentService",
    "MetricComparison",
    "RUN_STATUSES",
    "SampleComparison",
    "bootstrap_delta_ci",
    "build_verdict",
    "classify_samples",
    "compare_metric",
    "execute_run_background",
    "summarize_metric_buckets",
]
