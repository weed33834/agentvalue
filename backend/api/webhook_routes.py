"""
Webhook 接收路由

接收外部系统(飞书/GitLab/自定义)的事件回调,异步处理后立即响应。
所有 webhook 端点不使用 JWT 认证,改用签名/token 验证来源合法性。
事件处理失败仅记录日志,不向调用方返回错误(防止重试风暴)。

端点:
- POST /api/v1/webhooks/feishu        飞书事件回调 (验签 + challenge + 消息/卡片事件)
- POST /api/v1/webhooks/gitlab        GitLab Webhook (X-Gitlab-Token 验证 + push/MR/issue)
- POST /api/v1/webhooks/custom/{hook_id}  通用自定义 Webhook (HMAC-SHA256 验签 + 事件总线转发)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from core.database import get_db_session
from core.tenant_context import get_current_tenant
from integrations.settings import get_integrations_settings
from models.models import WebhookEvent

# 保存后台任务引用，防止被 GC 回收
_background_tasks: set = set()


def _spawn_task(coro):
    """创建后台任务并保存引用，完成后自动移除"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhook"])

# Webhook 事件状态常量
_EVENT_PENDING = "pending"
_EVENT_PROCESSED = "processed"
_EVENT_FAILED = "failed"


# ====== 签名验证 ======


def _verify_hmac_sha256(body: bytes, signature: Optional[str], secret: str) -> bool:
    """HMAC-SHA256 签名验证

    用 secret 作为密钥对 body 计算 HMAC-SHA256,与 signature 做恒等比较。
    signature 可带 "sha256=" 前缀(GitHub 风格),内部自动去除。
    """
    if not signature or not secret:
        return False
    # 兼容 "sha256=<hex>" 前缀格式
    sig = signature.removeprefix("sha256=").strip()
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def _verify_feishu_signature(
    body: bytes,
    signature: Optional[str],
    timestamp: Optional[str],
    nonce: Optional[str],
    secret: str,
) -> bool:
    """飞书 webhook 签名验证

    飞书 v2 事件回调签名:sha256(timestamp + nonce + body + secret)。
    同时兼容 HMAC-SHA256 模式(无 timestamp/nonce 时回退)。
    """
    if not signature or not secret:
        return False
    if timestamp and nonce:
        # 飞书官方 v2 签名:sha256(timestamp + nonce + body + secret)
        raw = f"{timestamp}{nonce}{body.decode('utf-8', errors='replace')}{secret}"
        expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return hmac.compare_digest(signature.strip(), expected)
    # 回退:HMAC-SHA256(body, secret)
    return _verify_hmac_sha256(body, signature, secret)


def _get_custom_webhook_secret(hook_id: str) -> Optional[str]:
    """查找自定义 webhook 的验签密钥

    当前从环境变量 CUSTOM_WEBHOOK_SECRETS 读取(JSON 格式: {"hook_id": "secret"}),
    后续可扩展为 DB 查表。未配置时返回 None(跳过验签)。
    """
    import os

    raw = os.environ.get("CUSTOM_WEBHOOK_SECRETS", "")
    if not raw:
        return None
    try:
        mapping = json.loads(raw)
        return mapping.get(hook_id)
    except (json.JSONDecodeError, TypeError):
        return None


# ====== 异步事件处理 ======


