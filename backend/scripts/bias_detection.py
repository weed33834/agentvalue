#!/usr/bin/env python3
"""
AgentValue-AI AI 偏差检测增强

在既有 scripts/fairness_audit.py (群体公平性) 基础上, 增加 4 类认知偏差检测:
1. 语言偏见 (language_bias): 评估文本中主观/情感词汇的频率
2. 近因偏见 (recency_bias): 最近事件对评分的过度影响
3. 对比效应 (contrast_bias): 与同组其他人的对比导致偏差
4. 晕轮效应 (halo_effect): 一个维度高分导致其他维度也偏高

外加 generate_bias_report(period) 综合偏差报告, 聚合以上所有维度。

每个检测方法返回统一结构:
    {
        "detected": bool,
        "severity": "low" / "medium" / "high",
        "details": str,
        "affected_count": int,
        "recommendations": list,
    }

输入支持两种形式:
- dict 列表 (与 scripts.fairness_audit 一致, 字段含 overall_score/department/level/period 等)
- ORM 对象列表 (models.models.Evaluation / models.models.DimensionScore), 内部自动归一化为 dict

用法:
    cd backend
    python -m scripts.bias_detection
    # 或在 service 中:
    from scripts.bias_detection import BiasDetector
    detector = BiasDetector()
    report = await detector.generate_bias_report(period="2026-W28")
"""

from __future__ import annotations

import logging
import re
import statistics
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Union

from scripts._stats_utils import std

logger = logging.getLogger(__name__)


# =====================================================================
# 主观/情感词汇表 (中文) - 用于语言偏见检测
# 高频出现这些词暗示评估者用主观印象而非客观证据评价
# =====================================================================
_SUBJECTIVE_WORDS: tuple[str, ...] = (
    # 程度副词 (绝对化)
    "总是", "从不", "永远", "经常", "常常", "一直", "完全", "彻底", "绝对",
    "毫无疑问", "毫无疑问地",
    # 主观判断词
    "感觉", "觉得", "认为", "以为", "似乎", "好像", "可能", "应该",
    "显然", "明显", "看不出", "说不清",
    # 情感色彩词 (正向)
    "优秀", "出色", "突出", "杰出", "完美", "极好", "卓越", "优异", "棒",
    # 情感色彩词 (负向)
    "糟糕", "差劲", "平庸", "懒散", "懈怠", "消极", "敷衍", "马虎", "粗心",
    "可惜", "遗憾", "令人失望",
    # 模糊评价词
    "还不错", "还行", "一般般", "凑合", "马马虎虎",
)

# 严重偏见词 (出现即强烈信号)
_SEVERE_BIAS_WORDS: tuple[str, ...] = (
    "总是", "从不", "永远", "完全", "彻底", "绝对",
    "糟糕", "差劲", "敷衍", "懒散", "令人失望",
)


def _to_dict(obj: Any) -> Dict[str, Any]:
    """将 ORM 对象或 dict 归一化为 dict。

    ORM 对象通过 __dict__ / __table__ 列名提取; 不可序列化的属性跳过。
    """
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    # ORM 实例: 优先用 __dict__ 过滤 SQLAlchemy 状态
    try:
        data: Dict[str, Any] = {}
        for key, value in obj.__dict__.items():
            if key.startswith("_sa_"):
                continue
            data[key] = value
        return data
    except Exception:
        # 兜底: 尝试 __table__ 列名
        try:
            cols = [c.name for c in obj.__table__.columns]
            return {c: getattr(obj, c, None) for c in cols}
        except Exception:
            return {}


def _normalize_evaluations(evaluations: Sequence[Any]) -> List[Dict[str, Any]]:
    """批量归一化: 接受 dict 列表或 ORM 对象列表, 统一返回 dict 列表。"""
    if not evaluations:
        return []
    return [_to_dict(e) for e in evaluations]


def _extract_text(evaluation: Dict[str, Any]) -> str:
    """从评估 dict 中提取所有可分析文本 (audit / manager_view / employee_view / feedback)。

    这些字段在 models.Evaluation 中是 JSON dict, 这里递归拼接其中所有字符串值。
    """
    chunks: List[str] = []

    def _walk(value: Any) -> None:
        if isinstance(value, str):
            chunks.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                _walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                _walk(v)

    for field in ("audit", "manager_view", "employee_view", "feedback", "comment", "summary"):
        _walk(evaluation.get(field))

    # feedback_text / comment_text 等顶层字符串字段
    for key, value in evaluation.items():
        if isinstance(value, str) and key not in ("evaluation_id", "employee_id", "period"):
            chunks.append(value)

    return "\n".join(chunks)


