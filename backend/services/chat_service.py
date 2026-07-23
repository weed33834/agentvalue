"""
Chat Service

Session/Message/Part 的 CRUD 服务层，对齐现有 services/evaluation_service.py 风格。
负责 DB 持久化，被 api/chat.py 与 agent/session_prompt.py 共用。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.tenant_context import get_current_tenant
from models.chat_models import ChatMessage, ChatPart, ChatSession
from models.models import DEFAULT_TENANT_ID

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ChatService:
    """Chat 会话 CRUD 服务"""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ============================================================
    # Session
    # ============================================================

    async def create_session(
        self,
        user_id: str,
        title: str = "新对话",
        model_name: str = "gpt-4o-mini",
        agent_name: str = "assistant",
        tenant_id: Optional[str] = None,
    ) -> ChatSession:
        tenant_id = tenant_id or get_current_tenant()
        session = ChatSession(
            user_id=user_id,
            title=title[:256],
            model_name=model_name[:128],
            agent_name=agent_name[:64],
            tenant_id=tenant_id,
        )
        self.db.add(session)
        await self.db.flush()
        await self.db.refresh(session)
        return session

    async def get_session(
        self, session_id: str, tenant_id: Optional[str] = None
    ) -> Optional[ChatSession]:
        tenant_id = tenant_id or get_current_tenant()
        stmt = select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.tenant_id == tenant_id,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_sessions(
        self,
        user_id: str,
        tenant_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[ChatSession]:
        tenant_id = tenant_id or get_current_tenant()
        stmt = (
            select(ChatSession)
            .where(
                ChatSession.user_id == user_id,
                ChatSession.tenant_id == tenant_id,
            )
            .order_by(ChatSession.updated_at.desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def delete_session(
        self, session_id: str, tenant_id: Optional[str] = None
    ) -> bool:
        tenant_id = tenant_id or get_current_tenant()
        session = await self.get_session(session_id, tenant_id)
        if session is None:
            return False
        await self.db.delete(session)
        await self.db.flush()
        return True

    async def update_session_title(
        self, session_id: str, title: str, tenant_id: Optional[str] = None
    ) -> Optional[ChatSession]:
        tenant_id = tenant_id or get_current_tenant()
        session = await self.get_session(session_id, tenant_id)
        if session is None:
            return None
        session.title = title[:256]
        await self.db.flush()
        return session

    # ============================================================
    # Message
    # ============================================================

    async def create_message(
        self,
        session_id: str,
        role: str,
        parent_id: Optional[str] = None,
        model_id: Optional[str] = None,
        provider_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> ChatMessage:
        tenant_id = tenant_id or get_current_tenant()
        msg = ChatMessage(
            session_id=session_id,
            role=role,
            parent_id=parent_id,
            model_id=model_id,
            provider_id=provider_id,
            tenant_id=tenant_id,
        )
        self.db.add(msg)
        await self.db.flush()
        await self.db.refresh(msg)
        return msg

    async def update_message_tokens(
        self, message_id: str, tokens: dict
    ) -> Optional[ChatMessage]:
        """更新消息的 token 用量"""
        stmt = select(ChatMessage).where(ChatMessage.id == message_id)
        result = await self.db.execute(stmt)
        msg = result.scalar_one_or_none()
        if msg is None:
            return None
        msg.tokens = tokens
        await self.db.flush()
        return msg

    async def list_messages(
        self, session_id: str, tenant_id: Optional[str] = None
    ) -> List[ChatMessage]:
        tenant_id = tenant_id or get_current_tenant()
        stmt = (
            select(ChatMessage)
            .where(
                ChatMessage.session_id == session_id,
                ChatMessage.tenant_id == tenant_id,
            )
            .order_by(ChatMessage.created_at.asc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def complete_message(
        self,
        message_id: str,
        finish_reason: Optional[str] = None,
        tokens: Optional[Dict[str, Any]] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> Optional[ChatMessage]:
        stmt = select(ChatMessage).where(ChatMessage.id == message_id)
        result = await self.db.execute(stmt)
        msg = result.scalar_one_or_none()
        if msg is None:
            return None
        msg.completed_at = _now()
        if finish_reason:
            msg.finish_reason = finish_reason
        if tokens:
            msg.tokens = tokens
        if error:
            msg.error = error
        await self.db.flush()
        return msg

    # ============================================================
    # Part
    # ============================================================

    async def create_part(
        self,
        message_id: str,
        session_id: str,
        part_type: str,
        sequence: int,
        text: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        tool_state: Optional[Dict[str, Any]] = None,
        step_index: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        tenant_id: Optional[str] = None,
    ) -> ChatPart:
        tenant_id = tenant_id or get_current_tenant()
        part = ChatPart(
            message_id=message_id,
            session_id=session_id,
            type=part_type,
            sequence=sequence,
            text=text,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_state=tool_state,
            step_index=step_index,
            metadata_=metadata,
            tenant_id=tenant_id,
        )
        self.db.add(part)
        await self.db.flush()
        await self.db.refresh(part)
        return part

    async def update_part(self, part_id: str, **fields: Any) -> Optional[ChatPart]:
        stmt = select(ChatPart).where(ChatPart.id == part_id)
        result = await self.db.execute(stmt)
        part = result.scalar_one_or_none()
        if part is None:
            return None
        # text 字段映射：ChatPart 用 metadata_ 列名，外部用 metadata
        if "metadata" in fields:
            fields["metadata_"] = fields.pop("metadata")
        for k, v in fields.items():
            if hasattr(part, k):
                setattr(part, k, v)
        await self.db.flush()
        return part

    async def list_parts_by_message(self, message_id: str) -> List[ChatPart]:
        stmt = (
            select(ChatPart)
            .where(ChatPart.message_id == message_id)
            .order_by(ChatPart.sequence.asc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def list_parts_by_session(
        self, session_id: str, tenant_id: Optional[str] = None
    ) -> List[ChatPart]:
        tenant_id = tenant_id or get_current_tenant()
        stmt = (
            select(ChatPart)
            .where(
                ChatPart.session_id == session_id,
                ChatPart.tenant_id == tenant_id,
            )
            .order_by(ChatPart.message_id.asc(), ChatPart.sequence.asc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    # ============================================================
    # 组合查询：消息 + parts
    # ============================================================

    async def list_messages_with_parts(
        self, session_id: str, tenant_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """返回 [{message: {...}, parts: [...]}] 供前端回显历史。"""
        tenant_id = tenant_id or get_current_tenant()
        messages = await self.list_messages(session_id, tenant_id)
        if not messages:
            return []
        msg_ids = [m.id for m in messages]
        stmt = (
            select(ChatPart)
            .where(
                ChatPart.message_id.in_(msg_ids),
                ChatPart.tenant_id == tenant_id,
            )
            .order_by(ChatPart.message_id.asc(), ChatPart.sequence.asc())
        )
        result = await self.db.execute(stmt)
        parts_by_msg: Dict[str, List[ChatPart]] = {}
        for p in result.scalars().all():
            parts_by_msg.setdefault(p.message_id, []).append(p)

        return [
            {
                "id": m.id,
                "session_id": m.session_id,
                "role": m.role,
                "parent_id": m.parent_id,
                "model_id": m.model_id,
                "provider_id": m.provider_id,
                "tokens": m.tokens,
                "cost": m.cost,
                "finish_reason": m.finish_reason,
                "error": m.error,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "completed_at": m.completed_at.isoformat() if m.completed_at else None,
                "parts": [_part_to_dict(p) for p in parts_by_msg.get(m.id, [])],
            }
            for m in messages
        ]

    async def commit(self) -> None:
        await self.db.commit()


def _part_to_dict(p: ChatPart) -> Dict[str, Any]:
    return {
        "id": p.id,
        "type": p.type,
        "sequence": p.sequence,
        "text": p.text,
        "tool_name": p.tool_name,
        "tool_call_id": p.tool_call_id,
        "tool_state": p.tool_state,
        "step_index": p.step_index,
        "metadata": p.metadata_,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }
