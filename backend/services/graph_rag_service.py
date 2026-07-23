"""GraphRAG 知识图谱服务

对标 RagFlow GraphRAG + RAPTOR:
- 从文档中抽取实体和关系 (LLM 驱动), 构建知识图谱
- 图增强检索: 向量检索获取相关文档 → 提取命中实体 → 图遍历获取关联实体 → 合并上下文
- 知识图谱可视化 (nodes + edges), 实体/关系 CRUD

异步任务:
- run_extraction 用 asyncio.create_task() 后台执行 (独立 session + tenant_scope)
- LLM 抽取结果用 call_llm_with_fallback 调用 (core/llm_call.py)
- LLM 返回 JSON 需容错解析 (json.loads 失败时正则回退提取)

事务边界由路由层控制 (create_task / run_extraction 内部不 commit)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import AsyncSessionLocal
from core.llm_call import call_llm_with_fallback
from core.providers.base import ChatMessage
from core.tenant_context import tenant_scope
from models.knowledge_graph_models import (
    KnowledgeGraphEntity,
    KnowledgeGraphRelation,
    KnowledgeGraphTask,
)

logger = logging.getLogger(__name__)

# 支持的实体类型
SUPPORTED_ENTITY_TYPES = {
    "person",
    "organization",
    "concept",
    "event",
    "location",
    "skill",
    "position",
    "department",
}

# 支持的关系类型
SUPPORTED_RELATION_TYPES = {
    "works_for",
    "manages",
    "reports_to",
    "collaborates_with",
    "requires_skill",
    "part_of",
    "located_in",
    "evaluates",
}

# 任务状态
TASK_STATUS_PENDING = "pending"
TASK_STATUS_PROCESSING = "processing"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"

# 实体抽取单次最大文本长度 (避免 LLM 上下文过长)
_MAX_EXTRACTION_TEXT_LEN = 4000

# 图遍历单跳最大邻居数 (防止超大规模图谱爆炸)
_MAX_NEIGHBORS_PER_HOP = 50


# 实体关系抽取 prompt (对标 RagFlow GraphRAG 抽取模板)
_EXTRACTION_PROMPT_TEMPLATE = """你是一个专业的知识图谱抽取助手。请从以下文本中抽取实体和关系。

## 实体类型
person(人员), organization(组织), concept(概念), event(事件), location(地点), skill(技能), position(职位), department(部门)

## 关系类型
works_for(任职于), manages(管理), reports_to(汇报给), collaborates_with(协作), requires_skill(要求技能), part_of(隶属), located_in(位于), evaluates(评估)

## 输出要求
仅返回 JSON 格式, 不要包含任何解释文字或 markdown 代码块标记:
{{"entities": [{{"name": "实体名称", "type": "person", "description": "实体描述"}}], "relations": [{{"source": "源实体名称", "target": "目标实体名称", "type": "works_for", "weight": 0.8}}]}}

## 注意事项
- name 为实体的规范化名称 (去除前后缀, 统一称呼)
- type 必须是上述实体类型之一
- weight 为关系强度 (0.0~1.0), 强关系 0.8~1.0, 一般关系 0.5~0.7, 弱关系 0.1~0.4
- source 和 target 必须是 entities 中已出现的实体 name
- 若文本无明确实体关系, 返回 {{"entities": [], "relations": []}}

