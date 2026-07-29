"""集成适配器工厂(P7)

按 settings 选择实现,未配置时返回 Dummy。
飞书/GitLab 适配器已完整实现,配置凭证后自动启用。
"""
import logging

from .base import CodeRepoAdapter, IMAdapter
from .dummy import DummyCodeRepoAdapter, DummyIMAdapter
from .settings import get_integrations_settings

logger = logging.getLogger(__name__)


def create_im_adapter() -> IMAdapter:
    """创建 IM 适配器。

    优先级:飞书(若配置 app_id + app_secret) → Dummy
    飞书适配器已完整实现 tenant_access_token 缓存、消息发送、webhook 解析。
    """
    s = get_integrations_settings()
    # 飞书
    if s.feishu_app_id and s.feishu_app_secret:
        try:
            from .feishu import FeishuIMAdapter

            return FeishuIMAdapter(s.feishu_app_id, s.feishu_app_secret)
        except Exception as e:
            logger.warning("飞书适配器初始化失败,降级为 Dummy: %s", e)
    return DummyIMAdapter()


def create_coderepo_adapter() -> CodeRepoAdapter:
    """创建代码仓库适配器。

    优先级:GitLab(若配置 base_url + token) → Dummy
    GitLab 适配器已完整实现 commit/MR 拉取、webhook 解析。
    """
    s = get_integrations_settings()
    if s.gitlab_token and s.gitlab_base_url:
        try:
            from .gitlab import GitLabCodeRepoAdapter

            return GitLabCodeRepoAdapter(s.gitlab_base_url, s.gitlab_token, s.gitlab_webhook_secret)
        except Exception as e:
            logger.warning("GitLab 适配器初始化失败,降级为 Dummy: %s", e)
    return DummyCodeRepoAdapter()
