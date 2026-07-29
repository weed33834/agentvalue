"""合规认证服务 (P1-31: SOC2 / ISO27001 合规认证框架)

对标 Vanta / Drata / Secureframe 等 GRC 自动化平台, 提供:
- 合规控制矩阵管理 (SOC2 Trust Services Criteria + ISO27001 Annex A 预置控制项)
- 自动化证据收集 (复用 check_prod_readiness / audit_service / db_backup / git / 安全扫描)
- 合规报告生成 (JSON 合规报告 + CSV 控制矩阵, 可导入 GRC 工具)

设计要点:
- 不引入新依赖, 仅使用标准库 + 现有项目模块
- 同一技术控制可同时满足多个框架 (如 JWT 密钥强度同时对应 SOC2-CC6.1 与 ISO27001 A.5.17),
  通过 check_key + check_name 关联到底层自动检查, 避免重复采集
- 事务边界由路由层控制 (service 层不 commit)
"""

from __future__ import annotations

import csv
import io
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.tenant_context import get_current_tenant
from models.compliance import (
    CONTROL_STATUS_FAIL,
    CONTROL_STATUS_NOT_APPLICABLE,
    CONTROL_STATUS_PASS,
    CONTROL_STATUS_WARNING,
    EVIDENCE_ACCESS_LOG,
    EVIDENCE_BACKUP_VERIFY,
    EVIDENCE_CHANGE_RECORD,
    EVIDENCE_CONFIG_AUDIT,
    EVIDENCE_SECURITY_SCAN,
    FRAMEWORK_ISO27001,
    FRAMEWORK_SOC2,
    ComplianceControl,
    ComplianceEvidence,
)

logger = logging.getLogger(__name__)

# ============================================================
# 合规控制矩阵 (SOC2 Trust Services Criteria + ISO27001 Annex A)
# ------------------------------------------------------------
# 每个控制项字段:
#   framework  : SOC2 / ISO27001
#   control_id : 框架内控制编号 (唯一)
#   title      : 控制项标题
#   description: 控制项描述
#   category   : SOC2 -> security/availability/processing_integrity/confidentiality/privacy
#                ISO27001 -> organizational/people/physical/technological
#   check_key  : 自动检查方法 (config_audit/access_log/change_record/backup_verify/security_scan)
#                为 None 表示仅人工评估
#   check_name : 该检查方法下的子检查项名称 (匹配 check_prod_readiness 的 name 或自定义)
#   owner      : 默认负责人
#
# SOC2 五个维度: 安全 / 可用性 / 处理完整性 / 机密性 / 隐私性
# ISO27001:2022 Annex A 四个主题: 组织 / 人员 / 物理 / 技术
# ============================================================

