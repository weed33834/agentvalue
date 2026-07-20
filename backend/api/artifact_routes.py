"""
Artifact API Router - 对标 Claude Artifacts / ChatGPT Canvas

端点:
- POST /api/v1/artifacts  创建 artifact
- GET /api/v1/artifacts/session/{session_id}  列出会话所有 artifacts
- GET /api/v1/artifacts/{id}  获取 artifact 详情
- PUT /api/v1/artifacts/{id}  更新 artifact 内容 (version +1)
- DELETE /api/v1/artifacts/{id}  删除
- POST /api/v1/artifacts/{id}/fork  复制 artifact 到新版本
- POST /api/v1/artifacts/extract  从消息文本中提取代码块作为 artifacts

事务边界由路由层控制, 使用 get_db_session 管理 DB 会话。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from core.database import get_db_session
from models.artifact import Artifact

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/artifacts", tags=["artifacts"])

# artifact 类型常量
ARTIFACT_TYPE_HTML = "html"
ARTIFACT_TYPE_SVG = "svg"
ARTIFACT_TYPE_MERMAID = "mermaid"
ARTIFACT_TYPE_MARKDOWN = "markdown"
ARTIFACT_TYPE_CODE = "code"
ARTIFACT_TYPE_REACT = "react"
ARTIFACT_TYPE_JSON = "json"

_VALID_TYPES = {
    ARTIFACT_TYPE_HTML,
    ARTIFACT_TYPE_SVG,
    ARTIFACT_TYPE_MERMAID,
    ARTIFACT_TYPE_MARKDOWN,
    ARTIFACT_TYPE_CODE,
    ARTIFACT_TYPE_REACT,
    ARTIFACT_TYPE_JSON,
}

# 内容长度上限
_MAX_CONTENT_LENGTH = 200000
_MAX_NAME_LENGTH = 256

# 匹配 markdown 围栏代码块: ```lang\n...```
_CODE_FENCE_RE = re.compile(
    r"```([a-zA-Z0-9_+\-]*)\n([\s\S]*?)```",
    re.MULTILINE,
)


# ---------------- Schemas ----------------


class CreateArtifactPayload(BaseModel):
    """创建 artifact 请求体"""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., max_length=64)
    message_id: Optional[str] = Field(default=None, max_length=64)
    name: Optional[str] = Field(default=None, max_length=_MAX_NAME_LENGTH)
    artifact_type: str = Field(..., min_length=1, max_length=32)
    language: Optional[str] = Field(default=None, max_length=32)
    content: str = Field(..., min_length=1, max_length=_MAX_CONTENT_LENGTH)
    metadata_: Optional[Dict[str, Any]] = Field(default=None)


class UpdateArtifactPayload(BaseModel):
    """更新 artifact 请求体 (version 自动 +1)"""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, max_length=_MAX_NAME_LENGTH)
    content: Optional[str] = Field(
        default=None, min_length=1, max_length=_MAX_CONTENT_LENGTH
    )
    language: Optional[str] = Field(default=None, max_length=32)
    metadata_: Optional[Dict[str, Any]] = None


class ExtractPayload(BaseModel):
    """从文本提取代码块请求体"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=_MAX_CONTENT_LENGTH * 5)


# ---------------- Helpers ----------------


def _serialize(art: Artifact) -> Dict[str, Any]:
    """序列化 Artifact 为 dict"""
    return {
        "id": art.id,
        "session_id": art.session_id,
        "message_id": art.message_id,
        "name": art.name,
        "artifact_type": art.artifact_type,
        "language": art.language,
        "content": art.content,
        "metadata": art.metadata_ or {},
        "version": art.version,
        "created_at": art.created_at.isoformat() if art.created_at else None,
        "updated_at": art.updated_at.isoformat() if art.updated_at else None,
    }


async def _load_or_404(art_id: int, db) -> Artifact:
    """按 id 加载 artifact, 不存在则 404"""
    stmt = select(Artifact).where(Artifact.id == art_id)
    obj = (await db.execute(stmt)).scalar_one_or_none()
    if obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Artifact 不存在"
        )
    return obj


def _detect_type(lang: str, content: str) -> Tuple[str, Optional[str]]:
    """根据语言标签和内容识别 artifact 类型与语言。

    返回 (artifact_type, language)
    """
    lang_lower = (lang or "").strip().lower()
    head = content.lstrip().lower()

    # 显式语言标签优先
    if lang_lower == "mermaid":
        return ARTIFACT_TYPE_MERMAID, "mermaid"
    if lang_lower in ("markdown", "md"):
        return ARTIFACT_TYPE_MARKDOWN, "markdown"
    if lang_lower == "json":
        return ARTIFACT_TYPE_JSON, "json"
    if lang_lower in ("react", "jsx", "tsx"):
        return ARTIFACT_TYPE_REACT, lang_lower
    if lang_lower == "svg":
        return ARTIFACT_TYPE_SVG, "svg"
    if lang_lower == "html":
        return ARTIFACT_TYPE_HTML, "html"

    # 内容检测: SVG (以 <svg 开头或前 200 字符含 <svg)
    if head.startswith("<svg") or "<svg" in content[:200]:
        return ARTIFACT_TYPE_SVG, lang_lower or "svg"
    # 内容检测: HTML (含 <!doctype html / <html / <body)
    if (
        head.startswith("<!doctype html")
        or head.startswith("<html")
        or "<html" in content[:200]
        or "<body" in content[:200]
    ):
        return ARTIFACT_TYPE_HTML, lang_lower or "html"

    # 默认: 代码
    return ARTIFACT_TYPE_CODE, lang_lower or None


