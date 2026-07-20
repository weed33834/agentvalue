#!/usr/bin/env python3
"""
AgentValue-AI 生产就绪检查脚本

检查生产部署前的安全清单，打印 PASS/FAIL/WARN 报告，
退出码 0 表示全部通过（允许 WARN），1 表示存在 FAIL 项。

检查项：
    1. AUTH_DEMO_MODE 是否关闭（避免身份伪造）
    2. JWT_SECRET_KEY 是否已修改（非空且非默认占位值）
    3. DATABASE_URL 是否非 SQLite（生产建议 PostgreSQL，SQLite 仅 WARN）
    4. MODEL_TIER 是否显式设置（auto 仅 WARN）
    5. FIELD_ENCRYPTION_KEY 是否已配置（生产必填，否则 manager_view/audit 明文落库）
    6. OCR_CLOUD_API_KEY / ASR_CLOUD_API_KEY 是否配置（生产建议，未配置仅 WARN）
    7. CORS_ORIGINS 是否显式配置且不含通配 *（生产必填，否则任意来源可调 API）
    8. JWT_ALGORITHM 是否为 HS256（生产环境给 WARN，建议改用 RS256/ES256 非对称算法）

用法：
    cd backend
    python -m scripts.check_prod_readiness
    python scripts/check_prod_readiness.py
"""

import sys
from pathlib import Path
from typing import Optional

# 兼容 `python scripts/xxx.py` 直接执行：将 backend 根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import Settings, get_settings


# 已知的 JWT_SECRET_KEY 默认/占位值（小写匹配），生产环境必须修改
DEFAULT_JWT_SECRETS = {
    "",
    "change-me",
    "your-secret-key",
    "change-this-to-a-strong-random-secret",
    "dev-only-please-change-me-32chars-or-more",
    "pilot-strong-random-secret-0x9f8e7d6c5b4a",
    "secret",
    "jwt-secret-key",
    "changeme",
}

# 已知的敏感凭据占位值（小写匹配），生产环境必须替换为真实值
# 覆盖 FIELD_ENCRYPTION_KEY / OCR_CLOUD_API_KEY / ASR_CLOUD_API_KEY 等
DEFAULT_SECRET_PLACEHOLDERS = {
    "",
    "your-field-encryption-key",
    "your-ocr-cloud-api-key",
    "your-asr-cloud-api-key",
    "change-me",
    "changeme",
    "placeholder",
    "example",
}


def _check_auth_demo_mode(settings: Settings) -> dict:
    """检查演示模式是否关闭。"""
    if settings.auth_demo_mode:
        return {
            "name": "auth_demo_mode",
            "status": "FAIL",
            "message": "AUTH_DEMO_MODE 已开启，生产环境必须关闭（避免身份伪造）",
        }
    return {
        "name": "auth_demo_mode",
        "status": "PASS",
        "message": "AUTH_DEMO_MODE 已关闭",
    }


def _check_jwt_secret(settings: Settings) -> dict:
    """检查 JWT 密钥是否已配置为非默认值。"""
    key = settings.jwt_secret_key
    if key is None or key.strip() == "" or key.strip().lower() in DEFAULT_JWT_SECRETS:
        return {
            "name": "jwt_secret_key",
            "status": "FAIL",
            "message": "JWT_SECRET_KEY 未设置或为默认占位值，生产环境必须配置强随机密钥",
        }
    return {
        "name": "jwt_secret_key",
        "status": "PASS",
        "message": "JWT_SECRET_KEY 已配置为非默认值",
    }


def _check_database_url(settings: Settings) -> dict:
    """检查数据库连接：生产建议 PostgreSQL，SQLite 给 WARN 但可继续。"""
    url = (settings.database_url or "").lower()
    if "sqlite" in url:
        return {
            "name": "database_url",
            "status": "WARN",
            "message": "DATABASE_URL 为 SQLite，生产环境建议使用 PostgreSQL",
        }
    return {
        "name": "database_url",
        "status": "PASS",
        "message": "DATABASE_URL 已使用非 SQLite 数据库",
    }


def _check_model_tier(settings: Settings) -> dict:
    """检查模型档位是否显式设置（auto 仅 WARN）。"""
    if settings.model_tier == "auto":
        return {
            "name": "model_tier",
            "status": "WARN",
            "message": "MODEL_TIER 为 auto，建议生产环境显式指定档位（L0/L1/L2/L3）",
        }
    return {
        "name": "model_tier",
        "status": "PASS",
        "message": f"MODEL_TIER 已显式设置为 {settings.model_tier}",
    }


