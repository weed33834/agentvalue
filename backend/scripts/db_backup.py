#!/usr/bin/env python3
"""P0: 自动化数据库备份脚本

支持 SQLite 和 PostgreSQL 两种数据库的自动化备份:
- SQLite: 直接复制 .db 文件(使用 VACUUM INTO 保证一致性)
- PostgreSQL: 使用 pg_dump 导出

使用方式:
    # 手动执行
    python scripts/db_backup.py

    # 定时执行(cron)
    0 2 * * * cd /app && python scripts/db_backup.py >> /var/log/db_backup.log 2>&1

    # 自定义保留天数
    python scripts/db_backup.py --retain-days 30

    # 通过 APScheduler 注册(在应用内调用)
    from scripts.db_backup import schedule_backup
    schedule_backup(scheduler)

依赖:
    - SQLite: 无额外依赖(Python 标准库)
    - PostgreSQL: 需要 pg_dump 命令行工具(PostgreSQL client 包)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 默认保留天数
DEFAULT_RETAIN_DAYS = 30
# 备份目录(相对于项目根目录)
BACKUP_DIR = Path(os.environ.get("DB_BACKUP_DIR", "backups"))


def _get_database_url() -> str:
    """从 Settings 获取数据库 URL"""
    from core.config import get_settings

    settings = get_settings()
    return settings.database_url


def _is_sqlite(db_url: str) -> bool:
    """判断是否为 SQLite 数据库"""
    return db_url.startswith("sqlite:///")


def _get_sqlite_path(db_url: str) -> Path:
    """从 SQLite URL 提取文件路径"""
    # sqlite:///path/to/db.sqlite → path/to/db.sqlite
    path = db_url.replace("sqlite:///", "", 1)
    return Path(path)


def backup_sqlite(db_url: str, backup_dir: Path) -> Path:
    """SQLite 数据库备份 — 使用 VACUUM INTO 保证一致性快照

    VACUUM INTO 在事务隔离下创建数据库的原子快照,
    比直接复制 .db 文件更安全(避免 WAL 模式下部分写入问题)。
    """
    db_path = _get_sqlite_path(db_url)
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite 数据库文件不存在: {db_path}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_file = backup_dir / f"db_{timestamp}.sqlite"

    # 使用 VACUUM INTO 创建一致性快照
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(f"VACUUM INTO '{backup_file}'")
    finally:
        conn.close()

    logger.info("SQLite 备份完成: %s (%.2f MB)", backup_file, backup_file.stat().st_size / 1024 / 1024)
    return backup_file


def backup_postgres(db_url: str, backup_dir: Path) -> Path:
    """PostgreSQL 数据库备份 — 使用 pg_dump 导出

    将连接字符串转换为 pg_dump 兼容的参数格式。
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_file = backup_dir / f"db_{timestamp}.sql"

    # 解析连接字符串
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
    db_name = parsed.path.lstrip("/") or "postgres"

    # 执行 pg_dump
    cmd = ["pg_dump", "--format=plain", "--no-owner", "--no-privileges", db_name]
    with open(backup_file, "w") as f:
        result = subprocess.run(
            cmd, env=env, stdout=f, stderr=subprocess.PIPE, text=True
        )

    if result.returncode != 0:
        raise RuntimeError(f"pg_dump 失败: {result.stderr}")

    logger.info(
        "PostgreSQL 备份完成: %s (%.2f MB)",
        backup_file,
        backup_file.stat().st_size / 1024 / 1024,
    )
    return backup_file


