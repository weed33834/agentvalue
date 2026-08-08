"""AgentValue 开放 API 的 Python SDK（WS-3）

对标 Stripe / Svix 的官方 SDK 使用方式：

.. code-block:: python

    from agentvalue import Client

    client = Client(base_url="https://your-host.example", api_key="ak_xxxx")
    me = client.get_me()                      # 连通性自检
    job = client.create_evaluation("u_001", "2026-Q3")
    ev = client.get_evaluation(job["job_id"])

Webhook 签名校验（与 services/webhook_delivery_service.py 的配方完全一致）:

.. code-block:: python

    from agentvalue import verify_webhook_signature

    valid = verify_webhook_signature(
        secret="whsec_...",
        timestamp=1754630400,
        body='{"id":"whd_1","event":"evaluation.completed"}',
        signature="t=1754630400,v1=9f86d0...",   # X-AgentValue-Signature 原值
    )
"""

from __future__ import annotations

import hmac
import hashlib
import random
import time
from typing import Any, Dict, List, Optional

import httpx

__all__ = [
    "AgentValueError",
    "ApiError",
    "RetryableError",
    "Client",
    "AsyncClient",
    "verify_webhook_signature",
]

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT_SECONDS = 30.0
# 429 / 5xx 重试：最多 MAX_RETRIES 次，退避 base * factor^(n-1) + 抖动
DEFAULT_MAX_RETRIES = 5
DEFAULT_BACKOFF_BASE = 1.0
DEFAULT_BACKOFF_FACTOR = 2.0
# 与平台 SIGNATURE_TOLERANCE_SECONDS 一致
SIGNATURE_TOLERANCE_SECONDS = 300


class AgentValueError(Exception):
    """SDK 基础异常。"""


class ApiError(AgentValueError):
    """平台返回非 2xx（不重试）。携带状态码与响应体。"""

    def __init__(self, status_code: int, detail: Any):
        super().__init__(f"AgentValue API 错误: HTTP {status_code} {detail!r}")
        self.status_code = status_code
        self.detail = detail


class RetryableError(AgentValueError):
    """重试耗尽仍失败（429 / 5xx）。"""


# ---------------------------------------------------------------------------
# Webhook 签名校验（权威配方的忠实复刻）
# ---------------------------------------------------------------------------


def _parse_signature(header: str) -> Dict[str, str]:
    """解析 ``t=...,v1=...`` 签名头为 dict；非法输入返回空 dict。"""
    parsed: Dict[str, str] = {}
    if not header:
        return parsed
    for part in header.split(","):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        parsed[key.strip()] = value.strip()
    return parsed


