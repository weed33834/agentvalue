"""
Chat API Router

提供流式对话 API，移植 opencode 的 session prompt + SSE 事件推送能力。

端点：
- POST   /api/v1/chat/sessions               创建会话
- GET    /api/v1/chat/sessions                列出当前用户的会话
- GET    /api/v1/chat/sessions/{id}           获取会话详情
- PATCH  /api/v1/chat/sessions/{id}           更新会话（标题）
- DELETE /api/v1/chat/sessions/{id}           删除会话
- GET    /api/v1/chat/sessions/{id}/messages  列出会话消息（含 parts）
- POST   /api/v1/chat/sessions/{id}/messages 发送消息（SSE 流式响应）

SSE 事件名对齐 core.llm_events 的 LLMEvent.type：
step-start / text-start / text-delta / text-end / tool-input-start /
tool-input-delta / tool-input-end / tool-call / tool-result / tool-error /
step-finish / finish / provider-error / ping
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.session_prompt import SessionPrompt
from agent.tool_registry import ToolRegistry
from api.deps import AppState, get_app_state
from auth.rbac import get_current_user_id
from core.config import get_settings
from core.database import get_db, get_db_session
from core.llm_events import event_to_sse_dict
from core.tenant_context import get_current_tenant
from models.chat_models import ChatMessage, ChatPart, ChatSession
from services.chat_service import ChatService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])

# 会话级中断标志：session_id -> bool
# POST /stop 设置为 True，run_loop 每次迭代 / 每个 token 检查后中断
# POST /messages 和 GET /resume 开始时重置为 False
_stop_flags: Dict[str, bool] = {}

# sse-starlette 可选依赖（与 playground 一致）
try:
    from sse_starlette.sse import EventSourceResponse

    _SSE_AVAILABLE = True
except ImportError:  # pragma: no cover
    _SSE_AVAILABLE = False


# ============================================================
# 请求/响应模型
# ============================================================


class CreateSessionRequest(BaseModel):
    title: str = Field(default="新对话", max_length=256)
    model_name: str = Field(default="glm-4.7", max_length=128)
    agent_name: str = Field(default="assistant", max_length=64)


class UpdateSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=256)


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=10000)
    attachments: List[Dict[str, Any]] = Field(default_factory=list, max_length=20)


# ============================================================
# 依赖注入
# ============================================================


def get_chat_service(db: AsyncSession = Depends(get_db)) -> ChatService:
    return ChatService(db)


# ============================================================
# Session CRUD
# ============================================================


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def create_session(
    req: CreateSessionRequest,
    user_id: str = Depends(get_current_user_id),
    chat_svc: ChatService = Depends(get_chat_service),
):
    """创建聊天会话"""
    session = await chat_svc.create_session(
        user_id=user_id,
        title=req.title,
        model_name=req.model_name,
        agent_name=req.agent_name,
    )
    await chat_svc.commit()
    return _session_to_dict(session)


@router.get("/sessions")
async def list_sessions(
    user_id: str = Depends(get_current_user_id),
    chat_svc: ChatService = Depends(get_chat_service),
):
    """列出当前用户的会话"""
    sessions = await chat_svc.list_sessions(user_id)
    return [_session_to_dict(s) for s in sessions]


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    user_id: str = Depends(get_current_user_id),
    chat_svc: ChatService = Depends(get_chat_service),
):
    """获取会话详情"""
    session = await chat_svc.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.user_id != user_id:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    return _session_to_dict(session)


@router.patch("/sessions/{session_id}")
async def update_session(
    session_id: str,
    req: UpdateSessionRequest,
    user_id: str = Depends(get_current_user_id),
    chat_svc: ChatService = Depends(get_chat_service),
):
    """更新会话标题"""
    session = await chat_svc.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.user_id != user_id:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    updated = await chat_svc.update_session_title(session_id, req.title)
    await chat_svc.commit()
    return _session_to_dict(updated)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: str,
    user_id: str = Depends(get_current_user_id),
    chat_svc: ChatService = Depends(get_chat_service),
):
    """删除会话（级联删除消息和 parts）"""
    session = await chat_svc.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.user_id != user_id:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    await chat_svc.delete_session(session_id)
    await chat_svc.commit()
    return None


@router.get("/sessions/{session_id}/messages")
async def list_messages(
    session_id: str,
    user_id: str = Depends(get_current_user_id),
    chat_svc: ChatService = Depends(get_chat_service),
):
    """列出会话消息（含 parts，供前端回显历史）"""
    session = await chat_svc.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.user_id != user_id:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    return await chat_svc.list_messages_with_parts(session_id)


# ============================================================
# 流式发消息（核心 SSE）
# ============================================================


@router.post("/sessions/{session_id}/messages")
async def send_message(
    session_id: str,
    req: SendMessageRequest,
    http_request: Request,
    user_id: str = Depends(get_current_user_id),
    app_state: AppState = Depends(get_app_state),
):
    """发送消息，返回 SSE 流式响应。

    事件流（对齐 LLMEvent.type）：
    - step-start / text-start / text-delta / text-end
    - tool-input-start / tool-input-delta / tool-input-end / tool-call / tool-result / tool-error
    - step-finish / finish / provider-error
    - ping（25s 心跳）
    """
    if not _SSE_AVAILABLE:
        raise HTTPException(status_code=503, detail="sse-starlette 未安装")

    # 校验 session 归属
    async with get_db_session() as db:
        chat_svc = ChatService(db)
        session = await chat_svc.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        if session.user_id != user_id:
            raise HTTPException(status_code=403, detail="无权访问该会话")
        session_dict = _session_to_dict(session)

    return EventSourceResponse(
        _stream(req, session_dict, app_state, http_request),
        ping=15,
        send_timeout=5.0,
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


async def _stream(req: SendMessageRequest, session_dict: dict, app_state: AppState, http_request: Request):
    """SSE 流式输出（复用 playground _run_stream 三段式结构）。

    结构：queue + producer + disconnect 检查 + 25s ping + CancelledError reraise
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)

    async def producer():
        try:
            await _execute_chat(req, session_dict, app_state, queue)
        except asyncio.CancelledError:
            logger.info("chat producer 被取消 session=%s", session_dict.get("id"))
            raise
        except Exception as e:
            logger.exception("chat producer 异常 session=%s", session_dict.get("id"))
            await queue.put(_sse_event("error", {"message": str(e)}))
        finally:
            await queue.put(None)  # sentinel

    task = asyncio.create_task(producer())
    try:
        while True:
            # 断连检测
            if await http_request.is_disconnected():
                task.cancel()
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=25.0)
            except asyncio.TimeoutError:
                yield _sse_event("ping", {})
                continue
            if item is None:
                break
            yield item
    except asyncio.CancelledError:
        task.cancel()
        raise
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


