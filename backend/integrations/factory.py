"""集成适配器工厂(P7)

按 settings 选择实现,未配置时返回 Dummy。
真实适配器(Feishu/GitLab)当前 raise NotImplementedError,工厂捕获后降级为 Dummy。
"""
from .base import CodeRepoAdapter, IMAdapter
from .dummy import DummyCodeRepoAdapter, DummyIMAdapter
from .settings import get_integrations_settings


def create_im_adapter() -> IMAdapter:
    """创建 IM 适配器。

    优先级:飞书(若配置且已实现) → Dummy
    真实适配器未实现时(raise NotImplementedError)自动降级为 Dummy。
    """
    s = get_integrations_settings()
    # 飞书
    if s.feishu_app_id and s.feishu_app_secret:
        try:
            from .feishu import FeishuIMAdapter

            return FeishuIMAdapter(s.feishu_app_id, s.feishu_app_secret)
        except NotImplementedError:
            pass  # 真实接入未实现,降级
    return DummyIMAdapter()


def create_coderepo_adapter() -> CodeRepoAdapter:
    """创建代码仓库适配器。

    优先级:GitLab(若配置且已实现) → Dummy
    真实适配器未实现时(raise NotImplementedError)自动降级为 Dummy。
    """
    s = get_integrations_settings()
    if s.gitlab_token and s.gitlab_base_url:
        try:
            from .gitlab import GitLabCodeRepoAdapter

            return GitLabCodeRepoAdapter(s.gitlab_base_url, s.gitlab_token, s.gitlab_webhook_secret)
        except NotImplementedError:
            pass  # 真实接入未实现,降级
    return DummyCodeRepoAdapter()
