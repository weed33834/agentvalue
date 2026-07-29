"""GitLab 代码仓库适配器 (P7, 对标 ADR-002)

接入要点:
1. Personal Access Token: scope=api + read_repository
2. list_commits: GET /api/v4/projects/{id}/repository/commits?ref_name=...&since=...&until=...
3. list_merge_requests: GET /api/v4/projects/{id}/merge_requests?state=opened
4. webhook 验签: 对比 X-Gitlab-Token 与配置的 webhook_secret
5. parse_webhook: 按 X-Gitlab-Event header 分发到 push/merge_request/pipeline

真实接入需要:
- 配置 GITLAB_BASE_URL + GITLAB_TOKEN + GITLAB_WEBHOOK_SECRET
- 注册 webhook 接收路由 (api/v1/webhooks/gitlab)

GitLab API 文档: https://docs.gitlab.com/ee/api/rest/
"""
import hmac
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from .base import CodeRepoAdapter, CodeRepoEvent

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30.0


class GitLabCodeRepoAdapter(CodeRepoAdapter):
    """GitLab 代码仓库适配器

    通过 GitLab REST API v4 拉取 commit / MR, 解析 webhook 事件。

    Args:
        base_url: GitLab 实例地址, 如 https://gitlab.example.com
        token: Personal Access Token (scope=api + read_repository)
        webhook_secret: Webhook 验签密钥 (对比 X-Gitlab-Token)
    """

    def __init__(self, base_url: str, token: str, webhook_secret: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.webhook_secret = webhook_secret

    # ============================================================
    # HTTP 请求封装
    # ============================================================

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> Any:
        """封装 GitLab API 请求

        header: PRIVATE-TOKEN: {token}
        返回 JSON 响应体, HTTP 非 2xx 时抛出异常。
        """
        url = f"{self.base_url}/api/v4/{path.lstrip('/')}"
        headers = kwargs.pop("headers", {})
        headers["PRIVATE-TOKEN"] = self.token

        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.request(method, url, headers=headers, **kwargs)
                resp.raise_for_status()
                if resp.status_code == 204:
                    return None
                return resp.json()
        except httpx.HTTPStatusError as e:
            logger.error(
                "GitLab API 请求失败: %s %s → %s %s",
                method,
                path,
                e.response.status_code,
                e.response.text[:500],
            )
            raise
        except Exception as e:
            logger.error("GitLab API 请求异常: %s %s → %s", method, path, e)
            raise

    def _project_id(self, repo: str) -> str:
        """将仓库标识 (如 group/project) 转为 URL 编码的 project ID"""
        return quote(repo, safe="")

    # ============================================================
    # Commit / MR 拉取
    # ============================================================

    async def list_commits(
        self, repo: str, ref: str, since: datetime, until: datetime
    ) -> List[CodeRepoEvent]:
        """列出时间范围内的 commit

        GET /api/v4/projects/{id}/repository/commits
        params: ref_name=ref, since=since.isoformat(), until=until.isoformat()
        """
        pid = self._project_id(repo)
        params = {
            "ref_name": ref,
            "since": since.isoformat(),
            "until": until.isoformat(),
            "per_page": 100,
        }

        try:
            data = await self._request(
                "GET", f"projects/{pid}/repository/commits", params=params
            )
        except Exception:
            return []

        events: List[CodeRepoEvent] = []
        for commit in data or []:
            created_at = commit.get("created_at", "")
            try:
                ts = datetime.fromisoformat(
                    created_at.replace("Z", "+00:00")
                ) if created_at else datetime.utcnow()
            except (ValueError, TypeError):
                ts = datetime.utcnow()

            events.append(
                CodeRepoEvent(
                    event_type="commit",
                    repo=repo,
                    branch=ref,
                    commit_sha=commit.get("id", ""),
                    author=commit.get("author_name", ""),
                    timestamp=ts,
                    raw=commit,
                )
            )
        return events

    async def list_merge_requests(
        self, repo: str, state: str = "opened"
    ) -> List[CodeRepoEvent]:
        """列出 Merge Request

        GET /api/v4/projects/{id}/merge_requests?state={state}
        """
        pid = self._project_id(repo)
        params = {"state": state, "per_page": 100}

        try:
            data = await self._request(
                "GET", f"projects/{pid}/merge_requests", params=params
            )
        except Exception:
            return []

        events: List[CodeRepoEvent] = []
        for mr in data or []:
            updated_at = mr.get("updated_at", "")
            try:
                ts = datetime.fromisoformat(
                    updated_at.replace("Z", "+00:00")
                ) if updated_at else datetime.utcnow()
            except (ValueError, TypeError):
                ts = datetime.utcnow()

            events.append(
                CodeRepoEvent(
                    event_type="merge_request",
                    repo=repo,
                    branch=mr.get("source_branch", ""),
                    commit_sha=mr.get("merge_commit_sha"),
                    author=mr.get("author", {}).get("name", ""),
                    timestamp=ts,
                    raw=mr,
                )
            )
        return events

    # ============================================================
    # Webhook 解析
    # ============================================================

    async def parse_webhook(
        self, payload: Dict[str, Any], event_type: str
    ) -> Optional[CodeRepoEvent]:
        """解析 GitLab Webhook 事件

        按 event_type (来自 X-Gitlab-Event header) 分发:
        - Push Hook: 提取最新 commit
        - Merge Request Hook: 提取 MR 状态
        - Pipeline Hook: 提取 pipeline 状态
        """
        if event_type == "Push Hook":
            commits = payload.get("commits", [])
            if not commits:
                return None
            latest = commits[-1]
            project = payload.get("project", {})
            ref = payload.get("ref", "")
            timestamp_str = latest.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(
                    timestamp_str.replace("Z", "+00:00")
                ) if timestamp_str else datetime.utcnow()
            except (ValueError, TypeError):
                ts = datetime.utcnow()

            return CodeRepoEvent(
                event_type="push",
                repo=project.get("path_with_namespace", ""),
                branch=ref,
                commit_sha=latest.get("id", ""),
                author=latest.get("author", {}).get("name", ""),
                timestamp=ts,
                raw=payload,
            )

        elif event_type == "Merge Request Hook":
            mr = payload.get("object_attributes", {})
            project = payload.get("project", {})
            updated_at = mr.get("updated_at", "")
            try:
                ts = datetime.fromisoformat(
                    updated_at.replace("Z", "+00:00")
                ) if updated_at else datetime.utcnow()
            except (ValueError, TypeError):
                ts = datetime.utcnow()

            return CodeRepoEvent(
                event_type="merge_request",
                repo=project.get("path_with_namespace", ""),
                branch=mr.get("source_branch", ""),
                commit_sha=mr.get("last_commit", {}).get("id"),
                author=mr.get("last_commit", {}).get("author", {}).get("name", ""),
                timestamp=ts,
                raw=payload,
            )

        elif event_type == "Pipeline Hook":
            attrs = payload.get("object_attributes", {})
            project = payload.get("project", {})
            return CodeRepoEvent(
                event_type="pipeline",
                repo=project.get("path_with_namespace", ""),
                branch=attrs.get("ref", ""),
                commit_sha=attrs.get("sha", ""),
                author=None,
                timestamp=datetime.utcnow(),
                raw=payload,
            )

        else:
            logger.debug("GitLab 未处理的 webhook 事件类型: %s", event_type)
            return None

    async def verify_webhook_signature(
        self, payload: Dict[str, Any], signature: str
    ) -> bool:
        """验证 GitLab Webhook 签名

        GitLab 使用 X-Gitlab-Token header 做简单 token 比对 (非 HMAC)。
        将 signature 与配置的 webhook_secret 做恒等比较。
        """
        if not signature or not self.webhook_secret:
            return False
        return hmac.compare_digest(signature.strip(), self.webhook_secret)
