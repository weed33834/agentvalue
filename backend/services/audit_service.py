"""
审计日志服务
记录所有对评估结果的关键操作，便于 HR 复核与合规追溯。

WS-4 防篡改哈希链
-----------------
仅靠 DB 层 append-only 触发器（见 alembic c4d5e6f7a8b9）只能挡住普通 UPDATE/DELETE，
拥有 DB superuser 的攻击者仍可 `ALTER TABLE ... DISABLE TRIGGER` 后改写历史。
因此每条审计记录额外存 `prev_hash` / `entry_hash`：

    entry_hash = sha256(canonical_json(业务字段) + prev_hash)

链**按租户独立**（避免多租户高并发写入互相争用同一条链尾）。任何一条历史记录被
改写，都会导致它自己的 entry_hash 对不上，且它之后所有记录的 prev_hash 断裂——
`verify_chain()` 能定位到第一处断点。

已知限制（诚实声明）：
- 哈希链只能**检测**篡改，不能阻止；要做到不可否认还需把链尾定期锚定到外部
  （如对象存储 WORM / 时间戳服务 / 区块链），本模块预留 `get_chain_head()` 供锚定。
- 多进程并发写同一租户时，靠 `SELECT ... FOR UPDATE`（PostgreSQL）串行化取链尾；
  SQLite 不支持行锁，退化为进程内 asyncio 锁，跨进程并发下可能出现分叉，
  verify_chain 会将其报告为断链（宁可误报，不可漏报）。
"""

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.tenant_context import get_current_tenant
from core.utils.pii import redact_audit_details, redact_pii
from models import AuditLog

logger = logging.getLogger(__name__)

# 创世条目的 prev_hash（每个租户的第一条）
GENESIS_HASH = "0" * 64

# 参与哈希计算的业务字段（顺序无关，canonical_json 会排序）
_HASHED_FIELDS = (
    "log_id",
    "tenant_id",
    "actor_id",
    "action",
    "evaluation_id",
    "employee_id",
    "details",
    "ip_address",
    "created_at",
)

# 进程内按租户串行化链尾读取（SQLite 无行锁时的兜底）
_chain_locks: Dict[str, asyncio.Lock] = {}


def _get_chain_lock(tenant_id: str) -> asyncio.Lock:
    lock = _chain_locks.get(tenant_id)
    if lock is None:
        lock = asyncio.Lock()
        _chain_locks[tenant_id] = lock
    return lock


def _iso_utc(value: Any) -> Any:
    """时间统一成 UTC ISO 字符串。

    SQLite 读回的 datetime 不带 tzinfo，按 UTC 解释（写入时本来就是 UTC），
    否则同一条记录写入时与校验时的哈希会不一致。
    """
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    return value


def canonical_payload(entry: AuditLog) -> Dict[str, Any]:
    """提取参与哈希的字段（值规范化，None 保留为 null）。"""
    payload: Dict[str, Any] = {}
    for name in _HASHED_FIELDS:
        payload[name] = _iso_utc(getattr(entry, name, None))
    return payload


