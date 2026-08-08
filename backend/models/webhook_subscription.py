"""出站 Webhook 订阅与投递数据模型 (WS-3 集成与开放能力)

对标 Svix / Stripe Webhooks / Segment:
- WebhookSubscription: 订阅注册表 (url / events[] / secret / headers / enabled)
- WebhookDelivery:     投递日志 (请求载荷 / 响应码 / 耗时 / 重试次数 / 死信)

与 models/models.py 中 ``WebhookEvent`` 的区别:
- ``WebhookEvent`` 记录**入站** webhook (飞书/GitLab/自定义回调进来的事件);
- 本模块记录**出站** webhook (平台主动把事件推给用户自己的 HTTP 端点)。
两者互不替代, 表结构与生命周期完全不同。

投递状态机:
    pending → delivering → success
                        ↘ failed (可重试, 由 next_retry_at 控制)
                        ↘ dead   (attempt >= max_attempts, 进入死信, 仅可手动重放)

多租户隔离: 所有模型包含 tenant_id, 未显式指定时落 DEFAULT_TENANT_ID。
"""

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base
from models.models import DEFAULT_TENANT_ID, now_utc

# ---------------------------------------------------------------------------
# 取值常量 (仅作文档与服务层校验参考, DB 层不加 CheckConstraint,
# 避免新增状态时必须走迁移)
# ---------------------------------------------------------------------------

DELIVERY_STATUS_PENDING = "pending"
DELIVERY_STATUS_DELIVERING = "delivering"
DELIVERY_STATUS_SUCCESS = "success"
DELIVERY_STATUS_FAILED = "failed"
DELIVERY_STATUS_DEAD = "dead"

DELIVERY_STATUSES = (
    DELIVERY_STATUS_PENDING,
    DELIVERY_STATUS_DELIVERING,
    DELIVERY_STATUS_SUCCESS,
    DELIVERY_STATUS_FAILED,
    DELIVERY_STATUS_DEAD,
)

# 可重投的状态 (retry poller 扫描范围)
DELIVERY_RETRYABLE_STATUSES = (DELIVERY_STATUS_PENDING, DELIVERY_STATUS_FAILED)

# 默认重试上限与超时 (与 services/webhook_delivery_service.py 保持一致)
DEFAULT_MAX_ATTEMPTS = 6
DEFAULT_TIMEOUT_SECONDS = 10

# 响应体入库截断长度: 防止对端返回超大 HTML 页面撑爆投递日志表
RESPONSE_BODY_MAX_CHARS = 2000


class WebhookSubscription(Base):
    """出站 Webhook 订阅

    一条订阅 = 一个用户 HTTP 端点 + 它关心的事件集合。
    ``events`` 支持三种写法, 由服务层的 ``_event_matches`` 解释:
    - ``"*"``               匹配全部事件
    - ``"evaluation.*"``    前缀通配, 匹配 evaluation.completed / evaluation.approved
    - ``"alert.triggered"`` 精确匹配

    连续失败保护: 每次投递失败 ``consecutive_failures`` +1, 成功归零;
    达到阈值后服务层将 ``enabled`` 置 False 并写入 ``disabled_reason``,
    避免长期不可达的端点持续消耗投递资源。
    """

    __tablename__ = "webhook_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # 订阅名称 (UI 展示用)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 目标 URL (仅允许 http/https, 投递前过 SSRF 内网黑名单)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    # 订阅的事件名列表, 如 ["evaluation.*", "alert.triggered"]
    events: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # HMAC-SHA256 签名密钥, 明文存储 (用户需用同一串校验签名)
    secret: Mapped[str] = mapped_column(String(128), nullable=False)
    # 附加请求头 (如 {"Authorization": "Bearer xxx"}), 与内置签名头合并
    headers: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 单条投递最大尝试次数, 超过后进入死信
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_MAX_ATTEMPTS
    )
    # 单次 HTTP 请求超时 (秒)
    timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_TIMEOUT_SECONDS
    )
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # 最近一次投递时间与结果 (success / failed / dead), 供列表页快速展示健康度
    last_delivery_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # 连续失败次数 (成功即归零)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    # 自动禁用原因 (人工重新启用时由服务层清空)
    disabled_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        Index("ix_webhook_sub_tenant_enabled", "tenant_id", "enabled"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"<WebhookSubscription id={self.id} tenant={self.tenant_id} "
            f"name={self.name!r} enabled={self.enabled}>"
        )


class WebhookDelivery(Base):
    """出站 Webhook 单次投递记录 (投递日志 + 重试队列)

    一条 (subscription, event) 对应一行; 同一行会被重试多次,
    ``attempt`` 累加、``next_retry_at`` 由指数退避推进, 因此**不是**每次尝试一行,
    而是一条投递的完整生命周期一行 (最后一次尝试的响应覆盖前一次)。

    幂等: ``(subscription_id, event_id)`` 唯一。业务侧带上同一 ``event_id`` 重复
    dispatch 不会产生重复投递 (event_id 为 NULL 时不参与去重, 各 DB 均视 NULL 互不相等)。

    ``next_retry_at`` 单列索引 + ``(status, next_retry_at)`` 复合索引: 前者服务于
    时间范围扫描, 后者服务于 retry poller 的 ``status IN (...) AND next_retry_at <= now``。
    """

    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # 所属订阅 (逻辑外键, 不建 DB FK, 与仓库既有约定一致)
    subscription_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, default=DEFAULT_TENANT_ID
    )
    # 事件名, 如 evaluation.completed
    event: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    # 幂等键 (业务方生成), 同一订阅下唯一
    event_id: Mapped[Optional[str]] = mapped_column(
        String(128), index=True, nullable=True
    )
    # 完整事件信封 (即实际 POST 出去的 JSON 结构)
    payload: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    # pending / delivering / success / failed / dead
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DELIVERY_STATUS_PENDING
    )
    # 已尝试次数
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 本条投递的尝试上限 (从订阅快照而来, 订阅改配置不影响在途投递)
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_MAX_ATTEMPTS
    )
    # 下次重试时间; poller 扫描 status IN (pending, failed) AND next_retry_at <= now
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )
    response_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 响应体, 入库前截断到 RESPONSE_BODY_MAX_CHARS
    response_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 传输层/超时/SSRF 等非 HTTP 响应类错误描述
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 最后一次尝试耗时 (毫秒)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 投递成功时间
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        # retry poller 的主查询路径
        Index("ix_webhook_delivery_status_retry", "status", "next_retry_at"),
        # 订阅详情页按时间倒序翻投递日志
        Index("ix_webhook_delivery_sub_created", "subscription_id", "created_at"),
        # 租户维度统计
        Index("ix_webhook_delivery_tenant_status", "tenant_id", "status"),
        # 幂等约束
        UniqueConstraint(
            "subscription_id", "event_id", name="uq_webhook_delivery_sub_event"
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"<WebhookDelivery id={self.id} sub={self.subscription_id} "
            f"event={self.event!r} status={self.status} attempt={self.attempt}>"
        )
