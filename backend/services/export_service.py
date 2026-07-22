"""
数据导出服务

封装评估结果、审计日志、分析报告、通知记录的数据查询与格式转换。
支持 CSV / Excel / JSON 三种导出格式，大数据量时使用流式输出。

事务边界由路由层控制，本服务只读查询、不写库。
"""

import csv
import io
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.tenant_context import get_current_tenant
from models import (
    AuditLog,
    Evaluation,
    Notification,
)

logger = logging.getLogger(__name__)

# openpyxl 可选依赖（缺失时 to_excel 抛 RuntimeError）
try:
    from openpyxl import Workbook

    _OPENPYXL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _OPENPYXL_AVAILABLE = False


def _parse_date(date_str: Optional[str]) -> Optional[datetime]:
    """解析 ISO 日期字符串为带时区 datetime；None / 空串返回 None。"""
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


class ExportService:
    """数据导出服务

    提供评估结果、审计日志、分析报告、通知记录的查询与格式转换。
    所有查询方法返回 list[dict]，由 to_csv / to_excel / to_json 转为目标格式。
    所有查询限制最多 MAX_EXPORT_ROWS 条，防止 OOM。
    """

    MAX_EXPORT_ROWS = 10000  # 单次导出最大行数

    def __init__(self, session: AsyncSession):
        self.session = session

    # ============================================================
    # 数据查询
    # ============================================================

    async def export_evaluations(
        self,
        tenant_id: str,
        employee_id: Optional[str] = None,
        cycle: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """导出评估结果

        支持按员工 ID、评估周期、创建时间范围过滤。
        返回扁平化的 dict 列表，适合 CSV/Excel 导出。
        """
        stmt = (
            select(Evaluation)
            .where(Evaluation.tenant_id == tenant_id)
            .order_by(Evaluation.created_at.desc())
        )
        if employee_id:
            stmt = stmt.where(Evaluation.employee_id == employee_id)
        if cycle:
            stmt = stmt.where(Evaluation.period == cycle)

        start_dt = _parse_date(start_date)
        end_dt = _parse_date(end_date)
        if start_dt:
            stmt = stmt.where(Evaluation.created_at >= start_dt)
        if end_dt:
            stmt = stmt.where(Evaluation.created_at <= end_dt)

        result = await self.session.execute(stmt.limit(self.MAX_EXPORT_ROWS))
        evaluations = result.scalars().all()

        rows: List[Dict[str, Any]] = []
        for ev in evaluations:
            rows.append(
                {
                    "evaluation_id": ev.evaluation_id,
                    "employee_id": ev.employee_id,
                    "period": ev.period,
                    "overall_score": ev.overall_score,
                    "status": ev.status,
                    "archived": ev.archived,
                    "created_at": ev.created_at.isoformat()
                    if ev.created_at
                    else None,
                    "updated_at": ev.updated_at.isoformat()
                    if ev.updated_at
                    else None,
                    "approved_at": ev.approved_at.isoformat()
                    if ev.approved_at
                    else None,
                    "approver_id": ev.approver_id,
                }
            )
        return rows

    async def export_audit_logs(
        self,
        tenant_id: str,
        user_id: Optional[str] = None,
        action: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """导出审计日志

        支持按操作人（actor_id）、动作类型、时间范围过滤。
        """
        stmt = (
            select(AuditLog)
            .where(AuditLog.tenant_id == tenant_id)
            .order_by(AuditLog.created_at.desc())
        )
        if user_id:
            stmt = stmt.where(AuditLog.actor_id == user_id)
        if action:
            stmt = stmt.where(AuditLog.action == action)

        start_dt = _parse_date(start_date)
        end_dt = _parse_date(end_date)
        if start_dt:
            stmt = stmt.where(AuditLog.created_at >= start_dt)
        if end_dt:
            stmt = stmt.where(AuditLog.created_at <= end_dt)

        result = await self.session.execute(stmt.limit(self.MAX_EXPORT_ROWS))
        logs = result.scalars().all()

        rows: List[Dict[str, Any]] = []
        for log in logs:
            rows.append(
                {
                    "log_id": log.log_id,
                    "actor_id": log.actor_id,
                    "action": log.action,
                    "evaluation_id": log.evaluation_id,
                    "employee_id": log.employee_id,
                    "details": json.dumps(log.details, ensure_ascii=False)
                    if log.details
                    else "",
                    "ip_address": log.ip_address,
                    "created_at": log.created_at.isoformat()
                    if log.created_at
                    else None,
                }
            )
        return rows

    async def export_analytics(
        self,
        tenant_id: str,
        report_type: str,
    ) -> List[Dict[str, Any]]:
        """导出分析报告

        report_type 支持：
        - team_roi: 团队 ROI（从评估数据聚合各员工均分与趋势）
        - growth: 员工成长路径（各员工历次评估得分趋势）
        - turnover: 离职风险（低分员工清单，启发式风险标记）
        - nine_box: 人才九宫格（绩效×潜力分布）
        """
        if report_type == "team_roi":
            return await self._export_team_roi(tenant_id)
        elif report_type == "growth":
            return await self._export_growth(tenant_id)
        elif report_type == "turnover":
            return await self._export_turnover(tenant_id)
        elif report_type == "nine_box":
            return await self._export_nine_box(tenant_id)
        else:
            raise ValueError(
                f"不支持的分析报告类型: {report_type}，"
                f"可选: team_roi / growth / turnover / nine_box"
            )

    async def export_notifications(
        self,
        tenant_id: str,
        user_id: Optional[str] = None,
        is_read: Optional[bool] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """导出通知记录

        支持按用户、已读状态、时间范围过滤。
        """
        stmt = (
            select(Notification)
            .where(Notification.tenant_id == tenant_id)
            .order_by(Notification.created_at.desc())
        )
        if user_id:
            stmt = stmt.where(Notification.user_id == user_id)
        if is_read is not None:
            stmt = stmt.where(Notification.is_read.is_(is_read))

        start_dt = _parse_date(start_date)
        end_dt = _parse_date(end_date)
        if start_dt:
            stmt = stmt.where(Notification.created_at >= start_dt)
        if end_dt:
            stmt = stmt.where(Notification.created_at <= end_dt)

        result = await self.session.execute(stmt.limit(self.MAX_EXPORT_ROWS))
        notifications = result.scalars().all()

        rows: List[Dict[str, Any]] = []
        for n in notifications:
            rows.append(
                {
                    "notification_id": n.notification_id,
                    "user_id": n.user_id,
                    "title": n.title,
                    "type": getattr(n, "type", n.category),
                    "category": n.category,
                    "is_read": n.is_read,
                    "read_at": n.read_at.isoformat() if n.read_at else None,
                    "created_at": n.created_at.isoformat()
                    if n.created_at
                    else None,
                }
            )
        return rows

    # ============================================================
    # 分析报告内部实现
    # ============================================================

    async def _export_team_roi(self, tenant_id: str) -> List[Dict[str, Any]]:
        """团队 ROI 导出：各员工评估次数、均分、最高分、最低分"""
        stmt = (
            select(Evaluation)
            .where(Evaluation.tenant_id == tenant_id)
            .order_by(Evaluation.employee_id, Evaluation.created_at)
        )
        result = await self.session.execute(stmt.limit(self.MAX_EXPORT_ROWS))
        evaluations = result.scalars().all()

        # 按员工聚合
        emp_data: Dict[str, List[float]] = {}
        for ev in evaluations:
            emp_data.setdefault(ev.employee_id, []).append(ev.overall_score)

        rows: List[Dict[str, Any]] = []
        for emp_id, scores in emp_data.items():
            rows.append(
                {
                    "employee_id": emp_id,
                    "evaluation_count": len(scores),
                    "avg_score": round(sum(scores) / len(scores), 2),
                    "max_score": round(max(scores), 2),
                    "min_score": round(min(scores), 2),
                    "latest_score": round(scores[-1], 2),
                }
            )
        return rows

    async def _export_growth(self, tenant_id: str) -> List[Dict[str, Any]]:
        """成长路径导出：各员工历次评估得分（按时间排序）"""
        stmt = (
            select(Evaluation)
            .where(Evaluation.tenant_id == tenant_id)
            .order_by(Evaluation.employee_id, Evaluation.created_at)
        )
        result = await self.session.execute(stmt.limit(self.MAX_EXPORT_ROWS))
        evaluations = result.scalars().all()

        rows: List[Dict[str, Any]] = []
        for ev in evaluations:
            rows.append(
                {
                    "employee_id": ev.employee_id,
                    "period": ev.period,
                    "overall_score": ev.overall_score,
                    "status": ev.status,
                    "created_at": ev.created_at.isoformat()
                    if ev.created_at
                    else None,
                }
            )
        return rows

    async def _export_turnover(self, tenant_id: str) -> List[Dict[str, Any]]:
        """离职风险导出：低分员工清单（启发式：最近评分 < 60 标记为高风险）"""
        stmt = (
            select(Evaluation)
            .where(Evaluation.tenant_id == tenant_id)
            .order_by(Evaluation.employee_id, Evaluation.created_at.desc())
        )
        result = await self.session.execute(stmt.limit(self.MAX_EXPORT_ROWS))
        evaluations = result.scalars().all()

        # 取每个员工最近一次评估（按 created_at desc 排序，第一个就是最新的）
        latest_by_emp: Dict[str, Evaluation] = {}
        for ev in evaluations:
            if ev.employee_id not in latest_by_emp:
                latest_by_emp[ev.employee_id] = ev  # 取第一条（最新的）

        rows: List[Dict[str, Any]] = []
        for emp_id, ev in latest_by_emp.items():
            risk_level = "high" if ev.overall_score < 60 else (
                "medium" if ev.overall_score < 70 else "low"
            )
            rows.append(
                {
                    "employee_id": emp_id,
                    "latest_score": ev.overall_score,
                    "latest_period": ev.period,
                    "risk_level": risk_level,
                    "status": ev.status,
                }
            )
        rows.sort(key=lambda r: r["latest_score"])
        return rows

    async def _export_nine_box(self, tenant_id: str) -> List[Dict[str, Any]]:
        """人才九宫格导出：绩效×潜力 3x3 分布"""
        stmt = (
            select(Evaluation)
            .where(Evaluation.tenant_id == tenant_id)
            .order_by(Evaluation.employee_id, Evaluation.created_at.desc())
        )
        result = await self.session.execute(stmt.limit(self.MAX_EXPORT_ROWS))
        evaluations = result.scalars().all()

        # 取每个员工最近一次评估（按 created_at desc 排序，第一个就是最新的）
        latest_by_emp: Dict[str, Evaluation] = {}
        for ev in evaluations:
            if ev.employee_id not in latest_by_emp:
                latest_by_emp[ev.employee_id] = ev

        rows: List[Dict[str, Any]] = []
        for emp_id, ev in latest_by_emp.items():
            score = ev.overall_score
            # 绩效：低(<60) / 中(60-80) / 高(>80)
            if score < 60:
                perf = "low"
            elif score <= 80:
                perf = "medium"
            else:
                perf = "high"
            # 潜力：启发式，用分数趋势近似（此处用单次分数模拟）
            if score < 55:
                potential = "low"
            elif score <= 75:
                potential = "medium"
            else:
                potential = "high"

            rows.append(
                {
                    "employee_id": emp_id,
                    "overall_score": score,
                    "performance": perf,
                    "potential": potential,
                    "box": f"{perf}-{potential}",
                    "period": ev.period,
                }
            )
        return rows

    # ============================================================
    # 格式转换
    # ============================================================

    def to_csv(self, data: List[Dict[str, Any]], headers: List[str]) -> str:
        """将 dict 列表转为 CSV 字符串

        headers 指定列顺序与列名。
        自动对以 =/+/-/@ 开头的单元格加前缀 \\t 防止 CSV 注入。
        """
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(headers)
        for row in data:
            values = []
            for h in headers:
                val = str(row.get(h, ""))
                # CSV 注入防护: 以 =/+/-/@ 开头的值加 Tab 前缀
                if val and val[0] in ("=", "+", "-", "@"):
                    val = "\t" + val
                values.append(val)
            writer.writerow(values)
        return output.getvalue()

    def to_excel(
        self,
        data: List[Dict[str, Any]],
        headers: List[str],
        sheet_name: str = "Sheet1",
    ) -> bytes:
        """将 dict 列表转为 Excel (.xlsx) 字节流

        需要安装 openpyxl 依赖。
        """
        if not _OPENPYXL_AVAILABLE:
            raise RuntimeError(
                "导出 Excel 需要 openpyxl 依赖，请执行: pip install openpyxl"
            )

        wb = Workbook()
        ws = wb.active
        ws.title = sheet_name
        # 写入表头
        ws.append(headers)
        # 写入数据
        for row in data:
            ws.append([row.get(h, "") for h in headers])

        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)
        return buffer.getvalue()

    def to_json(self, data: List[Dict[str, Any]]) -> str:
        """将 dict 列表转为 JSON 字符串"""
        return json.dumps(data, ensure_ascii=False, default=str, indent=2)

    # ============================================================
    # 流式 CSV 输出（大数据量场景）
    # ============================================================

    def stream_csv(
        self,
        data: List[Dict[str, Any]],
        headers: List[str],
    ) -> io.StringIO:
        """返回 CSV StringIO（供 StreamingResponse 使用）

        大数据量时建议分批查询后写入同一 StringIO。
        """
        return io.StringIO(self.to_csv(data, headers))