COMPLIANCE_CONTROLS: List[Dict[str, Any]] = [
    # ============================================================
    # SOC2 Trust Services Criteria
    # ============================================================
    # --- 安全 (Security / Common Criteria) ---
    {
        "framework": FRAMEWORK_SOC2,
        "control_id": "CC1.1",
        "title": "演示模式关闭",
        "description": "生产环境 AUTH_DEMO_MODE 必须关闭, 避免身份伪造风险",
        "category": "security",
        "check_key": EVIDENCE_CONFIG_AUDIT,
        "check_name": "auth_demo_mode",
        "owner": "security-team",
    },
    {
        "framework": FRAMEWORK_SOC2,
        "control_id": "CC1.2",
        "title": "默认密码已修改",
        "description": "DEMO_DEFAULT_PASSWORD 不允许使用黑名单默认值, 生产环境必须修改为强口令",
        "category": "security",
        "check_key": EVIDENCE_CONFIG_AUDIT,
        "check_name": "demo_default_password",
        "owner": "security-team",
    },
    {
        "framework": FRAMEWORK_SOC2,
        "control_id": "CC6.1",
        "title": "JWT 密钥强度",
        "description": "JWT_SECRET_KEY 必须配置为非默认强随机密钥, 防止令牌伪造",
        "category": "security",
        "check_key": EVIDENCE_CONFIG_AUDIT,
        "check_name": "jwt_secret_key",
        "owner": "security-team",
    },
    {
        "framework": FRAMEWORK_SOC2,
        "control_id": "CC6.2",
        "title": "JWT 算法安全性",
        "description": "生产环境建议使用 RS256/ES256 非对称算法, 分离签发与验证密钥",
        "category": "security",
        "check_key": EVIDENCE_CONFIG_AUDIT,
        "check_name": "jwt_algorithm",
        "owner": "security-team",
    },
    {
        "framework": FRAMEWORK_SOC2,
        "control_id": "CC6.3",
        "title": "KMS 密钥管理",
        "description": "生产环境必须使用 vault/aws/aliyun KMS backend, 禁止明文密钥配置",
        "category": "security",
        "check_key": EVIDENCE_CONFIG_AUDIT,
        "check_name": "kms_configured",
        "owner": "security-team",
    },
    {
        "framework": FRAMEWORK_SOC2,
        "control_id": "CC6.4",
        "title": "CORS 来源限制",
        "description": "CORS_ORIGINS 必须显式配置允许的前端域名, 禁止通配 *",
        "category": "security",
        "check_key": EVIDENCE_CONFIG_AUDIT,
        "check_name": "cors_origins",
        "owner": "security-team",
    },
    {
        "framework": FRAMEWORK_SOC2,
        "control_id": "CC6.5",
        "title": "字段级加密",
        "description": "FIELD_ENCRYPTION_KEY 必须配置, 防止敏感字段明文落库",
        "category": "security",
        "check_key": EVIDENCE_CONFIG_AUDIT,
        "check_name": "field_encryption_key",
        "owner": "security-team",
    },
    {
        "framework": FRAMEWORK_SOC2,
        "control_id": "CC7.1",
        "title": "调试模式关闭",
        "description": "生产环境 DEBUG 必须关闭, 避免堆栈与调试信息泄露",
        "category": "security",
        "check_key": EVIDENCE_CONFIG_AUDIT,
        "check_name": "debug_mode_off",
        "owner": "security-team",
    },
    {
        "framework": FRAMEWORK_SOC2,
        "control_id": "CC7.2",
        "title": "HTTPS 强制",
        "description": "生产环境必须强制 HTTPS, CORS 来源应使用 https:// 前缀",
        "category": "security",
        "check_key": EVIDENCE_CONFIG_AUDIT,
        "check_name": "https_enforced",
        "owner": "security-team",
    },
    {
        "framework": FRAMEWORK_SOC2,
        "control_id": "CC7.3",
        "title": "审计日志启用",
        "description": "审计日志服务必须启用并可写入, 记录关键操作供合规追溯",
        "category": "security",
        "check_key": EVIDENCE_ACCESS_LOG,
        "check_name": "audit_log_enabled",
        "owner": "security-team",
    },
    {
        "framework": FRAMEWORK_SOC2,
        "control_id": "CC7.4",
        "title": "PII 脱敏",
        "description": "审计日志写入前必须进行 PII 脱敏, 防止手机号/邮箱等明文落库",
        "category": "security",
        "check_key": EVIDENCE_ACCESS_LOG,
        "check_name": "pii_redaction_working",
        "owner": "security-team",
    },
    {
        "framework": FRAMEWORK_SOC2,
        "control_id": "CC8.1",
        "title": "变更管理 (Git 历史)",
        "description": "所有变更必须通过 Git 提交记录追溯, 定期检查近期变更频率",
        "category": "security",
        "check_key": EVIDENCE_CHANGE_RECORD,
        "check_name": "git_recent_commits",
        "owner": "devops-team",
    },
    {
        "framework": FRAMEWORK_SOC2,
        "control_id": "CC8.2",
        "title": "安全编码扫描 (pip-audit)",
        "description": "依赖漏洞扫描 (pip-audit) 必须定期执行, 无高危漏洞",
        "category": "security",
        "check_key": EVIDENCE_SECURITY_SCAN,
        "check_name": "pip_audit_scan",
        "owner": "devops-team",
    },
    {
        "framework": FRAMEWORK_SOC2,
        "control_id": "CC8.3",
        "title": "密钥泄漏扫描 (gitleaks)",
        "description": "代码库密钥泄漏扫描 (gitleaks) 必须定期执行, 无硬编码凭据",
        "category": "security",
        "check_key": EVIDENCE_SECURITY_SCAN,
        "check_name": "gitleaks_scan",
        "owner": "devops-team",
    },
    # --- 可用性 (Availability) ---
    {
        "framework": FRAMEWORK_SOC2,
        "control_id": "A1.1",
        "title": "数据库就绪",
        "description": "生产环境建议使用 PostgreSQL, SQLite 仅作开发兼容",
        "category": "availability",
        "check_key": EVIDENCE_CONFIG_AUDIT,
        "check_name": "database_url",
        "owner": "devops-team",
    },
    {
        "framework": FRAMEWORK_SOC2,
        "control_id": "A1.2",
        "title": "备份文件存在",
        "description": "数据库备份文件必须定期生成且存在, 满足 RPO 要求",
        "category": "availability",
        "check_key": EVIDENCE_BACKUP_VERIFY,
        "check_name": "backup_file_exists",
        "owner": "devops-team",
    },
    {
        "framework": FRAMEWORK_SOC2,
        "control_id": "A1.3",
        "title": "备份恢复验证",
        "description": "备份文件必须可完整恢复, 定期执行恢复验证满足 RTO 要求",
        "category": "availability",
        "check_key": EVIDENCE_BACKUP_VERIFY,
        "check_name": "restore_test_passed",
        "owner": "devops-team",
    },
    {
        "framework": FRAMEWORK_SOC2,
        "control_id": "A2.1",
        "title": "云端凭据配置",
        "description": "OCR/ASR 云端凭据应配置, 避免多模态抽取降级",
        "category": "availability",
        "check_key": EVIDENCE_CONFIG_AUDIT,
        "check_name": "cloud_credentials",
        "owner": "devops-team",
    },
    # --- 处理完整性 (Processing Integrity) ---
    {
        "framework": FRAMEWORK_SOC2,
        "control_id": "PI1.1",
        "title": "审计日志完整性",
        "description": "审计日志必须可追溯且未被篡改, 保证处理过程完整性",
        "category": "processing_integrity",
        "check_key": EVIDENCE_ACCESS_LOG,
        "check_name": "audit_log_enabled",
        "owner": "security-team",
    },
    {
        "framework": FRAMEWORK_SOC2,
        "control_id": "PI1.2",
        "title": "模型档位配置",
        "description": "MODEL_TIER 应显式指定, 避免生产环境档位漂移",
        "category": "processing_integrity",
        "check_key": EVIDENCE_CONFIG_AUDIT,
        "check_name": "model_tier",
        "owner": "devops-team",
    },
    # --- 机密性 (Confidentiality) ---
    {
        "framework": FRAMEWORK_SOC2,
        "control_id": "C1.1",
        "title": "字段加密 (机密性)",
        "description": "敏感字段必须加密存储, 保证静态数据机密性",
        "category": "confidentiality",
        "check_key": EVIDENCE_CONFIG_AUDIT,
        "check_name": "field_encryption_key",
        "owner": "security-team",
    },
    {
        "framework": FRAMEWORK_SOC2,
        "control_id": "C2.1",
        "title": "PII 机密性保护",
        "description": "PII 数据必须脱敏处理, 保证机密性",
        "category": "confidentiality",
        "check_key": EVIDENCE_ACCESS_LOG,
        "check_name": "pii_redaction_working",
        "owner": "security-team",
    },
    # --- 隐私性 (Privacy) ---
    {
        "framework": FRAMEWORK_SOC2,
        "control_id": "P1.1",
        "title": "PII 隐私保护",
        "description": "员工 PII 数据必须脱敏与审计, 满足隐私性要求",
        "category": "privacy",
        "check_key": EVIDENCE_ACCESS_LOG,
        "check_name": "pii_redaction_working",
        "owner": "security-team",
    },
    {
        "framework": FRAMEWORK_SOC2,
        "control_id": "P2.1",
        "title": "数据留存策略",
        "description": "必须配置数据留存策略, 满足隐私法规留存期要求 (人工评估)",
        "category": "privacy",
        "check_key": None,
        "check_name": None,
        "owner": "dpo",
    },
    # ============================================================
    # ISO27001:2022 Annex A
    # ============================================================
    # --- A.5 组织控制 (Organizational) ---
    {
        "framework": FRAMEWORK_ISO27001,
        "control_id": "A.5.1",
        "title": "信息安全策略",
        "description": "必须制定并发布信息安全策略, 经管理层批准 (人工评估)",
        "category": "organizational",
        "check_key": None,
        "check_name": None,
        "owner": "dpo",
    },
    {
        "framework": FRAMEWORK_ISO27001,
        "control_id": "A.5.15",
        "title": "访问控制策略",
        "description": "必须建立访问控制规则, CORS 来源显式配置",
        "category": "organizational",
        "check_key": EVIDENCE_CONFIG_AUDIT,
        "check_name": "cors_origins",
        "owner": "security-team",
    },
    {
        "framework": FRAMEWORK_ISO27001,
        "control_id": "A.5.17",
        "title": "认证信息管理",
        "description": "认证信息 (JWT 密钥) 必须安全生成、存储与轮换",
        "category": "organizational",
        "check_key": EVIDENCE_CONFIG_AUDIT,
        "check_name": "jwt_secret_key",
        "owner": "security-team",
    },
    {
        "framework": FRAMEWORK_ISO27001,
        "control_id": "A.5.30",
        "title": "业务连续性 ICT 就绪",
        "description": "必须定期备份并验证可恢复, 保证业务连续性",
        "category": "organizational",
        "check_key": EVIDENCE_BACKUP_VERIFY,
        "check_name": "backup_file_exists",
        "owner": "devops-team",
    },
    {
        "framework": FRAMEWORK_ISO27001,
        "control_id": "A.5.34",
        "title": "PII 隐私与保护",
        "description": "必须识别并保护 PII, 满足个人信息保护要求",
        "category": "organizational",
        "check_key": EVIDENCE_ACCESS_LOG,
        "check_name": "pii_redaction_working",
        "owner": "dpo",
    },
    # --- A.6 人员控制 (People) ---
    {
        "framework": FRAMEWORK_ISO27001,
        "control_id": "A.6.3",
        "title": "信息安全意识教育",
        "description": "必须定期开展信息安全意识培训 (人工评估)",
        "category": "people",
        "check_key": None,
        "check_name": None,
        "owner": "hr",
    },
    # --- A.7 物理控制 (Physical) ---
    {
        "framework": FRAMEWORK_ISO27001,
        "control_id": "A.7.4",
        "title": "物理安全监控",
        "description": "必须对物理办公场所进行安全监控 (人工评估)",
        "category": "physical",
        "check_key": None,
        "check_name": None,
        "owner": "facilities",
    },
    # --- A.8 技术控制 (Technological) ---
    {
        "framework": FRAMEWORK_ISO27001,
        "control_id": "A.8.3",
        "title": "信息访问限制",
        "description": "必须限制信息访问, CORS 来源显式配置",
        "category": "technological",
        "check_key": EVIDENCE_CONFIG_AUDIT,
        "check_name": "cors_origins",
        "owner": "security-team",
    },
    {
        "framework": FRAMEWORK_ISO27001,
        "control_id": "A.8.5",
        "title": "安全认证",
        "description": "必须使用安全认证机制, JWT 算法建议非对称",
        "category": "technological",
        "check_key": EVIDENCE_CONFIG_AUDIT,
        "check_name": "jwt_algorithm",
        "owner": "security-team",
    },
    {
        "framework": FRAMEWORK_ISO27001,
        "control_id": "A.8.7",
        "title": "恶意软件防护",
        "description": "必须防护恶意软件, 依赖漏洞扫描无高危项",
        "category": "technological",
        "check_key": EVIDENCE_SECURITY_SCAN,
        "check_name": "pip_audit_scan",
        "owner": "devops-team",
    },
    {
        "framework": FRAMEWORK_ISO27001,
        "control_id": "A.8.9",
        "title": "配置管理",
        "description": "必须安全配置硬件/软件, 调试模式关闭",
        "category": "technological",
        "check_key": EVIDENCE_CONFIG_AUDIT,
        "check_name": "debug_mode_off",
        "owner": "devops-team",
    },
    {
        "framework": FRAMEWORK_ISO27001,
        "control_id": "A.8.11",
        "title": "数据掩码",
        "description": "必须对 PII 进行数据掩码, 防止明文泄露",
        "category": "technological",
        "check_key": EVIDENCE_ACCESS_LOG,
        "check_name": "pii_redaction_working",
        "owner": "security-team",
    },
    {
        "framework": FRAMEWORK_ISO27001,
        "control_id": "A.8.12",
        "title": "数据泄漏防护",
        "description": "必须防护数据泄漏, 代码库无硬编码凭据",
        "category": "technological",
        "check_key": EVIDENCE_SECURITY_SCAN,
        "check_name": "gitleaks_scan",
        "owner": "devops-team",
    },
    {
        "framework": FRAMEWORK_ISO27001,
        "control_id": "A.8.13",
        "title": "信息备份",
        "description": "必须定期备份信息, 备份文件存在且可恢复",
        "category": "technological",
        "check_key": EVIDENCE_BACKUP_VERIFY,
        "check_name": "backup_file_exists",
        "owner": "devops-team",
    },
    {
        "framework": FRAMEWORK_ISO27001,
        "control_id": "A.8.16",
        "title": "监控活动",
        "description": "必须监控异常活动, 审计日志启用且可追溯",
        "category": "technological",
        "check_key": EVIDENCE_ACCESS_LOG,
        "check_name": "audit_log_enabled",
        "owner": "security-team",
    },
    {
        "framework": FRAMEWORK_ISO27001,
        "control_id": "A.8.25",
        "title": "安全开发生命周期",
        "description": "必须建立安全开发流程, 变更通过 Git 追溯",
        "category": "technological",
        "check_key": EVIDENCE_CHANGE_RECORD,
        "check_name": "git_recent_commits",
        "owner": "devops-team",
    },
    {
        "framework": FRAMEWORK_ISO27001,
        "control_id": "A.8.28",
        "title": "安全编码",
        "description": "必须遵循安全编码规范, 依赖漏洞扫描通过",
        "category": "technological",
        "check_key": EVIDENCE_SECURITY_SCAN,
        "check_name": "pip_audit_scan",
        "owner": "devops-team",
    },
]