async def _execute_chat(
    req: SendMessageRequest,
    session_dict: dict,
    app_state: AppState,
    queue: asyncio.Queue,
):
    """实际执行：开 db session → SessionPrompt.run_loop(event_callback=推 queue)"""
    settings = get_settings()
    session_id = session_dict["id"]
    # 重置中断标志，确保新一轮生成不被旧标志影响
    _stop_flags[session_id] = False
    async with get_db_session() as db:
        chat_svc = ChatService(db)
        # 重新加载 session ORM 对象
        session = await chat_svc.get_session(session_id)
        if session is None:
            await queue.put(_sse_event("error", {"message": "会话不存在"}))
            return

        tool_registry = ToolRegistry(
            toolkit=getattr(app_state, "toolkit", None), settings=settings
        )
        sp = SessionPrompt(
            db=db,
            chat_svc=chat_svc,
            session=session,
            settings=settings,
            tool_registry=tool_registry,
        )

        async def on_event(event: Any) -> None:
            sse = event_to_sse_dict(event)
            await queue.put(sse)

        await sp.run_loop(
            user_text=req.content,
            attachments=req.attachments,
            event_callback=on_event,
            should_stop=lambda: _stop_flags.get(session_id, False),
        )


def _sse_event(event: str, data: dict) -> Dict[str, str]:
    """构造 SSE 事件 dict"""
    return {
        "event": event,
        "data": json.dumps(data, ensure_ascii=False, default=str),
    }


def _session_to_dict(session) -> Dict[str, Any]:
    return {
        "id": session.id,
        "title": session.title,
        "model_name": session.model_name,
        "provider": session.provider,
        "agent_name": session.agent_name,
        "metadata": getattr(session, "metadata_", None) or {},
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "updated_at": session.updated_at.isoformat() if session.updated_at else None,
    }


