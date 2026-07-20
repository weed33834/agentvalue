#!/usr/bin/env python3
"""
AgentValue-AI 年度第三方安全审计数据导出（Phase 9.3）

为第三方安全审计员打包脱敏后的系统数据包，包含：
- 系统配置（脱敏，不含密钥）
- RBAC 权限矩阵
- 审计日志采样（最近 1000 条）
- 测试覆盖率报告
- 已知漏洞 / 技术债清单
- 数据流图（mermaid）

输出 ZIP 包到 backend/data/security-audit-{date}.zip。

CLI：
    python -m scripts.security_audit_export --output data/
"""

import argparse
import asyncio
import io
import json
import logging
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.rbac import VIEW_PERMISSIONS, Role
from core.config import get_settings
from core.database import AsyncSessionLocal
from models import AuditLog

logger = logging.getLogger(__name__)

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = BACKEND_ROOT / "data"

# 配置中需脱敏的字段名关键字：命中任一即替换为 ***REDACTED***
SENSITIVE_KEYWORDS = ("key", "secret", "password", "token", "hash")

# RBAC 端点权限矩阵（依据 api/routes.py 中 require_role 实际调用梳理）
# 格式：endpoint -> 允许角色集合
ENDPOINT_RBAC_MATRIX: Dict[str, List[str]] = {
    "POST /api/v1/inputs": ["employee", "manager", "hr", "admin"],
    "GET /api/v1/inputs": ["employee", "manager", "hr", "admin"],
    "GET /api/v1/inputs/{input_id}": ["employee", "manager", "hr", "admin"],
    "POST /api/v1/attachments": ["employee", "manager", "hr", "admin"],
    "POST /api/v1/evaluations": ["employee", "manager", "hr", "admin"],
    "GET /api/v1/evaluations/{evaluation_id}": ["employee", "manager", "hr", "admin"],
    "GET /api/v1/evaluations/{evaluation_id}/employee-view": [
        "employee",
        "manager",
        "hr",
        "admin",
    ],
    "GET /api/v1/evaluations/{evaluation_id}/manager-view": ["manager", "hr", "admin"],
    "POST /api/v1/evaluations/{evaluation_id}/approve": ["manager", "hr", "admin"],
    "POST /api/v1/evaluations/{evaluation_id}/reject": ["manager", "hr", "admin"],
    "POST /api/v1/evaluations/{evaluation_id}/request-hr-review": [
        "manager",
        "hr",
        "admin",
    ],
    "POST /api/v1/evaluations/{evaluation_id}/require-reeval": ["hr", "admin"],
    "POST /api/v1/evaluations/{evaluation_id}/appeal": [
        "employee",
        "manager",
        "hr",
        "admin",
    ],
    "POST /api/v1/evaluations/{evaluation_id}/re-evaluate": ["manager", "hr", "admin"],
    "POST /api/v1/evaluations/{evaluation_id}/feedback": [
        "employee",
        "manager",
        "hr",
        "admin",
    ],
    "GET /api/v1/evaluations/{evaluation_id}/audit-logs": ["manager", "hr", "admin"],
    "GET /api/v1/manager/pending-approvals": ["manager", "hr", "admin"],
    "GET /api/v1/manager/dashboard": ["manager", "hr", "admin"],
    "GET /api/v1/hr/audit-queue": ["hr", "admin"],
    "GET /api/v1/admin/audit-logs": ["admin"],
    "POST /api/v1/admin/model-switch": ["admin"],
    "GET /api/v1/admin/model-status": ["admin"],
    "POST /api/v1/kb": ["hr", "admin"],
    "DELETE /api/v1/kb/{kb_id}": ["admin"],
    "POST /api/v1/periods": ["hr", "admin"],
    "POST /api/v1/periods/{period}/close": ["hr", "admin"],
    "POST /api/v1/watermark/verify": ["employee", "manager", "hr", "admin"],
}

# 已知漏洞 / 技术债清单（人工维护，供审计员核对整改进度）
KNOWN_TECH_DEBT: List[Dict[str, Any]] = [
    {
        "id": "TD-001",
        "category": "认证",
        "title": "演示模式 auth_demo_mode 在非生产环境默认可用",
        "risk": "中",
        "status": "已缓解",
        "mitigation": "生产环境（AGENTVALUE_ENV=production）通过 model_validator 强制禁止开启演示模式",
    },
    {
        "id": "TD-002",
        "category": "密钥管理",
        "title": "JWT 密钥需通过环境变量注入，未接入 KMS",
        "risk": "中",
        "status": "待整改",
        "mitigation": "建议接入 Vault / KMS 统一管理，当前依赖环境变量与部署侧校验",
    },
    {
        "id": "TD-003",
        "category": "审计",
        "title": "审计日志仅落库，未做防篡改与异地备份",
        "risk": "低",
        "status": "待整改",
        "mitigation": "建议接入只读对象存储 + 哈希链校验",
    },
    {
        "id": "TD-004",
        "category": "数据留存",
        "title": "Feedback / Memory 表无 archived 字段，GDPR 删除依赖后续清理",
        "risk": "低",
        "status": "已记录",
        "mitigation": "delete_employee_data 已记审计日志，由留存策略周期处理",
    },
]


