"""数据集管理 Admin API

路由前缀: /api/v1/admin/datasets
权限: Role.ADMIN (router 级 dependencies)

完整端点 (12 个):
- POST   /                      - 创建数据集
- GET    /                      - 列表 (分页 + 类型过滤)
- GET    /{dataset_id}          - 详情
- PUT    /{dataset_id}          - 更新
- DELETE /{dataset_id}          - 删除
- POST   /{dataset_id}/items    - 添加单条条目
- POST   /{dataset_id}/items/batch - 批量导入
- GET    /{dataset_id}/items    - 条目列表 (分页)
- PUT    /{dataset_id}/items/{item_id} - 更新条目
- DELETE /{dataset_id}/items/{item_id} - 删除条目
- GET    /{dataset_id}/stats    - 统计
- GET    /{dataset_id}/export   - 导出 (JSON/CSV)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.rbac import Role, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from services.dataset_service import VALID_DATASET_TYPES, DatasetService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/datasets",
    tags=["admin-datasets"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class DatasetCreate(BaseModel):
    """创建数据集请求"""

    name: str = Field(..., min_length=1, max_length=256, description="数据集名称")
    description: Optional[str] = Field(default=None, description="描述")
    dataset_type: str = Field(default="test", description="类型: test/train/eval")
    tags: List[str] = Field(default_factory=list, description="标签列表")


class DatasetUpdate(BaseModel):
    """更新数据集请求"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=256)
    description: Optional[str] = None
    dataset_type: Optional[str] = None
    tags: Optional[List[str]] = None


class ItemCreate(BaseModel):
    """添加单条条目请求"""

    input: Dict[str, Any] = Field(..., description="输入内容")
    expected_output: Optional[Dict[str, Any]] = Field(
        default=None, description="期望输出"
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default_factory=dict, description="附加元数据"
    )
    label: Optional[str] = Field(default=None, description="标签")
    status: str = Field(default="pending", description="状态: pending/labeled/reviewed")


class ItemBatchCreate(BaseModel):
    """批量导入条目请求"""

    items: List[Dict[str, Any]] = Field(..., description="条目列表")
    format: Optional[str] = Field(
        default=None, description="格式提示 (json/csv), 不传时按 items 数组处理"
    )


class ItemBatchImport(BaseModel):
    """文件导入条目请求"""

    file_content: str = Field(..., description="文件内容字符串")
    format: str = Field(default="json", description="格式: json/csv")


class ItemUpdate(BaseModel):
    """更新条目请求"""

    input: Optional[Dict[str, Any]] = None
    expected_output: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    label: Optional[str] = None
    status: Optional[str] = None


# ============================================================
# 数据集 CRUD 路由
# ============================================================


