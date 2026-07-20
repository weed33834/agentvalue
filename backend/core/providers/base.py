"""
Provider 抽象基类
所有 LLM 调用方必须实现此接口，便于云端/本地/其他模型统一接入。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional, Union


@dataclass
class ProviderConfig:
    """Provider 配置"""

    model_name: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    temperature: float = 0.1
    max_tokens: int = 4096
    extra: Optional[Dict[str, Any]] = None
    # 模型档位（L0/L1/L2/L3），由 ModelRouter 注入，用于可观测性埋点；未设置时记为 unknown
    model_tier: Optional[str] = None
    # Embedding 模型名（由 ModelRouter 从 Settings 注入；未注入时子类按自身默认兜底）
    embedding_model: Optional[str] = None
    # 视觉模型名（由 ModelRouter 从 Settings 注入；未注入时子类按自身默认兜底）
    vision_model: Optional[str] = None
    # 单次请求超时（秒），由 ModelRouter 从 Settings 注入；未注入时子类按自身默认兜底
    request_timeout: float = 30.0


@dataclass
class ChatMessage:
    """统一消息格式

    content 通常为 str（纯文本）；当消息包含图片等附件时可为 multi-modal
    content 列表，形如：
        [
            {"type": "text", "text": "..."},
            {"type": "image_url", "image_url": {"url": "data:image/...;base64,..."}}
        ]
    OpenAI 兼容 provider 会原样透传给 chat.completions API。
    """

    role: str
    content: Union[str, List[Dict[str, Any]]]


@dataclass
class ChatCompletion:
    """统一补全结果"""

    content: str
    model: str
    usage: Optional[Dict[str, int]] = None
    # P1 增强: function calling / tool calling 时模型可能返回工具调用而非文本
    # 透传给上层 LangGraph ToolNode 执行。None 表示纯文本补全无工具调用。
    tool_calls: Optional[List[Dict[str, Any]]] = None


@dataclass
class ToolCallDelta:
    """流式 tool_call 增量(对标 OpenAI delta.tool_calls[i])。

    index: 并行 tool_call 的索引,用于拼接同一调用的多个 chunk。
    name: 仅首个 chunk 携带,后续 None。
    id: 仅首个 chunk 携带,后续 None。
    arguments: JSON 字符串增量,需跨 chunk 累加。
    """

    index: int
    name: Optional[str] = None
    id: Optional[str] = None
    arguments: Optional[str] = None


@dataclass
class StreamChunk:
    """流式补全的单个 chunk(对标 OpenAI stream chunk)。

    content: 文本增量(逐 token),None 表示本 chunk 无文本。
    tool_calls: 工具调用增量列表,稀疏(很多 chunk 为 None)。
    finish_reason: 仅最后一个 chunk 携带(stop/tool_calls/length)。
    usage: 仅最后一个 chunk 携带(token 用量统计)。
    """

    content: Optional[str] = None
    tool_calls: Optional[List[ToolCallDelta]] = None
    finish_reason: Optional[str] = None
    usage: Optional[Dict[str, int]] = None


class BaseProvider(ABC):
    """LLM Provider 抽象基类"""

    def __init__(self, config: ProviderConfig):
        self.config = config

    @abstractmethod
    async def chat_completion(
        self,
        messages: List[ChatMessage],
        response_format: Optional[Dict[str, str]] = None,
    ) -> ChatCompletion:
        """非流式聊天补全"""
        raise NotImplementedError

    async def stream_chat_completion(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[StreamChunk]:
        """流式聊天补全(逐 token 返回)。

        P2 深水区: 对标 OpenAI stream=True / LangChain astream()。
        yield StreamChunk,消费方按需拼接 content 或 tool_call arguments。

        子类未实现时默认 raise NotImplementedError。
        Playground SSE 用此接口实现打字机效果。
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} 不支持流式补全,请实现 stream_chat_completion"
        )
        # 这行不会被执行,但让 mypy 知道这是 AsyncIterator
        yield StreamChunk()  # type: ignore[unreachable]

    @abstractmethod
    async def health_check(self) -> bool:
        """检查该 Provider 是否可用"""
        raise NotImplementedError

    @abstractmethod
    def name(self) -> str:
        """Provider 名称"""
        raise NotImplementedError

    # ====== 能力扩展（默认 raise NotImplementedError，子类按需覆写，不破坏既有子类） ======

    async def vision_completion(
        self,
        prompt: str,
        image_data: str,  # base64 编码或 URL
        is_url: bool = False,
        model: Optional[str] = None,
    ) -> str:
        """视觉模型: 图片 + 文本 → 文本。"""
        raise NotImplementedError

    async def function_calling(
        self,
        messages: List[ChatMessage],
        tools: List[Dict[str, Any]],
        tool_choice: str = "auto",
        response_format: Optional[Dict[str, str]] = None,
    ) -> ChatCompletion:
        """支持 function calling / tool calling 的对话补全。

        P1 增强: 参考 OpenAI/LangGraph ToolNode 标准接口,让 LLM 决定是否调工具
        及其参数。返回的 ChatCompletion.tool_calls 包含模型生成的工具调用列表,
        透传给 LangGraph ToolNode 执行。

        Args:
            messages: 对话消息列表
            tools: 工具 schema 列表(OpenAI 格式,LangChain @tool 自动生成)
            tool_choice: "auto"(模型自行决定) | "none"(禁用) | "required"(强制调)
                       | {"type":"function","function":{"name":"xxx"}}(指定工具)
            response_format: 可选,强制 JSON 输出格式

        子类未实现时默认 raise NotImplementedError,提示该 Provider 不支持工具调用。
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} 不支持 function_calling,请实现该接口"
        )
