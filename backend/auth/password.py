"""
密码哈希工具（直接基于 bcrypt，避免 passlib 与新版 bcrypt 的兼容问题）
"""

import logging

import bcrypt

logger = logging.getLogger(__name__)

# bcrypt 限制密码最长 72 字节，超出部分截断（仅影响超长密码）
_MAX_PWD_BYTES = 72


def _truncate(plain: str) -> bytes:
    raw = plain.encode("utf-8")
    if len(raw) > _MAX_PWD_BYTES:
        logger.warning("密码长度超过 72 字节，已截断")
        raw = raw[:_MAX_PWD_BYTES]
    return raw


def hash_password(plain: str) -> str:
    """生成密码哈希"""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(_truncate(plain), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """校验密码"""
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(_truncate(plain), hashed.encode("utf-8"))
    except Exception:
        logger.warning("密码校验异常(hash 格式损坏或库错误),按失败处理", exc_info=True)
        return False
