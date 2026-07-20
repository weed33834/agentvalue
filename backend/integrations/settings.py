"""Integrations 独立配置(P7)

不污染主 core/config.py,单独管理飞书/GitLab 等外部集成的配置项。
通过环境变量读取(与主 Settings 风格一致),由 factory.py 消费。

环境变量:
- 飞书(ADR-001): FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_WEBHOOK_SECRET
- GitLab(ADR-002): GITLAB_BASE_URL / GITLAB_TOKEN / GITLAB_WEBHOOK_SECRET
"""
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class IntegrationsSettings(BaseSettings):
    """外部集成配置(飞书/GitLab 等)

    独立于主 Settings,避免 integrations 配置项污染 core/config.py。
    未配置时所有字段为 None,工厂返回 Dummy 实现。
    """

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.runtime"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 飞书 IM (ADR-001)
    # 配置 app_id + app_secret 后,工厂尝试实例化 FeishuIMAdapter(当前未实现,降级 Dummy)
    feishu_app_id: Optional[str] = None
    feishu_app_secret: Optional[str] = None
    # webhook 验签密钥(可选,用于校验 X-Lark-Signature)
    feishu_webhook_secret: Optional[str] = None

    # GitLab 代码仓库 (ADR-002)
    # 配置 base_url + token 后,工厂尝试实例化 GitLabCodeRepoAdapter(当前未实现,降级 Dummy)
    gitlab_base_url: Optional[str] = None
    gitlab_token: Optional[str] = None
    # webhook 验签密钥(对比 X-Gitlab-Token)
    gitlab_webhook_secret: Optional[str] = None


@lru_cache()
def get_integrations_settings() -> IntegrationsSettings:
    """获取 integrations 配置(单例,缓存)"""
    return IntegrationsSettings()