def _normalize_status(raw: str) -> str:
    """将检查脚本的原始状态 (PASS/FAIL/WARN 等) 归一化为合规状态常量。

    check_prod_readiness 使用大写 PASS/FAIL/WARN, 统一映射为小写 pass/fail/warning,
    未知值降级为 warning (避免阻断, 供人工复核)。
    """
    if not raw:
        return CONTROL_STATUS_WARNING
    raw_upper = raw.strip().upper()
    if raw_upper in ("PASS", "OK", "PASSED"):
        return CONTROL_STATUS_PASS
    if raw_upper in ("FAIL", "FAILED", "ERROR"):
        return CONTROL_STATUS_FAIL
    if raw_upper in ("WARN", "WARNING"):
        return CONTROL_STATUS_WARNING
    if raw_upper in ("NA", "NOT_APPLICABLE", "N/A", "SKIP", "SKIPPED"):
        return CONTROL_STATUS_NOT_APPLICABLE
    return CONTROL_STATUS_WARNING


class ComplianceService:
    """合规认证服务 (数据库实现)

    提供合规控制矩阵管理、自动化证据收集与合规报告生成。
    事务边界由路由层控制 (service 层不 commit)。
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.settings = get_settings()

    # ============================================================
    # 控制项管理
    # ============================================================

    async def initialize_controls(self, *, tenant_id: Optional[str] = None) -> int:
        """初始化预置合规控制项 (幂等: 已存在则跳过)。

        返回新建的控制项数量。
        """
        tenant_id = tenant_id or get_current_tenant()
        created = 0
        for spec in COMPLIANCE_CONTROLS:
            existing = (
                await self.session.execute(
                    select(ComplianceControl).where(
                        ComplianceControl.tenant_id == tenant_id,
                        ComplianceControl.framework == spec["framework"],
                        ComplianceControl.control_id == spec["control_id"],
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                continue
            control = ComplianceControl(
                tenant_id=tenant_id,
                framework=spec["framework"],
                control_id=spec["control_id"],
                title=spec["title"],
                description=spec.get("description", ""),
                category=spec.get("category", ""),
                status=CONTROL_STATUS_NOT_APPLICABLE,
                evidence={
                    "check_key": spec.get("check_key"),
                    "check_name": spec.get("check_name"),
                    "auto_checkable": spec.get("check_key") is not None,
                },
                owner=spec.get("owner"),
            )
            self.session.add(control)
            created += 1
        if created:
            await self.session.flush()
        return created

    async def list_controls(
        self,
        framework: Optional[str] = None,
        *,
        tenant_id: Optional[str] = None,
    ) -> List[ComplianceControl]:
        """列出所有控制项 (可按框架过滤)"""
        tenant_id = tenant_id or get_current_tenant()
        stmt = (
            select(ComplianceControl)
            .where(ComplianceControl.tenant_id == tenant_id)
            .order_by(
                ComplianceControl.framework.asc(),
                ComplianceControl.control_id.asc(),
            )
        )
        if framework:
            stmt = stmt.where(ComplianceControl.framework == framework)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_control(
        self,
        control_id: str,
        framework: Optional[str] = None,
        *,
        tenant_id: Optional[str] = None,
    ) -> Optional[ComplianceControl]:
        """获取控制项详情 (control_id 在预置集合内全局唯一, 可选 framework 二次确认)"""
        tenant_id = tenant_id or get_current_tenant()
        stmt = select(ComplianceControl).where(
            ComplianceControl.tenant_id == tenant_id,
            ComplianceControl.control_id == control_id,
        )
        if framework:
            stmt = stmt.where(ComplianceControl.framework == framework)
        result = await self.session.execute(stmt)
        # control_id 在预置集合内跨框架不冲突; 若出现多条则返回第一条
        return result.scalars().first()

    async def update_control(
        self,
        control_id: str,
        status: Optional[str] = None,
        owner: Optional[str] = None,
        framework: Optional[str] = None,
        evidence: Optional[Dict[str, Any]] = None,
        *,
        tenant_id: Optional[str] = None,
        collector: str = "manual",
    ) -> ComplianceControl:
        """手动更新控制项 (状态 / 负责人 / 证据)

        Args:
            control_id: 控制编号
            status: 新状态 (pass/fail/warning/not_applicable), None 表示不更新
            owner: 新负责人, None 表示不更新
            framework: 可选, 二次确认框架
            evidence: 可选, 附加证据 (合并入 evidence 快照)
            collector: 证据采集人 (默认 manual)

        Returns:
            更新后的 ComplianceControl 对象
        """
        tenant_id = tenant_id or get_current_tenant()
        control = await self.get_control(
            control_id, framework=framework, tenant_id=tenant_id
        )
        if control is None:
            raise ValueError(f"控制项 {control_id} 不存在")

        new_status = status
        if new_status is not None and new_status not in (
            CONTROL_STATUS_PASS,
            CONTROL_STATUS_FAIL,
            CONTROL_STATUS_WARNING,
            CONTROL_STATUS_NOT_APPLICABLE,
        ):
            raise ValueError(
                f"无效的控制项状态: {new_status}, 可选: pass/fail/warning/not_applicable"
            )

        now = datetime.now(timezone.utc)
        # 合并证据快照
        current_evidence = dict(control.evidence or {})
        if evidence:
            current_evidence.update(evidence)
        current_evidence["last_updated_by"] = collector
        current_evidence["last_updated_at"] = now.isoformat()

        if new_status is not None:
            control.status = new_status
            current_evidence["status"] = new_status
        if owner is not None:
            control.owner = owner
        # 手动更新视为一次评估, 更新 last_checked
        control.evidence = current_evidence
        control.last_checked = now
        await self.session.flush()

        # 追加证据记录 (人工更新也留痕)
        self.session.add(
            ComplianceEvidence(
                tenant_id=tenant_id,
                framework=control.framework,
                control_id=control.control_id,
                evidence_type="manual",
                status=control.status,
                evidence_data=current_evidence,
                collector=collector,
            )
        )
        await self.session.flush()
        return control

    # ============================================================
    # 自动化检查 (证据收集 + 控制项状态更新)
    # ============================================================

    async def run_automated_check(
        self,
        framework: Optional[str] = None,
        *,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """执行自动化检查: 收集证据 → 更新控制项状态 → 写入证据记录

        Args:
            framework: 仅检查指定框架 (None 表示全部框架)
            tenant_id: 租户 ID

        Returns:
            {"checked": N, "updated": M, "by_status": {...}, "evidence": {...}}
        """
        tenant_id = tenant_id or get_current_tenant()
        # 确保控制项已初始化
        await self.initialize_controls(tenant_id=tenant_id)

        # 并行收集各类证据 (均为本地快速操作, 顺序执行亦可)
        config_evidence = await self._collect_config_evidence()
        access_evidence = await self._collect_access_log_evidence()
        change_evidence = self._collect_change_evidence()
        backup_evidence = self._collect_backup_evidence()
        security_evidence = self._collect_security_scan_evidence()

        evidence_by_key: Dict[str, Dict[str, Any]] = {
            EVIDENCE_CONFIG_AUDIT: config_evidence,
            EVIDENCE_ACCESS_LOG: access_evidence,
            EVIDENCE_CHANGE_RECORD: change_evidence,
            EVIDENCE_BACKUP_VERIFY: backup_evidence,
            EVIDENCE_SECURITY_SCAN: security_evidence,
        }

        # 遍历控制项, 按关联的 check_key/check_name 应用检查结果
        controls = await self.list_controls(framework=framework, tenant_id=tenant_id)
        now = datetime.now(timezone.utc)
        checked = 0
        updated = 0
        by_status: Dict[str, int] = {}

        for control in controls:
            ev_snapshot = control.evidence or {}
            check_key = ev_snapshot.get("check_key")
            check_name = ev_snapshot.get("check_name")
            # 无自动检查的控制项保持原状态 (仅人工评估)
            if not check_key or not check_name:
                by_status[control.status] = by_status.get(control.status, 0) + 1
                continue

            subchecks = evidence_by_key.get(check_key, {})
            sub_result = subchecks.get(check_name)
            if sub_result is None:
                # 关联的子检查未执行 (配置漂移), 标记 warning 供复核
                new_status = CONTROL_STATUS_WARNING
                message = f"未找到子检查 {check_name} 的结果 (配置漂移, 请复核控制矩阵)"
                details: Dict[str, Any] = {}
            else:
                new_status = _normalize_status(sub_result.get("status"))
                message = sub_result.get("message", "")
                details = sub_result.get("evidence", {}) or {}

            checked += 1
            status_changed = control.status != new_status
            updated += 1 if status_changed else 0

            control.status = new_status
            control.last_checked = now
            control.evidence = {
                "check_key": check_key,
                "check_name": check_name,
                "status": new_status,
                "message": message,
                "details": details,
                "collected_at": now.isoformat(),
                "auto_checkable": True,
            }

            # 追加证据时间序列记录
            self.session.add(
                ComplianceEvidence(
                    tenant_id=tenant_id,
                    framework=control.framework,
                    control_id=control.control_id,
                    evidence_type=check_key,
                    status=new_status,
                    evidence_data={
                        "check_name": check_name,
                        "message": message,
                        "details": details,
                        "collected_at": now.isoformat(),
                    },
                    collector="system",
                )
            )

            by_status[new_status] = by_status.get(new_status, 0) + 1

        await self.session.flush()
        return {
            "checked": checked,
            "updated": updated,
            "by_status": by_status,
            "evidence_sources": {
                key: {
                    name: {
                        "status": _normalize_status(val.get("status")),
                        "message": val.get("message", ""),
                    }
                    for name, val in value.items()
                }
                for key, value in evidence_by_key.items()
            },
        }

    # ============================================================
    # 自动化证据收集方法 (复用现有项目模块)
    # ============================================================

    async def _collect_config_evidence(self) -> Dict[str, Dict[str, Any]]:
        """配置审计证据: 复用 scripts.check_prod_readiness + 扩展 (HTTPS / 调试模式)

        检查 JWT_SECRET_KEY 强度 / HTTPS / 调试模式关闭 / CORS / 字段加密 / KMS 等。
        返回 {check_name: {status, message, evidence}} 字典。
        """
        # 复用生产就绪检查逻辑
        from scripts.check_prod_readiness import check_readiness

        try:
            readiness = check_readiness(self.settings)
            raw_checks = {c["name"]: c for c in readiness["checks"]}
        except Exception as e:
            logger.warning("配置审计 check_readiness 失败: %s", e, exc_info=True)
            raw_checks = {}

        result: Dict[str, Dict[str, Any]] = {}
        for name, chk in raw_checks.items():
            result[name] = {
                "status": _normalize_status(chk.get("status")),
                "message": chk.get("message", ""),
                "evidence": {
                    "raw_status": chk.get("status"),
                    "name": name,
                },
            }

        # 扩展检查: 调试模式关闭
        debug_on = bool(getattr(self.settings, "debug", False))
        result["debug_mode_off"] = {
            "status": CONTROL_STATUS_PASS if not debug_on else CONTROL_STATUS_WARNING,
            "message": (
                "DEBUG 已关闭" if not debug_on else "DEBUG 开启, 生产环境应关闭"
            ),
            "evidence": {"debug": debug_on},
        }

        # 扩展检查: HTTPS 强制 (基于 CORS 来源是否 https 前缀)
        origins_raw = (self.settings.cors_origins or "").strip()
        parts = [o.strip() for o in origins_raw.split(",") if o.strip()]
        has_wildcard = any(o == "*" for o in parts)
        https_origins = [o for o in parts if o.startswith("https://")]
        if not parts or has_wildcard:
            https_status = CONTROL_STATUS_WARNING
            https_msg = "CORS 来源为空或含通配 *, 无法确认 HTTPS 强制"
        elif https_origins and len(https_origins) == len(parts):
            https_status = CONTROL_STATUS_PASS
            https_msg = f"全部 {len(parts)} 个 CORS 来源使用 HTTPS"
        elif https_origins:
            https_status = CONTROL_STATUS_WARNING
            https_msg = (
                f"仅 {len(https_origins)}/{len(parts)} 个 CORS 来源使用 HTTPS, "
                "建议全部启用 HTTPS"
            )
        else:
            https_status = CONTROL_STATUS_WARNING
            https_msg = "CORS 来源未使用 HTTPS 前缀, 生产环境应强制 HTTPS"
        result["https_enforced"] = {
            "status": https_status,
            "message": https_msg,
            "evidence": {
                "cors_origins": parts,
                "https_count": len(https_origins),
                "total": len(parts),
            },
        }

        return result

    async def _collect_access_log_evidence(self) -> Dict[str, Dict[str, Any]]:
        """访问日志证据: 审计日志是否启用 / PII 脱敏是否工作

        返回 {check_name: {status, message, evidence}} 字典。
        """
        result: Dict[str, Dict[str, Any]] = {}

        # 审计日志启用: 查询 AuditLog 表是否可读 (存在即视为启用)
        audit_count = 0
        audit_enabled = False
        try:
            from models import AuditLog

            audit_count = (
                await self.session.execute(
                    select(func.count(AuditLog.id))
                )
            ).scalar() or 0
            audit_enabled = True
        except Exception as e:
            logger.warning("审计日志表查询失败: %s", e, exc_info=True)
            audit_enabled = False

        if audit_enabled:
            audit_status = CONTROL_STATUS_PASS
            audit_msg = f"审计日志服务已启用, 当前 {audit_count} 条记录"
        else:
            audit_status = CONTROL_STATUS_FAIL
            audit_msg = "审计日志服务不可用 (表不存在或查询失败)"
        result["audit_log_enabled"] = {
            "status": audit_status,
            "message": audit_msg,
            "evidence": {
                "audit_enabled": audit_enabled,
                "audit_log_count": audit_count,
            },
        }

        # PII 脱敏: 用样本验证 redact_pii 是否真正掩码
        pii_working = False
        sample_input = "手机 13800138000 邮箱 test@example.com 身份证 110101199003073888"
        redacted_output = sample_input
        try:
            from core.utils.pii import redact_pii

            redacted_output = redact_pii(sample_input)
            pii_working = (
                "13800138000" not in redacted_output
                and "test@example.com" not in redacted_output
                and "110101199003073888" not in redacted_output
            )
        except Exception as e:
            logger.warning("PII 脱敏验证失败: %s", e, exc_info=True)
            pii_working = False

        if pii_working:
            pii_status = CONTROL_STATUS_PASS
            pii_msg = "PII 脱敏工作正常, 手机号/邮箱/身份证号已掩码"
        else:
            pii_status = CONTROL_STATUS_FAIL
            pii_msg = "PII 脱敏未生效, 敏感信息可能明文落库"
        result["pii_redaction_working"] = {
            "status": pii_status,
            "message": pii_msg,
            "evidence": {
                "pii_working": pii_working,
                "sample_input": sample_input,
                "redacted_output": redacted_output,
            },
        }

        return result

    def _collect_change_evidence(self) -> Dict[str, Dict[str, Any]]:
        """变更记录证据: Git commit 历史 (最近 7 天变更)

        返回 {check_name: {status, message, evidence}} 字典。
        """
        backend_root = Path(__file__).resolve().parent.parent
        since_date = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
            "%Y-%m-%d"
        )
        commits: List[str] = []
        git_available = False
        try:
            proc = subprocess.run(
                ["git", "log", "--oneline", f"--since={since_date}"],
                cwd=str(backend_root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode == 0:
                git_available = True
                commits = [
                    line for line in proc.stdout.strip().splitlines() if line.strip()
                ]
            else:
                git_available = False
        except Exception as e:
            logger.debug("git log 采集失败 (降级为未追踪): %s", e)
            git_available = False

        commit_count = len(commits)
        # 有近期变更即视为变更管理在运转; 无变更不阻断 (warning 提示复核)
        if not git_available:
            status_val = CONTROL_STATUS_WARNING
            msg = "Git 不可用或非 Git 仓库, 无法采集变更记录 (请确认部署为 Git 仓库)"
        elif commit_count == 0:
            status_val = CONTROL_STATUS_WARNING
            msg = f"最近 7 天无 Git 提交记录 (since={since_date}), 请确认变更流程是否正常"
        else:
            status_val = CONTROL_STATUS_PASS
            msg = f"最近 7 天有 {commit_count} 次提交, 变更可追溯"

        return {
            "git_recent_commits": {
                "status": status_val,
                "message": msg,
                "evidence": {
                    "git_available": git_available,
                    "since_date": since_date,
                    "commit_count": commit_count,
                    "recent_commits": commits[:10],
                },
            }
        }

    def _collect_backup_evidence(self) -> Dict[str, Dict[str, Any]]:
        """备份验证证据: 备份文件是否存在 / 恢复验证是否通过

        复用 scripts.db_backup 的 BACKUP_DIR 配置, 并对最新 SQLite 备份做完整性校验。
        返回 {check_name: {status, message, evidence}} 字典。
        """
        # 从 db_backup 模块读取备份目录 (支持运行时 monkeypatch, 便于测试)
        import scripts.db_backup as db_backup_mod

        backup_dir = Path(getattr(db_backup_mod, "BACKUP_DIR", Path("backups")))
        backup_files: List[str] = []
        if backup_dir.exists():
            backup_files = sorted(
                [
                    f.name
                    for f in backup_dir.iterdir()
                    if f.is_file()
                    and f.name.startswith("db_")
                    and (f.suffix in (".sqlite", ".sql"))
                ],
                reverse=True,
            )

        backup_exists = len(backup_files) > 0
        if backup_exists:
            backup_status = CONTROL_STATUS_PASS
            backup_msg = f"发现 {len(backup_files)} 个备份文件, 最新: {backup_files[0]}"
        else:
            backup_status = CONTROL_STATUS_WARNING
            backup_msg = (
                f"备份目录 {backup_dir} 无备份文件, 请确认备份任务已配置"
            )

        # 恢复验证: 对最新 SQLite 备份执行 PRAGMA integrity_check (轻量级完整性校验)
        restore_passed = False
        integrity_msg = "未执行完整性校验"
        if backup_exists:
            latest_path = backup_dir / backup_files[0]
            if backup_files[0].endswith(".sqlite"):
                try:
                    import sqlite3

                    conn = sqlite3.connect(str(latest_path))
                    try:
                        row = conn.execute("PRAGMA integrity_check").fetchone()
                        ok = row is not None and row[0] == "ok"
                        restore_passed = ok
                        integrity_msg = (
                            "PRAGMA integrity_check = ok" if ok else f"完整性校验失败: {row}"
                        )
                    finally:
                        conn.close()
                except Exception as e:
                    logger.warning("备份完整性校验失败: %s", e, exc_info=True)
                    restore_passed = False
                    integrity_msg = f"完整性校验异常: {e}"
            else:
                # SQL 备份: 检查文件非空即视为可恢复 (pg_dump 文本)
                restore_passed = latest_path.stat().st_size > 0
                integrity_msg = (
                    "SQL 备份文件非空" if restore_passed else "SQL 备份文件为空"
                )

        if restore_passed:
            restore_status = CONTROL_STATUS_PASS
            restore_msg = f"备份恢复验证通过 ({integrity_msg})"
        elif backup_exists:
            restore_status = CONTROL_STATUS_FAIL
            restore_msg = f"备份恢复验证失败 ({integrity_msg})"
        else:
            restore_status = CONTROL_STATUS_WARNING
            restore_msg = "无备份文件, 无法执行恢复验证"

        return {
            "backup_file_exists": {
                "status": backup_status,
                "message": backup_msg,
                "evidence": {
                    "backup_dir": str(backup_dir),
                    "backup_count": len(backup_files),
                    "latest_backup": backup_files[0] if backup_files else None,
                    "backup_files": backup_files[:10],
                },
            },
            "restore_test_passed": {
                "status": restore_status,
                "message": restore_msg,
                "evidence": {
                    "restore_passed": restore_passed,
                    "integrity_check": integrity_msg,
                    "latest_backup": backup_files[0] if backup_files else None,
                },
            },
        }

    def _collect_security_scan_evidence(self) -> Dict[str, Dict[str, Any]]:
        """安全扫描证据: 漏洞扫描结果 (gitleaks / pip-audit)

        工具未安装时降级为 warning (不阻断), 供人工补充扫描结果。
        返回 {check_name: {status, message, evidence}} 字典。
        """
        backend_root = Path(__file__).resolve().parent.parent
        gitleaks = self._run_security_tool(
            "gitleaks",
            ["gitleaks", "detect", "--source", ".", "--no-banner", "--no-git"],
            backend_root,
            clean_on_zero=True,
        )
        pip_audit = self._run_security_tool(
            "pip-audit",
            ["pip-audit", "--strict", "--desc"],
            backend_root,
            clean_on_zero=True,
        )
        return {
            "gitleaks_scan": gitleaks,
            "pip_audit_scan": pip_audit,
        }

    @staticmethod
    def _run_security_tool(
        tool_name: str,
        cmd: List[str],
        cwd: Path,
        *,
        clean_on_zero: bool = False,
    ) -> Dict[str, Any]:
        """执行安全扫描工具 (gitleaks / pip-audit), 返回标准化结果。

        工具未安装 (FileNotFoundError) -> warning;
        执行成功且无发现 -> pass; 执行成功但有发现 -> fail。

        Args:
            tool_name: 工具名称 (gitleaks / pip-audit)
            cmd: 命令行参数列表
            cwd: 执行目录
            clean_on_zero: 退出码 0 是否视为 clean (gitleaks 0=无泄漏, pip-audit 0=无漏洞)
        """
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError:
            return {
                "status": CONTROL_STATUS_WARNING,
                "message": f"{tool_name} 未安装, 跳过扫描 (请安装后定期执行)",
                "evidence": {
                    "tool": tool_name,
                    "installed": False,
                    "exit_code": None,
                },
            }
        except subprocess.TimeoutExpired:
            return {
                "status": CONTROL_STATUS_WARNING,
                "message": f"{tool_name} 扫描超时 (120s), 请检查仓库规模或超时配置",
                "evidence": {
                    "tool": tool_name,
                    "installed": True,
                    "timeout": True,
                },
            }
        except Exception as e:
            logger.warning("%s 扫描异常: %s", tool_name, e, exc_info=True)
            return {
                "status": CONTROL_STATUS_WARNING,
                "message": f"{tool_name} 扫描异常: {e}",
                "evidence": {
                    "tool": tool_name,
                    "installed": True,
                    "error": str(e),
                },
            }

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        exit_code = proc.returncode
        # gitleaks: 0=无泄漏, 1=有泄漏; pip-audit: 0=无漏洞, 非0=有漏洞或错误
        has_findings = exit_code != 0 and not (clean_on_zero and exit_code == 0)
        if exit_code == 0:
            status_val = CONTROL_STATUS_PASS
            msg = f"{tool_name} 扫描通过, 未发现风险"
        else:
            status_val = CONTROL_STATUS_FAIL
            msg = f"{tool_name} 扫描发现风险 (exit_code={exit_code})"

        return {
            "status": status_val,
            "message": msg,
            "evidence": {
                "tool": tool_name,
                "installed": True,
                "exit_code": exit_code,
                "has_findings": has_findings,
                "stdout": stdout[-2000:] if stdout else "",
                "stderr": stderr[-2000:] if stderr else "",
            },
        }

    # ============================================================
    # 合规报告生成
    # ============================================================

    async def generate_report(
        self,
        framework: Optional[str] = None,
        *,
        fmt: str = "json",
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """生成合规报告

        Args:
            framework: 仅包含指定框架 (None 表示全部)
            fmt: 报告格式 "json" 返回结构化 dict (含 CSV 字符串字段);
                 "csv" 仅返回 {"csv": "..."} 控制矩阵
            tenant_id: 租户 ID

        Returns:
            JSON: {"generated_at", "framework", "summary", "controls", "csv"}
            CSV : {"csv": "..."}
        """
        tenant_id = tenant_id or get_current_tenant()
        controls = await self.list_controls(framework=framework, tenant_id=tenant_id)
        now = datetime.now(timezone.utc)

        control_dicts = [self._control_to_dict(c) for c in controls]

        # 状态汇总
        by_status: Dict[str, int] = {}
        by_framework: Dict[str, Dict[str, int]] = {}
        by_category: Dict[str, Dict[str, int]] = {}
        for c in control_dicts:
            st = c["status"]
            by_status[st] = by_status.get(st, 0) + 1
            fw = c["framework"]
            by_framework.setdefault(fw, {})
            by_framework[fw][st] = by_framework[fw].get(st, 0) + 1
            cat = c["category"]
            by_category.setdefault(cat, {})
            by_category[cat][st] = by_category[cat].get(st, 0) + 1

        total = len(control_dicts)
        pass_count = by_status.get(CONTROL_STATUS_PASS, 0)
        fail_count = by_status.get(CONTROL_STATUS_FAIL, 0)
        warning_count = by_status.get(CONTROL_STATUS_WARNING, 0)
        na_count = by_status.get(CONTROL_STATUS_NOT_APPLICABLE, 0)
        # 合规度 = 通过 / (总 - 不适用)
        assessable = total - na_count
        compliance_score = round((pass_count / assessable) * 100, 2) if assessable else 0.0

        summary = {
            "total": total,
            "pass": pass_count,
            "fail": fail_count,
            "warning": warning_count,
            "not_applicable": na_count,
            "compliance_score": compliance_score,
            "by_status": by_status,
            "by_framework": by_framework,
            "by_category": by_category,
        }

        csv_str = self._render_csv(control_dicts)

        if fmt.lower() == "csv":
            return {"csv": csv_str}

        return {
            "generated_at": now.isoformat(),
            "framework": framework or "ALL",
            "summary": summary,
            "controls": control_dicts,
            "csv": csv_str,
        }

    @staticmethod
    def _render_csv(control_dicts: List[Dict[str, Any]]) -> str:
        """渲染 CSV 控制矩阵 (可导入 GRC 工具)"""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "framework",
                "control_id",
                "title",
                "category",
                "status",
                "owner",
                "last_checked",
                "check_key",
                "check_name",
                "message",
            ]
        )
        for c in control_dicts:
            ev = c.get("evidence") or {}
            writer.writerow(
                [
                    c["framework"],
                    c["control_id"],
                    c["title"],
                    c["category"],
                    c["status"],
                    c.get("owner") or "",
                    c.get("last_checked") or "",
                    ev.get("check_key") or "",
                    ev.get("check_name") or "",
                    ev.get("message") or "",
                ]
            )
        return output.getvalue()

    @staticmethod
    def _control_to_dict(c: ComplianceControl) -> Dict[str, Any]:
        """ComplianceControl → dict"""
        return {
            "id": c.id,
            "framework": c.framework,
            "control_id": c.control_id,
            "title": c.title,
            "description": c.description,
            "category": c.category,
            "status": c.status,
            "evidence": c.evidence,
            "last_checked": c.last_checked.isoformat() if c.last_checked else None,
            "owner": c.owner,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        }


# 便于外部按框架预览预置控制项数量
def count_controls_by_framework(framework: str) -> int:
    """返回指定框架的预置控制项数量"""
    return sum(1 for c in COMPLIANCE_CONTROLS if c["framework"] == framework)