# ============================================================
# 对话中断 / 恢复 (Interrupt / Resume)
# ============================================================


@router.post("/sessions/{session_id}/stop")
async def stop_session(
    session_id: str,
    user_id: str = Depends(get_current_user_id),
    chat_svc: ChatService = Depends(get_chat_service),
):
    """中断指定会话的流式生成。

    设置 _stop_flags[session_id] = True，run_loop 在下次迭代 / token 检查时
    会读取该标志并中断生成，同时 yield 一个 interrupted 事件。
    不需要复杂实现，只设置标志位让 run_loop 自行检查。
    """
    session = await chat_svc.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.user_id != user_id:
        raise HTTPException(status_code=403, detail="无权操作该会话")

    _stop_flags[session_id] = True
    return {"stopped": True}


@router.get("/sessions/{session_id}/resume")
async def resume_session(
    session_id: str,
    http_request: Request,
    user_id: str = Depends(get_current_user_id),
    app_state: AppState = Depends(get_app_state),
):
    """恢复被中断的对话流（SSE 流式响应）。

    重新调用 run_loop 但不添加新用户消息（resume=True），
    从 DB 中已有的历史消息继续上次中断的对话。
    """
    if not _SSE_AVAILABLE:
        raise HTTPException(status_code=503, detail="sse-starlette 未安装")

    # 校验 session 归属
    async with get_db_session() as db:
        chat_svc = ChatService(db)
        session = await chat_svc.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        if session.user_id != user_id:
            raise HTTPException(status_code=403, detail="无权访问该会话")
        session_dict = _session_to_dict(session)

    return EventSourceResponse(
        _stream_resume(session_dict, app_state, http_request),
        ping=15,
        send_timeout=5.0,
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


async def _stream_resume(
    session_dict: dict, app_state: AppState, http_request: Request
):
    """SSE 流式输出（恢复中断的对话，复用 _stream 的三段式结构）。"""
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)

    async def producer():
        try:
            await _execute_resume(session_dict, app_state, queue)
        except asyncio.CancelledError:
            logger.info("resume producer 被取消 session=%s", session_dict.get("id"))
            raise
        except Exception as e:
            logger.exception("resume producer 异常 session=%s", session_dict.get("id"))
            await queue.put(_sse_event("error", {"message": str(e)}))
        finally:
            await queue.put(None)  # sentinel

    task = asyncio.create_task(producer())
    try:
        while True:
            # 断连检测
            if await http_request.is_disconnected():
                task.cancel()
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=25.0)
            except asyncio.TimeoutError:
                yield _sse_event("ping", {})
                continue
            if item is None:
                break
            yield item
    except asyncio.CancelledError:
        task.cancel()
        raise
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


async def _execute_resume(
    session_dict: dict,
    app_state: AppState,
    queue: asyncio.Queue,
):
    """恢复执行：run_loop(resume=True)，不添加新用户消息，继续上次中断的对话。"""
    settings = get_settings()
    session_id = session_dict["id"]
    # 重置中断标志，确保恢复后不被旧标志影响
    _stop_flags[session_id] = False
    async with get_db_session() as db:
        chat_svc = ChatService(db)
        session = await chat_svc.get_session(session_id)
        if session is None:
            await queue.put(_sse_event("error", {"message": "会话不存在"}))
            return

        tool_registry = ToolRegistry(
            toolkit=getattr(app_state, "toolkit", None), settings=settings
        )
        sp = SessionPrompt(
            db=db,
            chat_svc=chat_svc,
            session=session,
            settings=settings,
            tool_registry=tool_registry,
        )

        async def on_event(event: Any) -> None:
            sse = event_to_sse_dict(event)
            await queue.put(sse)

        await sp.run_loop(
            user_text="",
            event_callback=on_event,
            should_stop=lambda: _stop_flags.get(session_id, False),
            resume=True,
        )


# ============================================================
# 新增端点（regenerate / feedback / delete-message / auto-title / search）
# ============================================================


class FeedbackRequest(BaseModel):
    """消息反馈请求体"""

    rating: Optional[str] = Field(default=None, pattern=r"^(like|dislike)$")
    comment: str = Field(default="", max_length=2000)


