"""
附件对象存储抽象：本地文件系统与 S3 兼容存储(MinIO)统一接口。
未配置 S3 时降级到本地目录，保证开发与测试零外部依赖；连接失败也不崩，回退本地。
"""

import logging
from abc import ABC, abstractmethod
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from typing import Optional

from core.config import Settings, get_settings

logger = logging.getLogger(__name__)


def _validate_key(key: str) -> str:
    """校验存储 key，禁止路径遍历与空字节，统一分隔符后去除前导斜杠。"""
    if not key or not key.strip():
        raise ValueError("storage key 不能为空")
    if "\x00" in key:
        raise ValueError("storage key 含空字节")
    # 统一为正斜杠，阻断反斜杠绕过
    norm = key.replace("\\", "/")
    parts = norm.split("/")
    if any(p == ".." for p in parts):
        raise ValueError(f"非法 storage key(含路径遍历): {key}")
    # 去前导斜杠，避免被当作绝对路径
    return norm.lstrip("/")


class AttachmentStorage(ABC):
    """附件存储抽象基类：upload/download/delete/presigned_url 四件套。"""

    @abstractmethod
    def upload(self, key: str, data: bytes, content_type: str) -> str:
        """上传二进制内容，返回可引用的 url。"""

    @abstractmethod
    def download(self, key: str) -> bytes:
        """下载对象内容为 bytes。"""

    @abstractmethod
    def delete(self, key: str) -> None:
        """删除对象，不存在时静默。"""

    @abstractmethod
    def presigned_url(self, key: str, expires: int = 3600) -> str:
        """生成可下载的预签名 url，本地实现即文件路径。"""


class LocalStorage(AttachmentStorage):
    """本地文件系统实现：向后兼容，测试与本地开发默认使用。"""

    def __init__(self, base_dir: str):
        self._base = Path(base_dir).resolve()
        self._base.mkdir(parents=True, exist_ok=True)

    def _full_path(self, key: str) -> Path:
        safe = _validate_key(key)
        full = (self._base / safe).resolve()
        # 二次校验：resolve 后必须仍在白名单根目录内
        if full != self._base and self._base not in full.parents:
            raise ValueError(f"路径越权: {key}")
        return full

    def upload(self, key: str, data: bytes, content_type: str) -> str:
        full = self._full_path(key)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)
        return str(full)

    def download(self, key: str) -> bytes:
        full = self._full_path(key)
        if not full.exists():
            raise FileNotFoundError(f"对象不存在: {key}")
        return full.read_bytes()

    def delete(self, key: str) -> None:
        full = self._full_path(key)
        try:
            full.unlink()
        except FileNotFoundError:
            pass

    def presigned_url(self, key: str, expires: int = 3600) -> str:
        # 本地存储无签名概念，直接返回文件绝对路径
        return str(self._full_path(key))


class S3Storage(AttachmentStorage):
    """S3 兼容对象存储实现(MinIO 原生 minio-py)。"""

    def __init__(
        self,
        endpoint: str,
        access_key: Optional[str],
        secret_key: Optional[str],
        bucket: str,
        secure: bool = True,
    ):
        # 惰性导入：未启用 S3 时不需要 minio 依赖
        from minio import Minio

        self._endpoint = endpoint
        self._bucket = bucket
        self._secure = secure
        self._client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        """bucket 不存在则自动创建，连接异常上抛由工厂降级处理。"""
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)
            logger.info("MinIO bucket 已自动创建: %s", self._bucket)

    def _object_url(self, key: str) -> str:
        scheme = "https" if self._secure else "http"
        return f"{scheme}://{self._endpoint}/{self._bucket}/{key}"

    def upload(self, key: str, data: bytes, content_type: str) -> str:
        safe = _validate_key(key)
        self._client.put_object(
            self._bucket,
            safe,
            BytesIO(data),
            length=len(data),
            content_type=content_type or "application/octet-stream",
        )
        return self._object_url(safe)

    def download(self, key: str) -> bytes:
        safe = _validate_key(key)
        resp = self._client.get_object(self._bucket, safe)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    def delete(self, key: str) -> None:
        safe = _validate_key(key)
        try:
            self._client.remove_object(self._bucket, safe)
        except Exception as e:
            logger.warning("删除对象失败 %s: %s", safe, e)

    def presigned_url(self, key: str, expires: int = 3600) -> str:
        safe = _validate_key(key)
        return self._client.presigned_get_object(
            self._bucket, safe, expires=timedelta(seconds=expires)
        )


def create_storage(settings: Optional[Settings] = None) -> AttachmentStorage:
    """
    存储工厂：配置了 s3_endpoint 且能连通则返回 S3Storage，否则降级 LocalStorage。
    连接失败记 warning 但不抛异常，确保服务可用性优先于存储后端。
    """
    settings = settings or get_settings()
    endpoint = settings.s3_endpoint
    if not endpoint:
        return LocalStorage(settings.attachment_dir)

    try:
        storage = S3Storage(
            endpoint=endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            bucket=settings.s3_bucket,
            secure=settings.s3_secure,
        )
        logger.info(
            "附件存储已切换到 S3: endpoint=%s bucket=%s", endpoint, settings.s3_bucket
        )
        return storage
    except Exception as e:
        # 连接/鉴权失败不崩，降级本地存储并告警
        logger.warning("S3 连接失败，降级到本地存储(endpoint=%s): %s", endpoint, e)
        return LocalStorage(settings.attachment_dir)


# 进程级单例：避免每次请求重复建连与 bucket 探活
_storage_singleton: Optional[AttachmentStorage] = None


def get_storage(settings: Optional[Settings] = None) -> AttachmentStorage:
    """获取进程级存储单例，首次调用按 settings 决定后端。"""
    global _storage_singleton
    if _storage_singleton is None:
        _storage_singleton = create_storage(settings)
    return _storage_singleton


def reset_storage() -> None:
    """重置单例，仅供测试切换后端使用。"""
    global _storage_singleton
    _storage_singleton = None