def canonical_json(payload: Dict[str, Any]) -> str:
    """稳定序列化：键排序 + 紧凑分隔符 + 不转义非 ASCII。

    三者缺一都会让校验偶发失败（dict 顺序、Python 版本间的默认空格、
    中文是否 \\u 转义都会改变字节流）。
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def compute_entry_hash(payload: Dict[str, Any], prev_hash: str) -> str:
    """entry_hash = sha256(canonical_json(payload) + prev_hash)"""
    material = canonical_json(payload) + (prev_hash or GENESIS_HASH)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class ChainVerifyResult:
    """哈希链校验结果"""

    # 链是否完整
    valid: bool
    # 实际校验的条目数
    checked: int
    # 租户
    tenant_id: str = ""
    # 第一处断链的 AuditLog.id（valid=True 时为 None）
    broken_entry_id: Optional[int] = None
    # 第一处断链的 log_id
    broken_log_id: Optional[str] = None
    # 断链原因描述
    reason: Optional[str] = None
    # 未参与链计算的条目数（entry_hash 为 NULL，通常是迁移前的历史数据）
    unchained: int = 0
    # 校验区间内的链尾哈希，可用于外部锚定
    head_hash: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "checked": self.checked,
            "tenant_id": self.tenant_id,
            "broken_entry_id": self.broken_entry_id,
            "broken_log_id": self.broken_log_id,
            "reason": self.reason,
            "unchained": self.unchained,
            "head_hash": self.head_hash,
        }


class AuditService:
    """审计服务（数据库实现）"""

    def __init__(self, session: AsyncSession):
        self.session = session
        # 同一 service 实例内多次 log() 时的链尾缓存：
        # log() 不 commit，未 flush 的行查不到，靠本缓存把同事务内的多条记录串起来
        self._pending_tail: Dict[str, str] = {}

    async def log(
        self,
        actor_id: str,
        action: str,
        evaluation_id: Optional[str] = None,
        employee_id: Optional[str] = None,
        details: Optional[Dict] = None,
        ip_address: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> AuditLog:
        """记录审计日志（不 commit，由调用方控制事务）

        P0-3: details 写库前先做 PII 脱敏，避免手机号/邮箱/身份证号等明文落库。
        脱敏递归处理嵌套 dict/list 中的字符串值，非字符串类型原样保留。

        WS-4: 追加防篡改哈希链。哈希计算失败时**仍然写入审计行**（prev_hash /
        entry_hash 留空 + WARNING 日志）——丢链接远比丢审计记录轻。
        """
        details = redact_audit_details(details or {})
        effective_tenant = tenant_id or get_current_tenant()
        # created_at 显式赋值：哈希要用它，不能等到 flush 时才由列默认值生成
        created_at = datetime.now(timezone.utc)
        entry = AuditLog(
            log_id=f"LOG-{created_at.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}",
            actor_id=actor_id,
            action=action,
            evaluation_id=evaluation_id,
            employee_id=employee_id,
            details=details or {},
            ip_address=ip_address,
            tenant_id=effective_tenant,
            created_at=created_at,
        )
        await self._attach_chain(entry, effective_tenant)
        self.session.add(entry)
        return entry

    async def _attach_chain(self, entry: AuditLog, tenant_id: str) -> None:
        """为待写入条目计算 prev_hash / entry_hash。失败不抛，只告警。

        性能：一次带 LIMIT 1 的索引查询（ix_audit_tenant_action + 主键倒序）
        + 一次 sha256。实测单条开销在亚毫秒级（见 tests/test_governance_v3.py
        的 test_hash_chain_write_overhead），相对一次 LLM 调用可忽略。
        """
        try:
            async with _get_chain_lock(tenant_id):
                prev_hash = self._pending_tail.get(tenant_id)
                if prev_hash is None:
                    prev_hash = await self._fetch_tail_hash(tenant_id)
                entry.prev_hash = prev_hash
                entry.entry_hash = compute_entry_hash(
                    canonical_payload(entry), prev_hash
                )
                self._pending_tail[tenant_id] = entry.entry_hash
        except Exception:
            # 审计行必须落库；链计算失败降级为无链接条目，verify_chain 会计入 unchained
            logger.warning(
                "审计哈希链计算失败 tenant_id=%s log_id=%s，该条目将不带链接",
                tenant_id,
                entry.log_id,
                exc_info=True,
            )
            entry.prev_hash = None
            entry.entry_hash = None

    async def _fetch_tail_hash(self, tenant_id: str) -> str:
        """取该租户链尾哈希；无历史条目时返回 GENESIS_HASH。

        PostgreSQL 上用 FOR UPDATE 锁住链尾行，串行化并发写入；
        SQLite 会忽略该子句（无行锁），此时依赖进程内 asyncio 锁。
        """
        stmt = (
            select(AuditLog.entry_hash)
            .where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.entry_hash.is_not(None),
            )
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
        if self.session.bind is not None and self.session.bind.dialect.name not in (
            "sqlite",
        ):
            stmt = stmt.with_for_update()
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() or GENESIS_HASH

    async def get_chain_head(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """返回当前链尾（供外部锚定：WORM 存储 / 时间戳服务 / 公证）。"""
        effective = tenant_id or get_current_tenant()
        head = await self._fetch_tail_hash(effective)
        return {
            "tenant_id": effective,
            "head_hash": head,
            "is_genesis": head == GENESIS_HASH,
        }

    async def verify_chain(
        self,
        tenant_id: Optional[str] = None,
        start: Optional[int] = None,
        end: Optional[int] = None,
    ) -> ChainVerifyResult:
        """走一遍指定租户的哈希链，定位第一处断裂。

        Args:
            tenant_id: 目标租户，缺省用当前请求租户。
            start: 起始 AuditLog.id（含），缺省从该租户最早一条开始。
            end: 结束 AuditLog.id（含），缺省到最新一条。

        Returns:
            ChainVerifyResult。`valid=False` 时 `broken_entry_id` 指向**第一条**
            对不上的记录，`reason` 说明是自身哈希不符（内容被改）还是 prev_hash
            断裂（前序记录被改/被删/被插入）。

        判定规则：
        1. entry_hash 为 NULL 的条目视为「未入链」，计入 unchained 但不判失败
           （迁移前的历史数据、链计算降级的条目）。
        2. 重算 entry_hash 与存储值不符 → 该条内容被篡改。
        3. prev_hash 与前一条已入链条目的 entry_hash 不符 → 链断裂。
        """
        effective = tenant_id or get_current_tenant()
        stmt = (
            select(AuditLog)
            .where(AuditLog.tenant_id == effective)
            .order_by(AuditLog.id.asc())
        )
        if start is not None:
            stmt = stmt.where(AuditLog.id >= start)
        if end is not None:
            stmt = stmt.where(AuditLog.id <= end)

        result = await self.session.execute(stmt)
        entries = list(result.scalars().all())

        checked = 0
        unchained = 0
        expected_prev: Optional[str] = None
        head_hash: Optional[str] = None

        for entry in entries:
            if not entry.entry_hash:
                unchained += 1
                continue

            # 2. 内容完整性
            recomputed = compute_entry_hash(
                canonical_payload(entry), entry.prev_hash or GENESIS_HASH
            )
            if recomputed != entry.entry_hash:
                return ChainVerifyResult(
                    valid=False,
                    checked=checked,
                    tenant_id=effective,
                    broken_entry_id=entry.id,
                    broken_log_id=entry.log_id,
                    reason=(
                        "条目内容与 entry_hash 不匹配（记录被改写）："
                        f"expected={entry.entry_hash[:16]}… actual={recomputed[:16]}…"
                    ),
                    unchained=unchained,
                    head_hash=head_hash,
                )

            # 3. 链接完整性（第一条已入链条目不校验 prev，可能是区间起点）
            if expected_prev is not None and entry.prev_hash != expected_prev:
                return ChainVerifyResult(
                    valid=False,
                    checked=checked,
                    tenant_id=effective,
                    broken_entry_id=entry.id,
                    broken_log_id=entry.log_id,
                    reason=(
                        "prev_hash 与前一条 entry_hash 不匹配"
                        "（前序记录被改写/删除，或有记录被插入）："
                        f"expected={expected_prev[:16]}… actual="
                        f"{(entry.prev_hash or 'NULL')[:16]}…"
                    ),
                    unchained=unchained,
                    head_hash=head_hash,
                )

            expected_prev = entry.entry_hash
            head_hash = entry.entry_hash
            checked += 1

        return ChainVerifyResult(
            valid=True,
            checked=checked,
            tenant_id=effective,
            unchained=unchained,
            head_hash=head_hash,
        )

    async def get_logs(
        self,
        evaluation_id: Optional[str] = None,
        employee_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[AuditLog]:
        stmt = (
            select(AuditLog)
            .where(AuditLog.tenant_id == get_current_tenant())
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        if evaluation_id:
            stmt = stmt.where(AuditLog.evaluation_id == evaluation_id)
        if employee_id:
            stmt = stmt.where(AuditLog.employee_id == employee_id)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def record_guard_check(
        self,
        guard_type: str,
        result: str,
        triggered_rules: Optional[List[str]] = None,
        would_be_false_positive: bool = False,
        evaluation_id: Optional[str] = None,
        employee_id: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> AuditLog:
        """记录一次护栏检查到审计日志。

        P1-5：在审计 details 中标注 would_be_false_positive，便于区分"真拦截"
        与"误报"（命中但实际为正常内容）。仅在 would_be_false_positive=True 时
        写入该键，避免污染正常拦截记录。误报判定为初版启发式，后续可接人工回标。

        参数：
            guard_type: 护栏类型（"input" / "output"）
            result: 检查结果（"clean" / "blocked"）
            triggered_rules: 触发的规则列表（clean 时为空）
            would_be_false_positive: 命中但实际为正常内容时置 True
        """
        details: Dict = {
            "guard_type": guard_type,
            "result": result,
        }
        if triggered_rules:
            # P0-3: triggered_rules 可能含被拦截原文，先逐条脱敏再写入；
            # log() 会再次整体脱敏 details（幂等，双重保险）
            details["triggered_rules"] = [redact_pii(str(r)) for r in triggered_rules]
        if would_be_false_positive:
            details["would_be_false_positive"] = True
        return await self.log(
            actor_id="system",
            action="guard_check",
            evaluation_id=evaluation_id,
            employee_id=employee_id,
            details=details,
            ip_address=ip_address,
        )

    async def list_logs(
        self,
        actor_id: Optional[str] = None,
        action: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict:
        """分页查询审计日志，支持按操作人、动作筛选"""
        stmt = (
            select(AuditLog)
            .where(AuditLog.tenant_id == get_current_tenant())
            .order_by(AuditLog.created_at.desc())
        )
        if actor_id:
            stmt = stmt.where(AuditLog.actor_id == actor_id)
        if action:
            stmt = stmt.where(AuditLog.action == action)

        count_stmt = select(func.count()).select_from(stmt.subquery())
        total_result = await self.session.execute(count_stmt)
        total = total_result.scalar() or 0

        offset = (page - 1) * page_size
        page_stmt = stmt.offset(offset).limit(page_size)
        result = await self.session.execute(page_stmt)
        logs = result.scalars().all()

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "logs": [
                {
                    "log_id": log.log_id,
                    "actor_id": log.actor_id,
                    "action": log.action,
                    "evaluation_id": log.evaluation_id,
                    "employee_id": log.employee_id,
                    "details": log.details,
                    "ip_address": log.ip_address,
                    "created_at": log.created_at.isoformat(),
                }
                for log in logs
            ],
        }