def cleanup_old_backups(backup_dir: Path, retain_days: int = DEFAULT_RETAIN_DAYS) -> int:
    """清理过期备份文件, 返回删除的文件数"""
    if not backup_dir.exists():
        return 0

    cutoff = datetime.now(timezone.utc).timestamp() - (retain_days * 86400)
    deleted = 0

    for f in backup_dir.iterdir():
        if not f.is_file():
            continue
        if f.name.startswith("db_") and (f.suffix == ".sqlite" or f.suffix == ".sql"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1
                logger.info("清理过期备份: %s", f.name)

    return deleted


def run_backup(retain_days: int = DEFAULT_RETAIN_DAYS) -> Path | None:
    """执行完整备份流程: 创建备份 → 清理旧备份

    Returns:
        备份文件路径, 失败返回 None
    """
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        db_url = _get_database_url()

        if _is_sqlite(db_url):
            backup_file = backup_sqlite(db_url, BACKUP_DIR)
        else:
            backup_file = backup_postgres(db_url, BACKUP_DIR)

        # 清理旧备份
        deleted = cleanup_old_backups(BACKUP_DIR, retain_days)
        if deleted > 0:
            logger.info("清理了 %d 个过期备份文件", deleted)

        return backup_file
    except Exception as e:
        logger.error("数据库备份失败: %s", e, exc_info=True)
        return None


def schedule_backup(scheduler, hour: int = 2, minute: int = 0):
    """通过 APScheduler 注册定时备份任务

    默认每天凌晨 2:00 执行备份。
    """
    import asyncio

    async def _register():
        await scheduler.add_task(
            name="数据库自动备份",
            func=run_backup,
            cron_expression=f"{minute} {hour} * * *",
            task_type="system",
            description="每天定时备份数据库并清理过期备份(默认保留30天)",
            task_id="db_backup",
        )

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(_register())
        else:
            loop.run_until_complete(_register())
    except Exception as e:
        logger.warning("注册数据库备份定时任务失败: %s", e)


def schedule_restore_test(scheduler, day_of_week: str = "sun", hour: int = 4, minute: int = 0):
    """通过 APScheduler 注册定期备份恢复验证任务

    默认每周日凌晨 4:00 执行(避开备份时间 2:00)。
    在 staging 环境运行,验证备份文件可完整恢复。

    Args:
        scheduler: APScheduler TaskScheduler 实例
        day_of_week: 执行日期(周几),默认 "sun"(周日)
        hour: 执行小时,默认 4
        minute: 执行分钟,默认 0
    """
    import asyncio

    async def _run_restore_verification():
        """执行恢复验证(异步包装)"""
        try:
            from scripts.db_restore_test import _run_sqlite_restore_test, _run_postgres_restore_test

            db_url = _get_database_url()
            if db_url.startswith("sqlite"):
                success = await _run_sqlite_restore_test(db_url)
            else:
                success = await _run_postgres_restore_test(db_url)

            if success:
                logger.info("✅ 定期备份恢复验证通过")
            else:
                logger.error("❌ 定期备份恢复验证失败,请检查备份文件完整性")
                # TODO: 可通过 alert_service 发送告警通知
        except Exception as e:
            logger.error("备份恢复验证异常: %s", e, exc_info=True)

    async def _register():
        await scheduler.add_task(
            name="数据库备份恢复验证",
            func=_run_restore_verification,
            cron_expression=f"{minute} {hour} * * {day_of_week}",
            task_type="system",
            description="每周定期验证备份文件可完整恢复(staging 环境)",
            task_id="db_restore_verify",
        )

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(_register())
        else:
            loop.run_until_complete(_register())
    except Exception as e:
        logger.warning("注册备份恢复验证定时任务失败: %s", e)


def main():
    parser = argparse.ArgumentParser(description="数据库备份工具")
    parser.add_argument(
        "--retain-days",
        type=int,
        default=DEFAULT_RETAIN_DAYS,
        help=f"备份保留天数(默认 {DEFAULT_RETAIN_DAYS} 天)",
    )
    parser.add_argument(
        "--restore-test",
        action="store_true",
        help="执行备份恢复验证(而非备份)",
    )
    args = parser.parse_args()

    if args.restore_test:
        # 运行恢复验证
        from scripts.db_restore_test import main as restore_main
        restore_main()
        return

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    backup_file = run_backup(retain_days=args.retain_days)
    if backup_file:
        print(f"备份成功: {backup_file}")
        sys.exit(0)
    else:
        print("备份失败!", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
