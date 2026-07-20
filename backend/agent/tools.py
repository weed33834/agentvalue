"""
Agent Tools 定义
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class MemoryStore(ABC):
    """员工长期记忆存储抽象"""

    @abstractmethod
    async def get_employee_history(
        self,
        employee_id: str,
        period: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """获取员工历史评估/记忆"""
        raise NotImplementedError

    @abstractmethod
    async def add_memory(
        self,
        employee_id: str,
        memory: Dict[str, Any],
    ) -> None:
        """添加一条记忆"""
        raise NotImplementedError


class CompanyKB(ABC):
    """公司知识库抽象"""

    @abstractmethod
    async def query(
        self,
        query: str,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """检索公司知识库"""
        raise NotImplementedError


class DummyMemoryStore(MemoryStore):
    """内存版 MemoryStore，用于测试"""

    def __init__(self):
        self._data: Dict[str, List[Dict[str, Any]]] = {}

    async def get_employee_history(
        self,
        employee_id: str,
        period: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        records = self._data.get(employee_id, [])
        if period:
            records = [r for r in records if r.get("period") != period]
        return records[-limit:]

    async def add_memory(self, employee_id: str, memory: Dict[str, Any]) -> None:
        self._data.setdefault(employee_id, []).append(memory)


class DummyCompanyKB(CompanyKB):
    """内存版公司知识库，用于测试"""

    def __init__(self, documents: Optional[List[Dict[str, Any]]] = None):
        self._documents = documents or []

    async def query(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        # 简单关键词匹配，生产环境应使用向量检索
        query_words = set(query.lower().split())
        scored = []
        for doc in self._documents:
            text = f"{doc.get('title', '')} {doc.get('content', '')}".lower()
            score = len(query_words & set(text.split()))
            if score > 0:
                scored.append((score, doc))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [doc for _, doc in scored[:top_k]]


class AgentToolkit:
    """Agent 可调用的工具集合"""

    def __init__(
        self,
        memory: MemoryStore,
        kb: CompanyKB,
    ):
        self.memory = memory
        self.kb = kb

    async def get_employee_history(
        self,
        employee_id: str,
        period: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        return await self.memory.get_employee_history(employee_id, period, limit)

    async def query_company_kb(
        self, query: str, top_k: int = 5
    ) -> List[Dict[str, Any]]:
        return await self.kb.query(query, top_k)
