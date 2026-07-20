"""
高级分析服务（Phase 9.2）
基于历史评估数据做纯统计/规则/启发式分析，不依赖真实 LLM。
包含团队 ROI 分析、员工成长路径推荐、离职风险预测三类能力。
事务边界由路由层控制，本服务只读查询、不写库。
"""

import logging
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

from services.evaluation_service import EvaluationService

logger = logging.getLogger(__name__)

# 维度关键词词典：用于成长方向识别与能力归类
_TECH_KEYWORDS = [
    "技术",
    "代码",
    "工程",
    "架构",
    "实现",
    "开发",
    "编程",
    "系统",
    "算法",
    "性能",
    "质量",
    "测试",
    "运维",
]
_MGMT_KEYWORDS = [
    "管理",
    "沟通",
    "协作",
    "领导",
    "团队",
    "协调",
    "决策",
    "计划",
    "组织",
    "项目",
    "辅导",
    "驱动",
]
# 积极投入词：用于员工 engagement 词频趋势判断
_ENGAGEMENT_POSITIVE = [
    "积极",
    "主动",
    "完成",
    "优秀",
    "突破",
    "提升",
    "稳定",
    "出色",
    "进步",
    "担当",
    "投入",
    "高效",
]

# 九宫格格子标签：(performance, potential) → 名称
_NINE_BOX_LABELS = {
    ("low", "low"): "待观察",
    ("low", "mid"): "待发展",
    ("low", "high"): "潜力待激发",
    ("mid", "low"): "稳定贡献者",
    ("mid", "mid"): "核心骨干",
    ("mid", "high"): "成长之星",
    ("high", "low"): "专业骨干",
    ("high", "mid"): "未来领袖",
    ("high", "high"): "超级明星",
}

_RISK_THRESHOLDS = {"low": 30, "high": 70}


# ---------------- 周期解析工具 ----------------


def _parse_iso_week(week_str: str) -> Tuple[int, int]:
    """解析 '2026-W20' → (2026, 20)"""
    parts = week_str.upper().split("-W")
    if len(parts) != 2:
        raise ValueError(f"周格式无效，需形如 2026-W20: {week_str}")
    return int(parts[0]), int(parts[1])


def _week_ordinal(year: int, week: int) -> int:
    """周序数，仅用于排序与比较"""
    return year * 53 + week


def _enumerate_weeks(start: str, end: str) -> List[str]:
    """枚举 [start, end] 闭区间内的所有 ISO 周字符串"""
    sy, sw = _parse_iso_week(start)
    ey, ew = _parse_iso_week(end)
    if _week_ordinal(ey, ew) < _week_ordinal(sy, sw):
        return []
    weeks: List[str] = []
    y, w = sy, sw
    while _week_ordinal(y, w) <= _week_ordinal(ey, ew):
        weeks.append(f"{y}-W{w:02d}")
        w += 1
        if w > 53:
            w = 1
            y += 1
    return weeks


def _bucket_performance(score: float) -> str:
    """绩效分桶：<60 低，60-85 中，>85 高"""
    if score >= 85:
        return "high"
    if score >= 60:
        return "mid"
    return "low"


def _bucket_potential(slope: float) -> str:
    """潜力分桶（按周得分变化斜率）：< -1 低，[-1,1] 中，> 1 高"""
    if slope > 1:
        return "high"
    if slope < -1:
        return "low"
    return "mid"


def _risk_level(score: float) -> str:
    """风险等级：低 <30，中 30-70，高 >70"""
    if score >= _RISK_THRESHOLDS["high"]:
        return "high"
    if score >= _RISK_THRESHOLDS["low"]:
        return "medium"
    return "low"


def _match_keywords(text: str, keywords: List[str]) -> int:
    """统计 text 命中关键词的个数"""
    if not text:
        return 0
    return sum(1 for kw in keywords if kw in text)


def _count_positive_words(summary: str) -> int:
    """统计 summary 中积极投入词出现次数"""
    if not summary:
        return 0
    return sum(summary.count(w) for w in _ENGAGEMENT_POSITIVE)


