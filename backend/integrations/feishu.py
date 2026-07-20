"""飞书 IM 适配器(P7,对标 ADR-001)

接入要点:
1. 应用凭证:app_id + app_secret(从 https://open.feishu.cn/app 拿)
2. tenant_access_token:缓存在内存,2h 过期前自动续
3. send_text: POST /open-apis/im/v1/messages?receive_id_type={user_id|open_id|chat_id}
4. send_card:同上,content 用 card JSON
5. webhook 验签:用 app_secret 计算 sha256(timestamp + nonce + body + app_secret) 对比 X-Lark-Signature
6. parse_webhook:解析 v2 event schema,提取 message_id/content/chat_id

真实接入需要:
- 配置 FEISHU_APP_ID + FEISHU_APP_SECRET
- 注册 webhook 接收路由(api/v1/webhooks/feishu)
- 用 CodeRepoAdapter 模式同理
"""
from typing import Any, Dict, Optional

from .base import IMAdapter, IMMessage, IMRecipient


class FeishuIMAdapter(IMAdapter):
    """飞书 IM 适配器(骨架,真实接入待实现,详见 ADR-001)。

    当前所有方法 raise NotImplementedError,工厂捕获后降级为 DummyIMAdapter。
    真实接入时移除 __init__ 中的 raise,逐个实现 TODO 方法。
    """

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._tenant_access_token: Optional[str] = None
        self._token_expires_at: int = 0
        raise NotImplementedError("FeishuIMAdapter 真实接入待实现,详见 ADR-001")

    # TODO: 调用 /open-apis/auth/v3/tenant_access_token/internal,缓存 + 2h 前自动续
    async def _refresh_tenant_access_token(self) -> str:
        raise NotImplementedError("TODO: tenant_access_token 刷新待实现")

    async def send_text(self, recipient: IMRecipient, text: str) -> str:
        # TODO: POST /open-apis/im/v1/messages?receive_id_type={user_id|open_id|chat_id}
        # body: {"receive_id": "...", "msg_type": "text", "content": json.dumps({"text": text})}
        raise NotImplementedError("TODO: send_text 待实现")

    async def send_card(self, recipient: IMRecipient, card: Dict[str, Any]) -> str:
        # TODO: 同 send_text,msg_type=interactive,content=card JSON
        raise NotImplementedError("TODO: send_card 待实现")

    async def parse_webhook(self, payload: Dict[str, Any]) -> Optional[IMMessage]:
        # TODO: 解析飞书 v2 event schema,提取 message_id/content/chat_id/sender
        # 注意:需先 verify_webhook_signature 通过后再解析
        raise NotImplementedError("TODO: parse_webhook 待实现")

    async def verify_webhook_signature(
        self, payload: Dict[str, Any], signature: str
    ) -> bool:
        # TODO: sha256(timestamp + nonce + body + app_secret) 对比 X-Lark-Signature
        raise NotImplementedError("TODO: verify_webhook_signature 待实现")
