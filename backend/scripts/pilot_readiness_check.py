#!/usr/bin/env python3
"""
AgentValue-AI 试点就绪检查脚本

按 docs/pilot-runbook.md 的 Go/No-Go 清单自动化检查试点启动前的就绪状态，
输出 Go/No-Go 判定 + 详细清单。

检查项（与 Runbook 第二章对齐）：
    1. 环境变量（.env 关键项：JWT/AUTH_DEMO_MODE/MODEL_TIER/DATABASE_URL）
    2. 数据库连通性
    3. 模型档位配置（auto 仅 WARN）
    4. 向量库初始化（目录存在且非空）
    5. 演示账号存在（seed_demo 写入的 E1001）
    6. 前端可访问（dist 目录存在 index.html）
    7. 测试基线（跑一个快速测试子集，可选）

设计要点：
    - 所有检查项返回统一结构 {name, status, message}，status ∈ {PASS, WARN, FAIL}
    - 任意 FAIL → No-Go；WARN 不阻断
    - 单项检查异常不中断整体流程，记为 FAIL 并附异常信息
    - 支持外部传入 settings/paths 便于单测 mock

用法：
    cd backend
    python -m scripts.pilot_readiness_check
"""

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

# 兼容 `python scripts/xxx.py` 直接执行：将 backend 根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))


# 后端根目录（用于定位 .env / data / dist 等）
BACKEND_DIR = Path(__file__).resolve().parent.parent

logger = logging.getLogger(__name__)

# 前端 dist 目录（与 backend 同级）
FRONTEND_DIST_DIR = BACKEND_DIR.parent / "frontend" / "dist"

# 快速测试子集：选取轻量、无外部依赖的测试模块，作为基线冒烟
QUICK_TEST_TARGETS = [
    "tests/test_schema.py",
    "tests/test_prod_readiness.py",
]


def _check_env_vars(env: dict, env_file: Optional[Path]) -> dict:
    """检查关键环境变量是否已配置（值非空且非占位）。"""
    keys = ["JWT_SECRET_KEY", "AUTH_DEMO_MODE", "MODEL_TIER", "DATABASE_URL"]
    missing = [k for k in keys if not env.get(k)]
    if missing:
        return {
            "name": "env_vars",
            "status": "FAIL",
            "message": f"环境变量缺失: {', '.join(missing)}"
            + (
                f"（未读取到 .env: {env_file}）"
                if env_file and not env_file.exists()
                else ""
            ),
        }
    # AUTH_DEMO_MODE 试点环境建议关闭
    if str(env.get("AUTH_DEMO_MODE", "")).lower() == "true":
        return {
            "name": "env_vars",
            "status": "FAIL",
            "message": "AUTH_DEMO_MODE=true，试点环境必须关闭以避免身份伪造",
        }
    return {
        "name": "env_vars",
        "status": "PASS",
        "message": f"关键环境变量已配置: {', '.join(keys)}",
    }


def _check_database(settings) -> dict:
    """检查数据库连通性：执行 SELECT 1，失败记为 FAIL。"""
    try:
        import asyncio
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(settings.database_url, future=True)

        async def _ping() -> None:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))

        asyncio.run(_ping())
        return {
            "name": "database",
            "status": "PASS",
            "message": f"数据库连通: {settings.database_url.split('://')[0]}",
        }
    except Exception as e:
        return {
            "name": "database",
            "status": "FAIL",
            "message": f"数据库连通失败: {type(e).__name__}: {e}",
        }


def _check_model_tier(settings) -> dict:
    """检查模型档位是否显式设置（auto 仅 WARN，不阻断）。"""
    if settings.model_tier == "auto":
        return {
            "name": "model_tier",
            "status": "WARN",
            "message": "MODEL_TIER=auto，建议试点环境显式指定档位（L0/L1/L2/L3）",
        }
    return {
        "name": "model_tier",
        "status": "PASS",
        "message": f"MODEL_TIER={settings.model_tier}",
    }


def _check_vector_store(settings) -> dict:
    """检查向量库目录是否已初始化（存在且包含 sqlite 索引文件）。"""
    vs_dir = Path(settings.vector_store_dir)
    if not vs_dir.exists():
        return {
            "name": "vector_store",
            "status": "WARN",
            "message": f"向量库目录不存在: {vs_dir}（首次启动会自动创建）",
        }
    # 已存在目录但未初始化（无 sqlite 文件）也给 WARN
    has_index = any(vs_dir.rglob("*.sqlite3")) or any(vs_dir.rglob("chroma.sqlite3"))
    if not has_index:
        return {
            "name": "vector_store",
            "status": "WARN",
            "message": f"向量库目录存在但未初始化: {vs_dir}",
        }
    return {
        "name": "vector_store",
        "status": "PASS",
        "message": f"向量库已初始化: {vs_dir}",
    }


def _check_demo_accounts(settings) -> dict:
    """检查演示账号是否已 seed（E1001 存在）。

    试点环境通常使用 SQLite，直接查表；查不到或表不存在均记 FAIL。
    """
    try:
        import asyncio
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(settings.database_url, future=True)

        async def _query():
            async with engine.connect() as conn:
                # 用裸 SQL 兼容未建表场景
                try:
                    row = (
                        await conn.execute(
                            text("SELECT user_id FROM users WHERE user_id = 'E1001'")
                        )
                    ).first()
                    return row[0] if row else None
                except Exception:
                    logger.debug("查询演示账号失败(可能未建表)", exc_info=True)
                    return None

        uid = asyncio.run(_query())
        if uid == "E1001":
            return {
                "name": "demo_accounts",
                "status": "PASS",
                "message": "演示账号 E1001 已存在",
            }
        return {
            "name": "demo_accounts",
            "status": "FAIL",
            "message": "演示账号 E1001 不存在，请先执行 scripts.seed_demo 或 POST /api/v1/auth/seed-demo-users",
        }
    except Exception as e:
        return {
            "name": "demo_accounts",
            "status": "FAIL",
            "message": f"演示账号检查异常: {type(e).__name__}: {e}",
        }