def _is_sensitive(field_name: str) -> bool:
    name = field_name.lower()
    return any(kw in name for kw in SENSITIVE_KEYWORDS)


def sanitize_settings(settings) -> Dict[str, Any]:
    """导出脱敏后的系统配置：密钥类字段替换为 ***REDACTED***。"""
    redacted: Dict[str, Any] = {}
    for field_name in settings.model_fields:
        value = getattr(settings, field_name, None)
        if _is_sensitive(field_name):
            # 密钥类字段：仅标识是否已配置，不输出值
            redacted[field_name] = "***REDACTED***" if value not in (None, "") else None
        else:
            redacted[field_name] = value
    return redacted


def build_rbac_matrix() -> Dict[str, Any]:
    """构建 RBAC 权限矩阵：视图级 + 端点级。"""
    view_matrix = {role.value: views for role, views in VIEW_PERMISSIONS.items()}
    return {
        "roles": [r.value for r in Role],
        "view_permissions": view_matrix,
        "endpoint_permissions": ENDPOINT_RBAC_MATRIX,
        "note": "manager 受团队归属约束（H7），仅能操作直属下属；HR/ADMIN 不受限",
    }


async def sample_audit_logs(
    session: AsyncSession, limit: int = 1000
) -> List[Dict[str, Any]]:
    """采样最近 limit 条审计日志（脱敏 IP 末段）。

    优先走 ORM；若库表处于多租户字段迁移中途（tenant_id 列尚未落库），
    回退到显式列名 raw SQL，确保审计包仍能导出。
    """
    try:
        rows = (
            (
                await session.execute(
                    select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "log_id": r.log_id,
                "action": r.action,
                "actor_id": r.actor_id,
                "employee_id": r.employee_id,
                "evaluation_id": r.evaluation_id,
                "ip_address": _redact_ip(r.ip_address),
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "details": r.details,
            }
            for r in rows
        ]
    except OperationalError:
        # 多租户迁移中途：ORM 生成的 SQL 带 tenant_id 列但实际表无此列，
        # 回退到显式列名查询绕开 tenant_id，保证审计包可用
        await session.rollback()
        result = await session.execute(
            text(
                "SELECT log_id, action, actor_id, employee_id, evaluation_id, "
                "ip_address, created_at, details "
                "FROM audit_logs ORDER BY created_at DESC LIMIT :lim"
            ),
            {"lim": limit},
        )
        out: List[Dict[str, Any]] = []
        for row in result:
            details = row.details
            if isinstance(details, str):
                try:
                    details = json.loads(details)
                except Exception:
                    details = {"_raw": details}
            created = row.created_at
            if hasattr(created, "isoformat"):
                created = created.isoformat()
            out.append(
                {
                    "log_id": row.log_id,
                    "action": row.action,
                    "actor_id": row.actor_id,
                    "employee_id": row.employee_id,
                    "evaluation_id": row.evaluation_id,
                    "ip_address": _redact_ip(row.ip_address),
                    "created_at": created,
                    "details": details,
                }
            )
        return out


def _redact_ip(ip: Optional[str]) -> Optional[str]:
    """IP 末段脱敏为 .0，保留网段用于审计统计。"""
    if not ip:
        return ip
    parts = ip.split(".")
    if len(parts) == 4:
        return ".".join(parts[:3] + ["0"])
    return ip


def build_test_coverage_report() -> Dict[str, Any]:
    """汇总测试覆盖率信息：测试文件数、用例数与 .coverage 文件状态。"""
    tests_dir = BACKEND_ROOT / "tests"
    test_files = list(tests_dir.glob("test_*.py"))
    # 统计以 def test_ 开头的用例数（粗略，不含 e2e/perf）
    case_count = 0
    for tf in test_files:
        try:
            for line in tf.read_text(encoding="utf-8").splitlines():
                if line.lstrip().startswith("def test_") or line.lstrip().startswith(
                    "async def test_"
                ):
                    case_count += 1
        except Exception:
            continue
    coverage_file = BACKEND_ROOT / ".coverage"
    return {
        "test_files": len(test_files),
        "approx_test_cases": case_count,
        "coverage_file_exists": coverage_file.exists(),
        "coverage_note": (
            ".coverage 存在，可执行 `coverage report` 获取行覆盖率明细"
            if coverage_file.exists()
            else "未发现 .coverage 文件，建议在 CI 中执行 `pytest --cov` 生成"
        ),
        "excluded": ["tests/e2e（需外部服务）", "tests/perf（性能压测）"],
    }


def build_data_flow_diagram() -> str:
    """以 mermaid 描述系统数据流，供审计员核对数据边界。"""
    return """# AgentValue-AI 数据流图

```mermaid
flowchart LR
    Emp[员工/主管] -->|日报/附件| API[FastAPI 路由]
    API -->|输入护栏| Guard[InputGuard]
    Guard -->|通过| DB[(SQLite/PG)]
    Guard -->|拦截| Audit[(审计日志)]
    API -->|异步任务| Queue[JobQueue]
    Queue --> Agent[LangGraph 评估图]
    Agent -->|调用| LLM[模型路由 L0-L3]
    Agent -->|读写| Mem[(向量记忆)]
    Agent -->|读写| KB[(公司知识库)]
    Agent -->|落库评估| DB
    API -->|审批状态机| Approval[ApprovalService]
    Approval -->|状态流转| DB
    API -->|全操作| Audit
    DB -->|留存策略| Archive[归档/清理]
    HR[HR/管理员] -->|复核/审计| API
    API -->|水印状态上报| Watermark[Watermark 校验]
```

数据边界说明：
- 员工原始输入 → 评估结果 → 长期记忆，全链路落库且写入审计日志
- 模型调用经模型路由统一管控，本地优先、云端兜底
- 留存策略按 GDPR/个保法周期归档清理，归档缓冲 30 天
- 水印状态由前端定期上报，后端记录用于截图溯源
"""


async def build_audit_package(session: AsyncSession, settings=None) -> Dict[str, bytes]:
    """组装审计数据包各文件，返回 {filename: content_bytes}。"""
    settings = settings or get_settings()
    now = datetime.now(timezone.utc)

    files: Dict[str, bytes] = {}

    # 1. 系统配置（脱敏）
    files["01_system_config.json"] = json.dumps(
        {
            "exported_at": now.isoformat(),
            "environment": settings.agentvalue_env or "development",
            "config": sanitize_settings(settings),
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")

    # 2. RBAC 权限矩阵
    files["02_rbac_matrix.json"] = json.dumps(
        build_rbac_matrix(), ensure_ascii=False, indent=2
    ).encode("utf-8")

    # 3. 审计日志采样
    files["03_audit_logs_sample.json"] = json.dumps(
        {
            "exported_at": now.isoformat(),
            "sample_size": 1000,
            "logs": await sample_audit_logs(session, limit=1000),
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")

    # 4. 测试覆盖率报告
    files["04_test_coverage.json"] = json.dumps(
        build_test_coverage_report(), ensure_ascii=False, indent=2
    ).encode("utf-8")

    # 5. 已知漏洞 / 技术债清单
    files["05_known_tech_debt.json"] = json.dumps(
        {
            "exported_at": now.isoformat(),
            "items": KNOWN_TECH_DEBT,
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")

    # 6. 数据流图（markdown + mermaid）
    files["06_data_flow.md"] = build_data_flow_diagram().encode("utf-8")

    # 7. 包索引 README
    readme = f"""# AgentValue-AI 安全审计数据包

生成时间：{now.isoformat()}

本数据包为第三方安全审计员提供脱敏后的系统数据，包含：

| 文件 | 内容 |
|---|---|
| 01_system_config.json | 系统配置（密钥已脱敏） |
| 02_rbac_matrix.json | RBAC 角色权限矩阵 |
| 03_audit_logs_sample.json | 审计日志采样（最近 1000 条，IP 末段脱敏） |
| 04_test_coverage.json | 测试覆盖率汇总 |
| 05_known_tech_debt.json | 已知漏洞 / 技术债清单 |
| 06_data_flow.md | 数据流图（mermaid） |

如需访问真实数据，请联系系统管理员按最小权限原则开通。
"""
    files["README.md"] = readme.encode("utf-8")

    return files


async def export_package(output_dir: Path, settings=None) -> Path:
    """打包数据包到 ZIP，返回 ZIP 文件路径。"""
    settings = settings or get_settings()
    output_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    zip_path = output_dir / f"security-audit-{date_str}.zip"

    async with AsyncSessionLocal() as session:
        files = await build_audit_package(session, settings=settings)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    zip_path.write_bytes(buffer.getvalue())
    return zip_path


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AgentValue-AI 安全审计数据导出")
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_DIR),
        help="ZIP 输出目录，默认 backend/data/",
    )
    args = parser.parse_args(argv)
    zip_path = asyncio.run(export_package(Path(args.output)))
    print(f"安全审计数据包已生成: {zip_path}")
    print(f"包含文件: README.md, 01-06 共 7 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