def _extract_dimension_scores(evaluation: Dict[str, Any]) -> Dict[str, float]:
    """从评估 dict 中提取各维度分数 (用于晕轮效应检测)。

    兼容多种数据形态:
    - evaluation["dimension_scores"]: {"领导力": 85, "执行力": 80, ...}
    - evaluation["employee_view"]["growth_areas"]: [{"dimension": "领导力", "score": 85}, ...]
    - evaluation["scores"]: {"领导力": 85, ...}
    - evaluation["employee_view"]["scores"]: {...}
    """
    scores: Dict[str, float] = {}

    # 形态1: 顶层 dimension_scores dict
    ds = evaluation.get("dimension_scores")
    if isinstance(ds, dict):
        for k, v in ds.items():
            try:
                scores[str(k)] = float(v)
            except (TypeError, ValueError):
                continue

    # 形态2: 顶层 scores dict
    sc = evaluation.get("scores")
    if isinstance(sc, dict):
        for k, v in sc.items():
            try:
                scores.setdefault(str(k), float(v))
            except (TypeError, ValueError):
                continue

    # 形态3: employee_view.growth_areas 列表
    ev = evaluation.get("employee_view")
    if isinstance(ev, dict):
        gas = ev.get("growth_areas")
        if isinstance(gas, list):
            for item in gas:
                if not isinstance(item, dict):
                    continue
                dim = item.get("dimension") or item.get("name") or item.get("area")
                val = item.get("score") or item.get("value")
                if dim is None or val is None:
                    continue
                try:
                    scores.setdefault(str(dim), float(val))
                except (TypeError, ValueError):
                    continue
        # 形态4: employee_view.scores dict
        ev_scores = ev.get("scores")
        if isinstance(ev_scores, dict):
            for k, v in ev_scores.items():
                try:
                    scores.setdefault(str(k), float(v))
                except (TypeError, ValueError):
                    continue

    # 形态5: manager_view.scores / manager_view.growth_areas (同上逻辑)
    mv = evaluation.get("manager_view")
    if isinstance(mv, dict):
        mvs = mv.get("scores")
        if isinstance(mvs, dict):
            for k, v in mvs.items():
                try:
                    scores.setdefault(str(k), float(v))
                except (TypeError, ValueError):
                    continue
        mvgas = mv.get("growth_areas")
        if isinstance(mvgas, list):
            for item in mvgas:
                if not isinstance(item, dict):
                    continue
                dim = item.get("dimension") or item.get("name")
                val = item.get("score") or item.get("value")
                if dim is None or val is None:
                    continue
                try:
                    scores.setdefault(str(dim), float(val))
                except (TypeError, ValueError):
                    continue

    return scores


def _severity_from_ratio(ratio: float, low: float = 0.1, high: float = 0.25) -> str:
    """根据受影响比例判定严重度。

    ratio < low  -> "low"
    low <= ratio < high -> "medium"
    ratio >= high -> "high"
    """
    if ratio >= high:
        return "high"
    if ratio >= low:
        return "medium"
    return "low"


def _empty_result(reason: str = "无数据") -> Dict[str, Any]:
    """空数据时的统一返回结构。"""
    return {
        "detected": False,
        "severity": "low",
        "details": reason,
        "affected_count": 0,
        "recommendations": [],
    }


