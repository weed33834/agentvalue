"""Dummy IM/CodeRepo 适配器(P7)

未配置外部集成时返回 Dummy,业务层调用不报错。
所有 send_* 返回 "dummy-msg-id",所有 list_* 返回 [],所有 verify_* 返回 True。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from .base import (
    CodeRepoAdapter,
    CodeRepoEvent,
    IMAdapter,
    IMMessage,
    IMRecipient,
)


class DummyIMAdapter(IMAdapter):
    async def send_text(self, recipient: IMRecipient, text: str) -> str:
        return "dummy-msg-id"

    async def send_card(self, recipient: IMRecipient, card: Dict[str, Any]) -> str:
        return "dummy-msg-id"

    async def parse_webhook(self, payload: Dict[str, Any]) -> Optional[IMMessage]:
        return None

    async def verify_webhook_signature(
        self, payload: Dict[str, Any], signature: str
    ) -> bool:
        return True


class DummyCodeRepoAdapter(CodeRepoAdapter):
    async def list_commits(
        self, repo: str, ref: str, since: datetime, until: datetime
    ) -> List[CodeRepoEvent]:
        return []

    async def list_merge_requests(
        self, repo: str, state: str = "opened"
    ) -> List[CodeRepoEvent]:
        return []

    async def parse_webhook(
        self, payload: Dict[str, Any], event_type: str
    ) -> Optional[CodeRepoEvent]:
        return None

    async def verify_webhook_signature(
        self, payload: Dict[str, Any], signature: str
    ) -> bool:
        return True