async def _process_webhook_event(
    source: str,
    event_type: str,
    payload: Dict[str, Any],
    tenant_id: str,
    raw_body: str,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """异步处理 webhook 事件:落库 + 业务分发。

    在独立 DB 会话中执行,失败仅记录日志(不抛异常,防止 asyncio 任务未捕获警告)。
    """
    try:
        async with get_db_session() as session:
            # 落库记录
            event_record = WebhookEvent(
                source=source,
                event_type=event_type,
                payload=raw_body,
                status=_EVENT_PENDING,
                tenant_id=tenant_id,
            )
            session.add(event_record)
            await session.flush()

            try:
                # 按来源分发到具体处理器
                if source == "feishu":
                    await _handle_feishu_event(event_type, payload, extra)
                elif source == "gitlab":
                    await _handle_gitlab_event(event_type, payload, extra)
                elif source == "custom":
                    await _handle_custom_event(event_type, payload, extra)

                event_record.status = _EVENT_PROCESSED
                event_record.processed_at = datetime.now(timezone.utc)
            except Exception as exc:
                event_record.status = _EVENT_FAILED
                event_record.error_message = str(exc)[:2000]
                event_record.processed_at = datetime.now(timezone.utc)
                logger.exception(
                    "webhook 事件处理失败 source=%s event_type=%s",
                    source,
                    event_type,
                )

            await session.commit()
    except Exception:
        # 落库本身失败:仅记录日志,不向调用方暴露
        logger.exception(
            "webhook 事件落库失败 source=%s event_type=%s", source, event_type
        )


async def _handle_feishu_event(
    event_type: str,
    payload: Dict[str, Any],
    extra: Optional[Dict[str, Any]],
) -> None:
    """处理飞书事件:消息接收 / 卡片回调。

    im.message.receive_v1:解析消息内容,转发给 AI 处理(当前仅记录日志,
    后续可对接 agent.session_processor)。
    card.action.trigger:卡片按钮回调,解析 action_value 做对应处理。
    """
    event = payload.get("event", payload)
    if event_type == "im.message.receive_v1":
        message = event.get("message", {})
        sender = event.get("sender", {})
        msg_type = message.get("message_type", "text")
        message_id = message.get("message_id", "")
        chat_id = message.get("chat_id", "")
        sender_id = sender.get("sender_id", {}).get("open_id", "")
        logger.info(
            "飞书消息接收 msg_type=%s message_id=%s chat_id=%s sender=%s",
            msg_type,
            message_id,
            chat_id,
            sender_id,
        )
        # TODO: 解析消息内容,转发给 AI 处理(agent.session_processor)
    elif event_type == "card.action.trigger":
        action = event.get("action", {})
        action_value = action.get("value", {})
        operator = event.get("operator", {})
        logger.info(
            "飞书卡片回调 action_value=%s operator=%s",
            action_value,
            operator.get("open_id", ""),
        )
        # TODO: 根据 action_value 分发到对应业务处理器
    else:
        logger.debug("飞书未处理的事件类型: %s", event_type)


async def _handle_gitlab_event(
    event_type: str,
    payload: Dict[str, Any],
    extra: Optional[Dict[str, Any]],
) -> None:
    """处理 GitLab Webhook 事件:Push / Merge Request / Issue。

    Push Event:提取 commits 列表,更新代码贡献统计。
    Merge Request Event:提取 MR 状态变化。
    Issue Event:提取 issue 创建/更新。
    """
    if event_type == "Push Hook":
        commits = payload.get("commits", [])
        ref = payload.get("ref", "")
        user = payload.get("user_name", "")
        project = payload.get("project", {}).get("name", "")
        logger.info(
            "GitLab Push: project=%s ref=%s user=%s commits=%d",
            project,
            ref,
            user,
            len(commits),
        )
        # TODO: 更新代码贡献统计(按 author 映射到 employee_id)
    elif event_type == "Merge Request Hook":
        mr = payload.get("object_attributes", {})
        action = mr.get("action", "")
        state = mr.get("state", "")
        title = mr.get("title", "")
        logger.info("GitLab MR: title=%s action=%s state=%s", title, action, state)
        # TODO: 提取 MR 状态变化,触发评估关联
    elif event_type == "Issue Hook":
        issue = payload.get("object_attributes", {})
        action = issue.get("action", "")
        title = issue.get("title", "")
        logger.info("GitLab Issue: title=%s action=%s", title, action)
        # TODO: 提取 issue 事件
    else:
        logger.debug("GitLab 未处理的事件类型: %s", event_type)


async def _handle_custom_event(
    event_type: str,
    payload: Dict[str, Any],
    extra: Optional[Dict[str, Any]],
) -> None:
    """处理自定义 webhook 事件:转发到内部事件总线。

    通过 core.event_bus 发布到 channel webhook:custom:{hook_id},
    订阅方可按 hook_id 监听并处理。
    """
    hook_id = (extra or {}).get("hook_id", "unknown")
    try:
        from core.event_bus import get_event_bus

        bus = get_event_bus()
        channel = f"webhook:custom:{hook_id}"
        await bus.publish(
            channel,
            {"event_type": event_type, "payload": payload, "extra": extra},
        )
        logger.info(
            "自定义 webhook 已转发到事件总线 channel=%s event_type=%s",
            channel,
            event_type,
        )
    except Exception:
        logger.exception("自定义 webhook 转发事件总线失败 hook_id=%s", hook_id)


# ====== 路由端点 ======


@router.post("/feishu")
async def feishu_webhook(
    request: Request,
    x_lark_signature: Optional[str] = Header(None, alias="X-Lark-Signature"),
    x_lark_request_timestamp: Optional[str] = Header(
        None, alias="X-Lark-Request-Timestamp"
    ),
    x_lark_request_nonce: Optional[str] = Header(None, alias="X-Lark-Request-Nonce"),
):
    """飞书事件回调端点

    - 验证签名 (X-Lark-Signature, HMAC-SHA256 / 飞书 v2 签名)
    - 处理 url_verification challenge (返回 challenge)
    - 处理 im.message.receive_v1 事件 (解析消息, 转发给AI处理)
    - 处理 card.action.trigger 事件 (卡片按钮回调)
    - 返回 {"code": 0} 表示成功
    """
    raw_body = await request.body()

    # 签名验证
    settings = get_integrations_settings()
    secret = settings.feishu_webhook_secret
    if secret:
        if not _verify_feishu_signature(
            raw_body,
            x_lark_signature,
            x_lark_request_timestamp,
            x_lark_request_nonce,
            secret,
        ):
            logger.warning("飞书 webhook 签名验证失败")
            return JSONResponse(
                status_code=401,
                content={"code": 401, "msg": "签名验证失败"},
            )
    else:
        logger.debug("飞书 webhook 未配置验签密钥,跳过签名验证")

    # 解析 payload
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.exception("飞书 webhook payload 解析失败")
        return JSONResponse(
            status_code=400,
            content={"code": 400, "msg": "payload 解析失败"},
        )

    # url_verification challenge:飞书首次配置 webhook 时发送,需原样返回 challenge
    if payload.get("type") == "url_verification":
        challenge = payload.get("challenge", "")
        return {"code": 0, "challenge": challenge}

    # 事件 schema v2:header.type 为事件类型
    header = payload.get("header", {})
    event_type = header.get("event_type", payload.get("type", "unknown"))
    event_id = header.get("event_id", str(uuid.uuid4()))

    tenant_id = get_current_tenant()

    # 异步处理(不阻塞响应)
    _spawn_task(
        _process_webhook_event(
            source="feishu",
            event_type=event_type,
            payload=payload,
            tenant_id=tenant_id,
            raw_body=raw_body.decode("utf-8", errors="replace"),
        )
    )

    logger.info("飞书 webhook 已接收 event_id=%s event_type=%s", event_id, event_type)
    return {"code": 0}


@router.post("/gitlab")
async def gitlab_webhook(
    request: Request,
    x_gitlab_token: Optional[str] = Header(None, alias="X-Gitlab-Token"),
    x_gitlab_event: Optional[str] = Header(None, alias="X-Gitlab-Event"),
):
    """GitLab Webhook 端点

    - 验证 X-Gitlab-Token header (与配置的 gitlab_webhook_secret 比对)
    - 处理 Push Event (提取 commits, 更新代码贡献统计)
    - 处理 Merge Request Event (提取 MR 状态变化)
    - 处理 Issue Event
    - 返回 {"status": "ok"}
    """
    raw_body = await request.body()

    # Token 验证
    settings = get_integrations_settings()
    secret = settings.gitlab_webhook_secret
    if secret:
        if not x_gitlab_token or not hmac.compare_digest(x_gitlab_token, secret):
            logger.warning("GitLab webhook token 验证失败")
            return JSONResponse(
                status_code=401,
                content={"status": "error", "msg": "token 验证失败"},
            )
    else:
        logger.debug("GitLab webhook 未配置验签密钥,跳过 token 验证")

    # 解析 payload
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.exception("GitLab webhook payload 解析失败")
        return JSONResponse(
            status_code=400,
            content={"status": "error", "msg": "payload 解析失败"},
        )

    event_type = x_gitlab_event or payload.get("object_kind", "unknown")

    tenant_id = get_current_tenant()

    # 异步处理(不阻塞响应)
    _spawn_task(
        _process_webhook_event(
            source="gitlab",
            event_type=event_type,
            payload=payload,
            tenant_id=tenant_id,
            raw_body=raw_body.decode("utf-8", errors="replace"),
            extra={"x_gitlab_event": event_type},
        )
    )

    logger.info("GitLab webhook 已接收 event_type=%s", event_type)
    return {"status": "ok"}


@router.post("/custom/{hook_id}")
async def custom_webhook(
    hook_id: str,
    request: Request,
    x_webhook_signature: Optional[str] = Header(None, alias="X-Webhook-Signature"),
):
    """通用自定义 Webhook 端点

    - 通过 hook_id 查找配置(验签密钥)
    - 验证签名 (X-Webhook-Signature, HMAC-SHA256)
    - 转发事件到内部消息队列(事件总线)
    - 返回 {"status": "ok"}

    hook_id 对应的密钥通过环境变量 CUSTOM_WEBHOOK_SECRETS 配置
    (JSON: {"hook_id": "secret"}),未配置时跳过验签。
    """
    raw_body = await request.body()

    # 查找配置
    secret = _get_custom_webhook_secret(hook_id)
    if secret:
        if not _verify_hmac_sha256(raw_body, x_webhook_signature, secret):
            logger.warning("自定义 webhook 签名验证失败 hook_id=%s", hook_id)
            return JSONResponse(
                status_code=401,
                content={"status": "error", "msg": "签名验证失败"},
            )
    else:
        logger.debug("自定义 webhook 未配置验签密钥 hook_id=%s,跳过签名验证", hook_id)

    # 解析 payload
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.exception("自定义 webhook payload 解析失败 hook_id=%s", hook_id)
        return JSONResponse(
            status_code=400,
            content={"status": "error", "msg": "payload 解析失败"},
        )

    event_type = payload.get("event_type", payload.get("type", "unknown"))

    tenant_id = get_current_tenant()

    # 异步处理(不阻塞响应)
    _spawn_task(
        _process_webhook_event(
            source="custom",
            event_type=event_type,
            payload=payload,
            tenant_id=tenant_id,
            raw_body=raw_body.decode("utf-8", errors="replace"),
            extra={"hook_id": hook_id},
        )
    )

    logger.info("自定义 webhook 已接收 hook_id=%s event_type=%s", hook_id, event_type)
    return {"status": "ok"}