class BiasDetector:
    """AI 偏差检测器

    检测维度:
    1. 语言偏见 (主观/情感词汇频率)
    2. 近因偏见 (最近事件权重过高)
    3. 对比效应 (与同组他人对比导致偏差)
    4. 晕轮效应 (单维度高分带动其他维度)

    综合报告:
    - generate_bias_report(period) 查询指定周期评估, 聚合以上所有维度
    """

    # 语言偏见: 主观词频次阈值 (每千字)
    LANGUAGE_BIAS_PER_1K_LOW = 5.0
    LANGUAGE_BIAS_PER_1K_HIGH = 15.0
    # 严重偏见词出现即计入 affected
    SEVERE_BIAS_WEIGHT = 3.0

    # 近因偏见: 最近评分偏离历史均值的标准差倍数阈值
    RECENCY_DEVIATION_LOW = 1.0
    RECENCY_DEVIATION_HIGH = 1.5

    # 对比效应: 群体内偏离均值的标准差倍数阈值
    CONTRAST_DEVIATION_LOW = 1.0
    CONTRAST_DEVIATION_HIGH = 1.5
    # 对比效应: 群体最小样本数 (太小不做判断)
    CONTRAST_MIN_GROUP_SIZE = 3

    # 晕轮效应: 维度间标准差阈值 (低 std 表示各维度打分趋同)
    HALO_STD_LOW = 3.0
    HALO_STD_HIGH = 6.0
    HALO_MIN_DIMENSIONS = 3

    def __init__(self, db_session_factory=None):
        """
        Args:
            db_session_factory: 可选, async context manager 工厂用于查询数据库
                (如 core.database.get_db_session)。generate_bias_report 用其查 DB。
                未提供时 generate_bias_report 接受外部传入的 evaluations。
        """
        self.db_session_factory = db_session_factory

    # ================================================================
    # 1. 语言偏见
    # ================================================================

    def detect_language_bias(self, evaluations: list) -> dict:
        """检测语言偏见: 评估文本中主观/情感词汇的频率。

        统计每份评估的可分析文本中:
        - 主观/情感词出现次数 (加权: 严重偏见词 ×3)
        - 每千字频次 (normalized frequency)
        - 感叹号密度 (情感强度辅助信号)

        判定:
        - 任一评估的严重偏见词 > 0 -> 计入 affected
        - 或每千字频次 >= LANGUAGE_BIAS_PER_1K_LOW -> 计入 affected
        """
        try:
            evals = _normalize_evaluations(evaluations)
            if not evals:
                return _empty_result("无评估数据")

            affected_indices: List[int] = []
            max_per_1k = 0.0
            total_subjective = 0
            total_severe = 0

            for idx, evaluation in enumerate(evals):
                text = _extract_text(evaluation)
                if not text:
                    continue
                text_len = max(len(text), 1)

                subjective_count = 0
                severe_count = 0
                for word in _SUBJECTIVE_WORDS:
                    occurrences = text.count(word)
                    if occurrences > 0:
                        if word in _SEVERE_BIAS_WORDS:
                            severe_count += occurrences
                        subjective_count += occurrences

                # 加权频次: 严重词权重 ×3
                weighted = subjective_count + severe_count * (
                    self.SEVERE_BIAS_WEIGHT - 1.0
                )
                per_1k = weighted * 1000.0 / text_len
                max_per_1k = max(max_per_1k, per_1k)
                total_subjective += subjective_count
                total_severe += severe_count

                # 感叹号密度 (辅助信号)
                excl_count = text.count("!")
                excl_per_1k = excl_count * 1000.0 / text_len

                is_affected = (
                    severe_count > 0
                    or per_1k >= self.LANGUAGE_BIAS_PER_1K_LOW
                    or excl_per_1k >= 10.0
                )
                if is_affected:
                    affected_indices.append(idx)

            affected_count = len(affected_indices)
            ratio = affected_count / len(evals) if evals else 0.0
            detected = affected_count > 0

            if max_per_1k >= self.LANGUAGE_BIAS_PER_1K_HIGH or total_severe > 0:
                severity = _severity_from_ratio(ratio, low=0.05, high=0.2)
                if total_severe > 0 and ratio >= 0.2:
                    severity = "high"
            else:
                severity = _severity_from_ratio(ratio)

            details = (
                f"共分析 {len(evals)} 份评估, {affected_count} 份存在主观/情感词汇偏多; "
                f"最高每千字频次={max_per_1k:.2f}, "
                f"主观词总数={total_subjective}, 严重偏见词总数={total_severe}"
            )

            recommendations: List[str] = []
            if detected:
                recommendations.extend(
                    [
                        "在评估模板中增加客观证据字段, 限制主观形容词使用",
                        "对含严重偏见词(总是/从不/永远等)的评估强制人工复核",
                        "向评估者提供中性化表达培训与示例对照表",
                    ]
                )
                if severity == "high":
                    recommendations.append(
                        "高风险: 建议本周期评估结果暂缓发布, 由 HRBP 复核后再开放"
                    )

            return {
                "detected": detected,
                "severity": severity,
                "details": details,
                "affected_count": affected_count,
                "recommendations": recommendations,
            }
        except Exception as e:
            logger.exception("detect_language_bias 失败: %s", e)
            return {
                "detected": False,
                "severity": "low",
                "details": f"检测异常: {e}",
                "affected_count": 0,
                "recommendations": [],
            }

    # ================================================================
    # 2. 近因偏见
    # ================================================================

    def detect_recency_bias(self, evaluations: list) -> dict:
        """检测近因偏见: 最近事件对评分的影响权重过高。

        方法:
        - 按员工分组, 按时间(created_at 或 period)排序
        - 计算每位员工历史评分的均值与标准差
        - 若最近一次评分偏离历史均值超过 RECENCY_DEVIATION_LOW 个标准差, 计入 affected
        - 同时统计: 最近评分均值 vs 非最近评分均值的差异 (整体信号)

        需要: 每位员工至少 2 个周期的评估才参与判定。
        """
        try:
            evals = _normalize_evaluations(evaluations)
            if not evals:
                return _empty_result("无评估数据")

            # 按员工分组
            by_employee: Dict[str, List[Dict[str, Any]]] = {}
            for evaluation in evals:
                emp_id = evaluation.get("employee_id")
                score = evaluation.get("overall_score")
                if emp_id is None or score is None:
                    continue
                by_employee.setdefault(str(emp_id), []).append(evaluation)

            affected_employees: List[str] = []
            affected_eval_ids: List[str] = []
            deviations: List[float] = []

            for emp_id, records in by_employee.items():
                if len(records) < 2:
                    continue
                # 按时间排序 (created_at 优先, 其次 period)
                def _sort_key(r: Dict[str, Any]):
                    ca = r.get("created_at")
                    if isinstance(ca, datetime):
                        return ca.timestamp()
                    if isinstance(ca, str):
                        try:
                            return datetime.fromisoformat(ca).timestamp()
                        except Exception:
                            pass
                    period = r.get("period") or ""
                    return period

                records_sorted = sorted(records, key=_sort_key)
                scores = [
                    float(r.get("overall_score"))
                    for r in records_sorted
                    if r.get("overall_score") is not None
                ]
                if len(scores) < 2:
                    continue

                # 历史均值/标准差 (排除最近一次)
                history = scores[:-1]
                latest = scores[-1]
                hist_mean = statistics.mean(history)
                hist_std = std(history) if len(history) >= 2 else 0.0

                if hist_std <= 0:
                    # 历史无波动, 用绝对差异 5 分作为信号
                    dev_abs = abs(latest - hist_mean)
                    if dev_abs >= 5.0:
                        affected_employees.append(emp_id)
                        affected_eval_ids.append(
                            str(records_sorted[-1].get("evaluation_id") or emp_id)
                        )
                        deviations.append(dev_abs)
                    continue

                z_score = abs(latest - hist_mean) / hist_std
                if z_score >= self.RECENCY_DEVIATION_LOW:
                    affected_employees.append(emp_id)
                    affected_eval_ids.append(
                        str(records_sorted[-1].get("evaluation_id") or emp_id)
                    )
                    deviations.append(z_score)

            affected_count = len(affected_employees)
            # 总参与判定的员工数 (>=2 周期的)
            eligible_count = sum(1 for v in by_employee.values() if len(v) >= 2)
            ratio = affected_count / eligible_count if eligible_count else 0.0
            detected = affected_count > 0

            avg_deviation = (
                statistics.mean(deviations) if deviations else 0.0
            )
            if avg_deviation >= self.RECENCY_DEVIATION_HIGH:
                severity = _severity_from_ratio(ratio, low=0.1, high=0.3)
                if ratio >= 0.3:
                    severity = "high"
            else:
                severity = _severity_from_ratio(ratio)

            details = (
                f"参与判定员工 {eligible_count} 名 (每位>=2周期), "
                f"{affected_count} 名员工最近评分显著偏离历史均值; "
                f"平均偏离={avg_deviation:.2f} 标准差"
            )

            recommendations: List[str] = []
            if detected:
                recommendations.extend(
                    [
                        "在评估流程中要求评估者回顾全周期事件, 而非仅凭最近印象",
                        "对偏离历史均值 > 1.5 标准差的评分强制人工复核",
                        "引入关键事件日志 (Critical Incident Log), 按时间均匀采样事件",
                    ]
                )
                if severity == "high":
                    recommendations.append(
                        "高风险: 建议本周期评估重新收集至少 4 周的事件证据后再评分"
                    )

            return {
                "detected": detected,
                "severity": severity,
                "details": details,
                "affected_count": affected_count,
                "recommendations": recommendations,
            }
        except Exception as e:
            logger.exception("detect_recency_bias 失败: %s", e)
            return {
                "detected": False,
                "severity": "low",
                "details": f"检测异常: {e}",
                "affected_count": 0,
                "recommendations": [],
            }

    # ================================================================
    # 3. 对比效应
    # ================================================================

    def detect_contrast_bias(self, evaluations: list) -> dict:
        """检测对比效应: 评估者将员工与同组其他人对比导致评分偏差。

        方法:
        - 按 department (或 level 兜底) 分组
        - 群体 >= CONTRAST_MIN_GROUP_SIZE 才参与判定
        - 计算群体均值与标准差
        - 偏离群体均值 > CONTRAST_DEVIATION_LOW 个标准差的评估计入 affected
        - 辅助信号: 群体标准差异常低 (CV < 0.05) 但有极端离群值, 强烈暗示对比效应

        对比效应的典型表现: 一名"明星员工"导致其他人在对比下被压低,
        或一名"后进员工"导致其他人被相对抬高。
        """
        try:
            evals = _normalize_evaluations(evaluations)
            if not evals:
                return _empty_result("无评估数据")

            # 按群体分组 (department 优先, 缺失则 level, 再缺失则 "unknown")
            def _group_key(r: Dict[str, Any]) -> str:
                if r.get("department"):
                    return str(r["department"])
                if r.get("level"):
                    return str(r["level"])
                return "unknown"

            buckets: Dict[str, List[Dict[str, Any]]] = {}
            for evaluation in evals:
                if evaluation.get("overall_score") is None:
                    continue
                key = _group_key(evaluation)
                buckets.setdefault(key, []).append(evaluation)

            affected_ids: List[str] = []
            low_cv_groups: List[str] = []
            extreme_outlier_count = 0

            for group_key, records in buckets.items():
                if len(records) < self.CONTRAST_MIN_GROUP_SIZE:
                    continue
                scores = [float(r["overall_score"]) for r in records]
                mean = statistics.mean(scores)
                std_val = std(scores)

                # 群体 CV (变异系数) - 低 CV + 有离群值 = 强烈对比信号
                cv = std_val / mean if mean > 0 else 0.0
                if cv < 0.05:
                    low_cv_groups.append(group_key)

                if std_val <= 0:
                    continue

                for r in records:
                    score = float(r["overall_score"])
                    z = abs(score - mean) / std_val
                    if z >= self.CONTRAST_DEVIATION_HIGH:
                        extreme_outlier_count += 1
                        affected_ids.append(
                            str(r.get("evaluation_id") or r.get("employee_id") or "")
                        )
                    elif z >= self.CONTRAST_DEVIATION_LOW:
                        affected_ids.append(
                            str(r.get("evaluation_id") or r.get("employee_id") or "")
                        )

            # 去重 (同一 evaluation 可能在多个判定中命中)
            affected_ids = list(dict.fromkeys(affected_ids))
            affected_count = len(affected_ids)
            total_count = len(evals)
            ratio = affected_count / total_count if total_count else 0.0
            detected = affected_count > 0 or len(low_cv_groups) > 0

            if extreme_outlier_count > 0 and len(low_cv_groups) > 0:
                severity = _severity_from_ratio(ratio, low=0.05, high=0.2)
                if ratio >= 0.2:
                    severity = "high"
            elif affected_count > 0:
                severity = _severity_from_ratio(ratio)
            else:
                severity = "low"

            details = (
                f"共 {len(buckets)} 个群体, {affected_count} 份评估显著偏离群体均值; "
                f"极端离群值={extreme_outlier_count}, "
                f"低变异群体(CV<0.05)={len(low_cv_groups)} "
                f"({', '.join(low_cv_groups) if low_cv_groups else '无'})"
            )

            recommendations: List[str] = []
            if detected:
                recommendations.extend(
                    [
                        "改用绝对评分标准 (rubric) 而非相对对比, 评估时屏蔽其他成员评分",
                        "对低变异群体中的离群值进行人工复核, 确认是否为对比效应所致",
                        "在评估者培训中明确禁止 '相比其他同事...' 的表述",
                    ]
                )
                if severity == "high":
                    recommendations.append(
                        "高风险: 建议对低变异群体整体重评, 引入独立评估者交叉验证"
                    )

            return {
                "detected": detected,
                "severity": severity,
                "details": details,
                "affected_count": affected_count,
                "recommendations": recommendations,
            }
        except Exception as e:
            logger.exception("detect_contrast_bias 失败: %s", e)
            return {
                "detected": False,
                "severity": "low",
                "details": f"检测异常: {e}",
                "affected_count": 0,
                "recommendations": [],
            }

    # ================================================================
    # 4. 晕轮效应
    # ================================================================

    def detect_halo_effect(self, evaluations: list) -> dict:
        """检测晕轮效应: 一个维度高分导致其他维度也偏高。

        方法:
        - 从每份评估中提取各维度分数 (dimension_scores / growth_areas)
        - 计算每份评估的维度间标准差 (inter-dimension std)
        - 维度数 >= HALO_MIN_DIMENSIONS 才参与判定
        - 维度间 std < HALO_STD_LOW 计入 affected (各维度打分趋同, 强烈暗示晕轮效应)
        - 辅助信号: 维度间相关系数极高 (跨员工看, 各维度同涨同跌)

        晕轮效应的典型表现: 评估者对某员工整体印象好, 所有维度都给高分;
        或整体印象差, 所有维度都给低分, 缺乏维度间区分度。
        """
        try:
            evals = _normalize_evaluations(evaluations)
            if not evals:
                return _empty_result("无评估数据")

            per_eval_stds: List[float] = []
            affected_ids: List[str] = []
            eligible_count = 0

            # 跨员工维度相关分析 (需要至少 5 个样本且有共同维度)
            dimension_columns: Dict[str, List[float]] = {}

            for evaluation in evals:
                dim_scores = _extract_dimension_scores(evaluation)
                # 过滤掉非合理分数
                dim_scores = {
                    k: v for k, v in dim_scores.items() if 0 <= v <= 100
                }
                if len(dim_scores) < self.HALO_MIN_DIMENSIONS:
                    continue
                eligible_count += 1

                values = list(dim_scores.values())
                inter_std = std(values)
                per_eval_stds.append(inter_std)

                # 收集维度列 (用于跨员工相关分析)
                for dim, val in dim_scores.items():
                    dimension_columns.setdefault(dim, []).append(val)

                if inter_std < self.HALO_STD_LOW:
                    affected_ids.append(
                        str(
                            evaluation.get("evaluation_id")
                            or evaluation.get("employee_id")
                            or ""
                        )
                    )

            affected_count = len(affected_ids)
            ratio = affected_count / eligible_count if eligible_count else 0.0

            # 跨员工维度相关分析: 计算维度两两 Pearson 相关系数
            high_correlation_pairs: List[str] = []
            avg_correlation: Optional[float] = None
            correlations: List[float] = []
            dims = list(dimension_columns.keys())
            if len(dims) >= 2:
                for i in range(len(dims)):
                    for j in range(i + 1, len(dims)):
                        col_a = dimension_columns[dims[i]]
                        col_b = dimension_columns[dims[j]]
                        # 对齐长度 (取较短)
                        n = min(len(col_a), len(col_b))
                        if n < 3:
                            continue
                        a = col_a[:n]
                        b = col_b[:n]
                        try:
                            corr = self._pearson(a, b)
                            if corr is not None:
                                correlations.append(corr)
                                if corr >= 0.85:
                                    high_correlation_pairs.append(
                                        f"{dims[i]}~{dims[j]}={corr:.2f}"
                                    )
                        except Exception:
                            continue
                if correlations:
                    avg_correlation = statistics.mean(correlations)

            detected = affected_count > 0 or (
                avg_correlation is not None and avg_correlation >= 0.85
            )

            # 严重度判定
            if affected_count > 0 and avg_correlation is not None and avg_correlation >= 0.85:
                severity = _severity_from_ratio(ratio, low=0.1, high=0.3)
                if ratio >= 0.3:
                    severity = "high"
            elif affected_count > 0:
                severity = _severity_from_ratio(ratio)
            elif avg_correlation is not None and avg_correlation >= 0.85:
                severity = "medium"
            else:
                severity = "low"

            avg_inter_std = (
                statistics.mean(per_eval_stds) if per_eval_stds else None
            )
            details_parts = [
                f"参与判定评估 {eligible_count} 份 (维度>={self.HALO_MIN_DIMENSIONS})",
                f"维度间标准差 < {self.HALO_STD_LOW} 的 {affected_count} 份",
            ]
            if avg_inter_std is not None:
                details_parts.append(f"平均维度间 std={avg_inter_std:.2f}")
            if avg_correlation is not None:
                details_parts.append(f"维度两两平均相关系数={avg_correlation:.2f}")
            if high_correlation_pairs:
                details_parts.append(
                    f"高相关维度对(>=0.85): {', '.join(high_correlation_pairs[:5])}"
                )
            details = "; ".join(details_parts)

            recommendations: List[str] = []
            if detected:
                recommendations.extend(
                    [
                        "在评估表中强制按维度独立打分, 禁止先给总分再分配维度分",
                        "对维度间标准差 < 3 的评估强制人工复核, 确认是否为晕轮效应",
                        "引入维度评分锚定描述 (rubric), 每个分数档有具体行为证据要求",
                    ]
                )
                if severity == "high":
                    recommendations.append(
                        "高风险: 建议对高相关维度对拆分评估者, 不同维度由不同评估者打分"
                    )

            return {
                "detected": detected,
                "severity": severity,
                "details": details,
                "affected_count": affected_count,
                "recommendations": recommendations,
            }
        except Exception as e:
            logger.exception("detect_halo_effect 失败: %s", e)
            return {
                "detected": False,
                "severity": "low",
                "details": f"检测异常: {e}",
                "affected_count": 0,
                "recommendations": [],
            }

    # ================================================================
    # 5. 综合偏差报告
    # ================================================================

    async def generate_bias_report(self, period: str) -> dict:
        """生成综合偏差报告, 包含所有检测维度。

        Args:
            period: 评估周期标识 (如 "2026-W28", "2026-07")

        Returns:
            {
                "period": str,
                "generated_at": str (ISO),
                "total_evaluations": int,
                "dimensions": {
                    "language_bias": {...},
                    "recency_bias": {...},
                    "contrast_bias": {...},
                    "halo_effect": {...},
                },
                "overall_severity": "low" / "medium" / "high",
                "summary": str,
                "recommendations": list,  # 聚合所有维度的建议
            }
        """
        try:
            evaluations = await self._load_evaluations_by_period(period)
            total = len(evaluations)

            if total == 0:
                return {
                    "period": period,
                    "generated_at": datetime.utcnow().isoformat(),
                    "total_evaluations": 0,
                    "dimensions": {
                        "language_bias": _empty_result("该周期无评估数据"),
                        "recency_bias": _empty_result("该周期无评估数据"),
                        "contrast_bias": _empty_result("该周期无评估数据"),
                        "halo_effect": _empty_result("该周期无评估数据"),
                    },
                    "overall_severity": "low",
                    "summary": f"周期 {period} 无评估数据, 无法生成偏差报告",
                    "recommendations": [],
                }

            # 为近因偏见加载历史评估 (当前周期 + 历史周期)
            historical = await self._load_historical_evaluations(period)
            recency_input = historical + evaluations

            language_result = self.detect_language_bias(evaluations)
            recency_result = self.detect_recency_bias(recency_input)
            contrast_result = self.detect_contrast_bias(evaluations)
            halo_result = self.detect_halo_effect(evaluations)

            dimensions = {
                "language_bias": language_result,
                "recency_bias": recency_result,
                "contrast_bias": contrast_result,
                "halo_effect": halo_result,
            }

            # 总体严重度: 取所有维度的最高严重度
            severity_rank = {"low": 1, "medium": 2, "high": 3}
            overall_severity = "low"
            for result in dimensions.values():
                if severity_rank.get(result.get("severity", "low"), 1) > severity_rank.get(
                    overall_severity, 1
                ):
                    overall_severity = result["severity"]

            detected_count = sum(1 for r in dimensions.values() if r.get("detected"))

            summary = (
                f"周期 {period} 共分析 {total} 份评估, "
                f"{detected_count}/{len(dimensions)} 个维度检测到潜在偏差, "
                f"总体严重度={overall_severity}"
            )

            # 聚合所有维度的建议 (去重保序)
            all_recommendations: List[str] = []
            seen = set()
            for result in dimensions.values():
                for rec in result.get("recommendations", []) or []:
                    if rec not in seen:
                        seen.add(rec)
                        all_recommendations.append(rec)

            return {
                "period": period,
                "generated_at": datetime.utcnow().isoformat(),
                "total_evaluations": total,
                "dimensions": dimensions,
                "overall_severity": overall_severity,
                "summary": summary,
                "recommendations": all_recommendations,
            }
        except Exception as e:
            logger.exception("generate_bias_report 失败 period=%s: %s", period, e)
            return {
                "period": period,
                "generated_at": datetime.utcnow().isoformat(),
                "total_evaluations": 0,
                "dimensions": {},
                "overall_severity": "low",
                "summary": f"生成偏差报告异常: {e}",
                "recommendations": [],
                "error": str(e),
            }

    # ================================================================
    # 辅助方法
    # ================================================================

    async def _load_evaluations_by_period(self, period: str) -> List[Dict[str, Any]]:
        """从数据库加载指定周期的评估记录 (dict 列表)。

        依赖 models.models.Evaluation 与 models.models.DimensionScore。
        无 db_session_factory 时返回空列表 (供单元测试 mock)。
        """
        if self.db_session_factory is None:
            logger.debug("db_session_factory 未配置, _load_evaluations_by_period 返回空")
            return []

        try:
            # 延迟 import 避免循环依赖
            from models.models import DimensionScore, Evaluation  # noqa: F401

            from sqlalchemy import select
            from sqlalchemy.orm import selectinload

            async with self.db_session_factory() as session:
                stmt = (
                    select(Evaluation)
                    .where(Evaluation.period == period)
                    .order_by(Evaluation.id.asc())
                )
                result = await session.execute(stmt)
                evals_orm = result.scalars().all()

                evaluations: List[Dict[str, Any]] = []
                for ev in evals_orm:
                    ev_dict = _to_dict(ev)
                    # 加载维度分数 (供晕轮效应检测)
                    try:
                        dim_stmt = select(DimensionScore).where(
                            DimensionScore.evaluation_id == ev.evaluation_id
                        )
                        dim_result = await session.execute(dim_stmt)
                        dim_rows = dim_result.scalars().all()
                        if dim_rows:
                            ev_dict["dimension_scores"] = {
                                dr.dimension: dr.score for dr in dim_rows
                            }
                    except Exception:
                        pass
                    evaluations.append(ev_dict)
                return evaluations
        except Exception as e:
            logger.warning("_load_evaluations_by_period 失败 period=%s: %s", period, e)
            return []

    async def _load_historical_evaluations(self, current_period: str) -> List[Dict[str, Any]]:
        """加载历史周期 (period < current_period) 的评估, 供近因偏见检测。

        最多取最近 6 个周期, 避免数据量过大。
        """
        if self.db_session_factory is None:
            return []

        try:
            from models.models import Evaluation

            from sqlalchemy import select

            async with self.db_session_factory() as session:
                stmt = (
                    select(Evaluation)
                    .where(Evaluation.period < current_period)
                    .order_by(Evaluation.period.desc(), Evaluation.id.asc())
                    .limit(500)
                )
                result = await session.execute(stmt)
                evals_orm = result.scalars().all()
                return [_to_dict(ev) for ev in evals_orm]
        except Exception as e:
            logger.warning(
                "_load_historical_evaluations 失败 current_period=%s: %s",
                current_period,
                e,
            )
            return []

    @staticmethod
    def _pearson(x: List[float], y: List[float]) -> Optional[float]:
        """计算 Pearson 相关系数; 样本不足或方差为 0 返回 None。"""
        n = min(len(x), len(y))
        if n < 3:
            return None
        x = x[:n]
        y = y[:n]
        mean_x = statistics.mean(x)
        mean_y = statistics.mean(y)
        num = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
        den_x = sum((xi - mean_x) ** 2 for xi in x) ** 0.5
        den_y = sum((yi - mean_y) ** 2 for yi in y) ** 0.5
        if den_x == 0 or den_y == 0:
            return None
        return num / (den_x * den_y)


