"""HashiCorp Vault KMS Provider (Transit Engine)

利用 Vault Transit Engine 实现 Envelope Encryption:
- generate_data_key:调用 transit/datakey/plaintext/:name,返回 plaintext + ciphertext
- decrypt:调用 transit/decrypt/:name,返回 plaintext
- rewrap:调用 transit/rewrap/:name,无需本地解密高效升级版本

认证方式 (vault_auth_method):
- "token":     使用静态 token (开发/dev vault,生产不推荐)
- "approle":   role_id + secret_id (传统部署 / 本地开发 / 裸机)
- "kubernetes": Pod SA JWT (生产 K8s 部署首选,自动轮换)

hvac SDK 是同步的 (基于 requests),用 asyncio.to_thread 包装融入 async 应用。
参考 rerank_provider.py:BGERerankProvider 已用此模式包装 CrossEncoder.predict。

参考:
- hvac 2.4.0 文档: https://python-hvac.org/en/latest/
- Vault Transit: https://developer.hashicorp.com/vault/docs/secrets/transit
- Vault AppRole: https://developer.hashicorp.com/vault/docs/auth/approle
- Vault K8s auth: https://developer.hashicorp.com/vault/docs/auth/kubernetes
"""

import asyncio
import base64
import json
import logging
import os
import threading
import time
from typing import Dict, Optional

from core.kms.base import (
    KMSAuthenticationError,
    KMSCiphertextInvalidError,
    KMSProvider,
    KMSProviderError,
    KMSUnavailableError,
)

logger = logging.getLogger(__name__)


def _encode_context(ctx: Optional[Dict[str, str]]) -> Optional[str]:
    """Vault context 必须是 base64 字符串"""
    if not ctx:
        return None
    return base64.urlsafe_b64encode(
        json.dumps(ctx, sort_keys=True).encode("utf-8")
    ).decode("ascii")


