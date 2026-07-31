"""
SQLAlchemy 数据模型定义
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base
from models.constants import EvaluationStatus

# 默认租户标识：单租户兼容，未显式传 tenant_id 时所有数据落 default
DEFAULT_TENANT_ID = "default"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Tenant(Base):
    """租户：多租户隔离的顶层主体，tenant_id 全局唯一"""

    __tablename__ = "tenants"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    plan: Mapped[str] = mapped_column(String(32), nullable=False, default="free")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )


class User(Base):
    """系统用户（员工/主管/HR/管理员）"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # 多租户下 user_id 按租户隔离：同一 user_id 可分属不同租户，由 uix_tenant_user 约束
    user_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str] = mapped_column(String(256), index=True, nullable=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="employee")
    department: Mapped[str] = mapped_column(String(128), nullable=True)
    # 员工直属主管 ID，用于 RBAC 团队归属校验，避免主管越权审批非下属评估
    manager_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
    password_hash: Mapped[str] = mapped_column(String(256), nullable=True)
    # 多租户归属：单租户兼容走 DEFAULT_TENANT_ID
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uix_tenant_user"),
        UniqueConstraint("tenant_id", "email", name="uix_tenant_email"),
        Index("ix_user_tenant_role", "tenant_id", "role"),
    )


class RawInput(Base):
    """员工原始输入：日报、任务进度、截图、语音等"""

    __tablename__ = "raw_inputs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    input_id: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    employee_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.user_id"), index=True, nullable=False
    )
    period: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="daily_report"
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    attachments: Mapped[dict] = mapped_column(JSON, default=list)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    # 留存策略标记：到期归档后置 True，缓冲期满后真删
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    archived_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("employee_id", "period", "input_id", name="uix_raw_input"),
        Index("ix_raw_tenant_employee_period", "tenant_id", "employee_id", "period"),
    )


class Evaluation(Base):
    """员工评估结果主表"""

    __tablename__ = "evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    evaluation_id: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    employee_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.user_id"), index=True, nullable=False
    )
    period: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    overall_score: Mapped[float] = mapped_column(Float, nullable=False)
    employee_view: Mapped[dict] = mapped_column(JSON, nullable=False)
    manager_view: Mapped[dict] = mapped_column(JSON, nullable=False)
    audit: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="ai_drafted",
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approver_id: Mapped[str] = mapped_column(String(64), nullable=True)
    # 留存策略标记：到期归档后置 True，缓冲期满后真删
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    archived_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "overall_score >= 0 AND overall_score <= 100",
            name="ck_evaluation_score_range",
        ),
        CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in EvaluationStatus.values())})",
            name="ck_evaluation_status_valid",
        ),
        Index("ix_eval_employee_status", "employee_id", "status"),
        Index("ix_eval_tenant_status", "tenant_id", "status"),
        Index("ix_eval_tenant_employee", "tenant_id", "employee_id"),
    )


class ApprovalAction(Base):
    """审批流动作记录"""

    __tablename__ = "approval_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    action_id: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    evaluation_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("evaluations.evaluation_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )

    __table_args__ = (
        Index("ix_approval_eval_created", "evaluation_id", "created_at"),
        Index("ix_approval_tenant_eval", "tenant_id", "evaluation_id"),
    )