# =====================================================================
# 命令行入口 (供 python -m scripts.bias_detection 调用)
# =====================================================================

def _demo_evaluations() -> List[Dict[str, Any]]:
    """构造演示用合成评估数据 (不依赖数据库)。"""
    return [
        {
            "evaluation_id": "EV001",
            "employee_id": "E001",
            "department": "Engineering",
            "level": "L2",
            "period": "2026-W27",
            "overall_score": 82,
            "created_at": "2026-07-07T10:00:00",
            "manager_view": {
                "summary": "该员工总是表现出色, 执行力很强, 但偶尔粗心",
                "growth_areas": [
                    {"dimension": "执行力", "score": 85},
                    {"dimension": "创新", "score": 84},
                    {"dimension": "协作", "score": 83},
                ],
            },
            "audit": {"summary": "整体优秀"},
        },
        {
            "evaluation_id": "EV002",
            "employee_id": "E002",
            "department": "Engineering",
            "level": "L2",
            "period": "2026-W27",
            "overall_score": 60,
            "created_at": "2026-07-07T10:00:00",
            "manager_view": {
                "summary": "最近一次提交有不少问题, 令人失望",
            },
            "audit": {"summary": "近期表现下滑"},
        },
        {
            "evaluation_id": "EV003",
            "employee_id": "E001",
            "department": "Engineering",
            "level": "L2",
            "period": "2026-W28",
            "overall_score": 92,
            "created_at": "2026-07-14T10:00:00",
            "manager_view": {
                "summary": "最近项目交付非常出色! 棒!",
                "growth_areas": [
                    {"dimension": "执行力", "score": 92},
                    {"dimension": "创新", "score": 91},
                    {"dimension": "协作", "score": 93},
                ],
            },
            "audit": {"summary": "近期表现突出"},
        },
        {
            "evaluation_id": "EV004",
            "employee_id": "E002",
            "department": "Engineering",
            "level": "L2",
            "period": "2026-W28",
            "overall_score": 88,
            "created_at": "2026-07-14T10:00:00",
            "manager_view": {
                "summary": "本周表现明显改善",
                "growth_areas": [
                    {"dimension": "执行力", "score": 88},
                    {"dimension": "创新", "score": 87},
                    {"dimension": "协作", "score": 89},
                ],
            },
            "audit": {"summary": "改善明显"},
        },
    ]


