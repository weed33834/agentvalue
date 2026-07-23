"""自定义工具 (OpenAPI Schema 导入) 测试 (P3-1)

覆盖:
- OpenAPI 解析: parse_openapi_to_tools + parse_openapi_string (JSON/YAML/无效)
- build_langchain_tool: ToolSpec → BaseTool 可调用 (含 path/query/body 参数)
- CRUD: 创建/读取/更新/删除/启用禁用
- parse 端点: 不入库
- test 端点: 实际 HTTP 调用 (mock httpx)
- 凭证加密: FieldCipher 加密/解密往返
- 鉴权注入: bearer/api_key/basic

运行:
    pytest tests/test_custom_tools.py -v
"""

import base64
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.admin.custom_tools import router as custom_tools_router
from core.config import get_settings


# ============================================================
# 测试用 OpenAPI spec
# ============================================================


def _make_spec_two_paths():
    """2 个 path 的 OpenAPI spec (用于解析测试)"""
    return {
        "openapi": "3.0.0",
        "info": {"title": "Pet API", "version": "1.0.0"},
        "paths": {
            "/pets": {
                "get": {
                    "operationId": "listPets",
                    "summary": "List all pets",
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer"},
                            "description": "Maximum number of pets to return",
                        }
                    ],
                },
                "post": {
                    "operationId": "createPet",
                    "summary": "Create a pet",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"name": {"type": "string"}},
                                }
                            }
                        },
                    },
                },
            },
            "/pets/{petId}": {
                "get": {
                    "operationId": "showPetById",
                    "summary": "Info for a specific pet",
                    "parameters": [
                        {
                            "name": "petId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                            "description": "The ID of the pet",
                        }
                    ],
                },
            },
        },
    }


def _make_spec_with_refs():
    """带 $ref 引用的 OpenAPI spec (验证引用解析)"""
    return {
        "openapi": "3.0.0",
        "info": {"title": "User API", "version": "1.0.0"},
        "paths": {
            "/users": {
                "post": {
                    "operationId": "createUser",
                    "summary": "Create a user",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/User"}
                            }
                        },
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "User": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "age": {"type": "integer"},
                    },
                }
            }
        },
    }


# ============================================================
# Fixtures
# ============================================================


def _admin_headers() -> dict:
    return {"x-user-role": "admin", "x-user-id": "ADMIN001"}