def _message_to_dict(msg) -> Dict[str, Any]:
    """将 ChatMessage ORM 对象转为 dict（含 metadata_ 字段）。"""
    return {
        "id": msg.id,
        "session_id": msg.session_id,
        "role": msg.role,
        "parent_id": msg.parent_id,
        "model_id": msg.model_id,
        "provider_id": msg.provider_id,
        "tokens": msg.tokens,
        "cost": msg.cost,
        "finish_reason": msg.finish_reason,
        "error": msg.error,
        "metadata": msg.metadata_,
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
        "completed_at": msg.completed_at.isoformat() if msg.completed_at else None,
    }


async def _get_user_message_text(chat_svc: ChatService, message_id: str) -> Optional[str]:
    """取出指定 message 的所有 text part 并拼接为完整文本。"""
    parts = await chat_svc.list_parts_by_message(message_id)
    text_parts = [p for p in parts if p.type == "text"]
    content = "\n".join(p.text for p in text_parts if p.text)
    return content or None


# ============================================================
# 1. POST /sessions/{session_id}/regenerate — 重新生成最后一条 assistant 消息
# ============================================================


@router.post("/sessions/{session_id}/regenerate")
async def regenerate_message(
    session_id: str,
    http_request: Request,
    user_id: str = Depends(get_current_user_id),
    app_state: AppState = Depends(get_app_state),
):
    """重新生成最后一条 assistant 消息（SSE 流式响应）。

    流程：
    1. 校验 session 归属
    2. 删除最后一条 assistant message（及其 parts，通过 cascade）
    3. 取倒数第二条 user message 的 content（即触发该 assistant 回复的用户输入）
    4. 同时删除该 user message（run_loop 会重新创建，避免历史重复）
    5. 复用 _stream + _execute_chat 模式重新执行 SessionPrompt.run_loop
    """
    if not _SSE_AVAILABLE:
        raise HTTPException(status_code=503, detail="sse-starlette 未安装")

    async with get_db_session() as db:
        chat_svc = ChatService(db)
        session = await chat_svc.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        if session.user_id != user_id:
            raise HTTPException(status_code=403, detail="无权访问该会话")

        messages = await chat_svc.list_messages(session_id)
        if not messages:
            raise HTTPException(status_code=400, detail="会话无消息，无法重新生成")

        # 从末尾向前找最后一条 assistant message 及其前一条 user message
        last_assistant = None
        last_user_msg = None
        for msg in reversed(messages):
            if last_assistant is None:
                if msg.role == "assistant":
                    last_assistant = msg
                    continue
                # 最后一条不是 assistant，无法重新生成
                break
            else:
                if msg.role == "user":
                    last_user_msg = msg
                    break

        if last_assistant is None:
            raise HTTPException(status_code=400, detail="无 assistant 消息可重新生成")
        if last_user_msg is None:
            raise HTTPException(status_code=400, detail="找不到对应的 user 消息")

        # 取 user message 的 text content
        user_text = await _get_user_message_text(chat_svc, last_user_msg.id)
        if not user_text:
            raise HTTPException(status_code=400, detail="user 消息无文本内容")

        # 删除最后一条 assistant message（cascade 删除其 parts）
        await db.delete(last_assistant)
        # 删除最后一条 user message（run_loop 会重新创建，避免历史重复）
        await db.delete(last_user_msg)
        await chat_svc.commit()

        session_dict = _session_to_dict(session)

    # 复用现有的 _stream + _execute_chat 模式
    req = SendMessageRequest(content=user_text)
    return EventSourceResponse(
        _stream(req, session_dict, app_state, http_request),
        ping=15,
        send_timeout=5.0,
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


# ============================================================
# 2. POST /sessions/{session_id}/messages/{message_id}/feedback — 消息反馈
# ============================================================


@router.post("/sessions/{session_id}/messages/{message_id}/feedback")
async def feedback_message(
    session_id: str,
    message_id: str,
    req: FeedbackRequest,
    user_id: str = Depends(get_current_user_id),
    chat_svc: ChatService = Depends(get_chat_service),
):
    """对消息进行点赞/点踩反馈。

    将 rating + comment 存入 ChatMessage.metadata_ JSON 字段（key: "feedback"）。
    rating 可选值: "like" / "dislike" / null（null 表示取消反馈）。
    """
    from sqlalchemy import select

    from models.chat_models import ChatMessage

    session = await chat_svc.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.user_id != user_id:
        raise HTTPException(status_code=403, detail="无权访问该会话")

    stmt = select(ChatMessage).where(
        ChatMessage.id == message_id,
        ChatMessage.session_id == session_id,
    )
    result = await chat_svc.db.execute(stmt)
    msg = result.scalar_one_or_none()
    if msg is None:
        raise HTTPException(status_code=404, detail="消息不存在")

    # 直接操作 ChatMessage.metadata_ 字段
    metadata = dict(msg.metadata_ or {})
    metadata["feedback"] = {
        "rating": req.rating,
        "comment": req.comment,
    }
    msg.metadata_ = metadata
    await chat_svc.commit()

    return _message_to_dict(msg)


# ============================================================
# 3. DELETE /sessions/{session_id}/messages/{message_id} — 删除消息
# ============================================================


@router.delete(
    "/sessions/{session_id}/messages/{message_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_message(
    session_id: str,
    message_id: str,
    user_id: str = Depends(get_current_user_id),
    chat_svc: ChatService = Depends(get_chat_service),
):
    """删除指定消息及其 parts（用于编辑用户消息时截断历史）。

    利用 ChatPart → ChatMessage 的 ondelete=CASCADE 外键约束，
    删除 message 后其关联 parts 会被自动级联删除。
    """
    from sqlalchemy import select

    from models.chat_models import ChatMessage

    session = await chat_svc.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.user_id != user_id:
        raise HTTPException(status_code=403, detail="无权访问该会话")

    stmt = select(ChatMessage).where(
        ChatMessage.id == message_id,
        ChatMessage.session_id == session_id,
    )
    result = await chat_svc.db.execute(stmt)
    msg = result.scalar_one_or_none()
    if msg is None:
        raise HTTPException(status_code=404, detail="消息不存在")

    await chat_svc.db.delete(msg)  # cascade 删除关联 parts
    await chat_svc.commit()
    return None


# ============================================================
# 4. POST /sessions/{session_id}/auto-title — 自动生成会话标题
# ============================================================


@router.post("/sessions/{session_id}/auto-title")
async def auto_title(
    session_id: str,
    user_id: str = Depends(get_current_user_id),
    chat_svc: ChatService = Depends(get_chat_service),
):
    """自动生成会话标题。

    流程：
    1. 获取会话前两条消息（user + assistant）
    2. 调用 LLM 生成简短标题（10 字以内）
    3. 更新 session.title
    4. 返回 {title: "新标题"}

    使用 provider_resolver.get_provider_for_model 获取 provider，
    调用非流式 chat_completion。如果 LLM 调用失败，回退到截取用户消息前 20 字。
    """
    from core.provider_resolver import get_provider_for_model
    from core.providers.base import ChatMessage as ProviderChatMessage

    session = await chat_svc.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.user_id != user_id:
        raise HTTPException(status_code=403, detail="无权访问该会话")

    messages = await chat_svc.list_messages(session_id)
    if not messages:
        raise HTTPException(status_code=400, detail="会话无消息，无法生成标题")

    # 获取前两条消息（第一条 user + 第一条 assistant）
    first_user = None
    first_assistant = None
    for msg in messages:
        if first_user is None and msg.role == "user":
            first_user = msg
        elif first_user is not None and msg.role == "assistant" and first_assistant is None:
            first_assistant = msg
            break

    if first_user is None:
        raise HTTPException(status_code=400, detail="无 user 消息，无法生成标题")

    # 获取 user 文本
    user_text = await _get_user_message_text(chat_svc, first_user.id)
    if not user_text:
        user_text = ""

    # 获取 assistant 文本（可选）
    assistant_text = ""
    if first_assistant is not None:
        assistant_text = await _get_user_message_text(chat_svc, first_assistant.id) or ""

    # 尝试调用 LLM 生成标题
    title: Optional[str] = None
    try:
        provider = await get_provider_for_model(session.model_name)
        if provider is not None:
            prompt_messages = [
                ProviderChatMessage(
                    role="system",
                    content=(
                        "你是一个标题生成器。根据以下对话内容生成一个简短的中文标题"
                        "（10字以内，不要加引号、不要加标点符号）。只返回标题文字本身。"
                    ),
                ),
                ProviderChatMessage(
                    role="user",
                    content=(
                        f"用户消息: {user_text[:1000]}\n\n"
                        f"助手回复: {assistant_text[:1000] if assistant_text else '(无回复)'}"
                    ),
                ),
            ]
            completion = await provider.chat_completion(messages=prompt_messages)
            if completion and completion.content:
                title = completion.content.strip()[:10]
                if not title:
                    title = None
    except Exception as e:
        logger.warning("auto-title LLM 调用失败，将回退到截取用户消息: %s", e, exc_info=True)
        title = None

    # 回退：截取用户消息前 20 字
    if not title:
        title = user_text[:20].strip()
    if not title:
        title = "新对话"

    # 更新 session title
    updated = await chat_svc.update_session_title(session_id, title)
    await chat_svc.commit()

    return {"title": updated.title}


# ============================================================
# 5. GET /sessions/search — 搜索当前用户的会话（按 title 模糊匹配）
# ============================================================


@router.get("/sessions/search")
async def search_sessions(
    q: str,
    user_id: str = Depends(get_current_user_id),
    chat_svc: ChatService = Depends(get_chat_service),
):
    """按标题模糊搜索当前用户的会话。

    通过 query 参数 q 传入关键词，返回 title 中包含该关键词的会话列表。
    搜索不区分大小写。q 为空时返回全部会话。
    """
    sessions = await chat_svc.list_sessions(user_id)
    if not q:
        return [_session_to_dict(s) for s in sessions]
    q_lower = q.lower()
    matched = [s for s in sessions if q_lower in (s.title or "").lower()]
    return [_session_to_dict(s) for s in matched]


# ============================================================
# 6. POST /sessions/{session_id}/share — 生成分享链接
# ============================================================


@router.post("/sessions/{session_id}/share")
async def share_session(
    session_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
    chat_svc: ChatService = Depends(get_chat_service),
):
    """生成会话分享链接。

    流程：
    1. 校验 session 归属
    2. 生成 share_id (uuid4 hex)，存入 session.metadata_["share_id"]
       （若已存在 share_id 则复用，不重复生成）
    3. 返回 {share_url, share_id}

    分享链接对应 GET /sessions/shared/{share_id}，无需认证即可只读访问。
    """
    session = await chat_svc.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.user_id != user_id:
        raise HTTPException(status_code=403, detail="无权分享该会话")

    metadata = dict(session.metadata_ or {})
    share_id = metadata.get("share_id")
    if not share_id:
        share_id = uuid.uuid4().hex
        metadata["share_id"] = share_id
        session.metadata_ = metadata
        await chat_svc.commit()

    # 拼接前端可访问的分享 URL（同源相对路径）
    base_url = str(request.base_url).rstrip("/")
    share_url = f"{base_url}/chat/shared/{share_id}"

    return {"share_url": share_url, "share_id": share_id}


# ============================================================
# 7. GET /sessions/shared/{share_id} — 通过 share_id 只读访问会话
# ============================================================


@router.get("/sessions/shared/{share_id}")
async def get_shared_session(
    share_id: str,
    chat_svc: ChatService = Depends(get_chat_service),
):
    """通过 share_id 只读访问会话内容（无需认证）。

    流程：
    1. 跨所有租户扫描 chat_sessions.metadata 中 share_id 字段匹配
    2. 返回会话基本信息 + 所有消息（含 parts），不含用户/租户敏感字段

    为安全考虑：
    - 不返回 user_id / tenant_id
    - 仅返回 role / parts / created_at 等会话内容字段
    """
    if not share_id:
        raise HTTPException(status_code=400, detail="share_id 不能为空")

    # 通过 JSON 字段扫描匹配 share_id（SQLite/PostgreSQL JSON 兼容写法：
    # 用 Python 端过滤避免方言差异，share_id 数量级通常很小）
    stmt = select(ChatSession)
    result = await chat_svc.db.execute(stmt)
    sessions = list(result.scalars().all())
    target = None
    for s in sessions:
        meta = getattr(s, "metadata_", None) or {}
        if meta.get("share_id") == share_id:
            target = s
            break

    if target is None:
        raise HTTPException(status_code=404, detail="分享链接无效或已被撤销")

    # 取消息与 parts（复用 chat_service 的组装逻辑）
    messages_with_parts = await chat_svc.list_messages_with_parts(target.id)

    return {
        "session": {
            "id": target.id,
            "title": target.title,
            "model_name": target.model_name,
            "created_at": target.created_at.isoformat() if target.created_at else None,
            "updated_at": target.updated_at.isoformat() if target.updated_at else None,
        },
        "messages": messages_with_parts,
    }


# ============================================================
# 8. POST /sessions/{session_id}/fork — 从某条消息分叉新会话
# ============================================================


class ForkRequest(BaseModel):
    """对话分叉请求体"""

    from_message_id: str = Field(..., description="从哪条消息分叉（包含该消息）")
    title: Optional[str] = Field(default=None, max_length=256)


@router.post("/sessions/{session_id}/fork", status_code=status.HTTP_201_CREATED)
async def fork_session(
    session_id: str,
    req: ForkRequest,
    user_id: str = Depends(get_current_user_id),
    chat_svc: ChatService = Depends(get_chat_service),
):
    """从指定消息处分叉出新会话。

    流程：
    1. 校验原 session 归属
    2. 取出原 session 中从开始到 from_message_id（含）的所有消息及其 parts
    3. 创建新会话（标题默认 "{原标题} (Fork)"，model_name 沿用原会话）
    4. 复制消息与 parts 到新会话（重新分配 ID，保持 sequence）
    5. 在新会话的 metadata_ 中记录 fork 来源 {source_session_id, from_message_id}
    6. 返回新会话

    Returns:
        新会话 dict（包含 id 等字段，前端可直接 selectSession 切换）
    """
    session = await chat_svc.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.user_id != user_id:
        raise HTTPException(status_code=403, detail="无权操作该会话")

    # 取原会话所有消息（按时间升序）
    messages = await chat_svc.list_messages(session_id)
    if not messages:
        raise HTTPException(status_code=400, detail="原会话无消息，无法分叉")

    # 找到 from_message_id 索引
    fork_idx = None
    for i, m in enumerate(messages):
        if m.id == req.from_message_id:
            fork_idx = i
            break
    if fork_idx is None:
        raise HTTPException(status_code=404, detail="from_message_id 不存在")
    if fork_idx < 0:
        raise HTTPException(status_code=400, detail="分叉点无效")

    # 截取要复制的消息
    messages_to_copy = messages[: fork_idx + 1]

    # 创建新会话
    new_title = req.title or f"{session.title} (Fork)"
    new_session = await chat_svc.create_session(
        user_id=user_id,
        title=new_title,
        model_name=session.model_name,
        agent_name=session.agent_name,
    )
    # 记录 fork 来源
    new_meta = dict(new_session.metadata_ or {}) if new_session.metadata_ else {}
    new_meta["fork_from"] = {
        "source_session_id": session.id,
        "from_message_id": req.from_message_id,
    }
    new_session.metadata_ = new_meta
    await chat_svc.db.flush()

    # 批量取这些消息的 parts（按 message_id 分组）
    msg_ids = [m.id for m in messages_to_copy]
    stmt_parts = (
        select(ChatPart)
        .where(ChatPart.message_id.in_(msg_ids))
        .order_by(ChatPart.message_id.asc(), ChatPart.sequence.asc())
    )
    parts_result = await chat_svc.db.execute(stmt_parts)
    parts_by_msg: Dict[str, List[ChatPart]] = {}
    for p in parts_result.scalars().all():
        parts_by_msg.setdefault(p.message_id, []).append(p)

    # 逐条复制消息 + parts
    for m in messages_to_copy:
        new_msg = await chat_svc.create_message(
            session_id=new_session.id,
            role=m.role,
            parent_id=m.parent_id,
            model_id=m.model_id,
            provider_id=m.provider_id,
        )
        # 复制 parts
        for p in parts_by_msg.get(m.id, []):
            await chat_svc.create_part(
                message_id=new_msg.id,
                session_id=new_session.id,
                part_type=p.type,
                sequence=p.sequence,
                text=p.text,
                tool_name=p.tool_name,
                tool_call_id=p.tool_call_id,
                tool_state=p.tool_state,
                step_index=p.step_index,
                metadata=p.metadata_,
            )

    await chat_svc.commit()
    # 重新刷新以拿到最新 metadata_
    await chat_svc.db.refresh(new_session)
    return _session_to_dict(new_session)


# ============================================================
# 9. GET/PUT /sessions/{session_id}/tools — 会话工具配置
# ============================================================


class UpdateToolsConfigRequest(BaseModel):
    """更新会话工具配置请求体

    tools_config 为工具名到启用状态的映射。
    PUT 采用合并语义：仅更新传入的 key，未传入的保持不变。
    示例: {"tools_config": {"calculator": true, "bash": false}}
    """

    tools_config: Dict[str, bool] = Field(
        default_factory=dict,
        description="工具名到启用状态的映射",
    )


@router.get("/sessions/{session_id}/tools")
async def get_session_tools(
    session_id: str,
    user_id: str = Depends(get_current_user_id),
    chat_svc: ChatService = Depends(get_chat_service),
    app_state: AppState = Depends(get_app_state),
):
    """获取会话可用工具列表及当前启用/禁用配置。

    返回结构：
    - tools: 所有可用工具（含 name / description / enabled）
    - config: 存储在 session.metadata_["tools_config"] 的配置字典
    - 未在 config 中显式配置的工具默认 enabled=True
    """
    session = await chat_svc.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.user_id != user_id:
        raise HTTPException(status_code=403, detail="无权访问该会话")

    # 读取会话工具配置
    metadata = dict(session.metadata_ or {})
    tools_config = metadata.get("tools_config", {})
    if not isinstance(tools_config, dict):
        tools_config = {}

    # 解析所有可用工具
    settings = get_settings()
    tool_registry = ToolRegistry(
        toolkit=getattr(app_state, "toolkit", None), settings=settings
    )
    try:
        tools = await tool_registry.resolve()
    except Exception as e:
        logger.warning("resolve tools 失败: %s", e, exc_info=True)
        tools = []

    tools_list = []
    for t in tools:
        name = getattr(t, "name", None)
        description = getattr(t, "description", "") or ""
        enabled = tools_config.get(name, True)
        tools_list.append(
            {
                "name": name,
                "description": description,
                "enabled": enabled,
            }
        )

    return {
        "session_id": session_id,
        "tools": tools_list,
        "config": tools_config,
    }


@router.put("/sessions/{session_id}/tools")
async def update_session_tools(
    session_id: str,
    req: UpdateToolsConfigRequest,
    user_id: str = Depends(get_current_user_id),
    chat_svc: ChatService = Depends(get_chat_service),
):
    """更新会话工具配置（启用/禁用特定工具）。

    采用合并语义：将 req.tools_config 合并到 session.metadata_["tools_config"]。
    未传入的工具保持原配置不变。

    配置存储在 ChatSession.metadata_ JSON 列中，run_loop 在构建工具 schema 时
    会读取该配置过滤掉被禁用的工具。
    """
    session = await chat_svc.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.user_id != user_id:
        raise HTTPException(status_code=403, detail="无权访问该会话")

    metadata = dict(session.metadata_ or {})
    existing_config = metadata.get("tools_config", {})
    if not isinstance(existing_config, dict):
        existing_config = {}
    # 合并新配置
    existing_config.update(req.tools_config)
    metadata["tools_config"] = existing_config
    session.metadata_ = metadata
    await chat_svc.commit()

    return {
        "session_id": session_id,
        "config": existing_config,
    }


# ============================================================
# 路由重排：将 /sessions/search 移到 /sessions/{session_id} 之前
# 避免 path 参数 {session_id} 遮蔽静态路由 /sessions/search
# （Starlette 按注册顺序匹配，先注册的 {session_id} 会吞掉 "search"）
# ============================================================


def _reorder_search_route_before_session_id() -> None:
    """将 /sessions/search 路由移到 /sessions/{session_id} 路由之前。

    此操作不修改任何已有端点代码，仅在所有路由注册完成后对 router.routes
    列表做一次重排，确保 GET /sessions/search 不被 GET /sessions/{session_id} 遮蔽。
    """
    routes = router.routes
    search_idx: Optional[int] = None
    session_id_idx: Optional[int] = None
    for i, r in enumerate(routes):
        path = getattr(r, "path", "")
        if path.endswith("/sessions/search") and search_idx is None:
            search_idx = i
        if path.endswith("/sessions/{session_id}") and session_id_idx is None:
            session_id_idx = i
    if (
        search_idx is not None
        and session_id_idx is not None
        and search_idx > session_id_idx
    ):
        search_route = routes.pop(search_idx)
        routes.insert(session_id_idx, search_route)


_reorder_search_route_before_session_id()
