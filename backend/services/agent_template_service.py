"""Agent 模板市场服务

对标 Coze 插件市场 / LobeChat 助手市场:
- 模板 CRUD (租户私有 + 公开市场)
- install_template: 从模板创建 Agent (复制 template_config 到新 Agent 配置)
- 评价系统 (评分 + 评论, 自动更新模板平均评分)
- 统计 (分类统计 / 总数 / 平均评分)
- 搜索 (关键词 + 分类过滤)

事务边界由路由层控制 (service 层不 commit)。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.agent_template_models import AgentTemplate, TemplateReview

logger = logging.getLogger(__name__)

# 支持的分类
TEMPLATE_CATEGORIES = {"hr", "recruitment", "evaluation", "training", "general"}

# template_config 的默认结构
DEFAULT_TEMPLATE_CONFIG: Dict[str, Any] = {
    "system_prompt": "",
    "model_config": {"tier": "L0", "temperature": 0.7, "max_tokens": 2000},
    "tools": [],
    "knowledge_base_ids": [],
    "workflow_id": None,
    "guardrails": {"input_guard": False, "output_guard": False, "sensitive_words": False},
}


class AgentTemplateService:
    """Agent 模板市场服务"""

    def __init__(self, session: AsyncSession):
        self.session = session

    # ===================== 模板 CRUD =====================

    async def create_template(
        self,
        name: str,
        description: Optional[str],
        category: str,
        template_config: Dict[str, Any],
        *,
        author: Optional[str] = None,
        version: str = "1.0.0",
        tags: Optional[List[str]] = None,
        is_public: bool = False,
        is_official: bool = False,
        tenant_id: str = "default",
    ) -> AgentTemplate:
        """创建 Agent 模板

        Args:
            name: 模板名称。
            description: 描述。
            category: 分类 (hr / recruitment / evaluation / training / general)。
            template_config: 模板配置 JSON。
            author: 作者。
            version: 版本号。
            tags: 标签列表。
            is_public: 是否公开到模板市场。
            is_official: 是否官方预置。
            tenant_id: 租户 ID。

        Returns:
            创建的 AgentTemplate 对象。

        Raises:
            ValueError: 参数无效。
        """
        if not name or not name.strip():
            raise ValueError("模板名称不能为空")
        if category not in TEMPLATE_CATEGORIES:
            raise ValueError(
                f"无效的 category: {category}, 可选: {TEMPLATE_CATEGORIES}"
            )

        # 合并默认配置
        merged_config = dict(DEFAULT_TEMPLATE_CONFIG)
        if isinstance(template_config, dict):
            merged_config.update(template_config)

        template = AgentTemplate(
            tenant_id=tenant_id,
            name=name.strip(),
            description=description,
            category=category,
            template_config=merged_config,
            author=author,
            version=version,
            tags=tags or [],
            is_public=is_public,
            is_official=is_official,
        )
        self.session.add(template)
        await self.session.flush()
        logger.info(
            "创建 Agent 模板 id=%s name=%s category=%s tenant=%s",
            template.id,
            name,
            category,
            tenant_id,
        )
        return template

    async def get_template(
        self, template_id: int, *, tenant_id: str = "default"
    ) -> Optional[AgentTemplate]:
        """获取模板 (租户私有 + 公开市场均可见)"""
        return (
            await self.session.execute(
                select(AgentTemplate).where(
                    AgentTemplate.id == template_id,
                    or_(
                        AgentTemplate.tenant_id == tenant_id,
                        AgentTemplate.is_public.is_(True),
                    ),
                )
            )
        ).scalar_one_or_none()

    async def list_templates(
        self,
        *,
        category: Optional[str] = None,
        page: int = 1,
        size: int = 20,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """列出租户模板 (租户私有, 不含公开市场)

        Args:
            category: 分类过滤 (None 表示全部)。
            page: 页码 (从 1 开始)。
            size: 每页条数。
            tenant_id: 租户 ID。

        Returns:
            {"items": [...], "total": N, "page": P, "size": S}
        """
        base = (
            select(AgentTemplate)
            .where(AgentTemplate.tenant_id == tenant_id)
            .order_by(AgentTemplate.created_at.desc())
        )
        if category:
            base = base.where(AgentTemplate.category == category)

        total = (
            await self.session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar() or 0

        offset = (page - 1) * size
        rows = (
            await self.session.execute(base.offset(offset).limit(size))
        ).scalars().all()

        return {
            "items": [self._template_to_dict(t) for t in rows],
            "total": total,
            "page": page,
            "size": size,
        }

    async def list_public_templates(
        self,
        *,
        category: Optional[str] = None,
        keyword: Optional[str] = None,
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """公开模板市场 (所有租户可见的公开模板)

        Args:
            category: 分类过滤。
            keyword: 搜索关键词 (匹配 name / description / tags)。
            page: 页码。
            size: 每页条数。

        Returns:
            {"items": [...], "total": N, "page": P, "size": S}
        """
        base = (
            select(AgentTemplate)
            .where(AgentTemplate.is_public.is_(True))
            .order_by(AgentTemplate.download_count.desc(), AgentTemplate.rating.desc())
        )
        if category:
            base = base.where(AgentTemplate.category == category)
        if keyword:
            pattern = f"%{keyword}%"
            base = base.where(
                or_(
                    AgentTemplate.name.ilike(pattern),
                    AgentTemplate.description.ilike(pattern),
                )
            )

        total = (
            await self.session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar() or 0

        offset = (page - 1) * size
        rows = (
            await self.session.execute(base.offset(offset).limit(size))
        ).scalars().all()

        return {
            "items": [self._template_to_dict(t) for t in rows],
            "total": total,
            "page": page,
            "size": size,
        }

    async def update_template(
        self,
        template_id: int,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        category: Optional[str] = None,
        template_config: Optional[Dict[str, Any]] = None,
        author: Optional[str] = None,
        version: Optional[str] = None,
        tags: Optional[List[str]] = None,
        is_public: Optional[bool] = None,
        is_official: Optional[bool] = None,
        tenant_id: str = "default",
    ) -> AgentTemplate:
        """更新模板 (仅租户自有模板可更新)"""
        template = (
            await self.session.execute(
                select(AgentTemplate).where(
                    AgentTemplate.id == template_id,
                    AgentTemplate.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if template is None:
            raise ValueError(f"模板 {template_id} 不存在或无权修改")

        if name is not None:
            if not name.strip():
                raise ValueError("模板名称不能为空")
            template.name = name.strip()
        if description is not None:
            template.description = description
        if category is not None:
            if category not in TEMPLATE_CATEGORIES:
                raise ValueError(
                    f"无效的 category: {category}, 可选: {TEMPLATE_CATEGORIES}"
                )
            template.category = category
        if template_config is not None:
            merged = dict(template.template_config or {})
            merged.update(template_config)
            template.template_config = merged
        if author is not None:
            template.author = author
        if version is not None:
            template.version = version
        if tags is not None:
            template.tags = tags
        if is_public is not None:
            template.is_public = is_public
        if is_official is not None:
            template.is_official = is_official

        await self.session.flush()
        logger.info("更新 Agent 模板 id=%s tenant=%s", template_id, tenant_id)
        return template

    async def delete_template(
        self, template_id: int, *, tenant_id: str = "default"
    ) -> bool:
        """删除模板 (仅租户自有模板可删除)

        Returns:
            True 表示删除成功, False 表示模板不存在。
        """
        template = (
            await self.session.execute(
                select(AgentTemplate).where(
                    AgentTemplate.id == template_id,
                    AgentTemplate.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if template is None:
            return False
        await self.session.delete(template)
        await self.session.flush()
        logger.info("删除 Agent 模板 id=%s tenant=%s", template_id, tenant_id)
        return True

    # ===================== 安装与统计 =====================

    async def install_template(
        self, template_id: int, *, tenant_id: str = "default"
    ) -> Dict[str, Any]:
        """从模板安装 Agent (复制 template_config 到新租户)

        将模板复制为当前租户的私有模板 (新 ID), 并增加下载计数。
        安装后的模板可由调用方进一步创建实际 Agent 实例。

        Returns:
            安装后的模板信息 dict。

        Raises:
            ValueError: 模板不存在。
        """
        source = await self.get_template(template_id, tenant_id=tenant_id)
        if source is None:
            raise ValueError(f"模板 {template_id} 不存在")

        # 复制为当前租户的私有模板
        installed = AgentTemplate(
            tenant_id=tenant_id,
            name=f"{source.name} (已安装)",
            description=source.description,
            category=source.category,
            template_config=dict(source.template_config or {}),
            author=source.author,
            version=source.version,
            tags=list(source.tags or []),
            is_public=False,
            is_official=False,
        )
        self.session.add(installed)
        await self.session.flush()

        # 增加源模板下载计数
        await self.increment_download(template_id, tenant_id=tenant_id)

        logger.info(
            "安装模板 source_id=%s → installed_id=%s tenant=%s",
            template_id,
            installed.id,
            tenant_id,
        )
        return self._template_to_dict(installed)

    async def increment_download(
        self, template_id: int, *, tenant_id: str = "default"
    ) -> None:
        """增加模板下载计数"""
        template = await self.get_template(template_id, tenant_id=tenant_id)
        if template is not None:
            template.download_count = (template.download_count or 0) + 1
            await self.session.flush()

    async def get_template_stats(
        self, *, tenant_id: str = "default"
    ) -> Dict[str, Any]:
        """模板统计 (分类统计 / 总数 / 平均评分)

        Returns:
            {
                "by_category": {category: count},
                "total": N,
                "avg_rating": float,
                "total_downloads": N,
            }
        """
        # 按分类统计 (租户私有 + 公开)
        category_rows = (
            await self.session.execute(
                select(AgentTemplate.category, func.count())
                .where(
                    or_(
                        AgentTemplate.tenant_id == tenant_id,
                        AgentTemplate.is_public.is_(True),
                    )
                )
                .group_by(AgentTemplate.category)
            )
        ).all()
        by_category = {row[0]: row[1] for row in category_rows}

        # 总数
        total = (
            await self.session.execute(
                select(func.count()).select_from(AgentTemplate).where(
                    or_(
                        AgentTemplate.tenant_id == tenant_id,
                        AgentTemplate.is_public.is_(True),
                    )
                )
            )
        ).scalar() or 0

        # 平均评分
        avg_rating = (
            await self.session.execute(
                select(func.avg(AgentTemplate.rating)).where(
                    or_(
                        AgentTemplate.tenant_id == tenant_id,
                        AgentTemplate.is_public.is_(True),
                    )
                )
            )
        ).scalar() or 0.0

        # 总下载量
        total_downloads = (
            await self.session.execute(
                select(func.sum(AgentTemplate.download_count)).where(
                    or_(
                        AgentTemplate.tenant_id == tenant_id,
                        AgentTemplate.is_public.is_(True),
                    )
                )
            )
        ).scalar() or 0

        return {
            "by_category": by_category,
            "total": total,
            "avg_rating": round(float(avg_rating), 2),
            "total_downloads": int(total_downloads),
        }

    # ===================== 评价 =====================

    async def add_review(
        self,
        template_id: int,
        reviewer_id: str,
        rating: int,
        comment: Optional[str],
        *,
        tenant_id: str = "default",
    ) -> TemplateReview:
        """添加评价 (同一用户对同一模板只能评价一次)

        添加后自动更新模板平均评分。

        Args:
            template_id: 模板 ID。
            reviewer_id: 评价人 ID。
            rating: 评分 1-5。
            comment: 评论 (可选)。
            tenant_id: 租户 ID。

        Returns:
            创建的 TemplateReview 对象。

        Raises:
            ValueError: 模板不存在 / 评分无效 / 已评价过。
        """
        template = await self.get_template(template_id, tenant_id=tenant_id)
        if template is None:
            raise ValueError(f"模板 {template_id} 不存在")
        if not 1 <= rating <= 5:
            raise ValueError("评分必须在 1-5 之间")

        # 检查是否已评价过
        existing = (
            await self.session.execute(
                select(TemplateReview).where(
                    TemplateReview.tenant_id == tenant_id,
                    TemplateReview.template_id == template_id,
                    TemplateReview.reviewer_id == reviewer_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ValueError("您已评价过此模板")

        review = TemplateReview(
            tenant_id=tenant_id,
            template_id=template_id,
            reviewer_id=reviewer_id,
            rating=rating,
            comment=comment,
        )
        self.session.add(review)
        await self.session.flush()

        # 重新计算模板平均评分
        avg = (
            await self.session.execute(
                select(func.avg(TemplateReview.rating)).where(
                    TemplateReview.template_id == template_id
                )
            )
        ).scalar() or float(rating)
        count = (
            await self.session.execute(
                select(func.count()).select_from(TemplateReview).where(
                    TemplateReview.template_id == template_id
                )
            )
        ).scalar() or 0
        template.rating = round(float(avg), 2)
        await self.session.flush()

        logger.info(
            "添加评价 template=%s reviewer=%s rating=%s (avg=%.2f, count=%s)",
            template_id,
            reviewer_id,
            rating,
            template.rating,
            count,
        )
        return review

    async def list_reviews(
        self,
        template_id: int,
        *,
        page: int = 1,
        size: int = 20,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """模板评价列表"""
        base = (
            select(TemplateReview)
            .where(TemplateReview.template_id == template_id)
            .order_by(TemplateReview.created_at.desc())
        )

        total = (
            await self.session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar() or 0

        offset = (page - 1) * size
        rows = (
            await self.session.execute(base.offset(offset).limit(size))
        ).scalars().all()

        return {
            "items": [self._review_to_dict(r) for r in rows],
            "total": total,
            "page": page,
            "size": size,
        }

    # ===================== 搜索 =====================

    async def search_templates(
        self,
        keyword: Optional[str] = None,
        category: Optional[str] = None,
        *,
        page: int = 1,
        size: int = 20,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """搜索模板 (租户私有 + 公开市场)

        Args:
            keyword: 搜索关键词 (匹配 name / description / tags)。
            category: 分类过滤。
            page: 页码。
            size: 每页条数。

        Returns:
            {"items": [...], "total": N, "page": P, "size": S}
        """
        base = (
            select(AgentTemplate)
            .where(
                or_(
                    AgentTemplate.tenant_id == tenant_id,
                    AgentTemplate.is_public.is_(True),
                )
            )
            .order_by(AgentTemplate.download_count.desc())
        )
        if category:
            base = base.where(AgentTemplate.category == category)
        if keyword:
            pattern = f"%{keyword}%"
            base = base.where(
                or_(
                    AgentTemplate.name.ilike(pattern),
                    AgentTemplate.description.ilike(pattern),
                )
            )

        total = (
            await self.session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar() or 0

        offset = (page - 1) * size
        rows = (
            await self.session.execute(base.offset(offset).limit(size))
        ).scalars().all()

        return {
            "items": [self._template_to_dict(t) for t in rows],
            "total": total,
            "page": page,
            "size": size,
        }

    # ===================== 内部方法 =====================

    @staticmethod
    def _template_to_dict(t: AgentTemplate) -> Dict[str, Any]:
        """AgentTemplate → dict"""
        return {
            "id": t.id,
            "tenant_id": t.tenant_id,
            "name": t.name,
            "description": t.description,
            "category": t.category,
            "template_config": t.template_config if isinstance(t.template_config, dict) else {},
            "author": t.author,
            "version": t.version,
            "tags": t.tags if isinstance(t.tags, list) else [],
            "download_count": t.download_count,
            "rating": t.rating,
            "is_public": t.is_public,
            "is_official": t.is_official,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        }

    @staticmethod
    def _review_to_dict(r: TemplateReview) -> Dict[str, Any]:
        """TemplateReview → dict"""
        return {
            "id": r.id,
            "tenant_id": r.tenant_id,
            "template_id": r.template_id,
            "reviewer_id": r.reviewer_id,
            "rating": r.rating,
            "comment": r.comment,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