def _slope(scores: List[float]) -> float:
    """计算序列首尾斜率（按周变化），不足两点返回 0"""
    n = len(scores)
    if n < 2:
        return 0.0
    return (scores[-1] - scores[0]) / (n - 1)


class AnalyticsService:
    """高级分析服务，复用 EvaluationService 的只读查询能力"""

    def __init__(self, eval_service: EvaluationService):
        self.eval_service = eval_service

    # ---------------- 内部查询 ----------------

    async def _fetch_evals(self, employee_id: str, limit: int = 200) -> List[Any]:
        """拉取某员工历史评估（按周期升序返回，便于趋势计算）"""
        result = await self.eval_service.list_evaluations(
            employee_id=employee_id, limit=limit
        )
        items = result["items"]
        items.sort(key=lambda e: (e.period, e.created_at))
        return items

    async def _fetch_feedback_count(self, employee_id: str) -> int:
        """统计员工近期反馈/申诉条数（用于离职风险）"""
        rows = await self.eval_service.list_feedback(employee_id=employee_id, limit=200)
        return len(rows)

    # ---------------- 9.2.1.1 团队 ROI ----------------

    async def get_team_roi(
        self,
        team_member_ids: List[str],
        period_range: Optional[Tuple[str, str]] = None,
    ) -> Dict[str, Any]:
        """团队 ROI 分析

        投入：评估次数 × 平均处理时长（代理成本）
        产出：平均分提升趋势 + 高分员工占比 + 风险降低率
        返回周度趋势、九宫格分布、top/bottom 员工与综合 ROI。
        """
        members = [m for m in (team_member_ids or []) if m]
        if not members:
            return self._empty_team_roi(period_range)

        # 周期窗口：未指定则取所有数据
        if period_range:
            start, end = period_range
            weeks = _enumerate_weeks(start, end)
            week_set = set(weeks)
        else:
            weeks = []
            week_set = None

        member_stats: List[Dict[str, Any]] = []
        weekly_agg: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"scores": [], "processing_ms": [], "eval_count": 0}
        )

        for mid in members:
            evals = await self._fetch_evals(mid)
            if week_set is not None:
                evals = [e for e in evals if e.period in week_set]
            if not evals:
                member_stats.append(self._empty_member_stat(mid))
                continue

            scores = [float(e.overall_score or 0) for e in evals]
            mean_score = sum(scores) / len(scores)
            slope = _slope(scores)
            risk_first = self._risk_flag_count(evals[0])
            risk_last = self._risk_flag_count(evals[-1])
            processing_ms = [
                float((e.audit or {}).get("processing_time_ms") or 0) for e in evals
            ]
            avg_proc_ms = (
                sum(processing_ms) / len(processing_ms) if processing_ms else 0
            )

            member_stats.append(
                {
                    "employee_id": mid,
                    "eval_count": len(evals),
                    "avg_score": round(mean_score, 2),
                    "latest_score": round(scores[-1], 2),
                    "first_score": round(scores[0], 2),
                    "score_slope": round(slope, 2),
                    "risk_first": risk_first,
                    "risk_last": risk_last,
                    "risk_reduced": risk_last < risk_first,
                    "avg_processing_ms": round(avg_proc_ms, 0),
                    "performance_bucket": _bucket_performance(mean_score),
                    "potential_bucket": _bucket_potential(slope),
                    "evals": [
                        {"period": e.period, "score": float(e.overall_score or 0)}
                        for e in evals
                    ],
                }
            )

            # 汇入周度聚合
            for e in evals:
                wk = e.period
                weekly_agg[wk]["scores"].append(float(e.overall_score or 0))
                weekly_agg[wk]["processing_ms"].append(
                    float((e.audit or {}).get("processing_time_ms") or 0)
                )
                weekly_agg[wk]["eval_count"] += 1

        trend = self._build_weekly_trend(weeks, weekly_agg)
        nine_box = self._build_nine_box(member_stats)
        top_bottom = self._build_top_bottom(member_stats)
        summary = self._build_roi_summary(member_stats, trend, weeks)

        return {
            "team_size": len(members),
            "period_range": (
                {"start": period_range[0], "end": period_range[1]}
                if period_range
                else None
            ),
            "summary": summary,
            "trend": trend,
            "nine_box": nine_box,
            "top_employees": top_bottom["top"],
            "bottom_employees": top_bottom["bottom"],
            "members": member_stats,
        }

    def _risk_flag_count(self, evaluation: Any) -> int:
        """统计评估 manager_view.risk_flags 数量"""
        mv = getattr(evaluation, "manager_view", None) or {}
        flags = mv.get("risk_flags") or []
        return len(flags)

    def _empty_member_stat(self, mid: str) -> Dict[str, Any]:
        return {
            "employee_id": mid,
            "eval_count": 0,
            "avg_score": 0,
            "latest_score": 0,
            "first_score": 0,
            "score_slope": 0,
            "risk_first": 0,
            "risk_last": 0,
            "risk_reduced": False,
            "avg_processing_ms": 0,
            "performance_bucket": "low",
            "potential_bucket": "mid",
            "evals": [],
        }

    def _empty_team_roi(
        self, period_range: Optional[Tuple[str, str]]
    ) -> Dict[str, Any]:
        return {
            "team_size": 0,
            "period_range": (
                {"start": period_range[0], "end": period_range[1]}
                if period_range
                else None
            ),
            "summary": {
                "roi": 0,
                "investment_index": 0,
                "output_index": 0,
                "improvement": 0,
                "high_score_ratio": 0,
                "risk_reduction_rate": 0,
            },
            "trend": [],
            "nine_box": self._build_nine_box([]),
            "top_employees": [],
            "bottom_employees": [],
            "members": [],
        }

    def _build_weekly_trend(
        self, weeks: List[str], weekly_agg: Dict[str, Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """构造周度 ROI 趋势序列"""
        if not weeks:
            weeks = sorted(weekly_agg.keys())
        trend: List[Dict[str, Any]] = []
        for wk in weeks:
            agg = weekly_agg.get(wk)
            if not agg or not agg["scores"]:
                trend.append(
                    {
                        "week": wk,
                        "avg_score": 0,
                        "eval_count": 0,
                        "high_score_ratio": 0,
                        "roi": 0,
                    }
                )
                continue
            avg_score = sum(agg["scores"]) / len(agg["scores"])
            high = sum(1 for s in agg["scores"] if s >= 85)
            high_ratio = high / len(agg["scores"]) * 100
            avg_proc_min = (
                (sum(agg["processing_ms"]) / len(agg["processing_ms"])) / 60000.0
                if agg["processing_ms"]
                else 0
            )
            investment = agg["eval_count"] * avg_proc_min
            output = (avg_score / 100.0) * 50 + high_ratio * 0.5
            roi = round(output / (investment + 0.5), 2) if investment >= 0 else 0
            trend.append(
                {
                    "week": wk,
                    "avg_score": round(avg_score, 2),
                    "eval_count": agg["eval_count"],
                    "high_score_ratio": round(high_ratio, 2),
                    "roi": roi,
                }
            )
        return trend

    def _build_nine_box(self, member_stats: List[Dict[str, Any]]) -> Dict[str, Any]:
        """构造绩效×潜力九宫格分布"""
        buckets = ["low", "mid", "high"]
        grid: Dict[str, Dict[str, Any]] = {}
        for p in buckets:
            for pot in buckets:
                key = f"{p}-{pot}"
                grid[key] = {
                    "performance": p,
                    "potential": pot,
                    "label": _NINE_BOX_LABELS[(p, pot)],
                    "count": 0,
                    "employees": [],
                }
        for m in member_stats:
            key = f"{m['performance_bucket']}-{m['potential_bucket']}"
            if key in grid:
                grid[key]["count"] += 1
                grid[key]["employees"].append(m["employee_id"])
        return {
            "performance_axis": ["low", "mid", "high"],
            "potential_axis": ["low", "mid", "high"],
            "cells": grid,
            "total": len(member_stats),
        }

    def _build_top_bottom(
        self, member_stats: List[Dict[str, Any]]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """按平均分取 top/bottom 员工"""
        active = [m for m in member_stats if m["eval_count"] > 0]
        ranked = sorted(active, key=lambda m: m["avg_score"], reverse=True)
        top = [
            {
                "employee_id": m["employee_id"],
                "avg_score": m["avg_score"],
                "eval_count": m["eval_count"],
                "score_slope": m["score_slope"],
            }
            for m in ranked[:3]
        ]
        bottom = [
            {
                "employee_id": m["employee_id"],
                "avg_score": m["avg_score"],
                "eval_count": m["eval_count"],
                "score_slope": m["score_slope"],
            }
            for m in ranked[-3:][::-1]
        ]
        return {"top": top, "bottom": bottom}

    def _build_roi_summary(
        self,
        member_stats: List[Dict[str, Any]],
        trend: List[Dict[str, Any]],
        weeks: List[str],
    ) -> Dict[str, Any]:
        """计算综合 ROI：产出指标 / 投入指标"""
        active = [m for m in member_stats if m["eval_count"] > 0]
        if not active:
            return {
                "roi": 0,
                "investment_index": 0,
                "output_index": 0,
                "improvement": 0,
                "high_score_ratio": 0,
                "risk_reduction_rate": 0,
            }

        total_eval = sum(m["eval_count"] for m in active)
        avg_proc_ms = sum(m["avg_processing_ms"] for m in active) / len(active)
        investment_index = total_eval * (avg_proc_ms / 1000.0)  # 评估·秒

        # 产出三件套
        improvement = 0.0
        if trend:
            scored = [t for t in trend if t["eval_count"] > 0]
            if scored:
                improvement = max(0.0, scored[-1]["avg_score"] - scored[0]["avg_score"])
        high_score_ratio = (
            sum(1 for m in active if m["avg_score"] >= 85) / len(active) * 100
        )
        risk_reduction_rate = (
            sum(1 for m in active if m["risk_reduced"]) / len(active) * 100
        )
        output_index = (improvement + high_score_ratio + risk_reduction_rate) / 3.0
        roi = round(output_index / (investment_index / 60.0 + 1.0), 2)

        return {
            "roi": roi,
            "investment_index": round(investment_index, 2),
            "output_index": round(output_index, 2),
            "improvement": round(improvement, 2),
            "high_score_ratio": round(high_score_ratio, 2),
            "risk_reduction_rate": round(risk_reduction_rate, 2),
            "total_evaluations": total_eval,
        }

    # ---------------- 9.2.1.2 成长路径 ----------------

    async def get_growth_path(self, employee_id: str) -> Dict[str, Any]:
        """员工成长路径推荐

        基于历史 4+ 周评估的 strengths/growth_areas 趋势，识别成长方向、
        推荐发展行动，并给出当前 vs 历史的能力雷达对比。
        """
        evals = await self._fetch_evals(employee_id, limit=50)
        if not evals:
            return self._empty_growth_path(employee_id)

        # 取最近 8 周窗口
        window = evals[-8:]
        sufficient = len(window) >= 4

        # 成长趋势：周度综合得分
        growth_trend = [
            {"period": e.period, "score": float(e.overall_score or 0)} for e in window
        ]

        # strengths / growth_areas 频次
        strength_counter: Counter = Counter()
        growth_dim_counter: Counter = Counter()
        growth_dim_scores: Dict[str, List[float]] = defaultdict(list)
        action_counter: Counter = Counter()
        for e in window:
            ev = e.employee_view or {}
            for s in ev.get("strengths", []) or []:
                if isinstance(s, str) and s.strip():
                    strength_counter[s.strip()] += 1
            for area in ev.get("growth_areas", []) or []:
                dim = (area.get("dimension") or "").strip()
                if dim:
                    growth_dim_counter[dim] += 1
                    growth_dim_scores[dim].append(float(area.get("score") or 0))
                for act in area.get("improvement_actions", []) or []:
                    if isinstance(act, str) and act.strip():
                        action_counter[act.strip()] += 1

        direction = self._identify_direction(growth_dim_counter)
        capability = self._capability_radar(window)
        actions = self._suggest_actions(action_counter, growth_dim_counter, direction)

        # 读取员工最近反馈,作为成长建议的补充输入
        employee_voice: List[Dict[str, Any]] = []
        try:
            feedback_rows = await self.eval_service.list_feedback(
                employee_id=employee_id, limit=5
            )
            for fb, ev in feedback_rows:
                employee_voice.append(
                    {
                        "period": ev.period,
                        "type": fb.type,
                        "content": fb.content,
                        "created_at": fb.created_at.isoformat() if fb.created_at else None,
                    }
                )
        except Exception:
            logger.warning("读取员工反馈失败,跳过 employee_voice", exc_info=True)

        return {
            "employee_id": employee_id,
            "status": "ok" if sufficient else "insufficient_data",
            "window_weeks": len(window),
            "growth_trend": growth_trend,
            "strengths": [
                {"item": k, "frequency": v} for k, v in strength_counter.most_common(8)
            ],
            "growth_areas": [
                {
                    "dimension": k,
                    "frequency": v,
                    "score_trend": growth_dim_scores.get(k, []),
                }
                for k, v in growth_dim_counter.most_common(10)
            ],
            "recommended_direction": direction,
            "capability_change": capability,
            "suggested_actions": actions,
            "employee_voice": employee_voice,
        }

    def _identify_direction(self, growth_dim_counter: Counter) -> Dict[str, Any]:
        """根据成长领域分布识别发展方向"""
        tech_hits = 0
        mgmt_hits = 0
        for dim in growth_dim_counter.keys():
            if _match_keywords(dim, _TECH_KEYWORDS) > 0:
                tech_hits += growth_dim_counter[dim]
            if _match_keywords(dim, _MGMT_KEYWORDS) > 0:
                mgmt_hits += growth_dim_counter[dim]

        if tech_hits == 0 and mgmt_hits == 0:
            direction = "技术深耕"
            reason = "成长领域维度数据不足，默认建议技术深耕"
        elif mgmt_hits > tech_hits and tech_hits > 0:
            direction = "管理转型"
            reason = "成长领域以管理/协作类为主，且具备技术基础，适合向管理转型"
        elif tech_hits > 0 and mgmt_hits == 0:
            direction = "技术深耕"
            reason = "成长领域集中在技术类维度，建议继续深耕技术能力"
        elif tech_hits > 0 and mgmt_hits > 0:
            direction = "跨领域"
            reason = "成长领域横跨技术与管理，适合跨领域复合发展"
        else:
            direction = "技术深耕"
            reason = "成长领域以技术类为主，建议继续深耕技术能力"

        return {
            "direction": direction,
            "reason": reason,
            "tech_signal": tech_hits,
            "management_signal": mgmt_hits,
        }

    def _capability_radar(self, window: List[Any]) -> Dict[str, Any]:
        """能力雷达对比：当前（最近一次）vs 历史（窗口最早一次）"""
        if not window:
            return {"dimensions": [], "current": [], "history": [], "delta": []}
        current = self._dim_scores(window[-1])
        history = self._dim_scores(window[0])
        dims = sorted(set(current.keys()) | set(history.keys()))
        cur_vals = [current.get(d, 0) for d in dims]
        his_vals = [history.get(d, 0) for d in dims]
        deltas = [round(c - h, 2) for c, h in zip(cur_vals, his_vals)]
        return {
            "dimensions": dims,
            "current": cur_vals,
            "history": his_vals,
            "delta": deltas,
        }

    def _dim_scores(self, evaluation: Any) -> Dict[str, float]:
        """抽取单次评估的维度得分"""
        ev = getattr(evaluation, "employee_view", None) or {}
        result: Dict[str, float] = {}
        for area in ev.get("growth_areas", []) or []:
            dim = (area.get("dimension") or "").strip()
            if dim:
                result[dim] = float(area.get("score") or 0)
        return result

    def _suggest_actions(
        self,
        action_counter: Counter,
        growth_dim_counter: Counter,
        direction: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """生成建议行动清单：取高频改进项，并按发展方向补充一条方向性建议"""
        actions: List[Dict[str, Any]] = []
        for act, freq in action_counter.most_common(5):
            actions.append({"action": act, "frequency": freq, "source": "growth_area"})
        # 方向性建议
        dir_name = direction.get("direction", "技术深耕")
        direction_hint = {
            "技术深耕": "选择 1-2 个核心技术领域做深度项目攻坚，输出可复用的技术资产",
            "管理转型": "主动承担跨团队协调与辅导任务，积累带人与项目统筹经验",
            "跨领域": "在技术与管理之间寻找结合点，承担兼具两端职责的混合型项目",
        }.get(dir_name, "围绕高频成长领域制定季度提升计划")
        actions.append(
            {"action": direction_hint, "frequency": 0, "source": "direction"}
        )
        return actions

    def _empty_growth_path(self, employee_id: str) -> Dict[str, Any]:
        return {
            "employee_id": employee_id,
            "status": "no_data",
            "window_weeks": 0,
            "growth_trend": [],
            "strengths": [],
            "growth_areas": [],
            "recommended_direction": {
                "direction": "技术深耕",
                "reason": "暂无历史评估数据，无法识别方向",
                "tech_signal": 0,
                "management_signal": 0,
            },
            "capability_change": {
                "dimensions": [],
                "current": [],
                "history": [],
                "delta": [],
            },
            "suggested_actions": [],
        }

    # ---------------- 9.2.1.3 离职风险 ----------------

    async def get_attrition_risk(self, team_member_ids: List[str]) -> Dict[str, Any]:
        """离职风险预测

        风险因子：评分持续下降、growth_areas 持续无改善、engagement 词频下降、申诉频次高。
        风险等级：低 <30，中 30-70，高 >70。
        """
        members = [m for m in (team_member_ids or []) if m]
        if not members:
            return {
                "team_size": 0,
                "distribution": {"low": 0, "medium": 0, "high": 0},
                "members": [],
                "avg_risk_score": 0,
            }

        results: List[Dict[str, Any]] = []
        for mid in members:
            results.append(await self._score_attrition(mid))

        distribution = {"low": 0, "medium": 0, "high": 0}
        for r in results:
            distribution[r["risk_level"]] += 1
        avg = sum(r["risk_score"] for r in results) / len(results) if results else 0

        # 团队风险趋势：按成员最高风险周近似，简单给出近 N 期平均分变化
        return {
            "team_size": len(members),
            "distribution": distribution,
            "avg_risk_score": round(avg, 2),
            "members": results,
        }

    async def _score_attrition(self, employee_id: str) -> Dict[str, Any]:
        """计算单员工离职风险分与主要因子"""
        evals = await self._fetch_evals(employee_id, limit=20)
        feedback_count = await self._fetch_feedback_count(employee_id)
        factors: List[Dict[str, Any]] = []
        score = 0.0

        if not evals:
            return {
                "employee_id": employee_id,
                "risk_score": 0,
                "risk_level": "low",
                "factors": [],
                "suggestions": ["暂无评估数据，建议先完成首轮评估以建立基线"],
                "recent_scores": [],
            }

        recent = evals[-6:]
        scores = [float(e.overall_score or 0) for e in recent]

        # 因子1：评分连续 3 周下降
        decline_streak = self._max_decline_streak(scores)
        if decline_streak >= 3:
            pts = min(30, decline_streak * 8)
            score += pts
            factors.append(
                {
                    "factor": "评分持续下降",
                    "weight": pts,
                    "detail": f"连续 {decline_streak} 周得分下滑",
                }
            )

        # 因子2：growth_areas 持续无改善
        stagnant = self._growth_stagnation(recent)
        if stagnant > 0:
            pts = min(25, stagnant * 8)
            score += pts
            factors.append(
                {
                    "factor": "成长领域无改善",
                    "weight": pts,
                    "detail": f"{stagnant} 个成长维度分数未见提升",
                }
            )

        # 因子3：engagement 词频下降
        engagement_decline = self._engagement_decline(recent)
        if engagement_decline:
            pts = 20
            score += pts
            factors.append(
                {
                    "factor": "投入度词频下降",
                    "weight": pts,
                    "detail": "近期评估中积极投入词出现频率较前期下降",
                }
            )

        # 因子4：申诉/反馈频次高
        if feedback_count >= 2:
            pts = min(25, feedback_count * 8)
            score += pts
            factors.append(
                {
                    "factor": "申诉反馈频次高",
                    "weight": pts,
                    "detail": f"近期共 {feedback_count} 条反馈/申诉",
                }
            )

        score = min(100.0, score)
        level = _risk_level(score)
        suggestions = self._risk_suggestions(level, factors)

        return {
            "employee_id": employee_id,
            "risk_score": round(score, 0),
            "risk_level": level,
            "factors": factors,
            "suggestions": suggestions,
            "recent_scores": [
                {"period": e.period, "score": float(e.overall_score or 0)}
                for e in recent
            ],
        }

    def _max_decline_streak(self, scores: List[float]) -> int:
        """最长连续下降周数"""
        max_streak = 0
        cur = 0
        prev = None
        for s in scores:
            if prev is not None and s < prev:
                cur += 1
                max_streak = max(max_streak, cur)
            else:
                cur = 0
            prev = s
        return max_streak

    def _growth_stagnation(self, evals: List[Any]) -> int:
        """统计在窗口内出现但分数未提升的成长维度数"""
        dim_scores: Dict[str, List[float]] = defaultdict(list)
        for e in evals:
            ev = e.employee_view or {}
            for area in ev.get("growth_areas", []) or []:
                dim = (area.get("dimension") or "").strip()
                if dim:
                    dim_scores[dim].append(float(area.get("score") or 0))
        stagnant = 0
        for dim, vals in dim_scores.items():
            if len(vals) >= 2 and vals[-1] <= vals[0]:
                stagnant += 1
        return stagnant

    def _engagement_decline(self, evals: List[Any]) -> bool:
        """对比前半段与后半段 summary 的积极词频率，判断是否下降"""
        if len(evals) < 4:
            return False
        mid = len(evals) // 2

        def _summary(e) -> str:
            ev = getattr(e, "employee_view", None) or {}
            return ev.get("summary", "") or ""

        early = sum(_count_positive_words(_summary(e)) for e in evals[:mid])
        late = sum(_count_positive_words(_summary(e)) for e in evals[mid:])
        # 归一化到段长，避免段长度不均
        early_norm = early / max(1, mid)
        late_norm = late / max(1, len(evals) - mid)
        return late_norm < early_norm

    def _risk_suggestions(self, level: str, factors: List[Dict[str, Any]]) -> List[str]:
        """按风险等级与主要因子给出留人建议"""
        suggestions: List[str] = []
        if level == "high":
            suggestions.append(
                "高风险：建议主管一周内进行一对一沟通，了解真实诉求与阻碍"
            )
        elif level == "medium":
            suggestions.append("中风险：建议在下一次评估周期前主动跟进，调整任务分配")
        else:
            suggestions.append("低风险：保持现有节奏，持续关注成长反馈")

        factor_names = {f["factor"] for f in factors}
        if "评分持续下降" in factor_names:
            suggestions.append("得分下滑明显，复盘近期任务难度与资源匹配是否失衡")
        if "成长领域无改善" in factor_names:
            suggestions.append("成长领域长期停滞，建议制定专项提升计划或调整发展方向")
        if "投入度词频下降" in factor_names:
            suggestions.append("投入度信号转弱，可考虑轮岗或赋予更有挑战的项目重新激发")
        if "申诉反馈频次高" in factor_names:
            suggestions.append("申诉较多，建议 HR 介入复核评估公平性并疏通反馈渠道")
        return suggestions

    # ---------------- 9.2.1.4 人才九宫格 ----------------

    async def get_talent_matrix(
        self,
        period: Optional[str] = None,
        member_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """人才九宫格: 绩效 × 潜力 3x3 矩阵

        - 查询所有已审批 (approved) 评估
        - 绩效分数 (performance): 取该员工在指定 period (或最新一次) 已审批评估的 overall_score
        - 潜力分数 (potential): 取该员工历史已审批评估 overall_score 的变化斜率 (slope),
          映射到 0-100 区间 (slope * 10 + 50, clamp 0-100)
        - 按 3x3 矩阵分类: 横轴=绩效(低/中/高), 纵轴=潜力(低/中/高)
        - 返回每个格子里的员工列表 + 各员工明细

        Args:
            period: 可选, 指定周期则只看该周期, 否则按员工取最新一次 approved
            member_ids: 可选, 限定员工集合 (manager 用以限定为直属下属)
        """
        # 1. 拉取所有已审批评估
        evals_result = await self.eval_service.list_evaluations(
            status="approved", limit=2000
        )
        all_evals = evals_result["items"]

        # 2. 按 employee_id 分组
        by_employee: Dict[str, List[Any]] = defaultdict(list)
        for e in all_evals:
            if member_ids is not None and e.employee_id not in member_ids:
                continue
            by_employee[e.employee_id].append(e)

        # 3. 计算每个员工的 performance + potential
        members: List[Dict[str, Any]] = []
        for employee_id, evals in by_employee.items():
            # 按周期升序排序
            evals.sort(key=lambda e: (e.period, e.created_at))

            # performance: 指定 period 取该周期, 否则取最新一次
            if period:
                target_evals = [e for e in evals if e.period == period]
                if not target_evals:
                    continue
                perf_eval = target_evals[-1]
                # 潜力仍用全部历史评估的斜率
                slope_evals = evals
            else:
                perf_eval = evals[-1]
                slope_evals = evals

            performance = float(perf_eval.overall_score or 0)
            scores = [float(e.overall_score or 0) for e in slope_evals]
            slope = _slope(scores)
            # slope 映射到 0-100 区间: slope=0 → 50, slope>5 → 100, slope<-5 → 0
            potential = max(0.0, min(100.0, slope * 10.0 + 50.0))

            members.append(
                {
                    "employee_id": employee_id,
                    "evaluation_id": perf_eval.evaluation_id,
                    "period": perf_eval.period,
                    "performance_score": round(performance, 2),
                    "potential_score": round(potential, 2),
                    "performance_bucket": _bucket_performance(performance),
                    "potential_bucket": _bucket_potential_score(potential),
                    "eval_count": len(evals),
                    "latest_score": round(scores[-1], 2) if scores else 0,
                    "first_score": round(scores[0], 2) if scores else 0,
                    "score_slope": round(slope, 2),
                }
            )

        # 4. 构造 3x3 矩阵
        buckets = ["low", "mid", "high"]
        cells: Dict[str, Dict[str, Any]] = {}
        for perf in buckets:
            for pot in buckets:
                key = f"{perf}-{pot}"
                cells[key] = {
                    "performance": perf,
                    "potential": pot,
                    "label": _NINE_BOX_LABELS[(perf, pot)],
                    "count": 0,
                    "employees": [],
                }

        for m in members:
            key = f"{m['performance_bucket']}-{m['potential_bucket']}"
            if key in cells:
                cells[key]["count"] += 1
                cells[key]["employees"].append(m)

        return {
            "performance_axis": ["low", "mid", "high"],
            "potential_axis": ["low", "mid", "high"],
            "cells": cells,
            "total": len(members),
            "period": period,
            "members": members,
        }


def _bucket_potential_score(score: float) -> str:
    """潜力分桶 (基于映射后的 0-100 分): <40 低, 40-60 中, >60 高

    与 _bucket_potential (slope based) 区别: 此处 score 已是 0-100 区间。
    """
    if score >= 60:
        return "high"
    if score >= 40:
        return "mid"
    return "low"