def _check_field_encryption_key(settings: Settings) -> dict:
    """检查字段级加密密钥是否已配置（生产环境必填，否则敏感字段明文落库）。

    仅在生产环境（AGENTVALUE_ENV=production）强制 FAIL；非生产环境未配置仅 WARN。
    占位值（空、change-me 等）视为未配置。
    """
    key = settings.field_encryption_key
    is_placeholder = (
        key is None
        or key.strip() == ""
        or key.strip().lower() in DEFAULT_SECRET_PLACEHOLDERS
    )
    is_production = settings.agentvalue_env == "production"

    if is_placeholder:
        if is_production:
            return {
                "name": "field_encryption_key",
                "status": "FAIL",
                "message": (
                    "FIELD_ENCRYPTION_KEY 未配置，生产环境 manager_view/audit 字段将以明文落库，DBA 可绕过应用层读取"
                ),
            }
        return {
            "name": "field_encryption_key",
            "status": "WARN",
            "message": "FIELD_ENCRYPTION_KEY 未配置，字段加密降级为明文透传（仅开发可接受）",
        }
    return {
        "name": "field_encryption_key",
        "status": "PASS",
        "message": "FIELD_ENCRYPTION_KEY 已配置",
    }


def _check_cloud_credentials(settings: Settings) -> dict:
    """检查云端 OCR/ASR 凭据是否已配置（生产环境建议配置，未配置仅 WARN）。

    留空时多模态抽取降级为本地 tesseract/DummyASR，功能受限但不阻断。
    """
    missing = []
    if (
        not settings.ocr_cloud_api_key
        or settings.ocr_cloud_api_key.strip().lower() in DEFAULT_SECRET_PLACEHOLDERS
    ):
        missing.append("OCR_CLOUD_API_KEY")
    if (
        not settings.asr_cloud_api_key
        or settings.asr_cloud_api_key.strip().lower() in DEFAULT_SECRET_PLACEHOLDERS
    ):
        missing.append("ASR_CLOUD_API_KEY")
    if missing:
        return {
            "name": "cloud_credentials",
            "status": "WARN",
            "message": (
                f"{' / '.join(missing)} 未配置，多模态抽取将降级为本地后端（功能受限，生产建议配置云端凭据）"
            ),
        }
    return {
        "name": "cloud_credentials",
        "status": "PASS",
        "message": "云端 OCR/ASR 凭据均已配置",
    }


def _check_cors_origins(settings: Settings) -> dict:
    """检查 CORS 配置是否为生产可用（不能为空或包含通配 *）。

    仅在生产环境强制 FAIL；非生产环境未配置或为默认值仅 WARN。
    """
    origins = (settings.cors_origins or "").strip()
    is_production = settings.agentvalue_env == "production"
    parts = [o.strip() for o in origins.split(",") if o.strip()]
    has_wildcard = any(o == "*" for o in parts)

    if not parts or has_wildcard:
        if is_production:
            return {
                "name": "cors_origins",
                "status": "FAIL",
                "message": "CORS_ORIGINS 为空或含通配 *,生产环境必须显式指定允许的前端域名",
            }
        return {
            "name": "cors_origins",
            "status": "WARN",
            "message": "CORS_ORIGINS 为空或含通配 *,生产环境必须显式指定允许的前端域名",
        }
    return {
        "name": "cors_origins",
        "status": "PASS",
        "message": f"CORS_ORIGINS 已显式配置 {len(parts)} 个域名",
    }


def _check_jwt_algorithm(settings: Settings) -> dict:
    """检查 JWT 算法（P1-6）。

    HS256 为对称算法，密钥泄露即可签发 token；生产环境建议改用 RS256/ES256
    非对称算法，将验证密钥（公钥）与签发密钥（私钥）分离，降低密钥泄露面。
    仅在生产环境对 HS256 给 WARN（不阻断，向后兼容），非生产环境直接 PASS。

    H5 (v1.5.0) 关联:启用 Vault backend 时 JWT 密钥从 KV v2 读取,
    H4 RS256 切换可在 KMS 落地后顺带处理 (此处不强制)。
    """
    algo = (settings.jwt_algorithm or "").strip().upper()
    is_production = settings.agentvalue_env == "production"
    if is_production and algo == "HS256":
        return {
            "name": "jwt_algorithm",
            "status": "WARN",
            "message": (
                "JWT_ALGORITHM 为 HS256(对称算法),生产环境建议改用 RS256/ES256 "
                "非对称算法以分离签发与验证密钥"
            ),
        }
    return {
        "name": "jwt_algorithm",
        "status": "PASS",
        "message": f"JWT_ALGORITHM 为 {algo or 'HS256'}",
    }


