"""数据集管理服务

对标 Langfuse 数据集管理 + 阿里百炼训练集/评测集:
- 数据集 CRUD (tenant_id 过滤)
- 条目 CRUD + 批量导入 (JSON/CSV)
- 统计信息 (总数/已标注数/待标注数)
- 导出 (JSON/CSV)

事务边界由路由层控制 (service 层不 commit)。
"""

from __future__ import annotations

import csv
import io
import json
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.dataset_models import DatasetItem, EvaluationDataset

logger = logging.getLogger(__name__)

# 允许的数据集类型
VALID_DATASET_TYPES = {"test", "train", "eval"}

# 允许的条目状态
VALID_ITEM_STATUSES = {"pending", "labeled", "reviewed"}


class DatasetService:
    """数据集管理服务 (数据库实现)"""

    def __init__(self, session: AsyncSession):
        self.session = session

    # ===================== 数据集 CRUD =====================

    async def create_dataset(
        self,
        name: str,
        *,
        tenant_id: str = "default",
        description: Optional[str] = None,
        dataset_type: str = "test",
        tags: Optional[List[str]] = None,
        created_by: Optional[str] = None,
    ) -> EvaluationDataset:
        """创建数据集

        Args:
            name: 数据集名称。
            tenant_id: 租户 ID。
            description: 描述。
            dataset_type: 类型 (test/train/eval)。
            tags: 标签列表。
            created_by: 创建人 ID。

        Returns:
            创建的 EvaluationDataset 对象。
        """
        if not name or not name.strip():
            raise ValueError("数据集名称不能为空")
        if dataset_type not in VALID_DATASET_TYPES:
            raise ValueError(
                f"无效的数据集类型: {dataset_type}, 可选: {VALID_DATASET_TYPES}"
            )

        entity = EvaluationDataset(
            tenant_id=tenant_id,
            name=name.strip(),
            description=description,
            dataset_type=dataset_type,
            tags=tags or [],
            item_count=0,
            created_by=created_by,
        )
        self.session.add(entity)
        await self.session.flush()
        logger.info(
            "创建数据集: %s (类型: %s, 租户: %s)", name, dataset_type, tenant_id
        )
        return entity

    async def get_dataset(
        self, dataset_id: int, *, tenant_id: str = "default"
    ) -> Optional[EvaluationDataset]:
        """获取数据集详情

        Args:
            dataset_id: 数据集 ID。
            tenant_id: 租户 ID。

        Returns:
            EvaluationDataset 对象, 不存在返回 None。
        """
        return (
            await self.session.execute(
                select(EvaluationDataset).where(
                    EvaluationDataset.id == dataset_id,
                    EvaluationDataset.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()

    async def list_datasets(
        self,
        *,
        tenant_id: str = "default",
        dataset_type: Optional[str] = None,
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """分页查询数据集列表

        Args:
            tenant_id: 租户 ID。
            dataset_type: 按类型过滤 (None 表示全部)。
            page: 页码 (从 1 开始)。
            size: 每页条数。

        Returns:
            {"items": [...], "total": N, "page": P, "size": S}
        """
        base = (
            select(EvaluationDataset)
            .where(EvaluationDataset.tenant_id == tenant_id)
            .order_by(EvaluationDataset.created_at.desc())
        )
        if dataset_type:
            base = base.where(EvaluationDataset.dataset_type == dataset_type)

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
            "items": [self._dataset_to_dict(d) for d in rows],
            "total": total,
            "page": page,
            "size": size,
        }

    async def update_dataset(
        self,
        dataset_id: int,
        *,
        tenant_id: str = "default",
        name: Optional[str] = None,
        description: Optional[str] = None,
        dataset_type: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> Optional[EvaluationDataset]:
        """更新数据集

        Args:
            dataset_id: 数据集 ID。
            tenant_id: 租户 ID。
            name: 新名称。
            description: 新描述。
            dataset_type: 新类型。
            tags: 新标签列表。

        Returns:
            更新后的 EvaluationDataset 对象, 不存在返回 None。
        """
        entity = await self.get_dataset(dataset_id, tenant_id=tenant_id)
        if entity is None:
            return None

        if name is not None:
            if not name.strip():
                raise ValueError("数据集名称不能为空")
            entity.name = name.strip()
        if description is not None:
            entity.description = description
        if dataset_type is not None:
            if dataset_type not in VALID_DATASET_TYPES:
                raise ValueError(
                    f"无效的数据集类型: {dataset_type}, 可选: {VALID_DATASET_TYPES}"
                )
            entity.dataset_type = dataset_type
        if tags is not None:
            entity.tags = tags

        await self.session.flush()
        return entity

    async def delete_dataset(
        self, dataset_id: int, *, tenant_id: str = "default"
    ) -> bool:
        """删除数据集 (同时删除所有条目)

        Args:
            dataset_id: 数据集 ID。
            tenant_id: 租户 ID。

        Returns:
            True 表示已删除, False 表示不存在。
        """
        entity = await self.get_dataset(dataset_id, tenant_id=tenant_id)
        if entity is None:
            return False

        # 删除所有关联条目
        items = (
            await self.session.execute(
                select(DatasetItem).where(
                    DatasetItem.dataset_id == dataset_id,
                    DatasetItem.tenant_id == tenant_id,
                )
            )
        ).scalars().all()
        for item in items:
            await self.session.delete(item)

        await self.session.delete(entity)
        await self.session.flush()
        logger.info("删除数据集 id=%s (含 %d 条目)", dataset_id, len(items))
        return True

    # ===================== 条目 CRUD =====================

    async def add_item(
        self,
        dataset_id: int,
        input_data: Dict[str, Any],
        *,
        tenant_id: str = "default",
        expected_output: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        label: Optional[str] = None,
        status: str = "pending",
    ) -> Optional[DatasetItem]:
        """添加单条数据集条目

        Args:
            dataset_id: 数据集 ID。
            input_data: 输入内容。
            tenant_id: 租户 ID。
            expected_output: 期望输出。
            metadata: 附加元数据。
            label: 标签。
            status: 标注状态。

        Returns:
            创建的 DatasetItem 对象, 数据集不存在返回 None。
        """
        dataset = await self.get_dataset(dataset_id, tenant_id=tenant_id)
        if dataset is None:
            return None

        if status not in VALID_ITEM_STATUSES:
            raise ValueError(f"无效的条目状态: {status}, 可选: {VALID_ITEM_STATUSES}")

        item = DatasetItem(
            tenant_id=tenant_id,
            dataset_id=dataset_id,
            input=input_data,
            expected_output=expected_output,
            metadata_=metadata or {},
            label=label,
            status=status,
        )
        self.session.add(item)
        await self.session.flush()

        # 更新数据集条目计数
        dataset.item_count = (dataset.item_count or 0) + 1
        await self.session.flush()
        return item

    async def batch_add_items(
        self,
        dataset_id: int,
        items: List[Dict[str, Any]],
        *,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """批量添加数据集条目

        支持 JSON 格式的条目列表, 每项含 input / expected_output / metadata / label / status。

        Args:
            dataset_id: 数据集 ID。
            items: 条目列表。
            tenant_id: 租户 ID。

        Returns:
            {"added": N, "skipped": N, "errors": [...]}
        """
        dataset = await self.get_dataset(dataset_id, tenant_id=tenant_id)
        if dataset is None:
            return {"added": 0, "skipped": len(items), "errors": ["数据集不存在"]}

        added = 0
        skipped = 0
        errors: List[str] = []
        for item_data in items:
            try:
                input_data = item_data.get("input")
                if input_data is None:
                    skipped += 1
                    errors.append("条目缺少 input 字段")
                    continue
                # input 可以是字符串或 dict, 统一包装
                if isinstance(input_data, str):
                    input_data = {"text": input_data}

                item = DatasetItem(
                    tenant_id=tenant_id,
                    dataset_id=dataset_id,
                    input=input_data,
                    expected_output=item_data.get("expected_output"),
                    metadata_=item_data.get("metadata") or {},
                    label=item_data.get("label"),
                    status=item_data.get("status", "pending"),
                )
                self.session.add(item)
                added += 1
            except Exception as e:
                skipped += 1
                errors.append(f"添加条目失败: {e}")

        # 批量更新计数
        if added > 0:
            dataset.item_count = (dataset.item_count or 0) + added
            await self.session.flush()

        logger.info(
            "批量添加条目到数据集 %s: 成功 %d, 跳过 %d", dataset_id, added, skipped
        )
        return {"added": added, "skipped": skipped, "errors": errors}

    async def import_items(
        self,
        dataset_id: int,
        file_content: str,
        format: str = "json",
        *,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """导入数据集条目 (支持 JSON / CSV 格式)

        JSON 格式: [{"input": {...}, "expected_output": {...}, ...}]
        CSV 格式: 每行含 input,expected_output,label,status 列

        Args:
            dataset_id: 数据集 ID。
            file_content: 文件内容字符串。
            format: 格式 (json / csv)。
            tenant_id: 租户 ID。

        Returns:
            {"added": N, "skipped": N, "errors": [...]}
        """
        items: List[Dict[str, Any]] = []
        if format.lower() == "csv":
            reader = csv.DictReader(io.StringIO(file_content))
            for row in reader:
                input_str = (row.get("input") or "").strip()
                expected_str = (row.get("expected_output") or "").strip()
                item: Dict[str, Any] = {
                    "input": {"text": input_str} if input_str else {},
                    "label": (row.get("label") or "").strip() or None,
                    "status": (row.get("status") or "pending").strip(),
                }
                if expected_str:
                    try:
                        item["expected_output"] = json.loads(expected_str)
                    except (json.JSONDecodeError, ValueError):
                        item["expected_output"] = {"text": expected_str}
                items.append(item)
        elif format.lower() == "json":
            data = json.loads(file_content)
            if not isinstance(data, list):
                raise ValueError("JSON 格式必须为数组")
            for entry in data:
                if isinstance(entry, dict):
                    items.append(entry)
                elif isinstance(entry, str):
                    items.append({"input": {"text": entry}})
        else:
            raise ValueError(f"不支持的格式: {format}, 可选: json / csv")

        return await self.batch_add_items(
            dataset_id, items, tenant_id=tenant_id
        )

    async def list_items(
        self,
        dataset_id: int,
        *,
        tenant_id: str = "default",
        status: Optional[str] = None,
        page: int = 1,
        size: int = 50,
    ) -> Dict[str, Any]:
        """分页查询数据集条目列表

        Args:
            dataset_id: 数据集 ID。
            tenant_id: 租户 ID。
            status: 按状态过滤 (None 表示全部)。
            page: 页码 (从 1 开始)。
            size: 每页条数。

        Returns:
            {"items": [...], "total": N, "page": P, "size": S}
        """
        base = (
            select(DatasetItem)
            .where(
                DatasetItem.dataset_id == dataset_id,
                DatasetItem.tenant_id == tenant_id,
            )
            .order_by(DatasetItem.created_at.desc())
        )
        if status:
            base = base.where(DatasetItem.status == status)

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
            "items": [self._item_to_dict(i) for i in rows],
            "total": total,
            "page": page,
            "size": size,
        }

    async def update_item(
        self,
        dataset_id: int,
        item_id: int,
        *,
        tenant_id: str = "default",
        input_data: Optional[Dict[str, Any]] = None,
        expected_output: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        label: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Optional[DatasetItem]:
        """更新数据集条目

        Args:
            dataset_id: 数据集 ID。
            item_id: 条目 ID。
            tenant_id: 租户 ID。
            input_data: 新输入内容。
            expected_output: 新期望输出。
            metadata: 新元数据。
            label: 新标签。
            status: 新状态。

        Returns:
            更新后的 DatasetItem 对象, 不存在返回 None。
        """
        item = (
            await self.session.execute(
                select(DatasetItem).where(
                    DatasetItem.id == item_id,
                    DatasetItem.dataset_id == dataset_id,
                    DatasetItem.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if item is None:
            return None

        if input_data is not None:
            item.input = input_data
        if expected_output is not None:
            item.expected_output = expected_output
        if metadata is not None:
            item.metadata_ = metadata
        if label is not None:
            item.label = label
        if status is not None:
            if status not in VALID_ITEM_STATUSES:
                raise ValueError(f"无效的条目状态: {status}, 可选: {VALID_ITEM_STATUSES}")
            item.status = status

        await self.session.flush()
        return item

    async def delete_item(
        self, dataset_id: int, item_id: int, *, tenant_id: str = "default"
    ) -> bool:
        """删除数据集条目

        Args:
            dataset_id: 数据集 ID。
            item_id: 条目 ID。
            tenant_id: 租户 ID。

        Returns:
            True 表示已删除, False 表示不存在。
        """
        item = (
            await self.session.execute(
                select(DatasetItem).where(
                    DatasetItem.id == item_id,
                    DatasetItem.dataset_id == dataset_id,
                    DatasetItem.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if item is None:
            return False

        await self.session.delete(item)

        # 更新数据集条目计数
        dataset = await self.get_dataset(dataset_id, tenant_id=tenant_id)
        if dataset is not None and dataset.item_count and dataset.item_count > 0:
            dataset.item_count = dataset.item_count - 1

        await self.session.flush()
        return True

    # ===================== 统计与导出 =====================

    async def get_dataset_stats(
        self, dataset_id: int, *, tenant_id: str = "default"
    ) -> Dict[str, Any]:
        """获取数据集统计信息

        Args:
            dataset_id: 数据集 ID。
            tenant_id: 租户 ID。

        Returns:
            {"total": N, "labeled": N, "pending": N, "reviewed": N}
        """
        dataset = await self.get_dataset(dataset_id, tenant_id=tenant_id)
        if dataset is None:
            return {"total": 0, "labeled": 0, "pending": 0, "reviewed": 0}

        # 按状态分组统计
        rows = (
            await self.session.execute(
                select(DatasetItem.status, func.count(DatasetItem.id)).where(
                    DatasetItem.dataset_id == dataset_id,
                    DatasetItem.tenant_id == tenant_id,
                ).group_by(DatasetItem.status)
            )
        ).all()

        status_counts = {row[0]: row[1] for row in rows}
        return {
            "total": dataset.item_count or 0,
            "labeled": status_counts.get("labeled", 0),
            "pending": status_counts.get("pending", 0),
            "reviewed": status_counts.get("reviewed", 0),
        }

    async def export_dataset(
        self,
        dataset_id: int,
        format: str = "json",
        *,
        tenant_id: str = "default",
    ) -> str:
        """导出数据集 (JSON / CSV 格式)

        Args:
            dataset_id: 数据集 ID。
            format: 格式 (json / csv)。
            tenant_id: 租户 ID。

        Returns:
            导出内容字符串。
        """
        dataset = await self.get_dataset(dataset_id, tenant_id=tenant_id)
        if dataset is None:
            raise ValueError(f"数据集 {dataset_id} 不存在")

        items = (
            await self.session.execute(
                select(DatasetItem)
                .where(
                    DatasetItem.dataset_id == dataset_id,
                    DatasetItem.tenant_id == tenant_id,
                )
                .order_by(DatasetItem.created_at.asc())
            )
        ).scalars().all()

        if format.lower() == "json":
            payload = {
                "dataset": self._dataset_to_dict(dataset),
                "items": [self._item_to_dict(i) for i in items],
            }
            return json.dumps(payload, ensure_ascii=False, indent=2)
        elif format.lower() == "csv":
            output = io.StringIO()
            writer = csv.DictWriter(
                output,
                fieldnames=[
                    "id",
                    "input",
                    "expected_output",
                    "metadata",
                    "label",
                    "status",
                    "created_at",
                ],
            )
            writer.writeheader()
            for item in items:
                writer.writerow(
                    {
                        "id": item.id,
                        "input": json.dumps(item.input, ensure_ascii=False),
                        "expected_output": json.dumps(
                            item.expected_output, ensure_ascii=False
                        )
                        if item.expected_output
                        else "",
                        "metadata": json.dumps(item.metadata_, ensure_ascii=False),
                        "label": item.label or "",
                        "status": item.status,
                        "created_at": item.created_at.isoformat()
                        if item.created_at
                        else "",
                    }
                )
            return output.getvalue()
        else:
            raise ValueError(f"不支持的格式: {format}, 可选: json / csv")

    # ===================== 序列化辅助 =====================

    @staticmethod
    def _dataset_to_dict(d: EvaluationDataset) -> Dict[str, Any]:
        """EvaluationDataset -> dict"""
        return {
            "id": d.id,
            "tenant_id": d.tenant_id,
            "name": d.name,
            "description": d.description,
            "dataset_type": d.dataset_type,
            "tags": d.tags,
            "item_count": d.item_count,
            "created_by": d.created_by,
            "created_at": d.created_at.isoformat() if d.created_at else None,
            "updated_at": d.updated_at.isoformat() if d.updated_at else None,
        }

    @staticmethod
    def _item_to_dict(i: DatasetItem) -> Dict[str, Any]:
        """DatasetItem -> dict"""
        return {
            "id": i.id,
            "tenant_id": i.tenant_id,
            "dataset_id": i.dataset_id,
            "input": i.input,
            "expected_output": i.expected_output,
            "metadata": i.metadata_,
            "label": i.label,
            "status": i.status,
            "created_at": i.created_at.isoformat() if i.created_at else None,
        }
