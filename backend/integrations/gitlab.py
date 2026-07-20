"""GitLab 代码仓库适配器(P7,对标 ADR-002)

接入要点:
1. Personal Access Token:scope=api + read_repository
2. list_commits: GET /api/v4/projects/{id}/repository/commits?ref_name=...&since=...&until=...
3. list_merge_requests: GET /api/v4/projects/{id}/merge_requests?state=opened
4. webhook 验签:对比 X-Gitlab-Token 与配置的 webhook_secret
5. parse_webhook:按 X-Gitlab-Event header 分发到 push/merge_request/pipeline

真实接入需要:
- 配置 GITLAB_BASE_URL + GITLAB_TOKEN + GITLAB_WEBHOOK_SECRET
- 注册 webhook 接收路由(api/v1/webhooks/gitlab)
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from .base import CodeRepoAdapter, CodeRepoEvent


class GitLabCodeRepoAdapter(CodeRepoAdapter):
    """GitLab 代码仓库适配器(骨架,真实接入待实现,详见 ADR-002)。

    当前所有方法 raise NotImplementedError,工厂捕获后降级为 DummyCodeRepoAdapter。
    真实接入时移除 __init__ 中的 raise,逐个实现 TODO 方法。
    """

    def __init__(self, base_url: str, token: str, webhook_secret: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.webhook_secret = webhook_secret
        raise NotImplementedError("GitLabCodeRepoAdapter 真实接入待实现,详见 ADR-002")

    # TODO: 封装 httpx.AsyncClient,header: PRIVATE-TOKEN: {token}
    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        raise NotImplementedError("TODO: GitLab API 请求封装待实现")

    async def list_commits(
        self, repo: str, ref: str, since: datetime, until: datetime
    ) -> List[CodeRepoEvent]:
        # TODO: GET /api/v4/projects/{id}/repository/commits
        # params: ref_name=ref, since=since.isoformat(), until=until.isoformat()
        raise NotImplementedError("TODO: list_commits 待实现")

    async def list_merge_requests(
        self, repo: str, state: str = "opened"
    ) -> List[CodeRepoEvent]:
        # TODO: GET /api/v4/projects/{id}/merge_requests?state={state}
        raise NotImplementedError("TODO: list_merge_requests 待实现")

    async def parse_webhook(
        self, payload: Dict[str, Any], event_type: str
    ) -> Optional[CodeRepoEvent]:
        # TODO: 按 event_type(push / merge_request / pipeline)解析 payload
        # event_type 来自 X-Gitlab-Event header
        raise NotImplementedError("TODO: parse_webhook 待实现")

    async def verify_webhook_signature(
        self, payload: Dict[str, Any], signature: str
    ) -> bool:
        # TODO: 对比 signature(X-Gitlab-Token)与配置的 webhook_secret
        raise NotImplementedError("TODO: verify_webhook_signature 待实现")
