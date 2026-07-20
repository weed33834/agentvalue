"""外部生态集成适配层(P7)

提供 IM / 代码仓库的统一抽象 + Dummy 实现 + 工厂。
业务层只依赖抽象接口,不依赖具体厂商。

详见 ADR-001(飞书 IM 集成)、ADR-002(GitLab 集成)。

用法:
    from integrations import create_im_adapter
    adapter = create_im_adapter()  # 未配置时返回 DummyIMAdapter
    await adapter.send_text(IMRecipient(chat_id="..."), "hello")
"""
from .base import (
    CodeRepoAdapter,
    CodeRepoEvent,
    IMAdapter,
    IMMessage,
    IMRecipient,
)
from .dummy import DummyCodeRepoAdapter, DummyIMAdapter
from .factory import create_coderepo_adapter, create_im_adapter

__all__ = [
    # 抽象
    "IMAdapter",
    "IMMessage",
    "IMRecipient",
    "CodeRepoAdapter",
    "CodeRepoEvent",
    # Dummy 实现
    "DummyIMAdapter",
    "DummyCodeRepoAdapter",
    # 工厂
    "create_im_adapter",
    "create_coderepo_adapter",
]
