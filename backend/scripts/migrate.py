#!/usr/bin/env python3
"""
AgentValue-AI 数据库迁移封装脚本。

基于 Alembic，默认读取 backend/alembic.ini，数据库连接串从 core.config 获取。
"""

import argparse
import os

from alembic import command
from alembic.config import Config


ALEMBIC_INI = os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini")


def _make_config() -> Config:
    return Config(ALEMBIC_INI)


def upgrade(revision: str = "head") -> None:
    """执行升级迁移。"""
    command.upgrade(_make_config(), revision)


def downgrade(revision: str = "-1") -> None:
    """执行降级迁移。"""
    command.downgrade(_make_config(), revision)


def revision(message: str, autogenerate: bool = False) -> None:
    """创建新的迁移脚本。"""
    command.revision(_make_config(), message=message, autogenerate=autogenerate)


def current() -> None:
    """查看当前数据库版本。"""
    command.current(_make_config())


def history() -> None:
    """查看迁移历史。"""
    command.history(_make_config())


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentValue-AI 数据库迁移工具")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("upgrade", help="升级到最新版本（head）")
    sub.add_parser("downgrade", help="回退一个版本（-1）")
    sub.add_parser("current", help="查看当前版本")
    sub.add_parser("history", help="查看迁移历史")

    rev_parser = sub.add_parser("revision", help="创建新迁移")
    rev_parser.add_argument("-m", "--message", required=True, help="迁移描述")
    rev_parser.add_argument(
        "--autogenerate",
        action="store_true",
        help="根据模型变化自动生成迁移脚本",
    )

    args = parser.parse_args()

    if args.command == "upgrade":
        upgrade()
    elif args.command == "downgrade":
        downgrade()
    elif args.command == "current":
        current()
    elif args.command == "history":
        history()
    elif args.command == "revision":
        revision(args.message, autogenerate=args.autogenerate)


if __name__ == "__main__":
    main()
