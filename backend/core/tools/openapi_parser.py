"""OpenAPI Schema 解析与 LangChain Tool 构建 (P3-1: 自定义工具上传)

对标 Dify Custom Tool:
- 用户粘贴 OpenAPI JSON/YAML → 解析 paths → 每个 operation 生成一个 Tool
- 工具执行时用 httpx 调用对应 HTTP endpoint
- 参数从 OpenAPI parameters / requestBody schema 自动生成 Pydantic schema
- 凭证通过 FieldCipher 解密后注入 Authorization / X-API-Key header

核心数据结构:
- ToolSpec: 一个 OpenAPI operation 的工具化表示 (name/description/method/url/parameters)
- AuthConfig: 鉴权配置 (auth_type + credentials 明文)

容错策略:
- parse_openapi_to_tools 对无效 spec 抛 ValueError,API 层捕获后返回 422
- build_langchain_tool 对 schema 异常时降级为无参数工具 (不阻断 import)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Type

from pydantic import BaseModel, Field, create_model

logger = logging.getLogger(__name__)

# 支持的 HTTP method (OpenAPI 中 path 下的方法名)
_SUPPORTED_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")

# OpenAPI type → Python type 映射
_OAS_TYPE_PY = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": List[Any],
    "object": Dict[str, Any],
}


@dataclass
class ToolSpec:
    """一个 OpenAPI operation 的工具化表示 (供 API 层返回 / DB 存储)

    Attributes:
        name: 工具名 (operationId 优先,无则 path_method slug 化)
        description: 工具描述 (summary 优先,回退 description)
        method: HTTP 方法 (get/post/put/...)
        url: 完整 URL (base_url + path,含 path 参数占位)
        path: OpenAPI path 模板 (如 /users/{id})
        parameters: 参数 schema 列表 (query/path/header/body 合并)
        operation_id: 原始 operationId (可空)
        summary: 原始 summary (可空)
    """

    name: str
    description: str
    method: str
    url: str
    path: str
    parameters: List[Dict[str, Any]] = field(default_factory=list)
    operation_id: Optional[str] = None
    summary: Optional[str] = None


@dataclass
class AuthConfig:
    """鉴权配置 (运行时使用,credentials 为解密后明文)"""

    auth_type: str = "none"  # none / bearer / api_key / basic
    credentials: Optional[str] = None  # 凭证明文

    def apply_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        """把鉴权信息注入 HTTP header。

        - bearer:  Authorization: Bearer <credentials>
        - api_key: X-API-Key: <credentials>
        - basic:   Authorization: Basic <credentials> (credentials 视作已 base64 编码)
        - none:    不注入
        """
        if not self.credentials:
            return headers
        if self.auth_type == "bearer":
            headers["Authorization"] = f"Bearer {self.credentials}"
        elif self.auth_type == "api_key":
            headers["X-API-Key"] = self.credentials
        elif self.auth_type == "basic":
            headers["Authorization"] = f"Basic {self.credentials}"
        return headers


# ============================================================
# OpenAPI 解析
# ============================================================


def _slugify(text: str) -> str:
    """把 path+method 转为合法 Python 标识符 (作为工具名 fallback)。

    /users/{id} GET → users_id_get
    """
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", text)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_").lower()
    return cleaned or "tool"


def _resolve_ref(spec: dict, ref: str) -> dict:
    """解析 $ref (仅本地引用,$ref: '#/components/schemas/Foo')

    ref 形如 '#/components/schemas/Pet',按 '/' 切分逐层下钻。
    解析失败返回空 dict (容错)。
    """
    if not ref.startswith("#/"):
        return {}
    parts = ref.lstrip("#/").split("/")
    node: Any = spec
    for p in parts:
        if not isinstance(node, dict):
            return {}
        node = node.get(p, {})
        if node is None:
            return {}
    return node if isinstance(node, dict) else {}


def _resolve_schema(spec: dict, schema: dict) -> dict:
    """递归解析 schema 内的 $ref (浅层,避免无限递归)"""
    if not isinstance(schema, dict):
        return {}
    if "$ref" in schema:
        return _resolve_ref(spec, schema["$ref"])
    return schema


def _oas_type_to_py(oas_type: Optional[str]) -> type:
    """OpenAPI type → Python type 映射,未知类型回退 str"""
    if not oas_type:
        return str
    return _OAS_TYPE_PY.get(oas_type, str)


def _extract_parameters(
    spec: dict, operation: dict, path: str
) -> List[Dict[str, Any]]:
    """提取 operation 的所有参数 (path/query/header/cookie)

    每个参数返回:
    - name: 参数名
    - location: in (path/query/header/cookie)
    - required: bool
    - type: OAS type 字符串 (如 "string"/"integer"/"object")
    - description: str
    - default: 默认值 (None 表示无默认)

    注意: 用 type 字符串 (而非 Python type) 保证 API 响应可 JSON 序列化,
    build_langchain_tool 时通过 _oas_type_to_py 映射回 Python 类型。
    """
    # path-level parameters + operation-level parameters (operation 覆盖 path)
    path_item = spec.get("paths", {}).get(path, {}) or {}
    path_params = path_item.get("parameters", []) or []
    op_params = operation.get("parameters", []) or []
    # 按 (in, name) 去重,operation 优先
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for p in path_params + op_params:
        p = _resolve_schema(spec, p)
        if not p or "name" not in p:
            continue
        key = (p.get("in", "query"), p["name"])
        merged[key] = p

    result: List[Dict[str, Any]] = []
    for p in merged.values():
        schema = _resolve_schema(spec, p.get("schema", {}))
        oas_type = schema.get("type", "string")
        result.append(
            {
                "name": p["name"],
                "location": p.get("in", "query"),
                "required": bool(p.get("required", False)),
                "type": oas_type,
                "description": p.get("description", "") or "",
                "default": p.get("default", None),
            }
        )
    return result


def _extract_body_parameter(
    spec: dict, operation: dict
) -> Optional[Dict[str, Any]]:
    """提取 requestBody 第一个 JSON body 作为单个 'body' 参数

    OpenAPI requestBody 在 OpenAPI 3.x 是 content.<media-type>.schema。
    简化处理: 取 application/json 的 schema,作为 body 参数 (dict 类型)。
    """
    body = operation.get("requestBody")
    if not body:
        return None
    body = _resolve_schema(spec, body)
    content = body.get("content", {})
    json_media = content.get("application/json") or {}
    schema = _resolve_schema(spec, json_media.get("schema", {}))
    required = bool(body.get("required", False))
    return {
        "name": "body",
        "location": "body",
        "required": required,
        "type": schema.get("type", "object"),
        "description": body.get("description", "") or "Request body (JSON)",
        "default": None,
        "schema": schema,  # 保留原始 schema 供 LLM 理解结构
    }


def parse_openapi_to_tools(
    spec: dict, base_url: str, auth: Optional[AuthConfig] = None
) -> List[ToolSpec]:
    """解析 OpenAPI 3.x spec 为 ToolSpec 列表

    Args:
        spec: OpenAPI spec dict (已 parse,JSON 或 YAML 都已转 dict)
        base_url: API base URL (如 https://api.example.com/v1)
        auth: 鉴权配置 (仅用于记录在 ToolSpec 中,实际注入由 build_langchain_tool 处理)

    Returns:
        ToolSpec 列表 (每个 path+method 一个)

    Raises:
        ValueError: spec 不是 dict / 缺 paths / paths 不是 dict
    """
    if not isinstance(spec, dict):
        raise ValueError("OpenAPI spec 必须是 dict (JSON 对象)")
    if "openapi" not in spec and "swagger" not in spec:
        # 容错: 不强求 openapi 字段,只要有 paths 即可解析
        logger.debug("OpenAPI spec 缺少 openapi/swagger 版本字段,继续解析")
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        raise ValueError("OpenAPI spec 缺少 paths 字段或 paths 不是对象")

    base = (base_url or "").rstrip("/")
    tool_specs: List[ToolSpec] = []
    seen_names: Dict[str, int] = {}

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method in _SUPPORTED_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue

            # 工具名: operationId 优先,否则 slug(path_method)
            op_id = operation.get("operationId")
            if op_id:
                name = op_id
            else:
                name = _slugify(f"{path}_{method}")

            # 重名时附加序号保证唯一 (Dify 同样行为)
            if name in seen_names:
                seen_names[name] += 1
                name = f"{name}_{seen_names[name]}"
            else:
                seen_names[name] = 0

            # 描述: summary 优先,回退 description
            summary = operation.get("summary")
            description = (
                summary or operation.get("description") or f"{method.upper()} {path}"
            )

            # 参数: path/query/header + body
            params = _extract_parameters(spec, operation, path)
            body_param = _extract_body_parameter(spec, operation)
            if body_param:
                params.append(body_param)

            tool_specs.append(
                ToolSpec(
                    name=name,
                    description=description,
                    method=method.upper(),
                    url=f"{base}{path}",
                    path=path,
                    parameters=params,
                    operation_id=op_id,
                    summary=summary,
                )
            )

    return tool_specs


# ============================================================
# LangChain BaseTool 构建
# ============================================================


def _build_pydantic_schema(tool_spec: ToolSpec) -> Type[BaseModel]:
    """根据 ToolSpec.parameters 构建 Pydantic schema 模型

    参数命名规则:
    - path/query/header 参数: 直接用参数名 (path 参数会自动从 kwargs 取出替换 URL 占位符)
    - body 参数: 用 'body' 作为字段名,类型为 dict

    Pydantic v2 create_model 用法:
    create_model('Model', field=(type, Field(default=..., description=...)))
    """
    fields: Dict[str, Any] = {}
    for p in tool_spec.parameters:
        name = p["name"]
        # type 是 OAS 类型字符串 (string/integer/number/boolean/array/object)
        py_type = _oas_type_to_py(p.get("type"))
        required = p["required"]
        desc = p.get("description", "")
        default = p.get("default", None)
        if required:
            fields[name] = (py_type, Field(..., description=desc))
        else:
            # 有默认值用默认值,无默认值 None
            fields[name] = (
                Optional[py_type],
                Field(default=default, description=desc),
            )
    if not fields:
        # 无参数工具: 空 schema (Pydantic 要求至少能 model_validate {})
        return create_model("EmptyArgs")

    # 模型名需为合法 Python 标识符
    model_name = (
        "".join(c if c.isalnum() else "_" for c in tool_spec.name) or "ToolArgs"
    )
    return create_model(model_name, **fields)


def _render_url_with_path_params(url: str, kwargs: Dict[str, Any]) -> str:
    """替换 URL 中的 {path_param} 占位符为 kwargs 中对应值,并移除该 key

    OpenAPI path: /users/{id} → URL: https://api.example.com/users/{id}
    调用时 kwargs = {"id": 42, "q": "foo"} → url = https://api.example.com/users/42
    """
    rendered = url
    for key in list(kwargs.keys()):
        placeholder = "{" + key + "}"
        if placeholder in rendered:
            rendered = rendered.replace(placeholder, str(kwargs.pop(key)))
    return rendered


def build_langchain_tool(
    tool_spec: ToolSpec, auth: Optional[AuthConfig] = None
) -> Any:
    """把 ToolSpec 包装为 LangChain BaseTool (执行时用 httpx 调 HTTP endpoint)

    Args:
        tool_spec: ToolSpec 实例
        auth: 鉴权配置 (credentials 为解密后明文)

    Returns:
        LangChain BaseTool 实例 (可 invoke / ainvoke)
        LangChain 未安装时抛 ImportError

    工具执行流程:
    1. Pydantic schema 校验输入参数
    2. 取出 path 参数填充 URL
    3. query 参数拼到 query string (GET) 或合并到 body (POST/PUT/PATCH)
    4. body 参数作为 JSON body 发送
    5. 注入鉴权 header
    6. httpx 调用 endpoint,返回响应文本
    """
    from langchain_core.tools import StructuredTool

    args_schema = _build_pydantic_schema(tool_spec)
    method = tool_spec.method
    url = tool_spec.url
    auth_cfg = auth or AuthConfig()
    # location → 参数名映射 (运行时分离 path/query/header/body)
    param_locations: Dict[str, str] = {p["name"]: p["location"] for p in tool_spec.parameters}

    def _do_call(**kwargs: Any) -> str:
        import httpx

        rendered_url = _render_url_with_path_params(url, kwargs)
        headers: Dict[str, str] = {"Accept": "application/json"}
        auth_cfg.apply_headers(headers)
        params: Dict[str, Any] = {}
        json_body: Optional[Dict[str, Any]] = None

        for name, location in param_locations.items():
            if name not in kwargs:
                continue
            value = kwargs[name]
            if value is None:
                continue
            if location == "path":
                continue  # 已替换到 URL
            elif location == "query":
                params[name] = value
            elif location == "header":
                headers[name] = str(value)
            elif location == "body":
                json_body = value if isinstance(value, dict) else {"data": value}

        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.request(
                    method,
                    rendered_url,
                    params=params or None,
                    json=json_body if method not in ("GET", "HEAD") else None,
                    headers=headers,
                )
            return _format_httpx_response(resp)
        except Exception as e:
            logger.warning("自定义工具 %s 调用失败: %s", tool_spec.name, e)
            return f"工具调用失败: {e}"

    async def _do_acall(**kwargs: Any) -> str:
        import httpx

        rendered_url = _render_url_with_path_params(url, kwargs)
        headers: Dict[str, str] = {"Accept": "application/json"}
        auth_cfg.apply_headers(headers)
        params: Dict[str, Any] = {}
        json_body: Optional[Dict[str, Any]] = None

        for name, location in param_locations.items():
            if name not in kwargs:
                continue
            value = kwargs[name]
            if value is None:
                continue
            if location == "path":
                continue
            elif location == "query":
                params[name] = value
            elif location == "header":
                headers[name] = str(value)
            elif location == "body":
                json_body = value if isinstance(value, dict) else {"data": value}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.request(
                    method,
                    rendered_url,
                    params=params or None,
                    json=json_body if method not in ("GET", "HEAD") else None,
                    headers=headers,
                )
            return _format_httpx_response(resp)
        except Exception as e:
            logger.warning("自定义工具 %s 异步调用失败: %s", tool_spec.name, e)
            return f"工具调用失败: {e}"

    return StructuredTool.from_function(
        func=_do_call,
        coroutine=_do_acall,
        name=tool_spec.name,
        description=tool_spec.description,
        args_schema=args_schema,
    )


def _format_httpx_response(resp: Any) -> str:
    """格式化 httpx 响应为字符串 (供 LLM 阅读)"""
    body = resp.text
    if len(body) > 4000:
        body = body[:4000] + f"\n...[truncated, total {len(body)} chars]"
    return f"HTTP {resp.status_code}\n{body}"


# ============================================================
# 便捷入口
# ============================================================


def parse_openapi_string(
    raw: str, base_url: str, auth: Optional[AuthConfig] = None
) -> List[ToolSpec]:
    """解析 OpenAPI JSON 或 YAML 字符串为 ToolSpec 列表

    Args:
        raw: OpenAPI spec 原文 (JSON 或 YAML)
        base_url: API base URL
        auth: 鉴权配置

    Returns:
        ToolSpec 列表

    Raises:
        ValueError: 解析失败 (JSON/YAML 语法错误或不是对象)
    """
    if not raw or not raw.strip():
        raise ValueError("OpenAPI spec 为空")

    # 先尝试 JSON (无需第三方依赖)
    spec: Any
    try:
        spec = json.loads(raw)
        if not isinstance(spec, dict):
            raise ValueError("OpenAPI spec 解析为 JSON 但不是对象")
    except (json.JSONDecodeError, ValueError) as json_err:
        # JSON 失败时尝试 YAML (PyYAML 可选)
        try:
            import yaml  # type: ignore[import]
        except ImportError as e:
            raise ValueError(
                "OpenAPI spec 不是合法 JSON,且未安装 PyYAML 无法解析 YAML"
            ) from e
        try:
            spec = yaml.safe_load(raw)
        except Exception as e:
            raise ValueError(
                f"OpenAPI spec 解析失败 (JSON 错误: {json_err}; YAML 错误: {e})"
            ) from e
        if not isinstance(spec, dict):
            raise ValueError("OpenAPI spec 解析为 YAML 但不是对象")

    return parse_openapi_to_tools(spec, base_url, auth=auth)


def build_langchain_tools_from_spec(
    spec: dict, base_url: str, auth: Optional[AuthConfig] = None
) -> List[Any]:
    """便捷入口: 解析 spec → ToolSpec list → LangChain BaseTool list"""
    tool_specs = parse_openapi_to_tools(spec, base_url, auth=auth)
    return [build_langchain_tool(ts, auth=auth) for ts in tool_specs]
