"""
JWT Token 生成与校验
注意：本模块不依赖 auth.rbac，避免循环导入。role 以字符串形式传递。

H5 (v1.5.0) 升级:JWT 签名密钥支持从 Vault KV v2 读取
- field_encryption_backend=vault:优先从 Vault KV 读 jwt_key_path
- 其他 backend 或 Vault 不可用:fallback 到 env jwt_secret_key (向后兼容)
- 生产环境 Vault 不可用时硬失败 (不降级明文)
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import jwt

from core.config import get_settings

logger = logging.getLogger(__name__)

# 模块级 JWT secret 缓存 (避免每次 token 签发都打 Vault)
_jwt_secret_cache: Optional[str] = None
_jwt_secret_fetched: bool = False  # 标记是否已尝试 fetch (失败也不重试,避免每次都失败)


def _ensure_secret_key(settings) -> str:
    """获取 JWT 签名密钥

    优先级:
    1. settings.field_encryption_backend == "vault":从 Vault KV v2 读 jwt_key_path
    2. settings.jwt_secret_key (env):直接用

    生产环境 (agentvalue_env=production):
    - backend=vault 且 Vault 不可用:硬失败 (不降级明文)
    - backend != vault 且未配 jwt_secret_key:硬失败

    非生产环境:Vault 失败时 fallback env (开发友好)
    """
    global _jwt_secret_cache, _jwt_secret_fetched

    # 已缓存:直接返回
    if _jwt_secret_fetched:
        if _jwt_secret_cache is None:
            # 之前 fetch 失败,直接抛 (避免每次重试)
            raise RuntimeError("JWT secret 之前 fetch 失败,请重启服务重试")
        return _jwt_secret_cache

    backend = (getattr(settings, "field_encryption_backend", "env") or "env").lower()
    is_production = settings.agentvalue_env == "production"

    # 1. 优先从 Vault KV v2 读
    if backend == "vault":
        try:
            from core.kms.factory import create_kms_provider

            kms = create_kms_provider(settings)
            if kms is not None and hasattr(kms, "read_jwt_secret"):
                # VaultKMSProvider 有 read_jwt_secret 方法
                import asyncio

                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop is not None and loop.is_running():
                    # 在 async 应用内被 sync 调用 → 新线程跑
                    import concurrent.futures

                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(asyncio.run, kms.read_jwt_secret())
                        secret = future.result()
                else:
                    secret = asyncio.run(kms.read_jwt_secret())

                if secret:
                    _jwt_secret_cache = secret
                    _jwt_secret_fetched = True
                    logger.info("JWT secret 从 Vault KV v2 加载成功")
                    return secret
        except Exception as e:
            logger.error("Vault JWT secret 加载失败: %s", e)
            if is_production:
                raise RuntimeError(f"生产环境 Vault JWT secret 加载失败: {e}") from e
            # 非生产:fallback 到 env (向下兼容)
            logger.warning("非生产环境,fallback 到 jwt_secret_key env")

    # 2. Fallback:从 env 读
    key = settings.jwt_secret_key
    if not key:
        raise RuntimeError(
            "JWT_SECRET_KEY 未配置，请在环境变量中设置强随机密钥后再启动服务"
            + (" 或启用 Vault backend" if backend != "env" else "")
        )
    _jwt_secret_cache = key
    _jwt_secret_fetched = True
    return key


def reset_jwt_secret_cache() -> None:
    """清空 JWT secret 缓存 (供测试 / Vault 轮换后重新 fetch)"""
    global _jwt_secret_cache, _jwt_secret_fetched
    _jwt_secret_cache = None
    _jwt_secret_fetched = False


def create_access_token(
    user_id: str,
    role: str,
    name: str = "",
    expires_minutes: Optional[int] = None,
) -> str:
    """生成 JWT access token"""
    settings = get_settings()
    secret_key = _ensure_secret_key(settings)
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=expires_minutes or settings.jwt_expire_minutes
    )
    payload: Dict[str, Any] = {
        "sub": user_id,
        "role": role,
        "name": name,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        # jti: token 唯一标识,用于主动吊销(登出/密码泄露应急)
        "jti": str(uuid.uuid4()),
    }
    # P0-2：配置了 audience/issuer 时写入 aud/iss claim，decode 端会做校验，
    # 防 token 跨服务复用。未配置时不写（向后兼容）。
    if settings.jwt_audience:
        payload["aud"] = settings.jwt_audience
    if settings.jwt_issuer:
        payload["iss"] = settings.jwt_issuer
    return jwt.encode(payload, secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    """解码并校验 JWT，返回 payload 或 None

    P0-2：按 settings 动态传 audience/issuer/leeway 校验，防 token 跨服务复用与
    分布式时钟漂移误判过期。未配置对应项时不校验（向后兼容）。
    """
    settings = get_settings()
    try:
        secret_key = _ensure_secret_key(settings)
        decode_kwargs: Dict[str, Any] = {"algorithms": [settings.jwt_algorithm]}
        if settings.jwt_audience:
            decode_kwargs["audience"] = settings.jwt_audience
        if settings.jwt_issuer:
            decode_kwargs["issuer"] = settings.jwt_issuer
        if settings.jwt_leeway_seconds:
            decode_kwargs["leeway"] = settings.jwt_leeway_seconds
        # require exp/iat：拒绝缺失过期/签发时间的 token（强校验）
        return jwt.decode(
            token,
            secret_key,
            options={"require": ["exp", "iat"]},
            **decode_kwargs,
        )
    except jwt.ExpiredSignatureError:
        logger.warning("JWT 已过期")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning("JWT 校验失败: %s", e)
        return None


def extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    """从 Authorization header 提取 Bearer token"""
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None