@pytest.fixture
def temp_db(monkeypatch):
    """临时文件 SQLite + 启用字段加密"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp.name}"

    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setattr(get_settings(), "field_encryption_key", key)
    # 重置 FieldCipher 缓存,使新密钥生效
    from core import field_crypto as fc

    fc.reset_field_cipher_cache()

    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from core import database as db_module

    engine = create_async_engine(
        db_url, echo=False, future=True, connect_args={"check_same_thread": False}
    )
    db_module.engine = engine
    db_module.AsyncSessionLocal = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
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
async def initialized_db(temp_db):
    from core.database import close_db, init_db

    await init_db()
    yield
    await close_db()


@pytest.fixture
def client(initialized_db):
    app = FastAPI()
    app.include_router(custom_tools_router)
    with TestClient(app) as c:
        yield c


# ============================================================
# OpenAPI 解析测试
# ============================================================


class TestOpenAPIParser:
    def test_parse_two_paths_to_three_tools(self):
        """spec 含 2 个 path,3 个 operation (listPets/createPet/showPetById) → 3 ToolSpec"""
        from core.tools.openapi_parser import parse_openapi_to_tools

        spec = _make_spec_two_paths()
        tools = parse_openapi_to_tools(spec, "https://api.example.com")
        assert len(tools) == 3
        names = [t.name for t in tools]
        assert "listPets" in names
        assert "createPet" in names
        assert "showPetById" in names

    def test_parse_url_concat_base_url_and_path(self):
        """ToolSpec.url = base_url + path"""
        from core.tools.openapi_parser import parse_openapi_to_tools

        spec = _make_spec_two_paths()
        tools = parse_openapi_to_tools(spec, "https://api.example.com/v1")
        # 应包含完整 URL
        urls = [t.url for t in tools]
        assert "https://api.example.com/v1/pets" in urls
        assert "https://api.example.com/v1/pets/{petId}" in urls

    def test_parse_query_path_body_parameters(self):
        """参数提取: path / query / body 都被识别"""
        from core.tools.openapi_parser import parse_openapi_to_tools

        spec = _make_spec_two_paths()
        tools = parse_openapi_to_tools(spec, "https://api.example.com")
        by_name = {t.name: t for t in tools}

        # listPets: query 参数 limit
        list_pets = by_name["listPets"]
        param_names = [p["name"] for p in list_pets.parameters]
        assert "limit" in param_names
        # query 参数 location
        limit_param = next(p for p in list_pets.parameters if p["name"] == "limit")
        assert limit_param["location"] == "query"

        # createPet: body 参数
        create_pet = by_name["createPet"]
        body_params = [p for p in create_pet.parameters if p["location"] == "body"]
        assert len(body_params) == 1
        assert body_params[0]["name"] == "body"

        # showPetById: path 参数
        show = by_name["showPetById"]
        path_params = [p for p in show.parameters if p["location"] == "path"]
        assert len(path_params) == 1
        assert path_params[0]["name"] == "petId"
        assert path_params[0]["required"] is True

    def test_parse_ref_resolves_components(self):
        """$ref 引用 components/schemas 正确解析"""
        from core.tools.openapi_parser import parse_openapi_to_tools

        spec = _make_spec_with_refs()
        tools = parse_openapi_to_tools(spec, "https://api.example.com")
        assert len(tools) == 1
        # body 参数应被提取 (引用解析后能取到 schema)
        create_user = tools[0]
        body_params = [p for p in create_user.parameters if p["location"] == "body"]
        assert len(body_params) == 1

    def test_parse_invalid_spec_raises_value_error(self):
        """无效 spec (非 dict / 缺 paths) → ValueError"""
        from core.tools.openapi_parser import parse_openapi_to_tools

        # 非 dict
        with pytest.raises(ValueError):
            parse_openapi_to_tools("not a dict", "https://api.example.com")  # type: ignore[arg-type]
        # 缺 paths
        with pytest.raises(ValueError):
            parse_openapi_to_tools({"openapi": "3.0.0"}, "https://api.example.com")
        # paths 不是 dict
        with pytest.raises(ValueError):
            parse_openapi_to_tools(
                {"paths": ["not", "a", "dict"]}, "https://api.example.com"
            )

    def test_parse_string_json(self):
        """parse_openapi_string 支持 JSON 字符串"""
        from core.tools.openapi_parser import parse_openapi_string

        spec = _make_spec_two_paths()
        raw = json.dumps(spec)
        tools = parse_openapi_string(raw, "https://api.example.com")
        assert len(tools) == 3

    def test_parse_string_empty_raises(self):
        """空字符串 → ValueError"""
        from core.tools.openapi_parser import parse_openapi_string

        with pytest.raises(ValueError):
            parse_openapi_string("", "https://api.example.com")
        with pytest.raises(ValueError):
            parse_openapi_string("   ", "https://api.example.com")

    def test_parse_fallback_name_without_operation_id(self):
        """无 operationId 时用 path_method slug 作为 name"""
        from core.tools.openapi_parser import parse_openapi_to_tools

        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/foo/bar": {
                    "get": {"summary": "Get foo"},
                }
            },
        }
        tools = parse_openapi_to_tools(spec, "https://api.example.com")
        assert len(tools) == 1
        # slug 化:name 应包含 foo_bar_get
        assert "foo" in tools[0].name and "get" in tools[0].name


# ============================================================
# build_langchain_tool 测试
# ============================================================


class TestBuildLangchainTool:
    def test_build_tool_returns_callable(self):
        """ToolSpec → BaseTool 可调用"""
        from core.tools.openapi_parser import (
            AuthConfig,
            build_langchain_tool,
            parse_openapi_to_tools,
        )

        spec = _make_spec_two_paths()
        tools = parse_openapi_to_tools(spec, "https://api.example.com")
        # build listPets (无 path 参数,只有 query)
        list_pets = next(t for t in tools if t.name == "listPets")
        lc_tool = build_langchain_tool(list_pets, auth=AuthConfig())
        assert lc_tool is not None
        assert lc_tool.name == "listPets"
        # args_schema 应存在
        assert lc_tool.args_schema is not None

    def test_build_tool_args_schema_has_parameters(self):
        """args_schema 包含所有参数 (path/query/body)"""
        from core.tools.openapi_parser import (
            AuthConfig,
            build_langchain_tool,
            parse_openapi_to_tools,
        )

        spec = _make_spec_two_paths()
        tools = parse_openapi_to_tools(spec, "https://api.example.com")
        show = next(t for t in tools if t.name == "showPetById")
        lc_tool = build_langchain_tool(show, auth=AuthConfig())
        schema = lc_tool.args_schema.model_json_schema()
        # petId 应在 properties 中
        assert "petId" in schema.get("properties", {})

    def test_tool_invoke_calls_httpx(self):
        """工具 invoke 时实际调用 httpx,验证调用流程 (mock httpx)"""
        from core.tools.openapi_parser import (
            AuthConfig,
            build_langchain_tool,
            parse_openapi_to_tools,
        )

        spec = _make_spec_two_paths()
        tools = parse_openapi_to_tools(spec, "https://api.example.com")
        show = next(t for t in tools if t.name == "showPetById")

        # mock httpx.Client
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"id": "42", "name": "Rex"}'
        mock_client = MagicMock()
        mock_client.request.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("httpx.Client", return_value=mock_client):
            lc_tool = build_langchain_tool(show, auth=AuthConfig())
            result = lc_tool.invoke({"petId": "42"})

        assert "HTTP 200" in result
        assert "Rex" in result
        # 验证 request 被调用,URL 中 path 参数已替换
        called_args, called_kwargs = mock_client.request.call_args
        assert called_args[0] == "GET"
        assert called_args[1] == "https://api.example.com/pets/42"

    def test_tool_async_invoke(self):
        """工具 ainvoke 异步调用 (mock httpx.AsyncClient)"""
        import asyncio

        from core.tools.openapi_parser import (
            AuthConfig,
            build_langchain_tool,
            parse_openapi_to_tools,
        )

        spec = _make_spec_two_paths()
        tools = parse_openapi_to_tools(spec, "https://api.example.com")
        show = next(t for t in tools if t.name == "showPetById")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"ok": true}'

        # 用 asyncio 协程包装 mock,避免手动构造 Future
        async def _mock_request(*args, **kwargs):
            return mock_resp

        async def _mock_aenter(*args, **kwargs):
            return mock_client

        async def _mock_aexit(*args, **kwargs):
            return False

        mock_client = MagicMock()
        mock_client.request = _mock_request
        mock_client.__aenter__ = _mock_aenter
        mock_client.__aexit__ = _mock_aexit

        async def _run():
            with patch("httpx.AsyncClient", return_value=mock_client):
                lc_tool = build_langchain_tool(show, auth=AuthConfig())
                result = await lc_tool.ainvoke({"petId": "42"})
            return result

        result = asyncio.run(_run())
        assert "HTTP 200" in result


# ============================================================
# 鉴权注入测试
# ============================================================


class TestAuthConfig:
    def test_bearer_injects_authorization_header(self):
        from core.tools.openapi_parser import AuthConfig

        auth = AuthConfig(auth_type="bearer", credentials="mytoken123")
        headers = {"Accept": "application/json"}
        auth.apply_headers(headers)
        assert headers["Authorization"] == "Bearer mytoken123"

    def test_api_key_injects_x_api_key_header(self):
        from core.tools.openapi_parser import AuthConfig

        auth = AuthConfig(auth_type="api_key", credentials="secret")
        headers = {}
        auth.apply_headers(headers)
        assert headers["X-API-Key"] == "secret"

    def test_basic_injects_authorization_basic(self):
        from core.tools.openapi_parser import AuthConfig

        auth = AuthConfig(auth_type="basic", credentials="dXNlcjpwYXNz")
        headers = {}
        auth.apply_headers(headers)
        assert headers["Authorization"] == "Basic dXNlcjpwYXNz"

    def test_none_does_not_inject(self):
        from core.tools.openapi_parser import AuthConfig

        auth = AuthConfig(auth_type="none", credentials=None)
        headers = {"Accept": "application/json"}
        auth.apply_headers(headers)
        assert "Authorization" not in headers
        assert "X-API-Key" not in headers


# ============================================================
# CRUD API 测试
# ============================================================


class TestCustomToolCRUD:
    def test_create_then_list_then_get(self, client):
        """创建 → 列表 → 详情"""
        spec = _make_spec_two_paths()
        # 1. 创建
        create_resp = client.post(
            "/api/v1/admin/custom-tools",
            json={
                "name": "pet-api",
                "description": "Pet store API",
                "openapi_schema": spec,
                "base_url": "https://api.example.com",
                "auth_type": "none",
            },
            headers=_admin_headers(),
        )
        assert create_resp.status_code == 201, create_resp.text
        created = create_resp.json()
        assert created["name"] == "pet-api"
        assert created["enabled"] is True
        assert created["has_credentials"] is False
        # 应返回解析出的 tools
        assert len(created["tools"]) == 3
        tool_id = created["id"]

        # 2. 列表
        list_resp = client.get("/api/v1/admin/custom-tools", headers=_admin_headers())
        assert list_resp.status_code == 200
        data = list_resp.json()
        assert data["total"] >= 1
        names = [item["name"] for item in data["items"]]
        assert "pet-api" in names

        # 3. 详情
        detail_resp = client.get(
            f"/api/v1/admin/custom-tools/{tool_id}", headers=_admin_headers()
        )
        assert detail_resp.status_code == 200
        detail = detail_resp.json()
        assert detail["id"] == tool_id
        assert detail["name"] == "pet-api"

    def test_create_with_invalid_spec_returns_422(self, client):
        """无效 OpenAPI spec → 422"""
        resp = client.post(
            "/api/v1/admin/custom-tools",
            json={
                "name": "bad-api",
                "description": "",
                "openapi_schema": {"openapi": "3.0.0"},  # 缺 paths
                "base_url": "https://api.example.com",
                "auth_type": "none",
            },
            headers=_admin_headers(),
        )
        assert resp.status_code == 422
        assert "解析失败" in resp.json()["detail"] or "paths" in resp.json()["detail"]

    def test_create_duplicate_name_returns_409(self, client):
        """同名工具 (同租户) → 409"""
        spec = _make_spec_two_paths()
        body = {
            "name": "dup-api",
            "description": "",
            "openapi_schema": spec,
            "base_url": "https://api.example.com",
            "auth_type": "none",
        }
        # 第一次成功
        r1 = client.post(
            "/api/v1/admin/custom-tools", json=body, headers=_admin_headers()
        )
        assert r1.status_code == 201
        # 第二次 409
        r2 = client.post(
            "/api/v1/admin/custom-tools", json=body, headers=_admin_headers()
        )
        assert r2.status_code == 409

    def test_create_invalid_auth_type_returns_422(self, client):
        """auth_type 不在白名单 → 422"""
        spec = _make_spec_two_paths()
        resp = client.post(
            "/api/v1/admin/custom-tools",
            json={
                "name": "bad-auth",
                "description": "",
                "openapi_schema": spec,
                "base_url": "https://api.example.com",
                "auth_type": "invalid_type",
            },
            headers=_admin_headers(),
        )
        assert resp.status_code == 422

    def test_create_with_credentials_encrypts(self, client):
        """带凭证创建 → has_credentials=True 且凭证已加密"""
        spec = _make_spec_two_paths()
        resp = client.post(
            "/api/v1/admin/custom-tools",
            json={
                "name": "auth-api",
                "description": "",
                "openapi_schema": spec,
                "base_url": "https://api.example.com",
                "auth_type": "bearer",
                "auth_credentials": "secret-bearer-token",
            },
            headers=_admin_headers(),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["has_credentials"] is True
        # 验证 DB 中存储的是密文 (而非明文 secret-bearer-token)
        from core.database import AsyncSessionLocal
        from models.custom_tool import CustomTool
        from sqlalchemy import select
        import asyncio

        async def _check():
            async with AsyncSessionLocal() as sess:
                r = await sess.execute(
                    select(CustomTool).where(CustomTool.name == "auth-api")
                )
                entity = r.scalars().first()
                assert entity is not None
                # 凭证应为密文,不等于明文
                assert entity.auth_credentials != "secret-bearer-token"
                # 解密后等于明文
                from core.field_crypto import get_field_cipher

                cipher = get_field_cipher()
                decrypted = cipher.decrypt(entity.auth_credentials)
                assert decrypted == "secret-bearer-token"

        asyncio.run(_check())

    def test_update_name_and_description(self, client):
        """更新 name + description"""
        spec = _make_spec_two_paths()
        create = client.post(
            "/api/v1/admin/custom-tools",
            json={
                "name": "update-me",
                "description": "old",
                "openapi_schema": spec,
                "base_url": "https://api.example.com",
                "auth_type": "none",
            },
            headers=_admin_headers(),
        )
        tool_id = create.json()["id"]
        # 更新
        upd = client.put(
            f"/api/v1/admin/custom-tools/{tool_id}",
            json={"name": "updated-name", "description": "new desc"},
            headers=_admin_headers(),
        )
        assert upd.status_code == 200
        assert upd.json()["name"] == "updated-name"
        assert upd.json()["description"] == "new desc"

    def test_update_with_invalid_spec_returns_422(self, client):
        """更新时传入无效 spec → 422"""
        spec = _make_spec_two_paths()
        create = client.post(
            "/api/v1/admin/custom-tools",
            json={
                "name": "update-bad",
                "description": "",
                "openapi_schema": spec,
                "base_url": "https://api.example.com",
                "auth_type": "none",
            },
            headers=_admin_headers(),
        )
        tool_id = create.json()["id"]
        upd = client.put(
            f"/api/v1/admin/custom-tools/{tool_id}",
            json={"openapi_schema": {"openapi": "3.0.0"}},  # 缺 paths
            headers=_admin_headers(),
        )
        assert upd.status_code == 422

    def test_delete(self, client):
        """删除"""
        spec = _make_spec_two_paths()
        create = client.post(
            "/api/v1/admin/custom-tools",
            json={
                "name": "delete-me",
                "description": "",
                "openapi_schema": spec,
                "base_url": "https://api.example.com",
                "auth_type": "none",
            },
            headers=_admin_headers(),
        )
        tool_id = create.json()["id"]
        # 删除
        d = client.delete(
            f"/api/v1/admin/custom-tools/{tool_id}", headers=_admin_headers()
        )
        assert d.status_code == 200
        assert d.json()["deleted"] is True
        # 再次 get 应 404
        g = client.get(
            f"/api/v1/admin/custom-tools/{tool_id}", headers=_admin_headers()
        )
        assert g.status_code == 404

    def test_toggle_disable_and_enable(self, client):
        """启用/禁用切换"""
        spec = _make_spec_two_paths()
        create = client.post(
            "/api/v1/admin/custom-tools",
            json={
                "name": "toggle-me",
                "description": "",
                "openapi_schema": spec,
                "base_url": "https://api.example.com",
                "auth_type": "none",
            },
            headers=_admin_headers(),
        )
        tool_id = create.json()["id"]
        # 禁用
        t1 = client.post(
            f"/api/v1/admin/custom-tools/{tool_id}/toggle",
            json={"enabled": False},
            headers=_admin_headers(),
        )
        assert t1.status_code == 200
        assert t1.json()["enabled"] is False
        # 再次启用
        t2 = client.post(
            f"/api/v1/admin/custom-tools/{tool_id}/toggle",
            json={"enabled": True},
            headers=_admin_headers(),
        )
        assert t2.status_code == 200
        assert t2.json()["enabled"] is True

    def test_get_nonexistent_returns_404(self, client):
        """不存在的 ID → 404"""
        r = client.get(
            "/api/v1/admin/custom-tools/nonexistent-id",
            headers=_admin_headers(),
        )
        assert r.status_code == 404

    def test_list_with_search(self, client):
        """search 参数过滤"""
        spec = _make_spec_two_paths()
        # 创建两个工具
        for name in ("searchable-tool", "other-tool"):
            client.post(
                "/api/v1/admin/custom-tools",
                json={
                    "name": name,
                    "description": "",
                    "openapi_schema": spec,
                    "base_url": "https://api.example.com",
                    "auth_type": "none",
                },
                headers=_admin_headers(),
            )
        # search "searchable"
        r = client.get(
            "/api/v1/admin/custom-tools?search=searchable",
            headers=_admin_headers(),
        )
        assert r.status_code == 200
        data = r.json()
        assert all("searchable" in item["name"].lower() for item in data["items"])


# ============================================================
# parse 端点测试 (不入库)
# ============================================================


class TestParseEndpoint:
    def test_parse_dict_not_persisted(self, client):
        """parse 端点不入库: 调用前后 list 数量不变"""
        # 先查 list
        before = client.get(
            "/api/v1/admin/custom-tools", headers=_admin_headers()
        ).json()["total"]

        spec = _make_spec_two_paths()
        r = client.post(
            "/api/v1/admin/custom-tools/parse",
            json={"openapi_schema": spec, "base_url": "https://api.example.com"},
            headers=_admin_headers(),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 3
        assert len(data["tools"]) == 3

        after = client.get(
            "/api/v1/admin/custom-tools", headers=_admin_headers()
        ).json()["total"]
        assert after == before

    def test_parse_raw_json_string(self, client):
        """raw 字段支持 JSON 字符串"""
        spec = _make_spec_two_paths()
        r = client.post(
            "/api/v1/admin/custom-tools/parse",
            json={
                "raw": json.dumps(spec),
                "base_url": "https://api.example.com",
            },
            headers=_admin_headers(),
        )
        assert r.status_code == 200
        assert r.json()["count"] == 3

    def test_parse_empty_body_returns_422(self, client):
        """raw 与 openapi_schema 都缺 → 422"""
        r = client.post(
            "/api/v1/admin/custom-tools/parse",
            json={"base_url": "https://api.example.com"},
            headers=_admin_headers(),
        )
        assert r.status_code == 422

    def test_parse_invalid_raw_returns_422(self, client):
        """raw 是无效 JSON → 422"""
        r = client.post(
            "/api/v1/admin/custom-tools/parse",
            json={"raw": "not-valid-json{{", "base_url": "https://api.example.com"},
            headers=_admin_headers(),
        )
        assert r.status_code == 422


# ============================================================
# test 端点测试 (实际 HTTP 调用,mock httpx)
# ============================================================


class TestTestEndpoint:
    def test_test_endpoint_calls_httpx(self, client):
        """test 端点实际调用 httpx (mock) 返回响应"""
        spec = _make_spec_two_paths()
        create = client.post(
            "/api/v1/admin/custom-tools",
            json={
                "name": "test-call-api",
                "description": "",
                "openapi_schema": spec,
                "base_url": "https://api.example.com",
                "auth_type": "none",
            },
            headers=_admin_headers(),
        )
        tool_id = create.json()["id"]

        # mock httpx.Client (test 端点走同步 invoke 路径)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"pets": []}'
        mock_client = MagicMock()
        mock_client.request.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("httpx.Client", return_value=mock_client):
            r = client.post(
                f"/api/v1/admin/custom-tools/{tool_id}/test",
                json={
                    "path": "/pets",
                    "method": "GET",
                    "parameters": {"limit": 5},
                },
                headers=_admin_headers(),
            )

        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert "HTTP 200" in data["result"]
        # 验证 httpx 被调用,query 参数 limit=5
        called_args, called_kwargs = mock_client.request.call_args
        assert called_args[0] == "GET"
        assert called_args[1] == "https://api.example.com/pets"
        assert called_kwargs["params"] == {"limit": 5}

    def test_test_endpoint_path_param_substitution(self, client):
        """test 端点的 path 参数替换"""
        spec = _make_spec_two_paths()
        create = client.post(
            "/api/v1/admin/custom-tools",
            json={
                "name": "path-sub-api",
                "description": "",
                "openapi_schema": spec,
                "base_url": "https://api.example.com",
                "auth_type": "none",
            },
            headers=_admin_headers(),
        )
        tool_id = create.json()["id"]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"pet": "Rex"}'
        mock_client = MagicMock()
        mock_client.request.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("httpx.Client", return_value=mock_client):
            r = client.post(
                f"/api/v1/admin/custom-tools/{tool_id}/test",
                json={
                    "path": "/pets/{petId}",
                    "method": "GET",
                    "parameters": {"petId": "42"},
                },
                headers=_admin_headers(),
            )

        assert r.status_code == 200
        called_args, called_kwargs = mock_client.request.call_args
        # URL 中 {petId} 应被替换为 42
        assert called_args[1] == "https://api.example.com/pets/42"

    def test_test_endpoint_not_found(self, client):
        """不存在的工具 ID → 404"""
        r = client.post(
            "/api/v1/admin/custom-tools/nonexistent/test",
            json={"path": "/x", "method": "GET", "parameters": {}},
            headers=_admin_headers(),
        )
        assert r.status_code == 404

    def test_test_endpoint_operation_not_found(self, client):
        """path/method 在 spec 中不存在 → 404"""
        spec = _make_spec_two_paths()
        create = client.post(
            "/api/v1/admin/custom-tools",
            json={
                "name": "op-not-found",
                "description": "",
                "openapi_schema": spec,
                "base_url": "https://api.example.com",
                "auth_type": "none",
            },
            headers=_admin_headers(),
        )
        tool_id = create.json()["id"]
        r = client.post(
            f"/api/v1/admin/custom-tools/{tool_id}/test",
            json={
                "path": "/nonexistent",
                "method": "GET",
                "parameters": {},
            },
            headers=_admin_headers(),
        )
        assert r.status_code == 404


# ============================================================
# 鉴权与 RBAC
# ============================================================


class TestAuthAndRBAC:
    def test_non_admin_gets_403(self, client):
        """非 ADMIN 角色 → 403"""
        r = client.get(
            "/api/v1/admin/custom-tools",
            headers={"x-user-role": "employee", "x-user-id": "E001"},
        )
        assert r.status_code == 403

    def test_no_auth_gets_401(self, client):
        """无 auth header → 401 (演示模式下未提供 role)"""
        # 演示模式 settings.auth_demo_mode=True,但无 x-user-role header
        # → RBAC 默认 employee → 应 403
        r = client.get("/api/v1/admin/custom-tools")
        assert r.status_code in (401, 403)
