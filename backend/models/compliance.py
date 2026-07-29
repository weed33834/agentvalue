"""合规认证数据模型 (P1-31: SOC2 / ISO27001 合规认证框架)

对标 Vanta / Drata / Secureframe 等 GRC 自动化平台:
- ComplianceControl: 合规控制项 (框架 + 控制编号 + 状态 + 证据快照)
- ComplianceEvidence: 证据时间序列记录 (每次自动化检查留痕, 供审计追溯)

控制项状态机:
    pass              — 自动检查通过 / 人工确认合规
    fail              — 自动检查失败 / 存在合规风险
    warning           — 自动检查告警 (非阻断, 但需关注)
    not_applicable    — 不适用 / 尚未评估 (控制项初始化默认值)
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base
from models.constants import now_utc as _now_utc

# ============================================================
# 框架与状态常量 (单一来源, 避免字符串散落)
# ============================================================

# 合规框架
FRAMEWORK_SOC2 = "SOC2"
FRAMEWORK_ISO27001 = "ISO27001"
SUPPORTED_FRAMEWORKS = {FRAMEWORK_SOC2, FRAMEWORK_ISO27001}

# 控制项状态
CONTROL_STATUS_PASS = "pass"
CONTROL_STATUS_FAIL = "fail"
CONTROL_STATUS_WARNING = "warning"
CONTROL_STATUS_NOT_APPLICABLE = "not_applicable"
CONTROL_STATUSES = {
    CONTROL_STATUS_PASS,
    CONTROL_STATUS_FAIL,
    CONTROL_STATUS_WARNING,
    CONTROL_STATUS_NOT_APPLICABLE,
}

# 证据采集方式 (对应 ComplianceService 中的自动化证据收集方法)
EVIDENCE_CONFIG_AUDIT = "config_audit"
EVIDENCE_ACCESS_LOG = "access_log"
EVIDENCE_CHANGE_RECORD = "change_record"
EVIDENCE_BACKUP_VERIFY = "backup_verify"
EVIDENCE_SECURITY_SCAN = "security_scan"


class ComplianceControl(Base):
    """合规控制项

    每条记录对应一个 SOC2 Trust Services Criteria 或 ISO27001 Annex A 控制项,
    记录其当前合规状态与最新证据快照。同一条技术控制可同时满足多个框架
    (如 JWT 密钥强度同时对应 SOC2-CC6.1 与 ISO27001 A.5.17), 因此每个框架
    各自维护一条记录, 通过 check_key + check_name 关联到同一底层自动检查。
    """

    __tablename__ = "compliance_controls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 多租户归属
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default="default"
    )
    # 合规框架: SOC2 / ISO27001
    framework: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # 控制编号 (框架内唯一, 如 SOC2 的 "CC6.1" / ISO27001 的 "A.5.17")
    control_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 控制项标题
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    # 控制项描述
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 控制类别 (SOC2: security/availability/processing_integrity/confidentiality/privacy;
    #          ISO27001: organizational/people/physical/technological)
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    # 当前状态: pass / fail / warning / not_applicable
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CONTROL_STATUS_NOT_APPLICABLE, index=True
    )
    # 最新证据快照 (JSON, 包含检查方法/状态/消息/采集时间等)
    evidence: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    # 最近检查时间
    last_checked: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 控制项负责人 (用户 ID)
    owner: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # 创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )
    # 更新时间
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, onupdate=_now_utc
    )

    __table_args__ = (
        # 框架内控制编号唯一 (同租户)
        UniqueConstraint(
            "tenant_id",
            "framework",
            "control_id",
            name="uix_compliance_control",
        ),
        Index("ix_compliance_framework_status", "framework", "status"),
        Index("ix_compliance_tenant_framework", "tenant_id", "framework"),
    )


class ComplianceEvidence(Base):
    """合规证据记录 (时间序列)

    每次自动化检查或人工更新时追加一条证据记录, 形成完整证据链,
    供审计师追溯控制项状态随时间的演变 (对标 GRC 工具的证据留痕要求)。
    """

    __tablename__ = "compliance_evidences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 多租户归属
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default="default"
    )
    # 合规框架 (冗余存储, 便于按框架聚合查询证据)
    framework: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # 关联的控制编号 (字符串引用, 避免外键级联删除丢失历史证据)
    control_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 证据采集方式: config_audit / access_log / change_record / backup_verify / security_scan / manual
    evidence_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 该次检查的状态: pass / fail / warning / not_applicable
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # 证据数据 (JSON, 含检查明细/子项结果/采集时间戳等)
    evidence_data: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    # 采集时间
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )
    # 采集人 (system 表示自动化采集, 否则为用户 ID)
    collector: Mapped[str] = mapped_column(String(64), nullable=False, default="system")

    __table_args__ = (
        Index("ix_compliance_ev_control_time", "control_id", "collected_at"),
        Index("ix_compliance_ev_tenant_time", "tenant_id", "collected_at"),
    )