class VaultKMSProvider(KMSProvider):
    """Vault Transit Engine 实现"""

    def __init__(
        self,
        addr: str,
        auth_method: str = "token",
        token: Optional[str] = None,
        role_id: Optional[str] = None,
        secret_id: Optional[str] = None,
        k8s_role: Optional[str] = None,
        namespace: Optional[str] = None,
        transit_mount: str = "transit",
        kv_mount: str = "secret",
        kek_name: str = "agentvalue-field-kek",
        jwt_key_path: str = "agentvalue/jwt-signing-key",
        verify_tls: bool = True,
        token_renew_interval_seconds: int = 300,
    ):
        self._addr = addr
        self._auth_method = auth_method
        self._token = token
        self._role_id = role_id
        self._secret_id = secret_id
        self._k8s_role = k8s_role
        self._namespace = namespace
        self._transit_mount = transit_mount
        self._kv_mount = kv_mount
        self._kek_name = kek_name
        self._jwt_key_path = jwt_key_path
        self._verify_tls = verify_tls
        self._token_renew_interval = max(60, token_renew_interval_seconds)
        self._client = None
        self._renewer_thread: Optional[threading.Thread] = None
        self._renewer_stop: Optional[threading.Event] = None
        self._init_lock = threading.Lock()
        self._initialized = False

    def _ensure_client(self):
        """惰性初始化 hvac Client (避免 import 时连接 Vault)"""
        if self._client is not None and self._is_client_alive():
            return self._client
        with self._init_lock:
            if self._client is not None and self._is_client_alive():
                return self._client
            try:
                import hvac
                from requests.adapters import HTTPAdapter
                from urllib3.util.retry import Retry
                import requests

                session = requests.Session()
                retry = Retry(
                    total=3,
                    connect=3,
                    read=3,
                    backoff_factor=0.5,
                    status_forcelist=(500, 502, 503, 504),
                    allowed_methods=("GET", "POST", "PUT"),
                )
                adapter = HTTPAdapter(
                    max_retries=retry, pool_connections=10, pool_maxsize=20
                )
                session.mount("https://", adapter)
                session.mount("http://", adapter)

                self._client = hvac.Client(
                    url=self._addr,
                    verify=self._verify_tls,
                    session=session,
                    timeout=30,
                    namespace=self._namespace,
                )
                self._authenticate()
                self._start_token_renewer()
                self._initialized = True
                logger.info(
                    "Vault client connected: %s (auth=%s)",
                    self._addr,
                    self._auth_method,
                )
            except ImportError as e:
                raise KMSProviderError(
                    "hvac 未安装,启用 Vault KMS: pip install hvac>=2.4.0",
                    provider=self.name,
                    cause=e,
                ) from e
            except Exception as e:
                # 转 KMS 异常类型
                self._translate_vault_exception(e, "init")
        return self._client

    def _is_client_alive(self) -> bool:
        try:
            return self._client is not None and self._client.is_authenticated()
        except Exception:
            return False

    def _authenticate(self):
        """按 auth_method 执行认证"""
        try:
            if self._auth_method == "token":
                if not self._token:
                    # 优先从环境变量 VAULT_TOKEN 取 (Vault CLI 兼容)
                    self._token = os.environ.get("VAULT_TOKEN")
                if not self._token:
                    raise KMSAuthenticationError(
                        "auth_method=token 需配置 vault_token 或环境变量 VAULT_TOKEN",
                        provider=self.name,
                    )
                self._client.token = self._token
            elif self._auth_method == "approle":
                if not self._role_id or not self._secret_id:
                    raise KMSAuthenticationError(
                        "auth_method=approle 需配置 vault_role_id + vault_secret_id",
                        provider=self.name,
                    )
                self._client.auth.approle.login(
                    role_id=self._role_id, secret_id=self._secret_id
                )
            elif self._auth_method == "kubernetes":
                if not self._k8s_role:
                    raise KMSAuthenticationError(
                        "auth_method=kubernetes 需配置 vault_k8s_role",
                        provider=self.name,
                    )
                jwt_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
                try:
                    with open(jwt_path) as f:
                        jwt = f.read()
                except OSError as e:
                    raise KMSAuthenticationError(
                        f"K8s SA JWT 不存在 ({jwt_path}),非 K8s 部署?",
                        provider=self.name,
                        cause=e,
                    ) from e
                self._client.auth.kubernetes.login(role=self._k8s_role, jwt=jwt)
            else:
                raise KMSAuthenticationError(
                    f"不支持的 vault_auth_method: {self._auth_method}",
                    provider=self.name,
                )
            if not self._client.is_authenticated():
                raise KMSAuthenticationError("Vault 认证失败", provider=self.name)
        except KMSAuthenticationError:
            raise
        except Exception as e:
            self._translate_vault_exception(e, "auth")

    def _start_token_renewer(self):
        """后台线程定时续约 token (AppRole / K8s 模式)"""
        if self._auth_method == "token":
            return  # token 模式不续约 (静态 token)
        if self._renewer_thread is not None:
            return
        self._renewer_stop = threading.Event()
        self._renewer_thread = threading.Thread(
            target=self._renewer_loop, daemon=True, name="vault-token-renewer"
        )
        self._renewer_thread.start()

    def _renewer_loop(self):
        while not self._renewer_stop.is_set():
            try:
                info = self._client.auth.token.lookup_self()
                ttl = info["data"]["ttl"]
                if ttl < 600:  # 剩余 < 10min 立即续约
                    self._client.auth.token.renew_self(increment="1h")
                    logger.info("Vault token renewed")
            except Exception as e:
                logger.warning("Vault token 续约失败,尝试重新认证: %s", e)
                try:
                    self._authenticate()
                except Exception as e2:
                    logger.error("Vault 重新认证失败,等待下次重试: %s", e2)
            self._renewer_stop.wait(self._token_renew_interval)

    def stop(self):
        """停止 token 续约线程 (应用 shutdown 时调用)"""
        if self._renewer_stop:
            self._renewer_stop.set()
        if self._renewer_thread:
            self._renewer_thread.join(timeout=10)
            self._renewer_thread = None

    @property
    def name(self) -> str:
        return "vault"

    async def generate_data_key(
        self,
        key_spec: str = "AES_256",
        encryption_context: Optional[Dict[str, str]] = None,
    ) -> Dict[str, bytes]:
        # key_type=plaintext 同时返回 plaintext + ciphertext (Envelope)
        # 参考 transit/datakey/plaintext/:name 端点
        def _call() -> Dict[str, bytes]:
            client = self._ensure_client()
            context = _encode_context(encryption_context)
            try:
                resp = client.secrets.transit.generate_data_key(
                    name=self._kek_name,
                    key_type="plaintext",
                    context=context,
                    mount_point=self._transit_mount,
                )
            except Exception as e:
                self._translate_vault_exception(e, "generate_data_key")
            plaintext_b64 = resp["data"]["plaintext"]
            ciphertext = resp["data"]["ciphertext"]  # "vault:v1:..."
            return {
                "plaintext": base64.urlsafe_b64decode(plaintext_b64),
                "ciphertext_blob": ciphertext.encode("utf-8"),
            }

        return await asyncio.to_thread(_call)

    async def decrypt(
        self,
        ciphertext_blob: bytes,
        encryption_context: Optional[Dict[str, str]] = None,
    ) -> Dict[str, bytes]:
        def _call() -> Dict[str, bytes]:
            client = self._ensure_client()
            # ciphertext_blob 是 bytes,转 str
            if isinstance(ciphertext_blob, bytes):
                ciphertext = ciphertext_blob.decode("utf-8")
            else:
                ciphertext = ciphertext_blob
            context = _encode_context(encryption_context)
            try:
                resp = client.secrets.transit.decrypt_data(
                    name=self._kek_name,
                    ciphertext=ciphertext,
                    context=context,
                    mount_point=self._transit_mount,
                )
            except Exception as e:
                self._translate_vault_exception(e, "decrypt")
            plaintext_b64 = resp["data"]["plaintext"]
            return {"plaintext": base64.urlsafe_b64decode(plaintext_b64)}

        return await asyncio.to_thread(_call)

    async def rewrap(
        self,
        ciphertext_blob: bytes,
        encryption_context: Optional[Dict[str, str]] = None,
    ) -> bytes:
        """Vault 原生 rewrap_data,无需本地解密高效升级版本"""

        def _call() -> bytes:
            client = self._ensure_client()
            ciphertext = (
                ciphertext_blob.decode("utf-8")
                if isinstance(ciphertext_blob, bytes)
                else ciphertext_blob
            )
            context = _encode_context(encryption_context)
            try:
                resp = client.secrets.transit.rewrap_data(
                    name=self._kek_name,
                    ciphertext=ciphertext,
                    context=context,
                    mount_point=self._transit_mount,
                )
            except Exception as e:
                self._translate_vault_exception(e, "rewrap")
            return resp["data"]["ciphertext"].encode("utf-8")

        return await asyncio.to_thread(_call)

    async def health_check(self) -> bool:
        def _call() -> bool:
            try:
                client = self._ensure_client()
                return client.sys.is_initialized() and not client.sys.is_sealed()
            except Exception as e:
                logger.debug("Vault health_check 失败: %s", e)
                return False

        return await asyncio.to_thread(_call)

    # ===== KV v2 (供 JWT secret 等静态密钥使用) =====

    async def read_secret(self, path: str) -> Dict:
        """从 KV v2 读取静态密钥 (如 JWT signing key)

        Returns:
            secret data dict (如 {"value": "base64...", "algorithm": "HS256"})
        """

        def _call() -> Dict:
            client = self._ensure_client()
            try:
                resp = client.secrets.kv.v2.read_secret_version(
                    path=path, mount_point=self._kv_mount
                )
            except Exception as e:
                self._translate_vault_exception(e, f"read_secret {path}")
            return resp["data"]["data"]

        return await asyncio.to_thread(_call)

    async def write_secret(self, path: str, data: Dict) -> None:
        """写入 KV v2 (供运维/初始化脚本使用)"""

        def _call():
            client = self._ensure_client()
            try:
                client.secrets.kv.v2.create_or_update_secret(
                    path=path, secret=data, mount_point=self._kv_mount
                )
            except Exception as e:
                self._translate_vault_exception(e, f"write_secret {path}")

        await asyncio.to_thread(_call)

    async def read_jwt_secret(self) -> str:
        """便捷方法:读 JWT 签名密钥"""
        secret = await self.read_secret(self._jwt_key_path)
        if "value" not in secret:
            raise KMSProviderError(
                f"Vault path {self._jwt_key_path} 缺少 value 字段",
                provider=self.name,
            )
        return secret["value"]

    @staticmethod
    def _translate_vault_exception(e: Exception, op: str):
        """把 hvac 异常转为 KMS* 异常"""
        try:
            import hvac.exceptions as hvac_exc
        except ImportError:
            hvac_exc = None

        # KMS 异常已 raise 的不再包装
        if isinstance(
            e, (KMSAuthenticationError, KMSCiphertextInvalidError, KMSUnavailableError)
        ):
            raise

        provider = "vault"
        msg = f"Vault {op} 失败: {e}"

        if hvac_exc is not None:
            if isinstance(e, hvac_exc.Unauthorized):
                raise KMSAuthenticationError(msg, provider=provider, cause=e) from e
            if isinstance(e, hvac_exc.Forbidden):
                raise KMSAuthenticationError(
                    msg + " (策略未授权)", provider=provider, cause=e
                ) from e
            if isinstance(e, hvac_exc.InvalidPath):
                raise KMSCiphertextInvalidError(
                    msg + " (path 不存在)", provider=provider, cause=e
                ) from e
            if isinstance(e, hvac_exc.InvalidRequest):
                raise KMSCiphertextInvalidError(msg, provider=provider, cause=e) from e
            if isinstance(e, hvac_exc.VaultDown):
                raise KMSUnavailableError(
                    msg + " (Vault sealed)", provider=provider, cause=e
                ) from e
            if isinstance(e, hvac_exc.RateLimitExceeded):
                raise KMSUnavailableError(
                    msg + " (rate limit)", provider=provider, cause=e
                ) from e
            if isinstance(e, hvac_exc.InternalServerError):
                raise KMSUnavailableError(msg, provider=provider, cause=e) from e

        # 网络异常
        if isinstance(e, (ConnectionError, TimeoutError, OSError)):
            raise KMSUnavailableError(
                msg + " (网络故障)", provider=provider, cause=e
            ) from e

        # 兜底
        raise KMSProviderError(msg, provider=provider, cause=e) from e