## 文本
{text}"""


class GraphRAGService:
    """GraphRAG 知识图谱服务

    提供知识图谱的构建 (实体关系抽取)、图增强检索、图谱可视化与 CRUD。

    Args:
        session: 异步数据库会话。
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    # ===================== 任务 CRUD =====================

    async def create_extraction_task(
        self,
        name: str,
        collection_name: str,
        document_ids: Optional[List[str]] = None,
        *,
        tenant_id: str = "default",
    ) -> KnowledgeGraphTask:
        """创建实体关系抽取任务

        Args:
            name: 任务名称。
            collection_name: ChromaDB collection 名称 (文档来源)。
            document_ids: 待抽取的文档 ID 列表, 为空时抽取 collection 内全部文档。
            tenant_id: 租户 ID。

        Returns:
            创建的 KnowledgeGraphTask 对象。

        Raises:
            ValueError: 参数无效。
        """
        if not name or not name.strip():
            raise ValueError("任务名称不能为空")
        if not collection_name or not collection_name.strip():
            raise ValueError("collection_name 不能为空")

        task = KnowledgeGraphTask(
            tenant_id=tenant_id,
            name=name.strip(),
            collection_name=collection_name.strip(),
            document_ids=document_ids if document_ids is not None else [],
            status=TASK_STATUS_PENDING,
        )
        self.session.add(task)
        await self.session.flush()
        logger.info(
            "创建知识图谱抽取任务 id=%s name=%s collection=%s tenant=%s docs=%s",
            task.id,
            name,
            collection_name,
            tenant_id,
            len(document_ids) if document_ids else 0,
        )
        return task

    async def get_task(
        self, task_id: int, *, tenant_id: str = "default"
    ) -> Optional[KnowledgeGraphTask]:
        """获取抽取任务 (按 task_id + tenant_id 过滤, 防跨租户访问)"""
        return (
            await self.session.execute(
                select(KnowledgeGraphTask).where(
                    KnowledgeGraphTask.id == task_id,
                    KnowledgeGraphTask.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()

    async def list_tasks(
        self,
        *,
        status_filter: Optional[str] = None,
        page: int = 1,
        size: int = 20,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """任务列表 (分页)"""
        base = (
            select(KnowledgeGraphTask)
            .where(KnowledgeGraphTask.tenant_id == tenant_id)
            .order_by(KnowledgeGraphTask.created_at.desc())
        )
        if status_filter:
            base = base.where(KnowledgeGraphTask.status == status_filter)

        total = (
            await self.session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar() or 0

        offset = (page - 1) * size
        rows = (
            (await self.session.execute(base.offset(offset).limit(size)))
            .scalars()
            .all()
        )

        return {
            "items": [self._task_to_dict(t) for t in rows],
            "total": total,
            "page": page,
            "size": size,
        }

    async def delete_task(self, task_id: int, *, tenant_id: str = "default") -> bool:
        """删除抽取任务 (仅删除任务记录, 不删除已抽取的实体/关系)"""
        task = await self.get_task(task_id, tenant_id=tenant_id)
        if task is None:
            return False
        await self.session.delete(task)
        await self.session.flush()
        return True

    # ===================== 任务执行 =====================

    def schedule_run(
        self,
        task_id: int,
        model_router: Any,
        kb_store: Any = None,
        *,
        tenant_id: str = "default",
    ) -> asyncio.Task:
        """用 asyncio.create_task() 后台执行抽取

        在独立 session 中执行 (避免与请求 session 冲突)。

        Args:
            task_id: 任务 ID。
            model_router: ModelRouter 实例 (用于 LLM 调用)。
            kb_store: ChromaCompanyKB 实例 (用于读取文档内容), 为 None 时按 collection_name 懒创建。
            tenant_id: 租户 ID (用于设置后台任务租户上下文)。

        Returns:
            asyncio.Task 对象。
        """
        return asyncio.create_task(
            self._run_extraction_async(
                task_id, model_router, kb_store, tenant_id=tenant_id
            )
        )

    async def _run_extraction_async(
        self,
        task_id: int,
        model_router: Any,
        kb_store: Any,
        *,
        tenant_id: str = "default",
    ) -> None:
        """后台执行抽取 (独立 session + tenant_scope)"""
        with tenant_scope(tenant_id):
            async with AsyncSessionLocal() as session:
                service = GraphRAGService(session)
                try:
                    await service.run_extraction(
                        task_id,
                        model_router=model_router,
                        kb_store=kb_store,
                        tenant_id=tenant_id,
                    )
                    await session.commit()
                except Exception as e:
                    logger.exception(
                        "知识图谱抽取后台任务失败 task_id=%s: %s", task_id, e
                    )
                    await session.rollback()

    async def run_extraction(
        self,
        task_id: int,
        *,
        model_router: Any,
        kb_store: Any = None,
        tenant_id: str = "default",
    ) -> KnowledgeGraphTask:
        """执行实体关系抽取任务

        1. 加载任务 (按 task_id + tenant_id 过滤, 防跨租户访问)
        2. 从 collection 中读取文档内容
        3. 用 LLM 抽取实体和关系 (分块处理, 避免上下文过长)
        4. 实体去重合并 (同租户同名实体合并 source_docs), 存储到数据库
        5. 解析关系 (按实体 name 映射到 entity_id), 存储到数据库
        6. 更新任务统计 (entity_count / relation_count) + status=completed

        Args:
            task_id: 任务 ID。
            model_router: ModelRouter 实例。
            kb_store: ChromaCompanyKB 实例, 为 None 时按 collection_name 懒创建。
            tenant_id: 租户 ID。

        Returns:
            更新后的 KnowledgeGraphTask 对象。

        Raises:
            ValueError: 任务不存在 / 无可抽取文档。
        """
        task = await self.get_task(task_id, tenant_id=tenant_id)
        if task is None:
            raise ValueError(f"抽取任务 {task_id} 不存在")

        task.status = TASK_STATUS_PROCESSING
        task.error_message = None
        await self.session.flush()

        try:
            # 1. 读取文档内容
            documents = await self._load_documents(
                task.collection_name,
                task.document_ids if isinstance(task.document_ids, list) else [],
                kb_store=kb_store,
                tenant_id=tenant_id,
            )
            if not documents:
                raise ValueError(
                    f"collection {task.collection_name} 中未找到可抽取的文档"
                )

            # 2. 逐文档抽取实体和关系
            all_entities: List[Dict[str, Any]] = []
            all_relations: List[Dict[str, Any]] = []
            for doc in documents:
                doc_id = doc.get("id", "")
                text = doc.get("content", "") or ""
                if not text.strip():
                    continue
                try:
                    entities, relations = await self._extract_entities_and_relations(
                        text, model_router=model_router
                    )
                except Exception as e:
                    logger.warning("文档 %s 实体抽取失败: %s", doc_id, e, exc_info=True)
                    continue
                # 标注来源文档
                for ent in entities:
                    ent["_source_doc"] = doc_id
                for rel in relations:
                    rel["_source_doc"] = doc_id
                all_entities.extend(entities)
                all_relations.extend(relations)

            # 3. 实体去重合并并持久化 (返回 name -> entity_id 映射)
            name_to_id = await self._persist_entities(all_entities, tenant_id=tenant_id)

            # 4. 关系解析并持久化
            relation_count = await self._persist_relations(
                all_relations, name_to_id, tenant_id=tenant_id
            )

            task.entity_count = len(name_to_id)
            task.relation_count = relation_count
            task.status = TASK_STATUS_COMPLETED
            task.completed_at = datetime.now(timezone.utc)
            task.error_message = None
            await self.session.flush()
            logger.info(
                "知识图谱抽取完成 task_id=%s entities=%s relations=%s",
                task_id,
                task.entity_count,
                task.relation_count,
            )
        except Exception as e:
            logger.exception("知识图谱抽取失败 task_id=%s: %s", task_id, e)
            task.status = TASK_STATUS_FAILED
            task.error_message = str(e)
            await self.session.flush()
            raise

        return task

    # ===================== 文档加载 =====================

    async def _load_documents(
        self,
        collection_name: str,
        document_ids: List[str],
        *,
        kb_store: Any = None,
        tenant_id: str = "default",
    ) -> List[Dict[str, Any]]:
        """从 ChromaDB collection 中读取文档内容

        Args:
            collection_name: ChromaDB collection 名称。
            document_ids: 指定文档 ID 列表, 为空时读取 collection 内全部文档。
            kb_store: ChromaCompanyKB 实例, 为 None 时按 collection_name 懒创建。
            tenant_id: 租户 ID。

        Returns:
            文档列表, 每项 {"id": str, "content": str, "title": str, "metadata": dict}。
        """
        store = self._resolve_kb_store(kb_store, collection_name, tenant_id)
        if store is None:
            logger.warning("无法获取向量库实例, 跳过文档加载")
            return []

        collection = getattr(store, "collection", None)
        client = getattr(store, "client", None)
        # 若指定 collection_name 与 store 默认 collection 不一致, 取对应 collection
        if client is not None:
            try:
                current_name = collection.name if collection is not None else None
                if current_name != collection_name:
                    embedding = getattr(store, "embedding", None)
                    kwargs: Dict[str, Any] = {
                        "name": collection_name,
                        "metadata": {"hnsw:space": "cosine"},
                    }
                    if embedding is not None:
                        kwargs["embedding_function"] = embedding
                    collection = await asyncio.to_thread(
                        client.get_or_create_collection, **kwargs
                    )
            except Exception as e:
                logger.warning("获取 collection %s 失败: %s", collection_name, e)

        if collection is None:
            return []

        try:
            get_kwargs: Dict[str, Any] = {"include": ["metadatas", "documents"]}
            if document_ids:
                get_kwargs["ids"] = list(document_ids)
            result = await asyncio.to_thread(collection.get, **get_kwargs)
        except Exception as e:
            logger.warning("读取 collection %s 文档失败: %s", collection_name, e)
            return []

        ids = result.get("ids", []) or []
        documents = result.get("documents", []) or []
        metadatas = result.get("metadatas", []) or []

        output: List[Dict[str, Any]] = []
        for i, doc_id in enumerate(ids):
            content = documents[i] if i < len(documents) else ""
            if not content or not content.strip():
                continue
            meta = metadatas[i] if i < len(metadatas) and metadatas[i] else {}
            # metadata 可能含嵌套 JSON 字符串 (ChromaCompanyKB.add_document 存储方式)
            title = meta.get("title", "") if isinstance(meta, dict) else ""
            output.append(
                {
                    "id": doc_id,
                    "content": content,
                    "title": title,
                    "metadata": meta,
                }
            )
        return output

    def _resolve_kb_store(
        self, kb_store: Any, collection_name: str, tenant_id: str
    ) -> Any:
        """解析向量库实例: 优先用传入的 kb_store, 否则按 collection_name 懒创建"""
        if kb_store is not None:
            return kb_store
        try:
            from memory.vector_store import ChromaCompanyKB

            return ChromaCompanyKB(collection_name=collection_name, tenant_id=tenant_id)
        except Exception as e:
            logger.warning("创建 ChromaCompanyKB 失败: %s", e)
            return None

    # ===================== 实体关系抽取 =====================

    async def _extract_entities_and_relations(
        self, text: str, *, model_router: Any
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """用 LLM 从文本中抽取实体和关系

        长文本分块处理, 每块独立抽取后合并。
        LLM 返回 JSON, 解析时容错: json.loads 失败则正则回退提取。

        Args:
            text: 待抽取的文本。
            model_router: ModelRouter 实例。

        Returns:
            (entities, relations):
            - entities: [{"name", "type", "description"}]
            - relations: [{"source", "target", "type", "weight"}]
        """
        chunks = self._split_text_for_extraction(text)
        all_entities: List[Dict[str, Any]] = []
        all_relations: List[Dict[str, Any]] = []

        for chunk in chunks:
            if not chunk.strip():
                continue
            prompt = _EXTRACTION_PROMPT_TEMPLATE.format(text=chunk)
            messages = [
                ChatMessage(role="system", content=prompt),
                ChatMessage(
                    role="user", content="请抽取上述文本中的实体和关系, 仅返回 JSON。"
                ),
            ]
            try:
                completion, _tier = await call_llm_with_fallback(
                    model_router, messages=messages
                )
                content = completion.content or ""
            except Exception as e:
                logger.warning("LLM 实体抽取调用失败: %s", e)
                continue

            entities, relations = self._parse_extraction_response(content)
            all_entities.extend(entities)
            all_relations.extend(relations)

        return all_entities, all_relations

    def _split_text_for_extraction(self, text: str) -> List[str]:
        """将长文本分块, 每块不超过 _MAX_EXTRACTION_TEXT_LEN 字符

        按段落边界切分, 避免截断句子。
        """
        if len(text) <= _MAX_EXTRACTION_TEXT_LEN:
            return [text]
        chunks: List[str] = []
        paragraphs = re.split(r"\n\s*\n", text)
        current = ""
        for para in paragraphs:
            if not para.strip():
                continue
            if len(current) + len(para) + 2 <= _MAX_EXTRACTION_TEXT_LEN:
                current = (current + "\n\n" + para) if current else para
            else:
                if current:
                    chunks.append(current)
                # 单段落超长时硬切分
                if len(para) > _MAX_EXTRACTION_TEXT_LEN:
                    for i in range(0, len(para), _MAX_EXTRACTION_TEXT_LEN):
                        chunks.append(para[i : i + _MAX_EXTRACTION_TEXT_LEN])
                    current = ""
                else:
                    current = para
        if current:
            chunks.append(current)
        return chunks

    @staticmethod
    def _parse_extraction_response(
        content: str,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """解析 LLM 抽取响应, 容错: JSON 解析失败时正则回退提取

        解析顺序:
        1. 直接 json.loads (标准 JSON)
        2. 提取 ```json ... ``` 或 { ... } 代码块后 json.loads
        3. 正则回退: 逐条提取 entities/relations 字段

        Returns:
            (entities, relations)
        """
        if not content or not content.strip():
            return [], []

        # 1. 直接 json.loads
        data = GraphRAGService._safe_json_loads(content)
        if data is None:
            # 2. 提取 JSON 代码块 / 最外层花括号
            data = GraphRAGService._extract_json_block(content)

        if isinstance(data, dict):
            entities = GraphRAGService._normalize_entities(data.get("entities", []))
            relations = GraphRAGService._normalize_relations(data.get("relations", []))
            return entities, relations

        # 3. 正则回退提取
        logger.warning("LLM 抽取响应 JSON 解析失败, 启用正则回退提取")
        return GraphRAGService._regex_fallback_extract(content)

    @staticmethod
    def _safe_json_loads(content: str) -> Optional[Any]:
        """安全 json.loads, 失败返回 None"""
        try:
            return json.loads(content)
        except (json.JSONDecodeError, ValueError, TypeError):
            return None

    @staticmethod
    def _extract_json_block(content: str) -> Optional[Any]:
        """提取 ```json ... ``` 代码块或最外层 { ... } 后 json.loads"""
        # markdown 代码块
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
        if m:
            parsed = GraphRAGService._safe_json_loads(m.group(1))
            if parsed is not None:
                return parsed
        # 最外层花括号
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if m:
            parsed = GraphRAGService._safe_json_loads(m.group(0))
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _normalize_entities(raw: Any) -> List[Dict[str, Any]]:
        """规范化实体列表, 过滤无效项"""
        if not isinstance(raw, list):
            return []
        output: List[Dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            ent_type = str(item.get("type", "concept")).strip().lower()
            if ent_type not in SUPPORTED_ENTITY_TYPES:
                ent_type = "concept"
            description = str(item.get("description", "")).strip()
            output.append({"name": name, "type": ent_type, "description": description})
        return output

    @staticmethod
    def _normalize_relations(raw: Any) -> List[Dict[str, Any]]:
        """规范化关系列表, 过滤无效项"""
        if not isinstance(raw, list):
            return []
        output: List[Dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source", "")).strip()
            target = str(item.get("target", "")).strip()
            if not source or not target:
                continue
            rel_type = str(item.get("type", "related_to")).strip().lower()
            if rel_type not in SUPPORTED_RELATION_TYPES:
                rel_type = "related_to"
            try:
                weight = float(item.get("weight", 0.5))
            except (TypeError, ValueError):
                weight = 0.5
            weight = max(0.0, min(1.0, weight))
            output.append(
                {
                    "source": source,
                    "target": target,
                    "type": rel_type,
                    "weight": weight,
                }
            )
        return output

    @staticmethod
    def _regex_fallback_extract(
        content: str,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """正则回退提取实体和关系 (JSON 解析全部失败时)

        尝试匹配形如 "name": "...", "type": "...", "description": "..." 的实体对象,
        以及 "source": "...", "target": "...", "type": "...", "weight": 0.8 的关系对象。
        """
        entities: List[Dict[str, Any]] = []
        relations: List[Dict[str, Any]] = []

        # 提取实体: 匹配 {"name": "x", "type": "y", "description": "z"}
        ent_pattern = re.compile(
            r'\{\s*"name"\s*:\s*"([^"]+)"\s*,\s*"type"\s*:\s*"([^"]+)"'
            r'(?:\s*,\s*"description"\s*:\s*"([^"]*)")?\s*\}',
            re.DOTALL,
        )
        for m in ent_pattern.finditer(content):
            name = m.group(1).strip()
            ent_type = m.group(2).strip().lower()
            if ent_type not in SUPPORTED_ENTITY_TYPES:
                ent_type = "concept"
            desc = (m.group(3) or "").strip()
            entities.append({"name": name, "type": ent_type, "description": desc})

        # 提取关系: 匹配 {"source": "x", "target": "y", "type": "z", "weight": 0.8}
        rel_pattern = re.compile(
            r'\{\s*"source"\s*:\s*"([^"]+)"\s*,\s*"target"\s*:\s*"([^"]+)"'
            r'\s*,\s*"type"\s*:\s*"([^"]+)"'
            r'(?:\s*,\s*"weight"\s*:\s*([0-9.]+))?\s*\}',
            re.DOTALL,
        )
        for m in rel_pattern.finditer(content):
            source = m.group(1).strip()
            target = m.group(2).strip()
            rel_type = m.group(3).strip().lower()
            if rel_type not in SUPPORTED_RELATION_TYPES:
                rel_type = "related_to"
            try:
                weight = float(m.group(4)) if m.group(4) else 0.5
            except (TypeError, ValueError):
                weight = 0.5
            weight = max(0.0, min(1.0, weight))
            relations.append(
                {
                    "source": source,
                    "target": target,
                    "type": rel_type,
                    "weight": weight,
                }
            )

        return entities, relations

    # ===================== 实体关系持久化 =====================

    async def _persist_entities(
        self, entities: List[Dict[str, Any]], *, tenant_id: str
    ) -> Dict[str, int]:
        """实体去重合并并持久化, 返回 name -> entity_id 映射

        同租户同名实体合并: 更新 description / source_docs / properties, 不重复创建。
        """
        name_to_id: Dict[str, int] = {}
        # 先加载租户内已有的同名实体 (减少单条查询)
        names = {e["name"] for e in entities if e.get("name")}
        existing_map: Dict[str, KnowledgeGraphEntity] = {}
        if names:
            existing_rows = (
                (
                    await self.session.execute(
                        select(KnowledgeGraphEntity).where(
                            KnowledgeGraphEntity.tenant_id == tenant_id,
                            KnowledgeGraphEntity.name.in_(names),
                        )
                    )
                )
                .scalars()
                .all()
            )
            existing_map = {r.name: r for r in existing_rows}

        for ent in entities:
            name = ent.get("name", "").strip()
            if not name:
                continue
            ent_type = ent.get("type", "concept")
            description = ent.get("description", "")
            source_doc = ent.get("_source_doc", "")

            if name in existing_map:
                # 数据库已存在或本批次已创建, 统一合并字段 (description + source_docs)
                row = existing_map[name]
                if description and description not in (row.description or ""):
                    row.description = (
                        (row.description + "\n" + description)
                        if row.description
                        else description
                    )
                self._merge_source_docs(row, source_doc)
                row.updated_at = datetime.now(timezone.utc)
                name_to_id[name] = row.id
            else:
                # 新建实体
                row = KnowledgeGraphEntity(
                    tenant_id=tenant_id,
                    name=name,
                    entity_type=ent_type,
                    description=description,
                    properties={},
                    source_docs=[source_doc] if source_doc else [],
                )
                self.session.add(row)
                await self.session.flush()
                name_to_id[name] = row.id
                existing_map[name] = row

        return name_to_id

    @staticmethod
    def _merge_source_docs(entity: KnowledgeGraphEntity, doc_id: str) -> None:
        """合并来源文档 ID 到实体 source_docs (去重)

        注意: JSON 列需重新赋值新 list (而非 in-place mutate), 否则 SQLAlchemy
        无法检测变更 (JSON 列默认不追踪原地修改)。
        """
        if not doc_id:
            return
        docs = list(entity.source_docs) if isinstance(entity.source_docs, list) else []
        if doc_id not in docs:
            docs.append(doc_id)
        # 重新赋值新 list 引用, 确保 SQLAlchemy 标记属性为 dirty
        entity.source_docs = docs

    async def _persist_relations(
        self,
        relations: List[Dict[str, Any]],
        name_to_id: Dict[str, int],
        *,
        tenant_id: str,
    ) -> int:
        """关系解析并持久化, 返回新增关系数

        按 (source_entity_id, target_entity_id, relation_type) 去重, 已存在则更新 weight。
        """
        # 加载租户内已有的同源同目标同类型关系 (批量去重)
        existing_rel_keys: Set[Tuple[int, int, str]] = set()
        existing_rel_map: Dict[Tuple[int, int, str], KnowledgeGraphRelation] = {}
        entity_ids = set(name_to_id.values())
        if entity_ids:
            existing_rels = (
                (
                    await self.session.execute(
                        select(KnowledgeGraphRelation).where(
                            KnowledgeGraphRelation.tenant_id == tenant_id,
                            KnowledgeGraphRelation.source_entity_id.in_(entity_ids),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for r in existing_rels:
                key = (r.source_entity_id, r.target_entity_id, r.relation_type)
                existing_rel_keys.add(key)
                existing_rel_map[key] = r

        count = 0
        seen_keys: Set[Tuple[int, int, str]] = set()
        for rel in relations:
            source_name = rel.get("source", "").strip()
            target_name = rel.get("target", "").strip()
            if not source_name or not target_name:
                continue
            source_id = name_to_id.get(source_name)
            target_id = name_to_id.get(target_name)
            if source_id is None or target_id is None:
                # 实体未在本次抽取中出现, 跳过 (避免悬空外键)
                continue
            rel_type = rel.get("type", "related_to")
            weight = rel.get("weight", 0.5)
            source_doc = rel.get("_source_doc", "")

            key = (source_id, target_id, rel_type)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            if key in existing_rel_map:
                # 已存在, 取较大 weight 并合并 source_docs
                row = existing_rel_map[key]
                row.weight = max(row.weight, weight)
                docs = (
                    list(row.source_docs) if isinstance(row.source_docs, list) else []
                )
                if source_doc and source_doc not in docs:
                    docs.append(source_doc)
                # 重新赋值新 list 引用, 确保 SQLAlchemy 标记 JSON 属性为 dirty
                row.source_docs = docs
                count += 1
            else:
                row = KnowledgeGraphRelation(
                    tenant_id=tenant_id,
                    source_entity_id=source_id,
                    target_entity_id=target_id,
                    relation_type=rel_type,
                    weight=weight,
                    properties={},
                    source_docs=[source_doc] if source_doc else [],
                )
                self.session.add(row)
                await self.session.flush()
                existing_rel_map[key] = row
                count += 1

        return count

    # ===================== 图增强检索 =====================

    async def search_with_graph(
        self,
        query: str,
        collection_name: str,
        depth: int = 2,
        *,
        top_k: int = 5,
        kb_store: Any = None,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """图增强检索

        1. 向量检索获取相关文档
        2. 从文档中提取命中的实体 (实体名称匹配)
        3. 做图遍历 (depth 跳) 获取关联实体
        4. 合并上下文返回 (文档 + 实体描述 + 关系描述)

        Args:
            query: 查询文本。
            collection_name: ChromaDB collection 名称。
            depth: 图遍历深度 (跳数)。
            top_k: 向量检索返回文档数。
            kb_store: ChromaCompanyKB 实例, 为 None 时懒创建。
            tenant_id: 租户 ID。

        Returns:
            {"query", "documents", "entities", "relations", "graph_context", "depth"}
        """
        depth = max(0, min(int(depth), 5))

        # 1. 向量检索获取相关文档
        store = self._resolve_kb_store(kb_store, collection_name, tenant_id)
        documents: List[Dict[str, Any]] = []
        if store is not None:
            try:
                results = await store.query(query, top_k=top_k)
            except Exception as e:
                logger.warning("向量检索失败: %s", e)
                results = []
            for r in results or []:
                if not isinstance(r, dict):
                    continue
                content = r.get("content", "") or ""
                documents.append(
                    {
                        "content": content,
                        "score": float(r.get("_retrieval_score", r.get("score", 0.0))),
                        "metadata": r.get("metadata", {}) or {},
                        "kb_id": r.get("kb_id", ""),
                    }
                )

        # 2. 从文档中提取命中的实体 (实体名称在文档中出现即为命中)
        hit_entity_ids: Set[int] = set()
        if documents:
            doc_text = "\n".join(d["content"] for d in documents)
            # 加载租户内全部实体名称 (规模可控; 大规模时可走倒排索引)
            all_entities = (
                await self.session.execute(
                    select(KnowledgeGraphEntity.id, KnowledgeGraphEntity.name).where(
                        KnowledgeGraphEntity.tenant_id == tenant_id
                    )
                )
            ).all()
            for eid, ename in all_entities:
                if ename and ename in doc_text:
                    hit_entity_ids.add(eid)

        # 3. 图遍历获取关联实体 (BFS, depth 跳)
        related_entity_ids = await self._graph_traverse(
            hit_entity_ids, depth=depth, tenant_id=tenant_id
        )

        # 4. 加载实体与关系详情
        all_entity_ids = hit_entity_ids | related_entity_ids
        entities: List[Dict[str, Any]] = []
        relations: List[Dict[str, Any]] = []
        if all_entity_ids:
            entity_rows = (
                (
                    await self.session.execute(
                        select(KnowledgeGraphEntity).where(
                            KnowledgeGraphEntity.tenant_id == tenant_id,
                            KnowledgeGraphEntity.id.in_(all_entity_ids),
                        )
                    )
                )
                .scalars()
                .all()
            )
            entities = [self._entity_to_dict(e) for e in entity_rows]

            relation_rows = (
                (
                    await self.session.execute(
                        select(KnowledgeGraphRelation).where(
                            KnowledgeGraphRelation.tenant_id == tenant_id,
                            KnowledgeGraphRelation.source_entity_id.in_(all_entity_ids),
                            KnowledgeGraphRelation.target_entity_id.in_(all_entity_ids),
                        )
                    )
                )
                .scalars()
                .all()
            )
            relations = [self._relation_to_dict(r) for r in relation_rows]

        # 5. 构建图上下文
        graph_context = self._build_graph_context(entities, relations, depth)

        return {
            "query": query,
            "documents": documents,
            "entities": entities,
            "relations": relations,
            "graph_context": graph_context,
            "depth": depth,
            "hit_entity_count": len(hit_entity_ids),
            "total_entity_count": len(all_entity_ids),
        }

    async def _graph_traverse(
        self,
        seed_entity_ids: Set[int],
        depth: int,
        *,
        tenant_id: str,
    ) -> Set[int]:
        """BFS 图遍历, 从种子实体出发 depth 跳, 返回关联实体 ID 集合 (不含种子)"""
        if not seed_entity_ids or depth <= 0:
            return set()

        visited: Set[int] = set(seed_entity_ids)
        frontier: deque[int] = deque(seed_entity_ids)
        result: Set[int] = set()

        for _hop in range(depth):
            if not frontier:
                break
            next_frontier: deque[int] = deque()
            # 批量查询当前层实体的邻居
            current_ids = list(frontier)
            # 正向邻居 (当前实体作为 source)
            fwd_rows = (
                (
                    await self.session.execute(
                        select(KnowledgeGraphRelation.target_entity_id).where(
                            KnowledgeGraphRelation.tenant_id == tenant_id,
                            KnowledgeGraphRelation.source_entity_id.in_(current_ids),
                        )
                    )
                )
                .scalars()
                .all()
            )
            # 反向邻居 (当前实体作为 target)
            bwd_rows = (
                (
                    await self.session.execute(
                        select(KnowledgeGraphRelation.source_entity_id).where(
                            KnowledgeGraphRelation.tenant_id == tenant_id,
                            KnowledgeGraphRelation.target_entity_id.in_(current_ids),
                        )
                    )
                )
                .scalars()
                .all()
            )

            neighbors = list(fwd_rows) + list(bwd_rows)
            # 限制单跳邻居数, 防止超大规模图谱爆炸
            if len(neighbors) > _MAX_NEIGHBORS_PER_HOP:
                neighbors = neighbors[:_MAX_NEIGHBORS_PER_HOP]

            for nid in neighbors:
                if nid not in visited:
                    visited.add(nid)
                    result.add(nid)
                    next_frontier.append(nid)
            frontier = next_frontier

        return result

    def _build_graph_context(
        self,
        entities: List[Dict[str, Any]],
        relations: List[Dict[str, Any]],
        depth: int,
    ) -> str:
        """构建图上下文文本 (供 LLM 检索增强使用)

        格式:
        ## 相关实体
        - [person] 张三: 资深前端工程师, 任职于技术部
        ## 实体关系
        - 张三 works_for 技术部 (权重: 0.9)
        """
        if not entities and not relations:
            return ""

        lines: List[str] = []
        if entities:
            lines.append("## 相关实体")
            for ent in entities:
                ent_type = ent.get("entity_type", "concept")
                name = ent.get("name", "")
                desc = ent.get("description", "")
                line = f"- [{ent_type}] {name}"
                if desc:
                    line += f": {desc}"
                lines.append(line)

        if relations:
            lines.append("")
            lines.append("## 实体关系")
            # 构建 id -> name 映射
            id_to_name = {e.get("id"): e.get("name", "") for e in entities}
            for rel in relations:
                source = id_to_name.get(
                    rel.get("source_entity_id"), rel.get("source_entity_id", "?")
                )
                target = id_to_name.get(
                    rel.get("target_entity_id"), rel.get("target_entity_id", "?")
                )
                rel_type = rel.get("relation_type", "related_to")
                weight = rel.get("weight", 0.5)
                lines.append(f"- {source} {rel_type} {target} (权重: {weight})")

        lines.append("")
        lines.append(f"(图遍历深度: {depth} 跳)")
        return "\n".join(lines)

    # ===================== 实体关系查询 =====================

    async def get_entities(
        self,
        tenant_id: str,
        entity_type: Optional[str] = None,
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """实体列表 (分页, 支持类型过滤)"""
        base = select(KnowledgeGraphEntity).where(
            KnowledgeGraphEntity.tenant_id == tenant_id
        )
        if entity_type:
            base = base.where(KnowledgeGraphEntity.entity_type == entity_type)
        base = base.order_by(KnowledgeGraphEntity.updated_at.desc())

        total = (
            await self.session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar() or 0

        offset = (page - 1) * size
        rows = (
            (await self.session.execute(base.offset(offset).limit(size)))
            .scalars()
            .all()
        )

        return {
            "items": [self._entity_to_dict(e) for e in rows],
            "total": total,
            "page": page,
            "size": size,
        }

    async def get_relations(
        self,
        tenant_id: str,
        entity_id: Optional[int] = None,
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """关系列表 (分页, 支持按实体过滤)"""
        base = select(KnowledgeGraphRelation).where(
            KnowledgeGraphRelation.tenant_id == tenant_id
        )
        if entity_id is not None:
            base = base.where(
                (KnowledgeGraphRelation.source_entity_id == entity_id)
                | (KnowledgeGraphRelation.target_entity_id == entity_id)
            )
        base = base.order_by(KnowledgeGraphRelation.weight.desc())

        total = (
            await self.session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar() or 0

        offset = (page - 1) * size
        rows = (
            (await self.session.execute(base.offset(offset).limit(size)))
            .scalars()
            .all()
        )

        # 加载实体名称用于展示
        entity_ids = set()
        for r in rows:
            entity_ids.add(r.source_entity_id)
            entity_ids.add(r.target_entity_id)
        id_to_name: Dict[int, str] = {}
        if entity_ids:
            ent_rows = (
                await self.session.execute(
                    select(KnowledgeGraphEntity.id, KnowledgeGraphEntity.name).where(
                        KnowledgeGraphEntity.id.in_(entity_ids)
                    )
                )
            ).all()
            id_to_name = {eid: ename for eid, ename in ent_rows}

        return {
            "items": [self._relation_to_dict(r, id_to_name=id_to_name) for r in rows],
            "total": total,
            "page": page,
            "size": size,
        }

    async def get_entity_detail(
        self, entity_id: int, *, tenant_id: str = "default"
    ) -> Optional[Dict[str, Any]]:
        """实体详情 + 关联关系"""
        entity = (
            await self.session.execute(
                select(KnowledgeGraphEntity).where(
                    KnowledgeGraphEntity.id == entity_id,
                    KnowledgeGraphEntity.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if entity is None:
            return None

        # 加载关联关系 (作为 source 或 target)
        rel_rows = (
            (
                await self.session.execute(
                    select(KnowledgeGraphRelation)
                    .where(
                        KnowledgeGraphRelation.tenant_id == tenant_id,
                        (KnowledgeGraphRelation.source_entity_id == entity_id)
                        | (KnowledgeGraphRelation.target_entity_id == entity_id),
                    )
                    .order_by(KnowledgeGraphRelation.weight.desc())
                )
            )
            .scalars()
            .all()
        )

        # 加载关联实体名称
        entity_ids = set()
        for r in rel_rows:
            entity_ids.add(r.source_entity_id)
            entity_ids.add(r.target_entity_id)
        id_to_name: Dict[int, str] = {entity.id: entity.name}
        related_ids = entity_ids - {entity.id}
        if related_ids:
            ent_rows = (
                await self.session.execute(
                    select(KnowledgeGraphEntity.id, KnowledgeGraphEntity.name).where(
                        KnowledgeGraphEntity.id.in_(related_ids)
                    )
                )
            ).all()
            for eid, ename in ent_rows:
                id_to_name[eid] = ename

        detail = self._entity_to_dict(entity)
        detail["relations"] = [
            self._relation_to_dict(r, id_to_name=id_to_name) for r in rel_rows
        ]
        detail["relation_count"] = len(rel_rows)
        return detail

    async def get_entity_relations(
        self, entity_id: int, *, tenant_id: str = "default"
    ) -> List[Dict[str, Any]]:
        """获取实体的关联关系列表"""
        # 先校验实体存在且属于当前租户
        entity = (
            await self.session.execute(
                select(KnowledgeGraphEntity.id, KnowledgeGraphEntity.name).where(
                    KnowledgeGraphEntity.id == entity_id,
                    KnowledgeGraphEntity.tenant_id == tenant_id,
                )
            )
        ).first()
        if entity is None:
            return []

        rel_rows = (
            (
                await self.session.execute(
                    select(KnowledgeGraphRelation)
                    .where(
                        KnowledgeGraphRelation.tenant_id == tenant_id,
                        (KnowledgeGraphRelation.source_entity_id == entity_id)
                        | (KnowledgeGraphRelation.target_entity_id == entity_id),
                    )
                    .order_by(KnowledgeGraphRelation.weight.desc())
                )
            )
            .scalars()
            .all()
        )

        # 加载关联实体名称
        entity_ids = set()
        for r in rel_rows:
            entity_ids.add(r.source_entity_id)
            entity_ids.add(r.target_entity_id)
        id_to_name: Dict[int, str] = {entity.id: entity.name}
        related_ids = entity_ids - {entity.id}
        if related_ids:
            ent_rows = (
                await self.session.execute(
                    select(KnowledgeGraphEntity.id, KnowledgeGraphEntity.name).where(
                        KnowledgeGraphEntity.id.in_(related_ids)
                    )
                )
            ).all()
            for eid, ename in ent_rows:
                id_to_name[eid] = ename

        return [self._relation_to_dict(r, id_to_name=id_to_name) for r in rel_rows]

    # ===================== 图谱可视化 =====================

    async def get_graph_visualization(
        self, entity_id: int, depth: int = 2, *, tenant_id: str = "default"
    ) -> Dict[str, Any]:
        """图谱可视化数据 (nodes + edges)

        从指定实体出发, BFS depth 跳, 返回节点和边列表 (供前端图谱渲染)。

        Args:
            entity_id: 起始实体 ID。
            depth: 遍历深度 (跳数)。
            tenant_id: 租户 ID。

        Returns:
            {"center_entity_id", "depth", "nodes": [...], "edges": [...]}
        """
        depth = max(0, min(int(depth), 5))

        # 校验起始实体存在
        center = (
            await self.session.execute(
                select(KnowledgeGraphEntity).where(
                    KnowledgeGraphEntity.id == entity_id,
                    KnowledgeGraphEntity.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if center is None:
            return {
                "center_entity_id": entity_id,
                "depth": depth,
                "nodes": [],
                "edges": [],
            }

        # BFS 收集节点 ID
        seed_ids: Set[int] = {entity_id}
        related_ids = await self._graph_traverse(
            seed_ids, depth=depth, tenant_id=tenant_id
        )
        all_ids = seed_ids | related_ids

        # 加载节点
        entity_rows = (
            (
                await self.session.execute(
                    select(KnowledgeGraphEntity).where(
                        KnowledgeGraphEntity.tenant_id == tenant_id,
                        KnowledgeGraphEntity.id.in_(all_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        nodes = [self._entity_to_dict(e) for e in entity_rows]

        # 加载边 (两端都在节点集合内)
        relation_rows = (
            (
                await self.session.execute(
                    select(KnowledgeGraphRelation).where(
                        KnowledgeGraphRelation.tenant_id == tenant_id,
                        KnowledgeGraphRelation.source_entity_id.in_(all_ids),
                        KnowledgeGraphRelation.target_entity_id.in_(all_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        id_to_name = {e["id"]: e["name"] for e in nodes}
        edges = [
            self._relation_to_dict(r, id_to_name=id_to_name) for r in relation_rows
        ]

        return {
            "center_entity_id": entity_id,
            "depth": depth,
            "nodes": nodes,
            "edges": edges,
            "node_count": len(nodes),
            "edge_count": len(edges),
        }

    # ===================== 删除 =====================

    async def delete_entity(
        self, entity_id: int, *, tenant_id: str = "default"
    ) -> bool:
        """删除实体 (应用层级联删除关联关系)

        SQLite 默认未启用 PRAGMA foreign_keys, FK ondelete=CASCADE 不会自动触发,
        因此在应用层显式删除该实体作为 source/target 的全部关系, 避免悬空外键。
        """
        entity = (
            await self.session.execute(
                select(KnowledgeGraphEntity).where(
                    KnowledgeGraphEntity.id == entity_id,
                    KnowledgeGraphEntity.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if entity is None:
            return False
        # 应用层级联: 删除该实体参与的全部关系 (作为 source 或 target)
        await self.session.execute(
            KnowledgeGraphRelation.__table__.delete().where(
                KnowledgeGraphRelation.tenant_id == tenant_id,
                (KnowledgeGraphRelation.source_entity_id == entity_id)
                | (KnowledgeGraphRelation.target_entity_id == entity_id),
            )
        )
        await self.session.delete(entity)
        await self.session.flush()
        return True

    async def delete_relation(
        self, relation_id: int, *, tenant_id: str = "default"
    ) -> bool:
        """删除关系"""
        relation = (
            await self.session.execute(
                select(KnowledgeGraphRelation).where(
                    KnowledgeGraphRelation.id == relation_id,
                    KnowledgeGraphRelation.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if relation is None:
            return False
        await self.session.delete(relation)
        await self.session.flush()
        return True

    # ===================== 序列化辅助 =====================

    @staticmethod
    def _task_to_dict(t: KnowledgeGraphTask) -> Dict[str, Any]:
        """KnowledgeGraphTask -> dict"""
        return {
            "id": t.id,
            "tenant_id": t.tenant_id,
            "name": t.name,
            "collection_name": t.collection_name,
            "document_ids": t.document_ids if isinstance(t.document_ids, list) else [],
            "status": t.status,
            "entity_count": t.entity_count,
            "relation_count": t.relation_count,
            "error_message": t.error_message,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        }

    @staticmethod
    def _entity_to_dict(e: KnowledgeGraphEntity) -> Dict[str, Any]:
        """KnowledgeGraphEntity -> dict"""
        return {
            "id": e.id,
            "tenant_id": e.tenant_id,
            "name": e.name,
            "entity_type": e.entity_type,
            "description": e.description,
            "properties": e.properties if isinstance(e.properties, dict) else {},
            "source_docs": e.source_docs if isinstance(e.source_docs, list) else [],
            "embedding_id": e.embedding_id,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "updated_at": e.updated_at.isoformat() if e.updated_at else None,
        }

    @staticmethod
    def _relation_to_dict(
        r: KnowledgeGraphRelation,
        id_to_name: Optional[Dict[int, str]] = None,
    ) -> Dict[str, Any]:
        """KnowledgeGraphRelation -> dict (可选附带实体名称)"""
        d: Dict[str, Any] = {
            "id": r.id,
            "tenant_id": r.tenant_id,
            "source_entity_id": r.source_entity_id,
            "target_entity_id": r.target_entity_id,
            "relation_type": r.relation_type,
            "weight": r.weight,
            "properties": r.properties if isinstance(r.properties, dict) else {},
            "source_docs": r.source_docs if isinstance(r.source_docs, list) else [],
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        if id_to_name is not None:
            d["source_entity_name"] = id_to_name.get(r.source_entity_id, "")
            d["target_entity_name"] = id_to_name.get(r.target_entity_id, "")
        return d