def _build_signature(secret: str, timestamp: int, body: str) -> str:
    """与平台 build_signature 逐字节一致：``HMAC_SHA256(secret, f"{t}.{body}")``。"""
    signed_payload = f"{timestamp}.{body}"
    digest = hmac.new(
        secret.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify_webhook_signature(
    secret: str,
    timestamp: int,
    body: str,
    signature: str,
    *,
    tolerance_seconds: int = SIGNATURE_TOLERANCE_SECONDS,
    now: Optional[int] = None,
) -> bool:
    """校验 webhook 签名（防重放 + 防篡改）。

    Args:
        secret: 订阅密钥（``whsec_`` 开头）。
        timestamp: 请求到达时的 unix 秒（防重放用）。若 ``signature`` 头里带
            ``t=`` 字段，则以头里的 ``t`` 为准做新鲜度校验。
        body: **原始**请求体字符串（未经反序列化/重新序列化）。
        signature: ``X-AgentValue-Signature`` 头原值（``t=...,v1=...``），
            也可只传 ``v1`` 的 hex。
        tolerance_seconds: 时间戳容忍窗口；<=0 表示不校验时间。
        now: 覆盖当前时间（unix 秒），仅测试使用。

    Returns:
        True 表示签名有效且未过期。
    """
    parsed = _parse_signature(signature)
    ts_raw = parsed.get("t")
    provided = parsed.get("v1")
    if not provided:
        # 兼容只传 v1 hex 的用法，此时用调用方传入的 timestamp
        provided = signature.strip()
    if not provided:
        return False
    if ts_raw is not None:
        try:
            event_ts = int(ts_raw)
        except ValueError:
            return False
    else:
        event_ts = timestamp

    if tolerance_seconds > 0:
        current = int(time.time()) if now is None else now
        if abs(current - event_ts) > tolerance_seconds:
            return False

    expected = _build_signature(secret, event_ts, body)
    expected_v1 = _parse_signature(expected).get("v1", "")
    return hmac.compare_digest(expected_v1, provided)


# ---------------------------------------------------------------------------
# 请求执行（重试 / 错误归一化）
# ---------------------------------------------------------------------------


def _should_retry(status_code: int) -> bool:
    """429（限流）与 5xx（服务端暂时故障）可重试。"""
    return status_code == 429 or status_code >= 500


def _backoff_delay(attempt: int, base: float, factor: float) -> float:
    """第 attempt 次（从 0 起）重试前的等待秒数：base * factor^n + [0,0.2) 抖动。"""
    return base * (factor ** attempt) + random.uniform(0.0, 0.2)


def _handle_response(response: httpx.Response) -> Any:
    """2xx → JSON；可重试状态抛 RetryableError；其余抛 ApiError。"""
    if response.is_success:
        if response.status_code == 204 or not response.content:
            return None
        return response.json()
    if _should_retry(response.status_code):
        raise RetryableError(f"HTTP {response.status_code}")
    try:
        detail = response.json()
    except Exception:
        detail = response.text[:500]
    raise ApiError(response.status_code, detail)


def _request_sync(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    max_retries: int,
    backoff_base: float,
    backoff_factor: float,
    **kwargs: Any,
) -> Any:
    """同步请求 + 指数退避重试。"""
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            response = client.request(method, url, **kwargs)
            return _handle_response(response)
        except RetryableError as exc:
            last_exc = exc
            if attempt >= max_retries:
                break
            time.sleep(_backoff_delay(attempt, backoff_base, backoff_factor))
    raise RetryableError(
        f"重试 {max_retries} 次后仍失败: {last_exc}" if last_exc else "请求失败"
    )


async def _request_async(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_retries: int,
    backoff_base: float,
    backoff_factor: float,
    **kwargs: Any,
) -> Any:
    """异步请求 + 指数退避重试。"""
    import asyncio

    last_exc: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            response = await client.request(method, url, **kwargs)
            return _handle_response(response)
        except RetryableError as exc:
            last_exc = exc
            if attempt >= max_retries:
                break
            await asyncio.sleep(_backoff_delay(attempt, backoff_base, backoff_factor))
    raise RetryableError(
        f"重试 {max_retries} 次后仍失败: {last_exc}" if last_exc else "请求失败"
    )


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class _CommonClient:
    """sync / async 共享的路径与序列化逻辑。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str = "",
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
        headers: Optional[Dict[str, str]] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_factor = backoff_factor
        self._extra_headers = dict(headers or {})

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json", **self._extra_headers}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def _path(self, path: str) -> str:
        return f"{self.base_url}/api/public/v1{path}"

    # ---- 身份自省 ----
    def get_me(self) -> Dict[str, Any]:
        return self._request("GET", self._path("/me"))

    # ---- 评估 ----
    def create_evaluation(
        self,
        employee_id: str,
        period: str,
        raw_inputs: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        body = {"employee_id": employee_id, "period": period}
        if raw_inputs:
            body["raw_inputs"] = raw_inputs
        return self._request("POST", self._path("/evaluations"), json=body)

    def list_evaluations(
        self,
        *,
        employee_id: Optional[str] = None,
        status: Optional[str] = None,
        period: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page": page, "page_size": page_size}
        if employee_id:
            params["employee_id"] = employee_id
        if status:
            params["status"] = status
        if period:
            params["period"] = period
        return self._request("GET", self._path("/evaluations"), params=params)

    def get_evaluation(self, evaluation_id: str) -> Dict[str, Any]:
        return self._request("GET", self._path(f"/evaluations/{evaluation_id}"))

    # ---- Agent ----
    def list_agents(
        self,
        *,
        category: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page": page, "page_size": page_size}
        if category:
            params["category"] = category
        return self._request("GET", self._path("/agents"), params=params)

    def invoke_agent(
        self, agent_id: int, input: str, context: Optional[str] = None
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"input": input}
        if context is not None:
            body["context"] = context
        return self._request(
            "POST", self._path(f"/agents/{agent_id}/invoke"), json=body
        )

    # ---- 数据集 ----
    def list_datasets(
        self,
        *,
        dataset_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page": page, "page_size": page_size}
        if dataset_type:
            params["dataset_type"] = dataset_type
        return self._request("GET", self._path("/datasets"), params=params)

    def list_dataset_items(
        self,
        dataset_id: int,
        *,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page": page, "page_size": page_size}
        if status:
            params["status"] = status
        return self._request(
            "GET", self._path(f"/datasets/{dataset_id}/items"), params=params
        )

    # ---- 链路追踪 ----
    def list_traces(
        self,
        *,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page": page, "page_size": page_size}
        if kind:
            params["kind"] = kind
        if status:
            params["status"] = status
        return self._request("GET", self._path("/traces"), params=params)


class Client(_CommonClient):
    """同步 HTTP 客户端。"""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._http = httpx.Client(timeout=self.timeout)

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        return _request_sync(
            self._http,
            method,
            url,
            headers=self._headers(),
            max_retries=self.max_retries,
            backoff_base=self.backoff_base,
            backoff_factor=self.backoff_factor,
            **kwargs,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class AsyncClient(_CommonClient):
    """异步 HTTP 客户端（推荐用于 FastAPI / asyncio 环境）。"""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._http = httpx.AsyncClient(timeout=self.timeout)

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        return await _request_async(
            self._http,
            method,
            url,
            headers=self._headers(),
            max_retries=self.max_retries,
            backoff_base=self.backoff_base,
            backoff_factor=self.backoff_factor,
            **kwargs,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "AsyncClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()
