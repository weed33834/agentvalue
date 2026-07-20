"""
H1 公司知识库 CRUD API 单元测试
覆盖 POST/GET/DELETE /api/v1/kb 与权限校验。
"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.config import get_settings
from core.database import close_db, init_db
from main import app


@pytest.fixture(autouse=True)
def temp_database(monkeypatch):
    """每个测试使用独立临时 SQLite 数据库"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp.name}"

    monkeypatch.setattr(get_settings(), "database_url", db_url)

    from core import database as db_module

    db_module.engine = db_module.create_async_engine(
        db_url,
        echo=False,
        future=True,
    )
    db_module.AsyncSessionLocal = db_module.async_sessionmaker(
        bind=db_module.engine,
        class_=db_module.AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )

    yield

    try:
        Path(tmp.name).unlink(missing_ok=True)
    except Exception:
        pass


@pytest.fixture
async def initialized_db(temp_database):
    await init_db()
    yield
    await close_db()


@pytest.fixture
def client(initialized_db):
    with TestClient(app) as c:
        yield c


def _hr_headers() -> dict:
    return {"x-user-role": "hr", "x-user-id": "HR001"}


def _admin_headers() -> dict:
    return {"x-user-role": "admin", "x-user-id": "ADMIN001"}


def _employee_headers() -> dict:
    return {"x-user-role": "employee", "x-user-id": "E1001"}


def test_create_kb_doc_hr_success(client):
    """HR 可创建知识库文档"""
    resp = client.post(
        "/api/v1/kb",
        json={
            "kb_id": "KB-TEST-1",
            "title": "测试文档",
            "content": "测试内容",
            "metadata": {"tag": "t1"},
        },
        headers=_hr_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["kb_id"] == "KB-TEST-1"
    assert data["title"] == "测试文档"
    assert data["metadata"] == {"tag": "t1"}


def test_create_kb_doc_duplicate_returns_409(client):
    """重复 kb_id 应返回 409"""
    payload = {
        "kb_id": "KB-DUP",
        "title": "重复",
        "content": "内容",
        "metadata": {},
    }
    client.post("/api/v1/kb", json=payload, headers=_hr_headers())
    resp = client.post("/api/v1/kb", json=payload, headers=_hr_headers())
    assert resp.status_code == 409


def test_create_kb_doc_employee_forbidden(client):
    """employee 无权创建"""
    resp = client.post(
        "/api/v1/kb",
        json={"kb_id": "KB-X", "title": "x", "content": "y", "metadata": {}},
        headers=_employee_headers(),
    )
    assert resp.status_code == 403


def test_list_kb_docs_paginated(client):
    """分页列表返回 items/total/page/page_size"""
    for i in range(3):
        client.post(
            "/api/v1/kb",
            json={
                "kb_id": f"KB-LIST-{i}",
                "title": f"文档{i}",
                "content": "内容",
                "metadata": {},
            },
            headers=_hr_headers(),
        )
    resp = client.get("/api/v1/kb?page=1&page_size=2", headers=_employee_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 3
    assert data["page"] == 1
    assert data["page_size"] == 2
    assert len(data["items"]) <= 2
    assert "kb_id" in data["items"][0]


def test_get_kb_doc_found_and_not_found(client):
    """详情查询：存在/不存在"""
    client.post(
        "/api/v1/kb",
        json={"kb_id": "KB-GET", "title": "g", "content": "c", "metadata": {}},
        headers=_hr_headers(),
    )
    ok = client.get("/api/v1/kb/KB-GET", headers=_employee_headers())
    assert ok.status_code == 200
    assert ok.json()["kb_id"] == "KB-GET"

    miss = client.get("/api/v1/kb/NOPE", headers=_employee_headers())
    assert miss.status_code == 404


def test_delete_kb_doc_admin_success(client):
    """ADMIN 可删除"""
    client.post(
        "/api/v1/kb",
        json={"kb_id": "KB-DEL", "title": "d", "content": "c", "metadata": {}},
        headers=_hr_headers(),
    )
    resp = client.delete("/api/v1/kb/KB-DEL", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    # 删除后查询应 404
    assert client.get("/api/v1/kb/KB-DEL", headers=_admin_headers()).status_code == 404


def test_delete_kb_doc_hr_forbidden(client):
    """HR 无权删除（仅 ADMIN）"""
    client.post(
        "/api/v1/kb",
        json={"kb_id": "KB-DEL2", "title": "d", "content": "c", "metadata": {}},
        headers=_hr_headers(),
    )
    resp = client.delete("/api/v1/kb/KB-DEL2", headers=_hr_headers())
    assert resp.status_code == 403


def test_delete_kb_doc_not_found(client):
    resp = client.delete("/api/v1/kb/NOPE", headers=_admin_headers())
    assert resp.status_code == 404