def _check_kms_configured(settings: Settings) -> dict:
    """检查 KMS / Vault 配置 (H5: 消除密钥明文配置)

    生产环境强制要求:
    - field_encryption_backend 不能为 "env" 或 "local" (拒绝明文密钥配置)
    - vault:必须配 vault_addr + 认证凭证
    - aws:必须配 aws_kms_key_id
    - aliyun:必须配 aliyun_kms_key_id + aliyun_kms_endpoint

    非生产环境:env / local 仅 WARN (开发友好)
    """
    backend = (getattr(settings, "field_encryption_backend", "env") or "env").lower()
    is_production = settings.agentvalue_env == "production"

    # env / local 模式 (明文配置)
    if backend in ("env", "local"):
        if is_production:
            return {
                "name": "kms_configured",
                "status": "FAIL",
                "message": (
                    f"FIELD_ENCRYPTION_BACKEND={backend} 生产环境不允许,"
                    "密钥明文配置 (FIELD_ENCRYPTION_KEY/JWT_SECRET_KEY) 泄漏面巨大,"
                    "请配置 vault/aws/aliyun KMS backend"
                ),
            }
        return {
            "name": "kms_configured",
            "status": "WARN",
            "message": (
                f"FIELD_ENCRYPTION_BACKEND={backend} (明文配置),"
                "生产环境必须切到 vault/aws/aliyun"
            ),
        }

    # Vault 配置检查
    if backend == "vault":
        missing = []
        if not settings.vault_addr:
            missing.append("VAULT_ADDR")
        if settings.vault_auth_method == "approle":
            if not settings.vault_role_id:
                missing.append("VAULT_ROLE_ID")
            if not settings.vault_secret_id:
                missing.append("VAULT_SECRET_ID")
        elif settings.vault_auth_method == "kubernetes":
            if not settings.vault_k8s_role:
                missing.append("VAULT_K8S_ROLE")
        elif settings.vault_auth_method == "token":
            if not settings.vault_token and not __import__("os").environ.get("VAULT_TOKEN"):
                missing.append("VAULT_TOKEN")
        if missing:
            status = "FAIL" if is_production else "WARN"
            return {
                "name": "kms_configured",
                "status": status,
                "message": f"Vault backend 缺少: {', '.join(missing)}",
            }
        return {
            "name": "kms_configured",
            "status": "PASS",
            "message": f"Vault backend 已配置 (addr={settings.vault_addr}, auth={settings.vault_auth_method})",
        }

    # AWS KMS 配置检查
    if backend == "aws":
        if not settings.aws_kms_key_id:
            status = "FAIL" if is_production else "WARN"
            return {
                "name": "kms_configured",
                "status": status,
                "message": "AWS KMS backend 缺少 AWS_KMS_KEY_ID",
            }
        return {
            "name": "kms_configured",
            "status": "PASS",
            "message": f"AWS KMS backend 已配置 (key_id={settings.aws_kms_key_id})",
        }

    # 阿里云 KMS 配置检查
    if backend == "aliyun":
        missing = []
        if not settings.aliyun_kms_key_id:
            missing.append("ALIYUN_KMS_KEY_ID")
        if not settings.aliyun_kms_endpoint:
            missing.append("ALIYUN_KMS_ENDPOINT")
        if missing:
            status = "FAIL" if is_production else "WARN"
            return {
                "name": "kms_configured",
                "status": status,
                "message": f"阿里云 KMS backend 缺少: {', '.join(missing)}",
            }
        return {
            "name": "kms_configured",
            "status": "PASS",
            "message": f"阿里云 KMS backend 已配置 (key_id={settings.aliyun_kms_key_id})",
        }

    # 未知 backend
    return {
        "name": "kms_configured",
        "status": "FAIL" if is_production else "WARN",
        "message": f"未知 FIELD_ENCRYPTION_BACKEND: {backend}",
    }


def check_readiness(settings: Optional[Settings] = None) -> dict:
    """
    执行生产就绪检查。

    参数：
        settings: 可选的 Settings 实例；不传时读取全局 get_settings()。
                  若生产环境守护校验器拦截实例化（demo_mode 在生产开启），
                  则直接返回该项 FAIL。

    返回：
        dict {
            checks: [{name, status, message}, ...],
            all_passed: bool  # 无任何 FAIL 即为 True（WARN 不影响）
        }
    """
    if settings is None:
        try:
            settings = get_settings()
        except ValueError as e:
            # 生产环境守护触发：AUTH_DEMO_MODE 在生产环境开启
            checks = [
                {
                    "name": "auth_demo_mode",
                    "status": "FAIL",
                    "message": f"生产环境守护拦截: {e}",
                }
            ]
            return {"checks": checks, "all_passed": False}

    checks = [
        _check_auth_demo_mode(settings),
        _check_jwt_secret(settings),
        _check_database_url(settings),
        _check_model_tier(settings),
        _check_field_encryption_key(settings),
        _check_cloud_credentials(settings),
        _check_cors_origins(settings),
        _check_jwt_algorithm(settings),
        _check_kms_configured(settings),
    ]
    all_passed = not any(c["status"] == "FAIL" for c in checks)
    return {"checks": checks, "all_passed": all_passed}


def print_report(result: dict) -> None:
    """打印生产就绪报告。"""
    print("=" * 60)
    print("AgentValue-AI 生产就绪检查")
    print("=" * 60)
    for check in result["checks"]:
        status = check["status"]
        if status == "PASS":
            mark = "✅"
        elif status == "FAIL":
            mark = "❌"
        else:
            mark = "⚠️ "
        print(f"{mark} {status:<5} [{check['name']}] {check['message']}")
    print("-" * 60)
    if result["all_passed"]:
        print("结论: ✅ 全部关键项通过（允许存在 WARN）")
    else:
        print("结论: ❌ 存在 FAIL 项，不具备生产就绪条件")
    print("=" * 60)


def main(argv: Optional[list[str]] = None) -> int:
    """命令行入口。返回 0 表示全部通过，1 表示存在 FAIL。"""
    result = check_readiness()
    print_report(result)
    return 0 if result["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