def main() -> int:
    """命令行入口: 用合成数据演示偏差检测。"""
    print("=" * 60)
    print("AgentValue-AI 偏差检测演示 (合成数据)")
    print("=" * 60)

    detector = BiasDetector()
    demo = _demo_evaluations()

    print("\n[1] 语言偏见检测")
    r = detector.detect_language_bias(demo)
    print(f"  detected={r['detected']} severity={r['severity']}")
    print(f"  details={r['details']}")
    for rec in r["recommendations"]:
        print(f"  - {rec}")

    print("\n[2] 近因偏见检测")
    r = detector.detect_recency_bias(demo)
    print(f"  detected={r['detected']} severity={r['severity']}")
    print(f"  details={r['details']}")
    for rec in r["recommendations"]:
        print(f"  - {rec}")

    print("\n[3] 对比效应检测")
    r = detector.detect_contrast_bias(demo)
    print(f"  detected={r['detected']} severity={r['severity']}")
    print(f"  details={r['details']}")
    for rec in r["recommendations"]:
        print(f"  - {rec}")

    print("\n[4] 晕轮效应检测")
    r = detector.detect_halo_effect(demo)
    print(f"  detected={r['detected']} severity={r['severity']}")
    print(f"  details={r['details']}")
    for rec in r["recommendations"]:
        print(f"  - {rec}")

    print("\n" + "=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
