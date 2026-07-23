"""GraphRAG 知识图谱数据模型

对标 RagFlow GraphRAG + RAPTOR:
- KnowledgeGraphEntity: 知识图谱实体 (从文档中抽取的人/组织/概念/事件等)
- KnowledgeGraphRelation: 实体间关系 (works_for/manages/reports_to/collaborates_with 等)
- KnowledgeGraphTask: 实体关系抽取任务 (从文档集合中异步抽取实体与关系, 构建知识图谱)

entity_type:
- person: 人员 (员工/候选人/外部联系人)
- organization: 组织 (公司/部门/团队)
- concept: 概念 (技能领域/业务概念/方法论)
- event: 事件 (项目/会议/培训/评估周期)
- location: 地点 (办公地/区域)
- skill: 技能 (硬技能/软技能)
- position: 职位 (岗位/职级)
- department: 部门 (业务单元)

relation_type:
- works_for: 任职于 (person -> organization/department)
- manages: 管理 (person -> person/department)
- reports_to: 汇报给 (person -> person)
- collaborates_with: 协作 (person -> person)
- requires_skill: 要求技能 (position/event -> skill)
- part_of: 隶属 (department -> organization, person -> department)
- located_in: 位于 (organization/person -> location)
- evaluates: 评估 (event -> person)
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


def _now_utc() -> datetime:
    """当前 UTC 时间"""
    return datetime.now(timezone.utc)


class KnowledgeGraphEntity(Base):
    """知识图谱实体

    从文档中抽取的实体, 同名实体在租户内唯一 (tenant_id + name 联合唯一索引)。
    source_docs 记录实体来源的文档 ID 列表, 便于溯源与增量更新。
    properties 存储实体附加属性 (如 person 的职位/工龄, skill 的熟练度等级等)。
    embedding_id 为实体描述在向量存储中的 ID, 支持实体级向量检索。
    """

    __tablename__ = "knowledge_graph_entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default="default"
    )
    # 实体名称 (规范化后的唯一标识, 同租户内同名实体合并)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    # 实体类型: person/organization/concept/event/location/skill/position/department
    entity_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="concept"
    )
    # 实体描述 (LLM 抽取的自然语言描述)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 附加属性 (键值对, 如 {"level": "senior", "department": "tech"})
    properties: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 来源文档 ID 列表 (便于溯源与增量更新)
    source_docs: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 向量存储 ID (实体描述的 embedding 在向量库中的 ID)
    embedding_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, onupdate=_now_utc
    )

    __table_args__ = (
        # 租户内实体名称唯一索引 (用于实体合并/去重)
        Index("ix_kg_entity_tenant_name", "tenant_id", "name"),
        # 租户内按实体类型查询索引
        Index("ix_kg_entity_tenant_type", "tenant_id", "entity_type"),
    )


class KnowledgeGraphRelation(Base):
    """知识图谱关系 (实体间的有向边)

    source_entity_id / target_entity_id 为实体外键, 删除实体时级联删除关联关系。
    weight 为关系强度 (0~1, LLM 抽取或聚合得出), 用于图遍历时的路径权重排序。
    properties 存储关系附加属性 (如 works_for 的入职时间, manages 的管理跨度等)。
    """

    __tablename__ = "knowledge_graph_relations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default="default"
    )
    # 源实体 ID (外键, 删除实体时级联删除关系)
    source_entity_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("knowledge_graph_entities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 目标实体 ID (外键, 删除实体时级联删除关系)
    target_entity_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("knowledge_graph_entities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 关系类型: works_for/manages/reports_to/collaborates_with/requires_skill/part_of/located_in/evaluates
    relation_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="related_to"
    )
    # 关系权重 (0.0~1.0, 用于图遍历路径排序)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    # 关系附加属性
    properties: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 来源文档 ID 列表
    source_docs: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )

    __table_args__ = (
        # 按源实体查询关联关系 (图遍历正向)
        Index("ix_kg_rel_tenant_source", "tenant_id", "source_entity_id"),
        # 按目标实体查询关联关系 (图遍历反向)
        Index("ix_kg_rel_tenant_target", "tenant_id", "target_entity_id"),
        # 按关系类型查询
        Index("ix_kg_rel_tenant_type", "tenant_id", "relation_type"),
    )


class KnowledgeGraphTask(Base):
    """知识图谱抽取任务

    从指定 collection 的文档集合中抽取实体与关系, 构建知识图谱。
    status: pending / processing / completed / failed
    document_ids: 待抽取的文档 ID 列表 (对应 ChromaDB collection 中的 kb_id)。
    entity_count / relation_count: 抽取完成后统计的实体/关系数量。
    """

    __tablename__ = "knowledge_graph_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default="default"
    )
    # 任务名称
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    # ChromaDB collection 名称 (文档来源)
    collection_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 待抽取的文档 ID 列表
    document_ids: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 状态: pending / processing / completed / failed
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    # 抽取的实体数量
    entity_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 抽取的关系数量
    relation_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 错误信息
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_kg_task_tenant_status", "tenant_id", "status"),
        Index("ix_kg_task_tenant_created", "tenant_id", "created_at"),
    )