class AuditLog(Base):
    """审计日志"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    log_id: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    evaluation_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("evaluations.evaluation_id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    employee_id: Mapped[str] = mapped_column(String(64), index=True, nullable=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    ip_address: Mapped[str] = mapped_column(String(64), nullable=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )

    __table_args__ = (
        Index("ix_audit_actor_created", "actor_id", "created_at"),
        Index("ix_audit_action_created", "action", "created_at"),
        Index("ix_audit_tenant_action", "tenant_id", "action"),
    )


class Feedback(Base):
    """员工反馈与申诉"""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    feedback_id: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    evaluation_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("evaluations.evaluation_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    employee_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.user_id"), index=True, nullable=False
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False, default="feedback")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )

    __table_args__ = (Index("ix_feedback_tenant_employee", "tenant_id", "employee_id"),)


class Memory(Base):
    """员工长期记忆（可后续对接向量库，当前用关系表兜底）"""

    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    employee_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.user_id"), index=True, nullable=False
    )
    period: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        UniqueConstraint("employee_id", "period", name="uix_employee_period_memory"),
        Index("ix_memory_tenant_employee", "tenant_id", "employee_id"),
    )


class CompanyKB(Base):
    """公司知识库（评分标准、价值观、培训材料）"""

    __tablename__ = "company_kb"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    kb_id: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )

    __table_args__ = (Index("ix_kb_tenant_created", "tenant_id", "created_at"),)


class EvaluationPeriod(Base):
    """评估周期定义（周/月/季/年）"""

    __tablename__ = "evaluation_periods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    period: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False
    )
    period_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="weekly"
    )
    start_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )

    __table_args__ = (Index("ix_period_tenant_status", "tenant_id", "status"),)


class DimensionScore(Base):
    """维度得分明细（从 evaluation.employee_view.growth_areas 拆出，便于横向分析）"""

    __tablename__ = "dimension_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    evaluation_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("evaluations.evaluation_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    employee_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    period: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    dimension: Mapped[str] = mapped_column(String(64), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    improvement_actions: Mapped[dict] = mapped_column(JSON, default=list)
    # 多租户隔离字段（与迁移 b3c4d5e6f7a8 对齐，通过 evaluation_id FK 级联也已隔离，
    # 此字段冗余但便于按租户直接聚合分析，避免每次 join evaluations 表）
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )


class EvidenceRef(Base):
    """证据引用关联（维度得分 → 原始输入片段）"""

    __tablename__ = "evidence_refs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    evaluation_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("evaluations.evaluation_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    dimension: Mapped[str] = mapped_column(String(64), nullable=False)
    input_id: Mapped[str] = mapped_column(String(128), index=True, nullable=True)
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False)
    # 多租户隔离字段（同 DimensionScore，与迁移 b3c4d5e6f7a8 对齐）
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )


# ====== P1 Prompt 管理(参考 Langfuse 数据模型) ======


class PromptTemplate(Base):
    """Prompt 模板（逻辑实体，同名多版本，参考 Langfuse Prompt Object）。

    一个 template 对应一个 prompt 名（如 daily_evaluation），
    其下可以有多个不可变的 PromptVersion，通过 PromptLabel 指针切换生产版本。
    """

    __tablename__ = "prompt_templates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # 同一租户内 name 唯一
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # text | chat：text 是单字符串，chat 是消息列表
    type: Mapped[str] = mapped_column(String(16), nullable=False, default="text")
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
    # 对标 LobeChat/Open WebUI 模板库: 分类/内容/变量/内置/公开
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="general")
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    variables: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uix_tenant_prompt_name"),
    )


class PromptVersion(Base):
    """Prompt 版本（不可变历史，每次更新新建一行，参考 Langfuse Version）。

    版本号自增（1, 2, 3...），不可修改已创建的版本内容（强制新建版本）。
    这保证了历史可追溯 + A/B 测试可对比 + 回滚可精确到某版本。
    """

    __tablename__ = "prompt_versions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    template_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("prompt_templates.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Prompt 正文（text 模式是字符串，chat 模式是 JSON 消息列表）
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 配置：model / temperature / max_tokens 等（参考 Langfuse Config）
    config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 变量 schema：变量名 + 类型 + 默认值 + 描述
    variables_schema: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )

    __table_args__ = (
        UniqueConstraint("template_id", "version", name="uix_prompt_template_version"),
        Index("ix_prompt_version_template", "template_id", "version"),
    )


class PromptLabel(Base):
    """Label 指针（指向具体 PromptVersion，参考 Langfuse Label）。

    Label 是版本指针，常见 label：
    - production: 生产默认（get_prompt 不指定 label 时返回此版本）
    - latest: 自动指向最新版本
    - staging / canary-10pct: 灰度
    - prod-a / prod-b: A/B 测试
    - <tenant_id>: 按租户隔离

    protected=True 时（参考 Langfuse Protected Labels），
    viewer/member 角色不能修改此 label，仅 admin 可改。
    """

    __tablename__ = "prompt_labels"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    template_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("prompt_templates.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("prompt_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    protected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        # 同一 template 下同一 label 唯一（避免 prod-a 出现两个版本）
        UniqueConstraint("template_id", "label", name="uix_prompt_template_label"),
    )


class PromptEvalRun(Base):
    """Prompt 评估运行（关联 Langfuse trace 与指标，便于版本对比）。

    用于评估某版本在测试数据集上的表现：
    - 跑多少样本、成功率、平均分、p50/p95 延迟、token 成本
    - 关联 trace_ids 让管理员可跳到 Langfuse 看具体调用
    """

    __tablename__ = "prompt_eval_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    template_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("prompt_templates.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("prompt_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    dataset_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # 评估指标：latency_p50 / latency_p95 / cost / score 等
    metrics: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 关联的 Langfuse trace_id 列表
    trace_ids: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )


# ====== Webhook 事件记录 (P7 外部集成) ======


class WebhookEvent(Base):
    """Webhook 事件记录

    所有外部系统(飞书/GitLab/自定义)的 webhook 回调落库,便于重放与排查。
    处理状态机:pending → processed / failed。
    """

    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # 来源:feishu / gitlab / custom
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)  # JSON 字符串
    # pending / processed / failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_webhook_event_source_status", "source", "status"),
        Index("ix_webhook_event_tenant_received", "tenant_id", "received_at"),
    )


# ====== 定时任务调度 ======


class ScheduledTask(Base):
    """定时任务配置

    持久化 APScheduler 的任务配置，支持动态增删改查与手动触发。
    task_type 标识任务来源：retention / sla / fairness / api_key / notification / custom。
    """

    __tablename__ = "scheduled_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    task_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cron_expression: Mapped[str] = mapped_column(String(128), nullable=False)
    # retention/sla/fairness/api_key/notification/custom
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    config: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # success / failed
    last_run_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    last_run_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (Index("ix_scheduled_task_tenant_type", "tenant_id", "task_type"),)


class ScheduledTaskRun(Base):
    """定时任务执行历史

    每次任务执行（含手动触发）记录一条，供 history 端点查询。
    """

    __tablename__ = "scheduled_task_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # success/failed
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now_utc
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[str] = mapped_column(
        String(32), nullable=False, default="scheduler"
    )  # scheduler / manual
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )

    __table_args__ = (Index("ix_task_run_task_started", "task_id", "started_at"),)


# ====== 通知 ======


class Notification(Base):
    """站内通知

    记录面向用户的通知消息（审批提醒、申诉进度、系统公告等），
    支持已读/未读状态与定时清理（30 天前已读通知自动归档删除）。
    type 标识通知大类:evaluation / approval / system / webhook,
    link 为点击跳转 URL,content 可空(纯标题通知)。
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    notification_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # 通知大类:evaluation / approval / system / webhook
    type: Mapped[str] = mapped_column(String(32), nullable=False, default="system")
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 点击通知跳转的 URL
    link: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # 兼容旧字段:细分类别 approval/appeal/system/reminder
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="system")
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    read_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )

    __table_args__ = (
        Index("ix_notif_tenant_user_read", "tenant_id", "user_id", "is_read"),
    )