def _extract_from_text(text: str) -> List[Dict[str, Any]]:
    """从 markdown 文本中提取代码块, 返回 artifact dict 列表 (不入库)"""
    items: List[Dict[str, Any]] = []
    for idx, m in enumerate(_CODE_FENCE_RE.finditer(text)):
        lang = m.group(1) or ""
        content = m.group(2)
        artifact_type, language = _detect_type(lang, content)
        items.append(
            {
                "name": f"{artifact_type}-{idx + 1}",
                "artifact_type": artifact_type,
                "language": language,
                "content": content,
            }
        )
    return items


# ---------------- Endpoints ----------------


@router.post("")
async def create_artifact(payload: CreateArtifactPayload):
    """创建 artifact

    - artifact_type 必须为合法类型
    - 创建后 version=1
    """
    if payload.artifact_type not in _VALID_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无效的 artifact 类型: {payload.artifact_type}",
        )
    async with get_db_session() as db:
        art = Artifact(
            session_id=payload.session_id,
            message_id=payload.message_id,
            name=payload.name,
            artifact_type=payload.artifact_type,
            language=payload.language,
            content=payload.content,
            metadata_=payload.metadata_ or {},
            version=1,
        )
        db.add(art)
        await db.commit()
        return _serialize(art)


@router.post("/extract")
async def extract_artifacts(payload: ExtractPayload):
    """从消息文本中提取代码块作为 artifacts (仅解析, 不入库)

    - 解析 markdown 围栏代码块: ```language\\n...```
    - 识别 HTML(含 <html/<svg 标签) / SVG / Mermaid(```mermaid) / 代码 / JSON
    - 返回提取的 artifact 列表
    """
    items = _extract_from_text(payload.text)
    return {"items": items, "total": len(items)}


@router.get("/session/{session_id}")
async def list_by_session(session_id: str):
    """列出会话下所有 artifacts (按创建时间升序)"""
    async with get_db_session() as db:
        stmt = (
            select(Artifact)
            .where(Artifact.session_id == session_id)
            .order_by(Artifact.created_at.asc())
        )
        rows = (await db.execute(stmt)).scalars().all()
        return {"items": [_serialize(a) for a in rows], "total": len(rows)}


@router.get("/{art_id}")
async def get_artifact(art_id: int):
    """获取 artifact 详情"""
    async with get_db_session() as db:
        art = await _load_or_404(art_id, db)
        return _serialize(art)


@router.put("/{art_id}")
async def update_artifact(art_id: int, payload: UpdateArtifactPayload):
    """更新 artifact 内容, version 自动 +1

    - 仅更新请求中提供的字段
    """
    async with get_db_session() as db:
        art = await _load_or_404(art_id, db)
        if payload.name is not None:
            art.name = payload.name
        if payload.content is not None:
            art.content = payload.content
        if payload.language is not None:
            art.language = payload.language
        if payload.metadata_ is not None:
            art.metadata_ = payload.metadata_
        art.version = (art.version or 1) + 1
        art.updated_at = datetime.utcnow()
        await db.commit()
        return _serialize(art)


@router.delete("/{art_id}")
async def delete_artifact(art_id: int):
    """删除 artifact"""
    async with get_db_session() as db:
        art = await _load_or_404(art_id, db)
        await db.delete(art)
        await db.commit()
        return {"deleted": True, "id": art_id}


@router.post("/{art_id}/fork")
async def fork_artifact(art_id: int):
    """复制 artifact 到新版本 (创建一条新的 version=1 记录)

    - 保留原 session_id / message_id / content / type
    - name 加 "(fork)" 后缀, metadata 记录 forked_from
    """
    async with get_db_session() as db:
        art = await _load_or_404(art_id, db)
        meta = dict(art.metadata_ or {})
        meta["forked_from"] = art.id
        forked = Artifact(
            session_id=art.session_id,
            message_id=art.message_id,
            name=(art.name or "Artifact") + " (fork)",
            artifact_type=art.artifact_type,
            language=art.language,
            content=art.content,
            metadata_=meta,
            version=1,
        )
        db.add(forked)
        await db.commit()
        return _serialize(forked)
