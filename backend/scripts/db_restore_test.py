"""P0-4: 数据库备份恢复验证脚本

定期在 staging 环境执行,验证备份文件可完整恢复。
CI 中可手动触发或定时执行。

用法:
    python scripts/db_restore_test.py                          # SQLite 自动测试
    python scripts/db_restore_test.py --db-url "postgresql://..."  # PostgreSQL
    python scripts/db_restore_test.py --backup-file /path/to/backup.sqlite  # 指定备份文件

验证流程:
1. 创建测试数据(插入若干记录)
2. 执行备份(db_backup.py)
3. 从备份恢复到临时数据库
4. 验证恢复数据与原始数据一致
5. 清理临时文件

退出码: 0=成功, 1=失败
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


async def _run_sqlite_restore_test(db_url: str) -> bool:
    """SQLite 备份恢复验证"""
    import sqlite3

    from core.database import AsyncSessionLocal, engine, init_db
    from models.models import User
    from sqlalchemy import select

    # 1. 初始化数据库并插入测试数据
    await init_db()
    test_user_id = f"restore-test-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    test_user_name = "恢复测试用户"

    async with AsyncSessionLocal() as session:
        user = User(
            user_id=test_user_id,
            name=test_user_name,
            role="employee",
            tenant_id="default",
        )
        session.add(user)
        await session.commit()
    logger.info("测试数据已插入: user_id=%s", test_user_id)

    # 2. 执行备份
    from scripts.db_backup import backup_sqlite

    backup_dir = Path(tempfile.mkdtemp(prefix="restore_test_"))
    backup_file = await asyncio.to_thread(backup_sqlite, db_url, backup_dir)
    logger.info("备份完成: %s", backup_file)

    # 3. 从备份恢复到临时数据库
    test_db_path = backup_dir / "restored.db"
    # SQLite 恢复: 直接复制备份文件(VACUUM INTO 已保证一致性)
    import shutil

    shutil.copy2(backup_file, test_db_path)

    # 4. 验证恢复数据
    conn = sqlite3.connect(str(test_db_path))
    cursor = conn.execute(
        "SELECT user_id, name FROM users WHERE user_id = ?", (test_user_id,)
    )
    row = cursor.fetchone()
    conn.close()

    if row is None:
        logger.error("恢复验证失败: 测试用户 %s 未在恢复数据中找到", test_user_id)
        return False

    if row[1] != test_user_name:
        logger.error(
            "恢复验证失败: 用户名不匹配 (期望=%s, 实际=%s)", test_user_name, row[1]
        )
        return False

    logger.info("恢复验证成功: 测试用户 %s 数据完整", test_user_id)

    # 5. 清理
    try:
        backup_file.unlink(missing_ok=True)
        test_db_path.unlink(missing_ok=True)
        backup_dir.rmdir()
    except Exception:
        pass

    # 清理原始数据库中的测试数据
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.user_id == test_user_id)
        )
        user = result.scalar_one_or_none()
        if user:
            await session.delete(user)
            await session.commit()

    await engine.dispose()
    return True


async def _run_postgres_restore_test(db_url: str) -> bool:
    """PostgreSQL 备份恢复验证

    使用 pg_restore 恢复到临时数据库,验证数据一致性。
    需要 psql/pg_restore 客户端工具。
    """
    import os
    import subprocess
    from urllib.parse import urlparse

    from scripts.db_backup import backup_postgres

    # 1. 创建测试数据
    from core.database import AsyncSessionLocal, init_db
    from models.models import User
    from sqlalchemy import select

    await init_db()
    test_user_id = f"restore-test-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    test_user_name = "恢复测试用户"

    async with AsyncSessionLocal() as session:
        user = User(
            user_id=test_user_id,
            name=test_user_name,
            role="employee",
            tenant_id="default",
        )
        session.add(user)
        await session.commit()
    logger.info("测试数据已插入: user_id=%s", test_user_id)

    # 2. 执行备份
    backup_dir = Path(tempfile.mkdtemp(prefix="restore_test_"))
    backup_file = await asyncio.to_thread(backup_postgres, db_url, backup_dir)
    logger.info("备份完成: %s", backup_file)

    # 3. 创建临时数据库并恢复
    parsed = urlparse(db_url)
    env = os.environ.copy()
    if parsed.username:
        env["PGUSER"] = parsed.username
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    if parsed.hostname:
        env["PGHOST"] = parsed.hostname
    if parsed.port:
        env["PGPORT"] = str(parsed.port)

    test_db_name = f"restore_test_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

    # 创建临时数据库
    admin_db = parsed.path.lstrip("/") or "postgres"
    result = subprocess.run(
        ["psql", "-d", admin_db, "-c", f"CREATE DATABASE {test_db_name};"],
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error("创建临时数据库失败: %s", result.stderr)
        return False

    try:
        # 恢复备份到临时数据库
        result = subprocess.run(
            ["psql", "-d", test_db_name, "-f", str(backup_file)],
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error("恢复失败: %s", result.stderr)
            return False

        # 4. 验证数据
        result = subprocess.run(
            [
                "psql",
                "-d",
                test_db_name,
                "-t",
                "-c",
                f"SELECT name FROM users WHERE user_id = '{test_user_id}';",
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        restored_name = result.stdout.strip()
        if restored_name != test_user_name:
            logger.error(
                "恢复验证失败: 用户名不匹配 (期望=%s, 实际=%s)",
                test_user_name,
                restored_name,
            )
            return False

        logger.info("恢复验证成功: 测试用户 %s 数据完整", test_user_id)
        return True
    finally:
        # 5. 清理临时数据库
        subprocess.run(
            ["psql", "-d", admin_db, "-c", f"DROP DATABASE {test_db_name};"],
            env=env,
            capture_output=True,
        )
        backup_file.unlink(missing_ok=True)
        backup_dir.rmdir(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="数据库备份恢复验证")
    parser.add_argument(
        "--db-url",
        default=None,
        help="数据库连接字符串(默认使用 settings.database_url)",
    )
    parser.add_argument(
        "--backup-file",
        default=None,
        help="指定备份文件(跳过备份步骤,直接验证恢复)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    from core.config import get_settings

    settings = get_settings()
    db_url = args.db_url or settings.database_url

    logger.info("开始数据库备份恢复验证: %s", db_url)

    try:
        if db_url.startswith("sqlite"):
            success = asyncio.run(_run_sqlite_restore_test(db_url))
        else:
            success = asyncio.run(_run_postgres_restore_test(db_url))
    except Exception as e:
        logger.error("恢复验证异常: %s", e, exc_info=True)
        success = False

    if success:
        logger.info("✅ 备份恢复验证通过")
        sys.exit(0)
    else:
        logger.error("❌ 备份恢复验证失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