@router.post("", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_dataset(
    payload: DatasetCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """创建数据集"""
    tenant_id = get_current_tenant()
    service = DatasetService(session)
    try:
        entity = await service.create_dataset(
            name=payload.name,
            tenant_id=tenant_id,
            description=payload.description,
            dataset_type=payload.dataset_type,
            tags=payload.tags,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    await session.commit()
    await session.refresh(entity)
    return DatasetService._dataset_to_dict(entity)


@router.get("", response_model=Dict[str, Any])
async def list_datasets(
    request: Request,
    session: AsyncSession = Depends(get_db),
    dataset_type: Optional[str] = Query(default=None, description="按类型过滤"),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页条数"),
):
    """数据集列表 (分页 + 类型过滤)"""
    if dataset_type and dataset_type not in VALID_DATASET_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无效的数据集类型: {dataset_type}, 可选: {VALID_DATASET_TYPES}",
        )
    tenant_id = get_current_tenant()
    service = DatasetService(session)
    return await service.list_datasets(
        tenant_id=tenant_id,
        dataset_type=dataset_type,
        page=page,
        size=size,
    )


@router.get("/{dataset_id}", response_model=Dict[str, Any])
async def get_dataset(
    dataset_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """获取数据集详情"""
    tenant_id = get_current_tenant()
    service = DatasetService(session)
    entity = await service.get_dataset(dataset_id, tenant_id=tenant_id)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"数据集 {dataset_id} 不存在",
        )
    return DatasetService._dataset_to_dict(entity)


@router.put("/{dataset_id}", response_model=Dict[str, Any])
async def update_dataset(
    dataset_id: int,
    payload: DatasetUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """更新数据集"""
    tenant_id = get_current_tenant()
    service = DatasetService(session)
    try:
        entity = await service.update_dataset(
            dataset_id,
            tenant_id=tenant_id,
            name=payload.name,
            description=payload.description,
            dataset_type=payload.dataset_type,
            tags=payload.tags,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"数据集 {dataset_id} 不存在",
        )
    await session.commit()
    await session.refresh(entity)
    return DatasetService._dataset_to_dict(entity)


@router.delete("/{dataset_id}", response_model=Dict[str, Any])
async def delete_dataset(
    dataset_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """删除数据集 (同时删除所有条目)"""
    tenant_id = get_current_tenant()
    service = DatasetService(session)
    deleted = await service.delete_dataset(dataset_id, tenant_id=tenant_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"数据集 {dataset_id} 不存在",
        )
    await session.commit()
    return {"deleted": True, "dataset_id": dataset_id}


# ============================================================
# 条目 CRUD 路由
# ============================================================


@router.post(
    "/{dataset_id}/items",
    response_model=Dict[str, Any],
    status_code=status.HTTP_201_CREATED,
)
async def add_item(
    dataset_id: int,
    payload: ItemCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """添加单条数据集条目"""
    tenant_id = get_current_tenant()
    service = DatasetService(session)
    try:
        item = await service.add_item(
            dataset_id,
            payload.input,
            tenant_id=tenant_id,
            expected_output=payload.expected_output,
            metadata=payload.metadata,
            label=payload.label,
            status=payload.status,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"数据集 {dataset_id} 不存在",
        )
    await session.commit()
    await session.refresh(item)
    return DatasetService._item_to_dict(item)


@router.post("/{dataset_id}/items/batch", response_model=Dict[str, Any])
async def batch_add_items(
    dataset_id: int,
    payload: ItemBatchCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """批量导入条目 (JSON 数组)

    如果 format 字段为 "csv" 或 "json", 则将 items 视为文件内容字符串列表;
    否则将 items 作为条目 dict 列表直接导入。
    """
    tenant_id = get_current_tenant()
    service = DatasetService(session)

    # 如果 format 指定了 csv/json 且 items[0] 是字符串, 走文件导入逻辑
    if payload.format and payload.items and isinstance(payload.items[0], str):
        results = {"added": 0, "skipped": 0, "errors": []}
        for content in payload.items:
            try:
                result = await service.import_items(
                    dataset_id,
                    str(content),
                    payload.format,
                    tenant_id=tenant_id,
                )
                results["added"] += result.get("added", 0)
                results["skipped"] += result.get("skipped", 0)
                results["errors"].extend(result.get("errors", []))
            except ValueError as e:
                results["skipped"] += 1
                results["errors"].append(str(e))
    else:
        results = await service.batch_add_items(
            dataset_id, payload.items, tenant_id=tenant_id
        )

    if results["added"] == 0 and results["skipped"] > 0 and not results["errors"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"数据集 {dataset_id} 不存在",
        )
    await session.commit()
    return results


@router.get("/{dataset_id}/items", response_model=Dict[str, Any])
async def list_items(
    dataset_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    item_status: Optional[str] = Query(default=None, alias="status", description="按状态过滤"),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=50, ge=1, le=200, description="每页条数"),
):
    """条目列表 (分页)"""
    tenant_id = get_current_tenant()
    service = DatasetService(session)
    return await service.list_items(
        dataset_id,
        tenant_id=tenant_id,
        status=item_status,
        page=page,
        size=size,
    )


@router.put("/{dataset_id}/items/{item_id}", response_model=Dict[str, Any])
async def update_item(
    dataset_id: int,
    item_id: int,
    payload: ItemUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """更新条目"""
    tenant_id = get_current_tenant()
    service = DatasetService(session)
    try:
        item = await service.update_item(
            dataset_id,
            item_id,
            tenant_id=tenant_id,
            input_data=payload.input,
            expected_output=payload.expected_output,
            metadata=payload.metadata,
            label=payload.label,
            status=payload.status,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"条目 {item_id} 不存在",
        )
    await session.commit()
    await session.refresh(item)
    return DatasetService._item_to_dict(item)


@router.delete("/{dataset_id}/items/{item_id}", response_model=Dict[str, Any])
async def delete_item(
    dataset_id: int,
    item_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """删除条目"""
    tenant_id = get_current_tenant()
    service = DatasetService(session)
    deleted = await service.delete_item(dataset_id, item_id, tenant_id=tenant_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"条目 {item_id} 不存在",
        )
    await session.commit()
    return {"deleted": True, "item_id": item_id}


# ============================================================
# 统计与导出
# ============================================================


@router.get("/{dataset_id}/stats", response_model=Dict[str, Any])
async def get_dataset_stats(
    dataset_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """数据集统计 (总数/已标注/待标注)"""
    tenant_id = get_current_tenant()
    service = DatasetService(session)
    return await service.get_dataset_stats(dataset_id, tenant_id=tenant_id)


@router.get("/{dataset_id}/export", response_class=PlainTextResponse)
async def export_dataset(
    dataset_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    format: str = Query(default="json", description="格式: json/csv"),
):
    """导出数据集 (JSON/CSV)"""
    tenant_id = get_current_tenant()
    service = DatasetService(session)
    try:
        content = await service.export_dataset(
            dataset_id, format, tenant_id=tenant_id
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        )

    media_type = "application/json" if format.lower() == "json" else "text/csv"
    filename = f"dataset_{dataset_id}.{format.lower()}"
    return PlainTextResponse(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