def _check_frontend(dist_dir: Path) -> dict:
    """检查前端构建产物是否就绪（dist/index.html 存在）。"""
    if not dist_dir.exists():
        return {
            "name": "frontend",
            "status": "FAIL",
            "message": f"前端 dist 目录不存在: {dist_dir}（请先执行 npm run build）",
        }
    if not (dist_dir / "index.html").exists():
        return {
            "name": "frontend",
            "status": "FAIL",
            "message": f"前端 dist/index.html 缺失: {dist_dir}",
        }
    return {
        "name": "frontend",
        "status": "PASS",
        "message": f"前端构建产物就绪: {dist_dir}",
    }


def _check_test_baseline(backend_dir: Path, run_tests: bool) -> dict:
    """跑一个快速测试子集作为基线冒烟（默认跳过，--with-tests 开启）。"""
    if not run_tests:
        return {
            "name": "test_baseline",
            "status": "WARN",
            "message": "已跳过测试基线检查（默认不跑，使用 --with-tests 显式开启）",
        }
    if not (backend_dir / "tests").exists():
        return {
            "name": "test_baseline",
            "status": "FAIL",
            "message": f"测试目录不存在: {backend_dir / 'tests'}",
        }
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", *QUICK_TEST_TARGETS, "-q"],
            cwd=str(backend_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {
            "name": "test_baseline",
            "status": "FAIL",
            "message": "测试基线超时（>120s），请检查测试环境",
        }
    if result.returncode == 0:
        return {
            "name": "test_baseline",
            "status": "PASS",
            "message": f"测试基线通过: {' '.join(QUICK_TEST_TARGETS)}",
        }
    # 取末尾 200 字避免太长
    tail = (result.stdout or result.stderr or "")[-200:]
    return {
        "name": "test_baseline",
        "status": "FAIL",
        "message": f"测试基线失败（exit={result.returncode}）: {tail!r}",
    }


def check_readiness(
    settings=None,
    env: Optional[dict] = None,
    env_file: Optional[Path] = None,
    dist_dir: Optional[Path] = None,
    backend_dir: Optional[Path] = None,
    run_tests: bool = False,
) -> dict:
    """
    执行试点就绪检查。

    参数：
        settings: 可选的 Settings 实例；不传时调用 get_settings()
        env: 可选的环境变量字典（用于单测 mock）；不传时读 os.environ
        env_file: .env 文件路径（仅用于错误提示）
        dist_dir: 前端 dist 目录；不传时使用默认 FRONTEND_DIST_DIR
        backend_dir: 后端根目录；不传时使用默认 BACKEND_DIR
        run_tests: 是否执行测试基线冒烟（默认关闭，避免双重跑测试）

    返回：
        dict {
            checks: [{name, status, message}, ...],
            all_passed: bool  # 无 FAIL 即 True（WARN 不影响）
            decision: "Go" | "No-Go"
        }
    """
    if settings is None:
        try:
            from core.config import get_settings

            settings = get_settings()
        except Exception as e:
            checks = [
                {
                    "name": "settings",
                    "status": "FAIL",
                    "message": f"Settings 实例化失败: {type(e).__name__}: {e}",
                }
            ]
            return {
                "checks": checks,
                "all_passed": False,
                "decision": "No-Go",
            }

    env = env if env is not None else dict(os.environ)
    env_file = env_file if env_file is not None else (BACKEND_DIR / ".env")
    dist_dir = dist_dir if dist_dir is not None else FRONTEND_DIST_DIR
    backend_dir = backend_dir if backend_dir is not None else BACKEND_DIR

    checks = [
        _check_env_vars(env, env_file),
        _check_database(settings),
        _check_model_tier(settings),
        _check_vector_store(settings),
        _check_demo_accounts(settings),
        _check_frontend(dist_dir),
        _check_test_baseline(backend_dir, run_tests),
    ]
    all_passed = not any(c["status"] == "FAIL" for c in checks)
    return {
        "checks": checks,
        "all_passed": all_passed,
        "decision": "Go" if all_passed else "No-Go",
    }


def print_report(result: dict) -> None:
    """打印可读的就绪检查报告。"""
    print("=" * 64)
    print("AgentValue-AI 试点就绪检查（Go/No-Go）")
    print("=" * 64)
    for check in result["checks"]:
        status = check["status"]
        if status == "PASS":
            mark = "✅"
        elif status == "FAIL":
            mark = "❌"
        else:
            mark = "⚠️ "
        print(f"{mark} {status:<5} [{check['name']}] {check['message']}")
    print("-" * 64)
    if result["all_passed"]:
        print("判定: ✅ Go（可启动试点，存在 WARN 项请关注）")
    else:
        print(f"判定: ❌ {result['decision']}（存在 FAIL 项，不具备试点启动条件）")
    print("=" * 64)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AgentValue-AI 试点就绪检查")
    parser.add_argument(
        "--with-tests",
        action="store_true",
        help="执行测试基线冒烟（默认跳过，避免与 CI 重复跑测试）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果（便于 CI 解析）",
    )
    args = parser.parse_args(argv)

    result = check_readiness(run_tests=args.with_tests)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_report(result)
    return 0 if result["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
