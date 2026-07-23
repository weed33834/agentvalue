"""
外部生态集成适配层抽象(P7)

设计原则:
1. 接口稳定:业务侧只依赖抽象,不依赖具体厂商
2. Dummy 默认:未配置时返回 Dummy 实现,不影响主流程
3. 真实接入留扩展点:子类按需实现,通过 create_xxx 工厂注入
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class IMMessage:
    """统一 IM 消息格式"""

    channel_id: str  # 群/会话 ID
    user_id: Optional[str]  # 发送者(可空,系统消息时)
    user_name: Optional[str]
    content: str  # 文本内容
    message_id: str  # 厂商消息 ID(用于回执)
    timestamp: datetime
    raw: Optional[Dict[str, Any]] = None  # 原始 payload(调试)


@dataclass
class IMRecipient:
    """IM 推送目标"""

    user_id: Optional[str] = None  # 单聊目标
    open_id: Optional[str] = None  # 飞书 open_id
    chat_id: Optional[str] = None  # 群聊 ID
    email: Optional[str] = None  # 邮箱(兜底)


class IMAdapter(ABC):
    """IM 适配器抽象:推送通知 + 接收 webhook 消息"""

    @abstractmethod
    async def send_text(self, recipient: IMRecipient, text: str) -> str:
        """发送文本消息,返回 message_id"""
        raise NotImplementedError

    @abstractmethod
    async def send_card(self, recipient: IMRecipient, card: Dict[str, Any]) -> str:
        """发送卡片消息(富文本/交互卡片),返回 message_id"""
        raise NotImplementedError

    @abstractmethod
    async def parse_webhook(self, payload: Dict[str, Any]) -> Optional[IMMessage]:
        """解析厂商 webhook 推送的消息(验证签名 + 解析正文)"""
        raise NotImplementedError

    @abstractmethod
    async def verify_webhook_signature(
        self, payload: Dict[str, Any], signature: str
    ) -> bool:
        """验证 webhook 签名,防伪造"""
        raise NotImplementedError


@dataclass
class CodeRepoEvent:
    """统一代码仓库事件(类似 GitLab PushEvent / GitHub Webhook)"""

    event_type: str  # push / merge_request / commit / pipeline
    repo: str  # 仓库名
    branch: Optional[str]
    commit_sha: Optional[str]
    author: Optional[str]
    timestamp: datetime
    raw: Optional[Dict[str, Any]] = None


class CodeRepoAdapter(ABC):
    """代码仓库适配器抽象:拉取 commit / MR / PR,解析 webhook"""

    @abstractmethod
    async def list_commits(
        self, repo: str, ref: str, since: datetime, until: datetime
    ) -> List[CodeRepoEvent]:
        """列出时间范围内的 commit"""
        raise NotImplementedError

    @abstractmethod
    async def list_merge_requests(
        self, repo: str, state: str = "opened"
    ) -> List[CodeRepoEvent]:
        """列出 MR/PR"""
        raise NotImplementedError

    @abstractmethod
    async def parse_webhook(
        self, payload: Dict[str, Any], event_type: str
    ) -> Optional[CodeRepoEvent]:
        """解析厂商 webhook 推送的事件"""
        raise NotImplementedError

    @abstractmethod
    async def verify_webhook_signature(
        self, payload: Dict[str, Any], signature: str
    ) -> bool:
        """验证 webhook 签名"""
        raise NotImplementedError
