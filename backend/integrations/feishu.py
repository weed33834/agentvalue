"""飞书 IM 适配器 (P7, 对标 ADR-001)

接入要点:
1. 应用凭证: app_id + app_secret (从 https://open.feishu.cn/app 拿)
2. tenant_access_token: 缓存在内存, 2h 过期前自动续
3. send_text: POST /open-apis/im/v1/messages?receive_id_type={user_id|open_id|chat_id}
4. send_card: 同上, content 用 card JSON
5. webhook 验签: 用 app_secret 计算 sha256(timestamp + nonce + body + app_secret) 对比 X-Lark-Signature
6. parse_webhook: 解析 v2 event schema, 提取 message_id/content/chat_id

真实接入需要:
- 配置 FEISHU_APP_ID + FEISHU_APP_SECRET
- 注册 webhook 接收路由 (api/v1/webhooks/feishu)

飞书开放平台文档: https://open.feishu.cn/document
"""
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from .base import IMAdapter, IMMessage, IMRecipient

logger = logging.getLogger(__name__)

_FEISHU_BASE = "https://open.feishu.cn"
_TOKEN_REFRESH_MARGIN = 300  # token 过期前 5 分钟提前刷新


class FeishuIMAdapter(IMAdapter):
    """飞书 IM 适配器

    实现 tenant_access_token 自动缓存与续期、文本/卡片消息发送、
    webhook 验签与事件解析。

    Args:
        app_id: 飞书应用 App ID
        app_secret: 飞书应用 App Secret
    """

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._tenant_access_token: Optional[str] = None
        self._token_expires_at: int = 0

    # ============================================================
    # Token 管理
    # ============================================================

    async def _refresh_tenant_access_token(self) -> str:
        """调用 /open-apis/auth/v3/tenant_access_token/internal 获取 token。

        token 有效期 2 小时, 缓存并在过期前 5 分钟自动续。
        """
        if self._tenant_access_token and time.time() < self._token_expires_at:
            return self._tenant_access_token

        url = f"{_FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal"
        payload = {"app_id": self.app_id, "app_secret": self.app_secret}

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.error("飞书 tenant_access_token 获取失败: %s", e)
            raise

        if data.get("code") != 0:
            raise RuntimeError(
                f"飞书 token 获取失败: code={data.get('code')} msg={data.get('msg')}"
            )

        self._tenant_access_token = data["tenant_access_token"]
        expire = data.get("expire", 7200)
        self._token_expires_at = int(time.time()) + expire - _TOKEN_REFRESH_MARGIN
        logger.info("飞书 tenant_access_token 刷新成功, 有效期 %ss", expire)
        return self._tenant_access_token

    async def _get_headers(self) -> Dict[str, str]:
        """构建带 Authorization header 的请求头"""
        token = await self._refresh_tenant_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _resolve_receive_id(self, recipient: IMRecipient) -> tuple[str, str]:
        """从 IMRecipient 中解析 receive_id 和 receive_id_type"""
        if recipient.chat_id:
            return recipient.chat_id, "chat_id"
        if recipient.open_id:
            return recipient.open_id, "open_id"
        if recipient.user_id:
            return recipient.user_id, "user_id"
        raise ValueError("IMRecipient 必须指定 chat_id / open_id / user_id 之一")

    # ============================================================
    # 消息发送
    # ============================================================

    async def send_text(self, recipient: IMRecipient, text: str) -> str:
        """发送文本消息

        POST /open-apis/im/v1/messages?receive_id_type={type}
        body: {"receive_id": "...", "msg_type": "text", "content": json.dumps({"text": text})}

        Returns:
            message_id (飞书消息 ID)
        """
        receive_id, receive_id_type = self._resolve_receive_id(recipient)
        url = f"{_FEISHU_BASE}/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
        payload = {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }

        headers = await self._get_headers()
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.error("飞书 send_text 失败: %s", e)
            raise

        if data.get("code") != 0:
            raise RuntimeError(
                f"飞书 send_text 失败: code={data.get('code')} msg={data.get('msg')}"
            )

        message_id = data.get("data", {}).get("message_id", "")
        logger.info("飞书消息发送成功 message_id=%s", message_id)
        return message_id

    async def send_card(self, recipient: IMRecipient, card: Dict[str, Any]) -> str:
        """发送交互卡片消息

        POST /open-apis/im/v1/messages?receive_id_type={type}
        body: {"receive_id": "...", "msg_type": "interactive", "content": json.dumps(card)}

        Args:
            card: 飞书卡片 JSON (遵循 Card Protocol)

        Returns:
            message_id
        """
        receive_id, receive_id_type = self._resolve_receive_id(recipient)
        url = f"{_FEISHU_BASE}/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
        payload = {
            "receive_id": receive_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }

        headers = await self._get_headers()
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.error("飞书 send_card 失败: %s", e)
            raise

        if data.get("code") != 0:
            raise RuntimeError(
                f"飞书 send_card 失败: code={data.get('code')} msg={data.get('msg')}"
            )

        message_id = data.get("data", {}).get("message_id", "")
        logger.info("飞书卡片发送成功 message_id=%s", message_id)
        return message_id

    # ============================================================
    # Webhook 解析
    # ============================================================

    async def parse_webhook(self, payload: Dict[str, Any]) -> Optional[IMMessage]:
        """解析飞书 v2 event schema, 提取消息内容

        飞书 v2 事件回调格式:
        {
            "schema": "2.0",
            "header": {
                "event_id": "...",
                "event_type": "im.message.receive_v1",
                "create_time": "..."
            },
            "event": {
                "sender": {"sender_id": {"open_id": "..."}},
                "message": {
                    "message_id": "...",
                    "chat_id": "...",
                    "message_type": "text",
                    "content": '{"text":"hello"}'
                }
            }
        }

        Returns:
            IMMessage 对象, 非消息事件返回 None
        """
        header = payload.get("header", {})
        event_type = header.get("event_type", "")

        if event_type != "im.message.receive_v1":
            logger.debug("飞书非消息事件, 跳过: %s", event_type)
            return None

        event = payload.get("event", {})
        sender = event.get("sender", {}).get("sender_id", {})
        message = event.get("message", {})

        message_id = message.get("message_id", "")
        chat_id = message.get("chat_id", "")
        msg_type = message.get("message_type", "text")
        sender_open_id = sender.get("open_id", "")

        # 解析消息内容
        content_str = message.get("content", "{}")
        try:
            content_obj = json.loads(content_str)
        except (json.JSONDecodeError, TypeError):
            content_obj = {}

        # 提取文本内容
        text = ""
        if msg_type == "text":
            text = content_obj.get("text", "")
        elif msg_type == "post":
            # 富文本: 遍历 title + content
            title = content_obj.get("title", "")
            content_lines = []
            for paragraph in content_obj.get("content", []):
                line_parts = []
                for elem in paragraph:
                    if elem.get("tag") == "text":
                        line_parts.append(elem.get("text", ""))
                    elif elem.get("tag") == "at":
                        line_parts.append(f"@{elem.get('user_id', '')}")
                content_lines.append("".join(line_parts))
            text = f"{title}\n" + "\n".join(content_lines) if title else "\n".join(content_lines)
        else:
            text = f"[{msg_type} message]"

        create_time = header.get("create_time", "")
        try:
            ts = int(create_time) / 1000 if create_time else time.time()
            timestamp = datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, TypeError):
            timestamp = datetime.now(timezone.utc)

        return IMMessage(
            channel_id=chat_id,
            user_id=sender_open_id,
            user_name=None,
            content=text,
            message_id=message_id,
            timestamp=timestamp,
            raw=payload,
        )

    async def verify_webhook_signature(
        self, payload: Dict[str, Any], signature: str
    ) -> bool:
        """验证飞书 webhook 签名

        飞书 v2 事件回调签名算法:
            sha256(timestamp + nonce + body + app_secret)

        X-Lark-Signature header 值需与此计算的值一致。

        注意: 本方法需要从 webhook 请求中获取 timestamp / nonce / body,
        因此在 webhook_routes.py 中的 _verify_feishu_signature 直接实现了验签逻辑。
        本方法提供独立的验签接口供其他调用场景使用。
        """
        if not signature or not self.app_secret:
            return False

        timestamp = str(payload.get("header", {}).get("create_time", ""))
        nonce = payload.get("header", {}).get("event_id", "")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        raw = f"{timestamp}{nonce}{body}{self.app_secret}"
        expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return hmac.compare_digest(signature.strip(), expected)
