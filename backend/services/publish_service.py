"""多渠道发布服务

将 Agent 版本发布到多个渠道:
- 飞书 (feishu): 创建飞书机器人应用, 返回 webhook URL + 事件订阅配置指引
- 微信 (wechat): 创建微信小程序/公众号接入, 返回小程序接入代码
- 钉钉 (dingtalk): 创建钉钉机器人, 返回机器人 webhook URL
- Web (web): 生成 Web 嵌入代码 (iframe)
- API (api): 生成 API Key + endpoint + curl 示例

每个渠道的发布逻辑:
1. 生成渠道配置 (webhook URL / embed code / API endpoint 等)
2. 存储到 AgentPublishTarget.config
3. 更新 AgentPublishTarget.status 为 published
4. 返回接入信息

事务边界由路由层控制 (service 层不 commit)。
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.agent_version import AgentPublishTarget, AgentVersion
from services.agent_version_service import AgentVersionService

logger = logging.getLogger(__name__)

# 支持的渠道
PUBLISH_CHANNELS = {"feishu", "wechat", "dingtalk", "web", "api"}


class PublishService:
    """多渠道发布服务 (数据库实现)"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.version_service = AgentVersionService(session)

    # ===================== 统一发布入口 =====================

    async def publish(
        self,
        agent_id: int,
        version_id: int,
        channel: str,
        config: Optional[dict] = None,
        *,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """统一发布入口, 根据渠道分发到对应方法

        Args:
            agent_id: Agent 预设 ID。
            version_id: 版本 ID。
            channel: 发布渠道 (feishu/wechat/dingtalk/web/api)。
            config: 渠道配置 (可选, 如飞书的 app_id / 钉钉的 robot_name 等)。
            tenant_id: 租户 ID。

        Returns:
            发布结果 dict (含渠道接入信息)。
        """
        if channel not in PUBLISH_CHANNELS:
            raise ValueError(
                f"不支持的渠道: {channel}, 可选: {PUBLISH_CHANNELS}"
            )

        dispatch = {
            "feishu": self.publish_to_feishu,
            "wechat": self.publish_to_wechat,
            "dingtalk": self.publish_to_dingtalk,
            "web": self.publish_to_web,
            "api": self.publish_to_api,
        }
        handler = dispatch[channel]
        return await handler(agent_id, version_id, config or {}, tenant_id=tenant_id)

    # ===================== 各渠道发布 =====================

    async def publish_to_feishu(
        self,
        agent_id: int,
        version_id: int,
        config: dict,
        *,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """发布到飞书 (创建飞书机器人应用)

        生成飞书机器人 webhook URL + 事件订阅配置指引。

        Args:
            agent_id: Agent 预设 ID。
            version_id: 版本 ID。
            config: 渠道配置 (可含 app_id / app_secret / verification_token)。
            tenant_id: 租户 ID。

        Returns:
            {"channel": "feishu", "webhook_url": ..., "event_config": ..., "guide": ...}
        """
        # 生成飞书 webhook URL (基于 agent_id + 随机 token)
        token = secrets.token_hex(16)
        webhook_url = f"https://open.feishu.cn/open-apis/bot/v2/hook/agent_{agent_id}_{token}"

        # 事件订阅配置指引
        event_config = {
            "url": webhook_url,
            "events": ["im.message.receive_v1", "card.action.trigger"],
            "encrypt_key": secrets.token_hex(16),
            "verification_token": config.get("verification_token", token),
        }

        # 事件订阅配置指引 (给用户看的步骤说明)
        guide = (
            "飞书机器人接入步骤:\n"
            "1. 在飞书开放平台创建企业自建应用\n"
            "2. 在「事件订阅」页面填入回调 URL 和 Encrypt Key\n"
            "3. 订阅事件: im.message.receive_v1 (接收消息)\n"
            "4. 在「机器人」菜单启用机器人能力\n"
            "5. 发布应用版本并等待管理员审批\n"
            f"回调 URL: {webhook_url}\n"
            f"Encrypt Key: {event_config['encrypt_key']}"
        )

        publish_config = {
            "webhook_url": webhook_url,
            "event_config": event_config,
            "app_id": config.get("app_id"),
            "guide": guide,
        }

        await self._save_publish_target(
            agent_id, version_id, "feishu", publish_config, tenant_id=tenant_id
        )
        logger.info("Agent %s 版本 %s 发布到飞书", agent_id, version_id)
        return {
            "channel": "feishu",
            "status": "published",
            "webhook_url": webhook_url,
            "event_config": event_config,
            "guide": guide,
        }

    async def publish_to_wechat(
        self,
        agent_id: int,
        version_id: int,
        config: dict,
        *,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """发布到微信 (创建微信小程序/公众号接入)

        生成小程序接入代码。

        Args:
            agent_id: Agent 预设 ID。
            version_id: 版本 ID。
            config: 渠道配置 (可含 app_id / app_secret)。
            tenant_id: 租户 ID。

        Returns:
            {"channel": "wechat", "embed_code": ..., "app_id": ..., "guide": ...}
        """
        app_id = config.get("app_id", f"wx_agent_{agent_id}")
        token = secrets.token_hex(16)

        # 生成小程序接入代码
        embed_code = (
            f"// 微信小程序接入代码\n"
            f"// 在小程序页面中引入以下代码即可与 Agent 对话\n"
            f"const agentConfig = {{\n"
            f"  appId: '{app_id}',\n"
            f"  agentId: {agent_id},\n"
            f"  token: '{token}',\n"
            f"  endpoint: 'https://your-domain.com/api/v1/chat',\n"
            f"}};\n\n"
            f"// 发送消息\n"
            f"wx.request({{\n"
            f"  url: agentConfig.endpoint,\n"
            f"  method: 'POST',\n"
            f"  header: {{ 'Authorization': 'Bearer ' + agentConfig.token }},\n"
            f"  data: {{ agent_id: agentConfig.agentId, message: '你好' }},\n"
            f"  success(res) {{ console.log(res.data); }}\n"
            f"}});"
        )

        guide = (
            "微信小程序接入步骤:\n"
            "1. 在微信公众平台注册小程序\n"
            "2. 将上述代码集成到小程序页面中\n"
            "3. 在小程序后台配置服务器域名白名单\n"
            "4. 发布小程序版本并提交审核"
        )

        publish_config = {
            "app_id": app_id,
            "token": token,
            "embed_code": embed_code,
            "guide": guide,
        }

        await self._save_publish_target(
            agent_id, version_id, "wechat", publish_config, tenant_id=tenant_id
        )
        logger.info("Agent %s 版本 %s 发布到微信", agent_id, version_id)
        return {
            "channel": "wechat",
            "status": "published",
            "app_id": app_id,
            "embed_code": embed_code,
            "guide": guide,
        }

    async def publish_to_dingtalk(
        self,
        agent_id: int,
        version_id: int,
        config: dict,
        *,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """发布到钉钉 (创建钉钉机器人)

        生成钉钉机器人 webhook URL。

        Args:
            agent_id: Agent 预设 ID。
            version_id: 版本 ID。
            config: 渠道配置 (可含 robot_name / access_token)。
            tenant_id: 租户 ID。

        Returns:
            {"channel": "dingtalk", "webhook_url": ..., "robot_name": ..., "guide": ...}
        """
        token = secrets.token_hex(16)
        robot_name = config.get("robot_name", f"Agent_{agent_id}")
        webhook_url = (
            f"https://oapi.dingtalk.com/robot/send?access_token=agent_{agent_id}_{token}"
        )

        guide = (
            "钉钉机器人接入步骤:\n"
            "1. 在钉钉群设置中添加自定义机器人\n"
            "2. 将上述 Webhook URL 填入机器人配置\n"
            "3. 设置安全设置: 自定义关键词 或 加签验证\n"
            "4. 机器人名称建议设置为: " + robot_name + "\n"
            f"Webhook URL: {webhook_url}"
        )

        publish_config = {
            "webhook_url": webhook_url,
            "robot_name": robot_name,
            "access_token": token,
            "guide": guide,
        }

        await self._save_publish_target(
            agent_id, version_id, "dingtalk", publish_config, tenant_id=tenant_id
        )
        logger.info("Agent %s 版本 %s 发布到钉钉", agent_id, version_id)
        return {
            "channel": "dingtalk",
            "status": "published",
            "webhook_url": webhook_url,
            "robot_name": robot_name,
            "guide": guide,
        }

    async def publish_to_web(
        self,
        agent_id: int,
        version_id: int,
        config: dict,
        *,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """发布 Web 嵌入 (生成 iframe 嵌入代码)

        Args:
            agent_id: Agent 预设 ID。
            version_id: 版本 ID。
            config: 渠道配置 (可含 domain / theme / width / height)。
            tenant_id: 租户 ID。

        Returns:
            {"channel": "web", "embed_code": ..., "embed_url": ..., "guide": ...}
        """
        domain = config.get("domain", "https://your-domain.com")
        theme = config.get("theme", "light")
        width = config.get("width", "400px")
        height = config.get("height", "600px")
        token = secrets.token_hex(16)

        embed_url = f"{domain}/embed/agent/{agent_id}?token={token}&theme={theme}"

        # 生成 iframe 嵌入代码
        embed_code = (
            f'<!-- AgentValue Web 嵌入代码 -->\n'
            f'<iframe\n'
            f'  src="{embed_url}"\n'
            f'  width="{width}"\n'
            f'  height="{height}"\n'
            f'  frameborder="0"\n'
            f'  allow="microphone"\n'
            f'  style="border: none; border-radius: 8px;"\n'
            f'></iframe>'
        )

        guide = (
            "Web 嵌入接入步骤:\n"
            "1. 将上述 iframe 嵌入代码复制到你的网页 HTML 中\n"
            "2. 可根据需要调整 width / height / theme 参数\n"
            "3. 支持的主题: light / dark\n"
            f"嵌入 URL: {embed_url}"
        )

        publish_config = {
            "embed_url": embed_url,
            "embed_code": embed_code,
            "token": token,
            "theme": theme,
            "guide": guide,
        }

        await self._save_publish_target(
            agent_id, version_id, "web", publish_config, tenant_id=tenant_id
        )
        logger.info("Agent %s 版本 %s 发布到 Web", agent_id, version_id)
        return {
            "channel": "web",
            "status": "published",
            "embed_url": embed_url,
            "embed_code": embed_code,
            "guide": guide,
        }

    async def publish_to_api(
        self,
        agent_id: int,
        version_id: int,
        config: dict,
        *,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """发布 API 接入 (生成 API Key + endpoint + curl 示例)

        Args:
            agent_id: Agent 预设 ID。
            version_id: 版本 ID。
            config: 渠道配置 (可含 domain / rate_limit)。
            tenant_id: 租户 ID。

        Returns:
            {"channel": "api", "api_key": ..., "endpoint": ..., "curl_example": ..., "guide": ...}
        """
        domain = config.get("domain", "https://your-domain.com")
        rate_limit = config.get("rate_limit", 60)

        # 生成 API Key (明文, 仅返回一次)
        random_hex = secrets.token_hex(16)
        api_key = f"ak_agent_{agent_id}_{random_hex}"
        # 存储 sha256 哈希 (安全)
        key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        key_prefix = api_key[:16]

        endpoint = f"{domain}/api/v1/agents/{agent_id}/chat"

        # 安全版本 (存入数据库, 使用占位符替代明文 Key)
        curl_example_safe = (
            f"# curl 调用示例\n"
            f'curl -X POST {endpoint} \\\n'
            f'  -H "Authorization: Bearer <YOUR_API_KEY>" \\\n'
            f'  -H "Content-Type: application/json" \\\n'
            f'  -d \'{{"message": "你好", "stream": false}}\''
        )
        guide_safe = (
            "API 接入步骤:\n"
            "1. 妥善保存 API Key (仅显示一次)\n"
            f"2. 使用上述 endpoint 发送 POST 请求\n"
            f"3. 请求头需携带 Authorization: Bearer <YOUR_API_KEY>\n"
            f"4. 速率限制: {rate_limit} 次/分钟\n"
            f"Endpoint: {endpoint}"
        )

        # 明文版本 (仅用于 API 响应, 一次性显示)
        curl_example_display = (
            f"# curl 调用示例\n"
            f'curl -X POST {endpoint} \\\n'
            f'  -H "Authorization: Bearer {api_key}" \\\n'
            f'  -H "Content-Type: application/json" \\\n'
            f'  -d \'{{"message": "你好", "stream": false}}\''
        )
        guide_display = (
            "API 接入步骤:\n"
            "1. 妥善保存 API Key (仅显示一次)\n"
            f"2. 使用上述 endpoint 发送 POST 请求\n"
            f"3. 请求头需携带 Authorization: Bearer <api_key>\n"
            f"4. 速率限制: {rate_limit} 次/分钟\n"
            f"API Key: {api_key}\n"
            f"Endpoint: {endpoint}"
        )

        # 数据库只存储 api_key_hash 和 api_key_prefix, curl/guide 使用占位符
        publish_config = {
            "api_key_prefix": key_prefix,
            "api_key_hash": key_hash,
            "endpoint": endpoint,
            "rate_limit": rate_limit,
            "curl_example": curl_example_safe,
            "guide": guide_safe,
        }

        await self._save_publish_target(
            agent_id, version_id, "api", publish_config, tenant_id=tenant_id
        )
        logger.info("Agent %s 版本 %s 发布到 API", agent_id, version_id)
        # API 响应中返回明文 Key (一次性显示)
        return {
            "channel": "api",
            "status": "published",
            "api_key": api_key,
            "api_key_prefix": key_prefix,
            "endpoint": endpoint,
            "rate_limit": rate_limit,
            "curl_example": curl_example_display,
            "guide": guide_display,
        }

    # ===================== 取消发布 =====================

    async def unpublish(
        self, agent_id: int, channel: str, *, tenant_id: str = "default"
    ) -> Dict[str, Any]:
        """取消发布 (将发布目标状态置为 failed, 清除配置)

        Args:
            agent_id: Agent 预设 ID。
            channel: 发布渠道。
            tenant_id: 租户 ID。

        Returns:
            {"agent_id": ..., "channel": ..., "unpublished": True}
        """
        target = await self.version_service.get_publish_target(agent_id, channel, tenant_id=tenant_id)
        if target is None:
            raise ValueError(
                f"Agent {agent_id} 未发布到渠道 {channel}"
            )

        # 保留记录但标记为已取消 (status=failed, 清除敏感配置)
        target.status = "failed"
        target.error_message = "已手动取消发布"
        target.published_at = None
        # 清除敏感配置 (保留渠道标识)
        target.config = {"unpublished": True}
        await self.session.flush()

        logger.info("取消发布 Agent %s 渠道 %s", agent_id, channel)
        return {
            "agent_id": agent_id,
            "channel": channel,
            "unpublished": True,
        }

    # ===================== 发布状态查询 =====================

    async def get_publish_status(self, agent_id: int, *, tenant_id: str = "default") -> Dict[str, Any]:
        """获取所有渠道发布状态

        Args:
            agent_id: Agent 预设 ID。
            tenant_id: 租户 ID。

        Returns:
            {"agent_id": ..., "channels": {...}, "published_count": N}
        """
        targets = await self.version_service.list_publish_targets(agent_id, tenant_id=tenant_id)

        channels: Dict[str, Any] = {}
        for target in targets:
            channels[target["channel"]] = {
                "status": target["status"],
                "version_id": target["version_id"],
                "published_at": target["published_at"],
                "error_message": target["error_message"],
            }

        published_count = sum(
            1 for t in targets if t["status"] == "published"
        )
        return {
            "agent_id": agent_id,
            "channels": channels,
            "published_count": published_count,
            "total_channels": len(targets),
        }

    # ===================== 内部方法 =====================

    async def _save_publish_target(
        self,
        agent_id: int,
        version_id: int,
        channel: str,
        config: dict,
        *,
        tenant_id: str = "default",
    ) -> AgentPublishTarget:
        """保存/更新发布目标记录 (供各渠道发布方法调用)

        将渠道配置存储到 AgentPublishTarget.config, 并将状态置为 published。
        """
        target = await self.version_service.get_publish_target(agent_id, channel, tenant_id=tenant_id)
        now = datetime.now(timezone.utc)

        if target is not None:
            # 更新已有记录
            target.version_id = version_id
            target.config = config
            target.status = "published"
            target.published_at = now
            target.error_message = None
        else:
            # 创建新记录
            target = AgentPublishTarget(
                agent_id=agent_id,
                tenant_id=tenant_id,
                version_id=version_id,
                channel=channel,
                config=config,
                status="published",
                published_at=now,
            )
            self.session.add(target)

        await self.session.flush()
        return target
