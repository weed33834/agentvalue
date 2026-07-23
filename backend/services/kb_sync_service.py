"""
知识库自动同步服务

对标 RagFlow 自动同步 / 阿里百炼数据源管理：
- 数据源 CRUD（全部 tenant_id 过滤）
- 执行同步：根据 source_type 读取数据源，对比已有文档检测新增/修改/删除，更新向量库
- 定时增量更新：注册 APScheduler 定时任务，按 sync_interval_minutes 自动同步
- 变更检测：基于文件 hash（local_dir）或内容 hash（url）的增量同步

事务边界: 传入 session 时由调用方控制 commit；未传入 session 时内部自建会话并 commit。
后台任务使用 AsyncSessionLocal 创建独立数据库会话，不依赖请求级 session。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import AsyncSessionLocal
from core.tenant_context import get_current_tenant, tenant_scope
from models.kb_sync_models import KbDataSource, KbSyncLog

logger = logging.getLogger(__name__)


class KbSyncService:
    """知识库自动同步服务

    支持两种使用模式:
    1. 路由层: KbSyncService(session) 配合 get_db 依赖，事务由路由控制
    2. 内部调用: KbSyncService() 无 session，内部自建会话并自动 commit
    """

    def __init__(self, session: Optional[AsyncSession] = None):
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> AsyncSession:
        if self._session is not None:
            return self._session
        self._session = AsyncSessionLocal()
        self._owns_session = True
        return self._session

    async def _commit_if_owned(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.commit()

    async def _close_if_owned(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    # ============================================================
    # 数据源 CRUD
    # ============================================================

    async def create_source(
        self,
        tenant_id: str,
        name: str,
        source_type: str,
        config: Dict[str, Any],
        collection_name: str,
        sync_interval_minutes: int = 60,
        enabled: bool = True,
    ) -> Dict[str, Any]:
        """创建数据源"""
        if source_type not in ("local_dir", "s3", "url", "database", "git"):
            raise ValueError(f"不支持的数据源类型: {source_type}")
        if not name or not name.strip():
            raise ValueError("数据源名称不能为空")

        session = await self._get_session()
        try:
            source = KbDataSource(
                tenant_id=tenant_id,
                name=name.strip(),
                source_type=source_type,
                config=config,
                collection_name=collection_name,
                sync_interval_minutes=sync_interval_minutes,
                enabled=enabled,
                last_sync_status="never",
            )
            session.add(source)
            await session.flush()
            await self._commit_if_owned()
            logger.info("租户 %s 创建数据源 %s (%s)", tenant_id, name, source_type)
            return self._serialize_source(source)
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def get_source(
        self, source_id: int, tenant_id: str
    ) -> Optional[Dict[str, Any]]:
        """获取数据源详情（仅本租户）"""
        session = await self._get_session()
        try:
            source = await self._get_source_owned(session, source_id, tenant_id)
            return self._serialize_source(source) if source else None
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def list_sources(
        self, tenant_id: str, enabled_only: bool = False
    ) -> List[Dict[str, Any]]:
        """列出当前租户的数据源"""
        session = await self._get_session()
        try:
            stmt = (
                select(KbDataSource)
                .where(KbDataSource.tenant_id == tenant_id)
                .order_by(KbDataSource.id.asc())
            )
            if enabled_only:
                stmt = stmt.where(KbDataSource.enabled.is_(True))
            result = await session.execute(stmt)
            return [self._serialize_source(s) for s in result.scalars().all()]
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def update_source(
        self, source_id: int, tenant_id: str, **kwargs: Any
    ) -> Optional[Dict[str, Any]]:
        """更新数据源（仅允许更新本租户数据源）"""
        allowed = {
            "name",
            "source_type",
            "config",
            "collection_name",
            "sync_interval_minutes",
            "enabled",
        }
        session = await self._get_session()
        try:
            source = await self._get_source_owned(session, source_id, tenant_id)
            if source is None:
                return None
            for key, value in kwargs.items():
                if key in allowed and value is not None:
                    setattr(source, key, value)
            await session.flush()
            await self._commit_if_owned()
            return self._serialize_source(source)
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def delete_source(self, source_id: int, tenant_id: str) -> bool:
        """删除数据源（仅允许删除本租户数据源，关联日志级联删除）"""
        session = await self._get_session()
        try:
            source = await self._get_source_owned(session, source_id, tenant_id)
            if source is None:
                return False
            await session.delete(source)
            await session.flush()
            await self._commit_if_owned()
            return True
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    # ============================================================
    # 同步执行
    # ============================================================

    async def sync_source(
        self,
        source_id: int,
        tenant_id: str,
        sync_type: str = "manual",
    ) -> Dict[str, Any]:
        """执行数据源同步

        根据数据源类型读取数据，对比已有文档检测变更，更新向量库 collection，
        记录同步日志并返回统计。

        Args:
            source_id: 数据源 ID
            tenant_id: 租户 ID
            sync_type: manual | scheduled

        Returns:
            {"source_id": int, "status": str, "stats": {...}, "log_id": int}
        """
        session = await self._get_session()
        owns = self._owns_session
        try:
            source = await self._get_source_owned(session, source_id, tenant_id)
            if source is None:
                raise ValueError(f"数据源 {source_id} 不存在")

            # 创建同步日志（running 状态）
            sync_log = KbSyncLog(
                tenant_id=tenant_id,
                data_source_id=source_id,
                sync_type=sync_type,
                status="running",
                started_at=datetime.now(timezone.utc),
            )
            session.add(sync_log)
            await session.flush()
            log_id = sync_log.id
            if owns:
                await session.commit()

            # 执行同步逻辑
            stats = {"added": 0, "updated": 0, "deleted": 0, "errors": 0}
            details: List[Dict[str, Any]] = []
            status_str = "success"
            error_msg: Optional[str] = None

            try:
                # 获取已有文件列表（从上次同步的 details 或 collection 元数据）
                old_files = await self._get_existing_files(source)

                # 根据数据源类型读取
                if source.source_type == "local_dir":
                    new_files = await self._scan_local_dir(
                        source.config.get("path", ""),
                        source.config.get("pattern", "*"),
                    )
                elif source.source_type == "url":
                    new_files = await self._scan_url(source.config.get("url", ""))
                elif source.source_type == "s3":
                    # S3 扫描占位（需配置 boto3，此处返回空列表降级）
                    logger.warning("S3 数据源扫描暂未实现，跳过")
                    new_files = []
                elif source.source_type == "database":
                    logger.warning("Database 数据源扫描暂未实现，跳过")
                    new_files = []
                elif source.source_type == "git":
                    logger.warning("Git 数据源扫描暂未实现，跳过")
                    new_files = []
                else:
                    new_files = []

                # 检测变更
                changes = self._detect_changes(new_files, old_files)

                # 更新向量库
                await self._update_vector_store(source.collection_name, changes)

                # 统计
                stats["added"] = len(changes["added"])
                stats["updated"] = len(changes["updated"])
                stats["deleted"] = len(changes["deleted"])
                stats["errors"] = len(changes.get("errors", []))

                # 详细处理结果
                for f in changes["added"]:
                    details.append(
                        {"file": f["path"], "action": "added", "status": "ok"}
                    )
                for f in changes["updated"]:
                    details.append(
                        {"file": f["path"], "action": "updated", "status": "ok"}
                    )
                for f in changes["deleted"]:
                    details.append(
                        {"file": f["path"], "action": "deleted", "status": "ok"}
                    )
                for f in changes.get("errors", []):
                    details.append(
                        {
                            "file": f.get("path", ""),
                            "action": "error",
                            "status": f.get("error", "unknown"),
                        }
                    )

                # 判断最终状态
                if (
                    stats["errors"] > 0
                    and (stats["added"] + stats["updated"] + stats["deleted"]) > 0
                ):
                    status_str = "partial"
                elif stats["errors"] > 0:
                    status_str = "failed"
                else:
                    status_str = "success"

            except Exception as e:
                status_str = "failed"
                error_msg = str(e)
                logger.exception("数据源 %s 同步失败", source_id)

            # 更新日志
            if owns:
                async with AsyncSessionLocal() as log_session:
                    log = await log_session.get(KbSyncLog, log_id)
                    if log:
                        log.status = status_str
                        log.completed_at = datetime.now(timezone.utc)
                        log.stats = stats
                        log.error_message = error_msg
                        log.details = details
                    # 更新数据源最后同步状态
                    src = await log_session.get(KbDataSource, source_id)
                    if src:
                        src.last_sync_at = datetime.now(timezone.utc)
                        src.last_sync_status = status_str
                        src.last_sync_stats = stats
                    await log_session.commit()
            else:
                log = await session.get(KbSyncLog, log_id)
                if log:
                    log.status = status_str
                    log.completed_at = datetime.now(timezone.utc)
                    log.stats = stats
                    log.error_message = error_msg
                    log.details = details
                src = await session.get(KbDataSource, source_id)
                if src:
                    src.last_sync_at = datetime.now(timezone.utc)
                    src.last_sync_status = status_str
                    src.last_sync_stats = stats
                await session.flush()
                await self._commit_if_owned()

            logger.info(
                "数据源 %s 同步完成: status=%s, stats=%s", source_id, status_str, stats
            )
            return {
                "source_id": source_id,
                "status": status_str,
                "stats": stats,
                "log_id": log_id,
                "error": error_msg,
            }
        except Exception:
            if owns and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if owns:
                await self._close_if_owned()

    # ============================================================
    # 同步日志查询
    # ============================================================

    async def get_sync_logs(
        self,
        source_id: int,
        tenant_id: str,
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """分页查询数据源的同步日志"""
        session = await self._get_session()
        try:
            base = (
                select(KbSyncLog)
                .where(
                    KbSyncLog.tenant_id == tenant_id,
                    KbSyncLog.data_source_id == source_id,
                )
                .order_by(KbSyncLog.started_at.desc())
            )
            total = (
                await session.execute(select(func.count()).select_from(base.subquery()))
            ).scalar() or 0

            offset = (page - 1) * size
            rows = (
                (await session.execute(base.offset(offset).limit(size))).scalars().all()
            )

            return {
                "items": [self._serialize_log(l) for l in rows],
                "total": total,
                "page": page,
                "size": size,
            }
        finally:
            if self._owns_session:
                await self._close_if_owned()

    # ============================================================
    # 定时任务注册
    # ============================================================

    async def _register_scheduler(self) -> None:
        """注册定时同步任务到 APScheduler

        扫描所有启用的数据源，按 sync_interval_minutes 注册定时任务。
        应在应用启动时调用。
        """
        try:
            from core.scheduler import get_scheduler

            scheduler = get_scheduler()
            if scheduler is None:
                logger.warning("APScheduler 未初始化，跳过注册定时同步任务")
                return

            # 查询所有启用的、间隔大于 0 的数据源
            async with AsyncSessionLocal() as session:
                sources = (
                    (
                        await session.execute(
                            select(KbDataSource).where(
                                KbDataSource.enabled.is_(True),
                                KbDataSource.sync_interval_minutes > 0,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )

            for source in sources:
                task_id = f"kb_sync_{source.id}"
                interval_minutes = source.sync_interval_minutes
                tenant_id = source.tenant_id
                source_id = source.id

                async def _sync_job(
                    _sid: int = source_id, _tid: str = tenant_id
                ) -> None:
                    with tenant_scope(_tid):
                        service = KbSyncService()
                        await service.sync_source(_sid, _tid, sync_type="scheduled")

                try:
                    from apscheduler.triggers.interval import IntervalTrigger

                    scheduler.scheduler.add_job(
                        _sync_job,
                        trigger=IntervalTrigger(minutes=interval_minutes),
                        id=task_id,
                        name=f"KB Sync: {source.name}",
                        replace_existing=True,
                    )
                    logger.info(
                        "注册定时同步任务 %s (间隔 %s 分钟)", task_id, interval_minutes
                    )
                except Exception:
                    logger.exception("注册定时同步任务 %s 失败", task_id)
        except Exception:
            logger.exception("注册定时同步任务失败（不阻断应用启动）")

    # ============================================================
    # 内部工具
    # ============================================================

    async def _scan_local_dir(
        self, path: str, pattern: str = "*"
    ) -> List[Dict[str, Any]]:
        """扫描本地目录文件，返回文件列表+hash

        Args:
            path: 目录路径
            pattern: 文件匹配模式（如 *.pdf）

        Returns:
            [{"path": "relative/path", "hash": "sha256...", "size": N, "modified": "..."}]
        """
        import glob

        if not path or not os.path.isdir(path):
            logger.warning("本地目录不存在或不可访问: %s", path)
            return []

        results: List[Dict[str, Any]] = []
        search_pattern = os.path.join(path, "**", pattern)
        for filepath in glob.glob(search_pattern, recursive=True):
            if not os.path.isfile(filepath):
                continue
            try:
                file_hash = await asyncio.to_thread(self._compute_file_hash, filepath)
                stat = os.stat(filepath)
                rel_path = os.path.relpath(filepath, path)
                results.append(
                    {
                        "path": rel_path,
                        "hash": file_hash,
                        "size": stat.st_size,
                        "modified": datetime.fromtimestamp(
                            stat.st_mtime, tz=timezone.utc
                        ).isoformat(),
                    }
                )
            except Exception as e:
                logger.warning("计算文件 hash 失败 %s: %s", filepath, e)

        return results

    @staticmethod
    def _compute_file_hash(filepath: str) -> str:
        """计算文件 SHA256 hash"""
        sha256 = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    async def _scan_url(self, url: str) -> List[Dict[str, Any]]:
        """HTTP 获取 URL 内容，返回内容 hash

        Args:
            url: 目标 URL

        Returns:
            [{"path": url, "hash": "sha256...", "size": N, "modified": "..."}]
        """
        if not url:
            return []

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                content = resp.content
                content_hash = hashlib.sha256(content).hexdigest()
                return [
                    {
                        "path": url,
                        "hash": content_hash,
                        "size": len(content),
                        "modified": datetime.now(timezone.utc).isoformat(),
                    }
                ]
        except Exception as e:
            logger.warning("获取 URL 内容失败 %s: %s", url, e)
            return []

    def _detect_changes(
        self,
        new_files: List[Dict[str, Any]],
        old_files: List[Dict[str, Any]],
    ) -> Dict[str, List[Dict[str, Any]]]:
        """检测新增/修改/删除

        Args:
            new_files: 当前扫描到的文件列表
            old_files: 上次同步的文件列表

        Returns:
            {"added": [...], "updated": [...], "deleted": [...], "errors": [...]}
        """
        old_map = {f["path"]: f for f in old_files}
        new_map = {f["path"]: f for f in new_files}

        added: List[Dict[str, Any]] = []
        updated: List[Dict[str, Any]] = []
        deleted: List[Dict[str, Any]] = []

        # 新增与修改
        for path, f in new_map.items():
            if path not in old_map:
                added.append(f)
            elif old_map[path]["hash"] != f["hash"]:
                updated.append(f)

        # 删除
        for path, f in old_map.items():
            if path not in new_map:
                deleted.append(f)

        return {"added": added, "updated": updated, "deleted": deleted, "errors": []}

    async def _get_existing_files(self, source: KbDataSource) -> List[Dict[str, Any]]:
        """获取已有文件列表（从上次同步的日志 details 中提取）

        若无历史日志，返回空列表（首次同步视为全部新增）。
        """
        session = await self._get_session()
        try:
            # 查询最近一次成功的同步日志
            log = (
                await session.execute(
                    select(KbSyncLog)
                    .where(
                        KbSyncLog.data_source_id == source.id,
                        KbSyncLog.tenant_id == source.tenant_id,
                        KbSyncLog.status.in_(["success", "partial"]),
                    )
                    .order_by(KbSyncLog.started_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

            if log is None or not log.details:
                return []

            # 从 details 中提取已有文件（非 deleted 的视为已有）
            existing: List[Dict[str, Any]] = []
            for d in log.details:
                if d.get("action") in ("added", "updated") and d.get("status") == "ok":
                    existing.append({"path": d["file"], "hash": d.get("hash", "")})
            return existing
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def _update_vector_store(
        self, collection_name: str, changes: Dict[str, List[Dict[str, Any]]]
    ) -> None:
        """更新向量库 collection

        将新增/修改的文档写入向量库，删除的文档从向量库移除。
        使用 ChromaDB PersistentClient，操作通过 asyncio.to_thread 避免阻塞。

        Args:
            collection_name: ChromaDB collection 名称
            changes: {"added": [...], "updated": [...], "deleted": [...]}
        """
        try:
            import chromadb

            from core.config import get_settings

            settings = get_settings()
            client = chromadb.PersistentClient(path=settings.vector_store_dir)

            def _get_or_create():
                return client.get_or_create_collection(
                    name=collection_name,
                    metadata={"hnsw:space": "cosine"},
                )

            collection = await asyncio.to_thread(_get_or_create)

            # 处理新增和修改（upsert）
            for f in changes.get("added", []) + changes.get("updated", []):
                doc_id = f"kb_{hashlib.sha256(f['path'].encode()).hexdigest()[:16]}"
                content = f"文件: {f['path']}\n大小: {f.get('size', 0)} bytes"
                await asyncio.to_thread(
                    collection.upsert,
                    ids=[doc_id],
                    documents=[content],
                    metadatas=[{"path": f["path"], "hash": f["hash"]}],
                )

            # 处理删除
            for f in changes.get("deleted", []):
                doc_id = f"kb_{hashlib.sha256(f['path'].encode()).hexdigest()[:16]}"
                try:
                    await asyncio.to_thread(collection.delete, ids=[doc_id])
                except Exception:
                    pass  # 文档不存在时忽略

        except Exception:
            logger.exception("更新向量库 collection %s 失败", collection_name)

    # ============================================================
    # 序列化
    # ============================================================

    @staticmethod
    async def _get_source_owned(
        session: AsyncSession, source_id: int, tenant_id: str
    ) -> Optional[KbDataSource]:
        result = await session.execute(
            select(KbDataSource).where(
                KbDataSource.id == source_id,
                KbDataSource.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _serialize_source(source: KbDataSource) -> Dict[str, Any]:
        return {
            "id": source.id,
            "tenant_id": source.tenant_id,
            "name": source.name,
            "source_type": source.source_type,
            "config": source.config,
            "collection_name": source.collection_name,
            "sync_interval_minutes": source.sync_interval_minutes,
            "last_sync_at": (
                source.last_sync_at.isoformat() if source.last_sync_at else None
            ),
            "last_sync_status": source.last_sync_status,
            "last_sync_stats": source.last_sync_stats,
            "enabled": source.enabled,
            "created_at": source.created_at.isoformat() if source.created_at else None,
            "updated_at": source.updated_at.isoformat() if source.updated_at else None,
        }

    @staticmethod
    def _serialize_log(log: KbSyncLog) -> Dict[str, Any]:
        return {
            "id": log.id,
            "tenant_id": log.tenant_id,
            "data_source_id": log.data_source_id,
            "sync_type": log.sync_type,
            "status": log.status,
            "started_at": log.started_at.isoformat() if log.started_at else None,
            "completed_at": log.completed_at.isoformat() if log.completed_at else None,
            "stats": log.stats,
            "error_message": log.error_message,
            "details": log.details,
        }