# ====== API Key 管理（外部调用方鉴权） ======


class ApiKey(Base):
    """外部调用方 API Key 管理

    用于服务间调用或第三方集成的 API Key 鉴权。
    明文 key 仅在创建时返回一次，库中仅存 sha256 哈希；
    key_prefix 保存明文前 12 位，供 UI 展示识别。
    """

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # 前缀标识 ak_xxx，全局唯一
    key_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    # sha256 哈希，验证时比对
    key_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    # 明文前 12 位，用于 UI 展示识别
    key_prefix: Mapped[str] = mapped_column(String(16), index=True)
    # 描述名称
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # JSON 字符串：["chat","evaluation","insights"]
    scopes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 每分钟请求限制
    rate_limit: Mapped[int] = mapped_column(Integer, default=60)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (Index("ix_apikey_tenant_active", "tenant_id", "is_active"),)


# ====== 混合检索配置（向量 + BM25 全文检索） ======


class SearchConfig(Base):
    """检索配置（混合检索 alpha 默认值、BM25 开关、RRF 参数等）

    采用 key-value 结构存储检索相关配置，便于灵活扩展新参数而无需迁移表结构。
    常见 config_key：
    - default_alpha: 混合检索默认权重（0=纯BM25, 1=纯向量, 0.5=等权混合）
    - bm25_enabled: 是否启用 BM25 全文检索
    - rrf_k: RRF (Reciprocal Rank Fusion) 常数 k，默认 60
    - bm25_k1: BM25 参数 k1（词频饱和度），默认 1.5
    - bm25_b: BM25 参数 b（文档长度归一化），默认 0.75
    """

    __tablename__ = "search_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # 配置键名，同一租户内唯一
    config_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 配置值（字符串存储，使用时按 config_key 约定的类型转换）
    config_value: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "config_key", name="uix_tenant_search_config_key"
        ),
    )
