"""DbPromptLoader: 从数据库加载 Prompt + A/B 测试 + 灰度发布。

P1 增强: 参考 Langfuse Prompt Management (https://langfuse.com/docs/prompt-management/data-model)

核心能力:
- 从 prompt_versions 表按 label 加载 Prompt
- A/B 测试: 同名 prompt 多个 label (prod-a / prod-b),按 hash(employee_id) 选版本
- 灰度发布: canary-Npct label,hash < N% 走新版本
- DB 不可达时 fallback 到文件 PromptLoader(向后兼容)
- 自动绑定 Langfuse generation: 调用方在 LLM 调用时把 prompt_version_id 写入 metadata

与现有 PromptLoader 并存:
- 文件版本用于本地开发 / DB 未启用场景
- DbPromptLoader 优先,失败 fallback 文件
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.prompt_loader import PromptLoader
from core.database import get_db_session
from core.tenant_context import get_current_tenant
from models.models import PromptLabel, PromptTemplate, PromptVersion

logger = logging.getLogger(__name__)

# 默认占位符白名单(与 PromptLoader.PLACEHOLDERS 对齐)
_DEFAULT_PLACEHOLDERS = [
    "raw_inputs",
    "employee_history",
    "company_kb",
    "employee_id",
    "period",
]


class DbPromptLoader:
    """从 DB 加载 Prompt,支持版本/Label/A/B 测试/灰度。

    用法:
        loader = DbPromptLoader()
        version = await loader.get_for_request(
            name="daily_evaluation",
            employee_id="emp-001",
        )
        rendered = loader.render(version, raw_inputs=[...], ...)

    Langfuse A/B 测试参考:
    https://langfuse.com/docs/prompt-management/features/a-b-testing
    """

    def __init__(self, file_fallback: Optional[PromptLoader] = None):
        # 文件 fallback: DB 不可达或表为空时用文件版本(向后兼容本地开发)
        self._file_loader = file_fallback or PromptLoader()
        # 进程内缓存(短期,避免每次请求都查 DB)。
        # 多副本共享缓存留 P2,Redis 实现。
        self._cache: Dict[str, Tuple[PromptVersion, float]] = {}
        self._cache_ttl = 30.0  # 30s TTL,与 health_cache 对齐

    async def get_for_request(
        self,
        name: str,
        employee_id: str = "",
        tenant_id: Optional[str] = None,
    ) -> Optional[PromptVersion]:
        """按业务请求获取 Prompt 版本,自动处理 A/B 测试与灰度。

        优先级:
        1. 检查 canary-Npct label(灰度),hash(employee_id) < N% 走灰度版本
        2. 检查 prod-a / prod-b label(A/B 测试),hash(employee_id) % 2 选版本
        3. 默认 production label
        4. 都没有时 fallback 到 latest(最新版本)
        5. DB 全部失败时返回 None,调用方应 fallback 文件 loader

        Args:
            name: Prompt 模板名(如 "daily_evaluation")
            employee_id: 员工 ID,用于稳定 hash 决定 A/B 分组
                       (同一员工每次走同一版本,避免体验跳变)
            tenant_id: 租户 ID,默认从 contextvar 取
        """
        tid = tenant_id or get_current_tenant()
        cache_key = f"{tid}:{name}:for_request:{employee_id}"
        cached = self._cache.get(cache_key)
        if cached:
            import time

            version, ts = cached
            if time.monotonic() - ts < self._cache_ttl:
                return version

        try:
            async with get_db_session() as session:
                template = await self._get_template(session, name, tid)
                if template is None:
                    return None

                version = await self._select_version_for_request(
                    session, template, employee_id
                )
                if version:
                    import time

                    self._cache[cache_key] = (version, time.monotonic())
                return version
        except Exception as e:
            logger.warning(
                "DbPromptLoader.get_for_request 失败 name=%s tenant=%s: %s",
                name,
                tid,
                e,
            )
            return None

    async def get_by_label(
        self,
        name: str,
        label: str = "production",
        tenant_id: Optional[str] = None,
    ) -> Optional[PromptVersion]:
        """按 label 加载 Prompt 版本(如 production / latest / staging)。

        参考 Langfuse get_prompt(label=...):
        https://langfuse.com/docs/prompt-management/features/prompt-version-control
        """
        tid = tenant_id or get_current_tenant()
        try:
            async with get_db_session() as session:
                template = await self._get_template(session, name, tid)
                if template is None:
                    return None

                # latest label: 取最新版本
                if label == "latest":
                    return await self._get_latest_version(session, template.id)

                # 其他 label: 通过 prompt_labels 表查指针
                stmt = select(PromptLabel).where(
                    and_(
                        PromptLabel.template_id == template.id,
                        PromptLabel.label == label,
                    )
                )
                result = await session.execute(stmt)
                label_row = result.scalar_one_or_none()
                if label_row is None:
                    return None
                return await self._get_version_by_id(session, label_row.version_id)
        except Exception as e:
            logger.warning(
                "DbPromptLoader.get_by_label 失败 name=%s label=%s: %s",
                name,
                label,
                e,
            )
            return None

    async def list_versions(
        self,
        name: str,
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """列出某 Prompt 的所有版本(供管理后台展示)"""
        tid = tenant_id or get_current_tenant()
        try:
            async with get_db_session() as session:
                template = await self._get_template(session, name, tid)
                if template is None:
                    return []
                stmt = (
                    select(PromptVersion)
                    .where(PromptVersion.template_id == template.id)
                    .order_by(desc(PromptVersion.version))
                )
                result = await session.execute(stmt)
                return [
                    {
                        "id": v.id,
                        "version": v.version,
                        "content_preview": (v.content or "")[:200],
                        "config": v.config,
                        "created_by": v.created_by,
                        "created_at": (
                            v.created_at.isoformat() if v.created_at else None
                        ),
                    }
                    for v in result.scalars().all()
                ]
        except Exception as e:
            logger.warning("DbPromptLoader.list_versions 失败 name=%s: %s", name, e)
            return []

    async def create_version(
        self,
        name: str,
        content: str,
        config: Optional[Dict[str, Any]] = None,
        variables_schema: Optional[Dict[str, Any]] = None,
        created_by: Optional[str] = None,
        tenant_id: Optional[str] = None,
        labels: Optional[List[str]] = None,
    ) -> Optional[PromptVersion]:
        """为新 Prompt 创建新版本(若 template 不存在则同时创建)。

        labels: 同时分配的 label 列表(如 ["latest"] 或 ["prod-a"])
        """
        import uuid as _uuid

        tid = tenant_id or get_current_tenant()
        try:
            async with get_db_session() as session:
                # 1) 找或建 template
                template = await self._get_template(session, name, tid)
                if template is None:
                    template = PromptTemplate(
                        id=str(_uuid.uuid4()),
                        tenant_id=tid,
                        name=name,
                        type="text",
                        description=None,
                        created_by=created_by,
                    )
                    session.add(template)
                    await session.flush()

                # 2) 算新版本号(已有最大版本 + 1)
                latest = await self._get_latest_version(session, template.id)
                new_version_no = (latest.version + 1) if latest else 1

                version = PromptVersion(
                    id=str(_uuid.uuid4()),
                    template_id=template.id,
                    version=new_version_no,
                    content=content,
                    config=config,
                    variables_schema=variables_schema,
                    created_by=created_by,
                )
                session.add(version)
                await session.flush()

                # 3) 分配 label(覆盖同名旧 label)
                if labels:
                    for label in labels:
                        await self._upsert_label(
                            session, template.id, version.id, label, created_by
                        )

                # 4) 自动维护 latest label 指向最新版本
                await self._upsert_label(
                    session, template.id, version.id, "latest", created_by
                )

                await session.commit()
                logger.info(
                    "创建 Prompt 版本 name=%s version=%d labels=%s",
                    name,
                    new_version_no,
                    labels or [],
                )
                return version
        except Exception as e:
            logger.exception("DbPromptLoader.create_version 失败 name=%s: %s", name, e)
            return None

    async def assign_label(
        self,
        name: str,
        version: int,
        label: str,
        updated_by: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> bool:
        """把某 label 指向指定版本(用于回滚 / 灰度切换 / A/B 切换)。

        参考 Langfuse rollback: 把 production label 重新指到旧版本即一键回滚。
        """
        tid = tenant_id or get_current_tenant()
        try:
            async with get_db_session() as session:
                template = await self._get_template(session, name, tid)
                if template is None:
                    return False
                stmt = select(PromptVersion).where(
                    and_(
                        PromptVersion.template_id == template.id,
                        PromptVersion.version == version,
                    )
                )
                result = await session.execute(stmt)
                version_row = result.scalar_one_or_none()
                if version_row is None:
                    return False
                await self._upsert_label(
                    session, template.id, version_row.id, label, updated_by
                )
                await session.commit()
                logger.info(
                    "Prompt label 已分配 name=%s version=%d label=%s",
                    name,
                    version,
                    label,
                )
                return True
        except Exception as e:
            logger.exception(
                "DbPromptLoader.assign_label 失败 name=%s v=%d label=%s: %s",
                name,
                version,
                label,
                e,
            )
            return False

    def render(
        self,
        version: PromptVersion,
        raw_inputs: List[Dict[str, Any]],
        employee_history: Optional[List[Dict[str, Any]]] = None,
        company_kb: Optional[List[Dict[str, Any]]] = None,
        employee_id: str = "",
        period: str = "",
        placeholders: Optional[List[str]] = None,
    ) -> str:
        """渲染 Prompt 版本(替换占位符)。

        复用 PromptLoader 的 _render_template 逻辑,保持文件/DB 两种来源渲染一致。
        """
        phs = placeholders or _DEFAULT_PLACEHOLDERS
        import json

        values = {
            "raw_inputs": json.dumps(raw_inputs, ensure_ascii=False, indent=2),
            "employee_history": json.dumps(
                employee_history or [], ensure_ascii=False, indent=2
            ),
            "company_kb": json.dumps(company_kb or [], ensure_ascii=False, indent=2),
            "employee_id": employee_id,
            "period": period,
        }

        def replacer(match: re.Match) -> str:
            key = match.group(1)
            return values.get(key, match.group(0))

        pattern = re.compile(r"\{(" + "|".join(phs) + r")\}")
        return pattern.sub(replacer, version.content or "")

    # ====== 内部 helper ======

    async def _get_template(
        self, session: AsyncSession, name: str, tenant_id: str
    ) -> Optional[PromptTemplate]:
        stmt = select(PromptTemplate).where(
            and_(
                PromptTemplate.tenant_id == tenant_id,
                PromptTemplate.name == name,
            )
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_latest_version(
        self, session: AsyncSession, template_id: str
    ) -> Optional[PromptVersion]:
        stmt = (
            select(PromptVersion)
            .where(PromptVersion.template_id == template_id)
            .order_by(desc(PromptVersion.version))
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_version_by_id(
        self, session: AsyncSession, version_id: str
    ) -> Optional[PromptVersion]:
        stmt = select(PromptVersion).where(PromptVersion.id == version_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _select_version_for_request(
        self,
        session: AsyncSession,
        template: PromptTemplate,
        employee_id: str,
    ) -> Optional[PromptVersion]:
        """按 A/B / 灰度 / production 优先级选版本。

        参考 Langfuse A/B 测试:
        https://langfuse.com/docs/prompt-management/features/a-b-testing
        """
        # 1) 灰度: canary-Npct label
        # 找所有 canary-Npct label,N < hash(employee_id) % 100 时走灰度
        stmt = select(PromptLabel).where(
            and_(
                PromptLabel.template_id == template.id,
                PromptLabel.label.like("canary-%pct"),
            )
        )
        result = await session.execute(stmt)
        for label_row in result.scalars().all():
            try:
                pct = int(label_row.label.replace("canary-", "").replace("pct", ""))
            except ValueError:
                continue
            if self._hash_pct(employee_id, template.id) < pct:
                return await self._get_version_by_id(session, label_row.version_id)

        # 2) A/B 测试: prod-a / prod-b 同时存在,按 hash % 2 选
        labels_for_ab = ["prod-a", "prod-b"]
        ab_versions: Dict[str, PromptLabel] = {}
        for label in labels_for_ab:
            stmt = select(PromptLabel).where(
                and_(
                    PromptLabel.template_id == template.id,
                    PromptLabel.label == label,
                )
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row:
                ab_versions[label] = row
        if len(ab_versions) == 2:
            # 同一员工稳定走同一版本(避免体验跳变)
            chosen = (
                "prod-a" if self._hash_pct(employee_id, template.id) < 50 else "prod-b"
            )
            return await self._get_version_by_id(
                session, ab_versions[chosen].version_id
            )

        # 3) 默认 production
        stmt = select(PromptLabel).where(
            and_(
                PromptLabel.template_id == template.id,
                PromptLabel.label == "production",
            )
        )
        result = await session.execute(stmt)
        label_row = result.scalar_one_or_none()
        if label_row:
            return await self._get_version_by_id(session, label_row.version_id)

        # 4) fallback latest
        return await self._get_latest_version(session, template.id)

    @staticmethod
    def _hash_pct(employee_id: str, template_id: str) -> int:
        """稳定 hash: 同一 (employee, template) 永远得同一 0-99 数"""
        h = hashlib.sha256(f"{employee_id}:{template_id}".encode()).hexdigest()
        return int(h[:8], 16) % 100

    async def _upsert_label(
        self,
        session: AsyncSession,
        template_id: str,
        version_id: str,
        label: str,
        updated_by: Optional[str],
    ) -> None:
        """分配 label,若已存在则覆盖(对应 Langfuse label 是指针的语义)"""
        stmt = select(PromptLabel).where(
            and_(
                PromptLabel.template_id == template_id,
                PromptLabel.label == label,
            )
        )
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing:
            existing.version_id = version_id
            existing.updated_by = updated_by
        else:
            session.add(
                PromptLabel(
                    id=str(uuid.uuid4()),
                    template_id=template_id,
                    version_id=version_id,
                    label=label,
                    protected=False,
                    updated_by=updated_by,
                )
            )


# 全局单例
_global_loader: Optional[DbPromptLoader] = None


def get_global_db_prompt_loader() -> DbPromptLoader:
    global _global_loader
    if _global_loader is None:
        _global_loader = DbPromptLoader()
    return _global_loader
