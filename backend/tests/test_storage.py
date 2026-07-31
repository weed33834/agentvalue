"""
对象存储抽象测试：LocalStorage / S3Storage(mock minio) / 工厂降级 / 附件上传端点。
S3 相关用例全部 mock minio 客户端，不依赖真实 MinIO 服务。
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.config import Settings
from core.storage import (
    AttachmentStorage,
    LocalStorage,
    S3Storage,
    _validate_key,
    create_storage,
    get_storage,
    reset_storage,
)


# ----------------- key 校验 -----------------


def test_validate_key_rejects_traversal():
    for bad in ("../etc/passwd", "a/../../b", "..", "a/../b"):
        with pytest.raises(ValueError):
            _validate_key(bad)


def test_validate_key_rejects_empty_and_null():
    with pytest.raises(ValueError):
        _validate_key("")
    with pytest.raises(ValueError):
        _validate_key("a\x00b")


def test_validate_key_normalizes_leading_slash_and_backslash():
    assert _validate_key("/a/b.txt") == "a/b.txt"
    assert _validate_key("\\a\\b.txt") == "a/b.txt"


def test_storage_abstract_not_instantiable():
    with pytest.raises(TypeError):
        AttachmentStorage()


# ----------------- LocalStorage -----------------


def test_local_storage_roundtrip(tmp_path):
    storage = LocalStorage(str(tmp_path))
    url = storage.upload("dir/a.txt", b"hello", "text/plain")
    assert Path(url).exists()
    assert storage.download("dir/a.txt") == b"hello"
    storage.delete("dir/a.txt")
    assert not Path(url).exists()


def test_local_storage_presigned_url_is_path(tmp_path):
    storage = LocalStorage(str(tmp_path))
    storage.upload("x.txt", b"data", "text/plain")
    url = storage.presigned_url("x.txt", expires=60)
    # 本地存储预签名即文件绝对路径
    assert url == str((tmp_path / "x.txt").resolve())


def test_local_storage_download_missing_raises(tmp_path):
    storage = LocalStorage(str(tmp_path))
    with pytest.raises(FileNotFoundError):
        storage.download("nope.txt")


def test_local_storage_delete_missing_silent(tmp_path):
    storage = LocalStorage(str(tmp_path))
    # 不存在不抛
    storage.delete("nope.txt")


def test_local_storage_path_traversal_blocked(tmp_path):
    storage = LocalStorage(str(tmp_path))
    with pytest.raises(ValueError):
        storage.upload("../escape.txt", b"x", "text/plain")
    with pytest.raises(ValueError):
        storage.download("../etc/passwd")
    # 确认未写出白名单根目录之外
    assert not (tmp_path.parent / "escape.txt").exists()


def test_local_storage_empty_key_rejected(tmp_path):
    storage = LocalStorage(str(tmp_path))
    with pytest.raises(ValueError):
        storage.upload("", b"x", "text/plain")


# ----------------- S3Storage (mock minio) -----------------


def _mock_minio(bucket_exists=True):
    """构造一个已注入 mock 客户端的 S3Storage。"""
    client = MagicMock()
    client.bucket_exists.return_value = bucket_exists
    with patch("minio.Minio", return_value=client):
        storage = S3Storage(
            "minio.local:9000", "ak", "sk", "agentvalue-attachments", secure=False
        )
    return storage, client


def test_s3_upload():
    storage, client = _mock_minio()
    url = storage.upload("dir/file.txt", b"hello", "text/plain")
    client.put_object.assert_called_once()
    args, kwargs = client.put_object.call_args
    assert args[0] == "agentvalue-attachments"
    assert args[1] == "dir/file.txt"
    assert kwargs["length"] == len(b"hello")
    assert kwargs["content_type"] == "text/plain"
    assert url == "http://minio.local:9000/agentvalue-attachments/dir/file.txt"


def test_s3_download():
    storage, client = _mock_minio()
    resp = MagicMock()
    resp.read.return_value = b"hello"
    client.get_object.return_value = resp
    data = storage.download("dir/file.txt")
    assert data == b"hello"
    client.get_object.assert_called_once_with("agentvalue-attachments", "dir/file.txt")
    resp.close.assert_called_once()
    resp.release_conn.assert_called_once()


def test_s3_delete():
    storage, client = _mock_minio()
    storage.delete("dir/file.txt")
    client.remove_object.assert_called_once_with(
        "agentvalue-attachments", "dir/file.txt"
    )


def test_s3_delete_swallows_error():
    storage, client = _mock_minio()
    client.remove_object.side_effect = Exception("boom")
    # 删除失败不应抛出
    storage.delete("dir/file.txt")


def test_s3_presigned_url():
    storage, client = _mock_minio()
    client.presigned_get_object.return_value = "https://signed/url"
    url = storage.presigned_url("dir/file.txt", expires=120)
    assert url == "https://signed/url"
    client.presigned_get_object.assert_called_once()


def test_s3_bucket_autocreate():
    # bucket 不存在时应自动创建
    storage, client = _mock_minio(bucket_exists=False)
    client.make_bucket.assert_called_once_with("agentvalue-attachments")


def test_s3_path_traversal_blocked():
    storage, client = _mock_minio()
    with pytest.raises(ValueError):
        storage.upload("../escape.txt", b"x", "text/plain")
    client.put_object.assert_not_called()


# ----------------- 工厂与降级 -----------------


def test_create_storage_no_endpoint_returns_local(tmp_path):
    settings = Settings(attachment_dir=str(tmp_path))
    storage = create_storage(settings)
    assert isinstance(storage, LocalStorage)


@patch("minio.Minio")
def test_create_storage_with_endpoint_returns_s3(mock_minio_cls, tmp_path):
    client = MagicMock()
    client.bucket_exists.return_value = True
    mock_minio_cls.return_value = client
    settings = Settings(
        attachment_dir=str(tmp_path),
        s3_endpoint="minio.local:9000",
        s3_access_key="ak",
        s3_secret_key="sk",
    )
    storage = create_storage(settings)
    assert isinstance(storage, S3Storage)


@patch("minio.Minio")
def test_create_storage_fallback_on_connection_error(mock_minio_cls, tmp_path):
    # 模拟连接失败：Minio 构造或 bucket_exists 抛异常
    mock_minio_cls.side_effect = Exception("connection refused")
    settings = Settings(
        attachment_dir=str(tmp_path),
        s3_endpoint="minio.local:9000",
        s3_access_key="ak",
        s3_secret_key="sk",
    )
    storage = create_storage(settings)
    # 降级到本地存储，服务不崩
    assert isinstance(storage, LocalStorage)


def test_get_storage_singleton_and_reset(tmp_path):
    reset_storage()
    settings = Settings(attachment_dir=str(tmp_path))
    s1 = get_storage(settings)
    s2 = get_storage(settings)
    assert s1 is s2  # 单例缓存
    reset_storage()
    s3 = get_storage(settings)
    assert s3 is not s1  # reset 后重新创建
    reset_storage()


# ----------------- 附件上传端点 -----------------


@pytest.fixture
def temp_database(monkeypatch):
    """临时 SQLite，隔离测试数据库。"""
    from core import database as db_module
    from core.config import get_settings

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp.name}"
    monkeypatch.setattr(get_settings(), "database_url", db_url)
    db_module.engine = db_module.create_async_engine(db_url, echo=False, future=True)
    db_module.AsyncSessionLocal = db_module.async_sessionmaker(
        bind=db_module.engine,
        class_=db_module.AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    yield db_url
    try:
        Path(db_url.split("///")[-1]).unlink(missing_ok=True)
    except Exception:
        pass


@pytest.fixture
async def initialized_db(temp_database):
    from core.database import close_db, init_db

    await init_db()
    yield
    await close_db()


@pytest.fixture
def api_client(initialized_db, tmp_path, monkeypatch):
    from api.routes import get_attachment_storage
    from core.config import get_settings
    from main import app

    att_dir = tmp_path / "attachments"
    monkeypatch.setattr(get_settings(), "attachment_dir", str(att_dir))
    reset_storage()
    local_storage = LocalStorage(str(att_dir))
    app.dependency_overrides[get_attachment_storage] = lambda: local_storage

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.pop(get_attachment_storage, None)
    reset_storage()


def test_upload_attachment_endpoint(api_client):
    resp = api_client.post(
        "/api/v1/attachments",
        files={"file": ("note.txt", b"hello world", "text/plain")},
        headers={"x-user-role": "employee", "x-user-id": "E1001"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["filename"] == "note.txt"
    assert data["size"] == len(b"hello world")
    assert data["mime"] == "text/plain"
    assert data["key"].endswith(".txt")
    # 本地存储 url 即文件路径，内容应可读
    assert os.path.exists(data["url"])
    with open(data["url"], "rb") as fh:
        assert fh.read() == b"hello world"


def test_upload_attachment_rejected_type(api_client):
    # .exe 不在白名单，应被 InputGuard 拦截
    resp = api_client.post(
        "/api/v1/attachments",
        files={"file": ("evil.exe", b"x", "application/octet-stream")},
        headers={"x-user-role": "employee", "x-user-id": "E1001"},
    )
    assert resp.status_code == 400


def test_upload_attachment_invalid_role(api_client):
    # 无效角色字符串应返回 400
    resp = api_client.post(
        "/api/v1/attachments",
        files={"file": ("note.txt", b"hello", "text/plain")},
        headers={"x-user-role": "superuser"},
    )
    assert resp.status_code == 400
