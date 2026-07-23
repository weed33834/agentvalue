"""敏感词字典管理服务

提供敏感词的增删改查 + 文本审核能力:
- check_text: 检查文本中的敏感词, 返回命中详情
- filter_text: 过滤文本 (按 action 处理: block/replace/mask)
- add_word / batch_add_words / remove_word / list_words: CRUD
- import_words / export_words: 导入导出

匹配算法:
- 优先使用 pyahocorasick (AC 自动机) 做高效多模式匹配
- 未安装时降级为简单字符串匹配

内置种子数据: 常见广告词、Spam 词。

事务边界由路由层控制 (service 层不 commit)。
"""

from __future__ import annotations

import csv
import io
import json
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.sensitive_word import SensitiveWord, SensitiveWordCategory

logger = logging.getLogger(__name__)

# 尝试导入 pyahocorasick (AC 自动机)
try:
    import ahocorasick

    _AHOCORASICK_AVAILABLE = True
except ImportError:
    _AHOCORASICK_AVAILABLE = False
    logger.info("pyahocorasick 未安装, 敏感词匹配降级为简单字符串匹配")

# 允许的分类 / 严重程度 / 处理动作
VALID_CATEGORIES = {"politics", "porn", "violence", "ad", "spam", "custom"}
VALID_SEVERITIES = {"low", "medium", "high"}
VALID_ACTIONS = {"block", "replace", "mask"}

# 内置种子数据: 常见广告词 + Spam 词
SEED_WORDS: List[Dict[str, Any]] = [
    # 广告词
    {"word": "加微信", "category": "ad", "severity": "medium", "action": "mask"},
    {"word": "加QQ", "category": "ad", "severity": "medium", "action": "mask"},
    {"word": "免费领取", "category": "ad", "severity": "medium", "action": "mask"},
    {"word": "点击链接", "category": "ad", "severity": "medium", "action": "mask"},
    {"word": "限时优惠", "category": "ad", "severity": "low", "action": "mask"},
    {"word": "日赚万", "category": "ad", "severity": "high", "action": "block"},
    {"word": "零投资", "category": "ad", "severity": "high", "action": "block"},
    {"word": "稳赚不赔", "category": "ad", "severity": "high", "action": "block"},
    {"word": "代理招商", "category": "ad", "severity": "medium", "action": "mask"},
    {"word": "低价出售", "category": "ad", "severity": "medium", "action": "mask"},
    # Spam 词
    {"word": "刷单", "category": "spam", "severity": "high", "action": "block"},
    {"word": "兼职刷", "category": "spam", "severity": "high", "action": "block"},
    {"word": "中奖了", "category": "spam", "severity": "high", "action": "block"},
    {"word": "恭喜获得", "category": "spam", "severity": "medium", "action": "mask"},
    {
        "word": "点击领取奖励",
        "category": "spam",
        "severity": "medium",
        "action": "mask",
    },
    {"word": "您已被选中", "category": "spam", "severity": "medium", "action": "mask"},
    {"word": "退订回T", "category": "spam", "severity": "low", "action": "mask"},
    {"word": "回TD退订", "category": "spam", "severity": "low", "action": "mask"},
]

# 预置分类
SEED_CATEGORIES = [
    {"name": "politics", "description": "政治敏感词"},
    {"name": "porn", "description": "色情敏感词"},
    {"name": "violence", "description": "暴力敏感词"},
    {"name": "ad", "description": "广告营销词"},
    {"name": "spam", "description": "垃圾信息词"},
    {"name": "custom", "description": "自定义敏感词"},
]


class SensitiveWordService:
    """敏感词字典管理服务 (数据库实现)"""

    def __init__(self, session: AsyncSession):
        self.session = session
        # AC 自动机实例 (懒加载, 词库变更后重建)
        self._automaton = None
        self._automaton_word_map: Dict[str, Dict[str, Any]] = {}
        self._automaton_dirty = True

    # ===================== 文本审核 =====================

    async def check_text(
        self, text: str, *, tenant_id: str = "default"
    ) -> List[Dict[str, Any]]:
        """检查文本中的敏感词

        使用 AC 自动机 (或简单匹配) 高效扫描文本, 返回所有命中的敏感词详情。

        Args:
            text: 待检查文本。
            tenant_id: 租户 ID。

        Returns:
            命中列表: [{word, category, severity, action, position}]
            position 为敏感词在文本中的起始索引。
        """
        if not text:
            return []

        # 确保 AC 自动机已初始化
        await self._ensure_automaton(tenant_id=tenant_id)

        matches: List[Dict[str, Any]] = []
        seen_positions: set = set()

        if _AHOCORASICK_AVAILABLE and self._automaton is not None:
            # AC 自动机匹配
            for end_idx, word in self._automaton.iter(text):
                start_idx = end_idx - len(word) + 1
                # 去重: 同一位置同一词只记一次
                key = (start_idx, word)
                if key in seen_positions:
                    continue
                seen_positions.add(key)
                word_info = self._automaton_word_map.get(word)
                if word_info is None:
                    continue
                matches.append(
                    {
                        "word": word,
                        "category": word_info["category"],
                        "severity": word_info["severity"],
                        "action": word_info["action"],
                        "position": start_idx,
                    }
                )
        else:
            # 简单字符串匹配 (降级方案)
            for word, word_info in self._automaton_word_map.items():
                start = 0
                while True:
                    idx = text.find(word, start)
                    if idx == -1:
                        break
                    key = (idx, word)
                    if key not in seen_positions:
                        seen_positions.add(key)
                        matches.append(
                            {
                                "word": word,
                                "category": word_info["category"],
                                "severity": word_info["severity"],
                                "action": word_info["action"],
                                "position": idx,
                            }
                        )
                    start = idx + len(word)

        # 按位置排序
        matches.sort(key=lambda m: m["position"])
        return matches

    async def filter_text(self, text: str, *, tenant_id: str = "default") -> str:
        """过滤文本 (按 action 处理)

        - block: 若命中 block 类敏感词, 返回错误提示字符串
        - replace: 用 replacement 字段替换敏感词
        - mask: 用 *** 替换敏感词

        Args:
            text: 待过滤文本。
            tenant_id: 租户 ID。

        Returns:
            过滤后的文本。若命中 block 类词, 返回 "[内容包含违禁词, 已拦截]"。
        """
        if not text:
            return text

        matches = await self.check_text(text, tenant_id=tenant_id)
        if not matches:
            return text

        # 检查是否有 block 类命中
        block_matches = [m for m in matches if m["action"] == "block"]
        if block_matches:
            blocked_words = [m["word"] for m in block_matches]
            return f"[内容包含违禁词, 已拦截] 命中词: {', '.join(blocked_words)}"

        # 对 replace / mask 类命中做替换
        # 从后往前替换, 避免索引偏移
        replace_matches = sorted(
            [m for m in matches if m["action"] in ("replace", "mask")],
            key=lambda m: m["position"],
            reverse=True,
        )
        result = text
        for m in replace_matches:
            word = m["word"]
            start = m["position"]
            end = start + len(word)
            if m["action"] == "replace":
                # 用 replacement 替换 (需要查库获取 replacement 字段)
                replacement = await self._get_replacement(
                    word, m["category"], tenant_id=tenant_id
                )
                result = result[:start] + replacement + result[end:]
            else:  # mask
                result = result[:start] + "***" + result[end:]

        return result

    # ===================== CRUD =====================

    async def add_word(
        self,
        word: str,
        category: str = "custom",
        severity: str = "medium",
        action: str = "mask",
        replacement: Optional[str] = None,
        created_by: Optional[str] = None,
        *,
        tenant_id: str = "default",
    ) -> SensitiveWord:
        """添加敏感词

        Args:
            word: 敏感词文本。
            category: 分类 (politics/porn/violence/ad/spam/custom)。
            severity: 严重程度 (low/medium/high)。
            action: 处理动作 (block/replace/mask)。
            replacement: 替换文本 (action=replace 时使用, 默认 ***)。
            created_by: 创建人 ID。
            tenant_id: 租户 ID。

        Returns:
            创建的 SensitiveWord 对象。
        """
        # 参数校验
        if not word or not word.strip():
            raise ValueError("敏感词不能为空")
        word = word.strip()
        if category not in VALID_CATEGORIES:
            raise ValueError(f"无效的分类: {category}, 可选: {VALID_CATEGORIES}")
        if severity not in VALID_SEVERITIES:
            raise ValueError(f"无效的严重程度: {severity}, 可选: {VALID_SEVERITIES}")
        if action not in VALID_ACTIONS:
            raise ValueError(f"无效的处理动作: {action}, 可选: {VALID_ACTIONS}")

        # 检查是否已存在 (同租户同分类同词)
        existing = (
            await self.session.execute(
                select(SensitiveWord).where(
                    SensitiveWord.category == category,
                    SensitiveWord.word == word,
                    SensitiveWord.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            # 已存在则更新
            existing.severity = severity
            existing.action = action
            existing.replacement = replacement or "***"
            existing.is_active = True
            await self.session.flush()
            self._automaton_dirty = True
            return existing

        entity = SensitiveWord(
            tenant_id=tenant_id,
            word=word,
            category=category,
            severity=severity,
            action=action,
            replacement=replacement or "***",
            is_active=True,
            created_by=created_by,
        )
        self.session.add(entity)
        await self.session.flush()
        self._automaton_dirty = True
        logger.info("添加敏感词: %s (分类: %s, 租户: %s)", word, category, tenant_id)
        return entity

    async def batch_add_words(
        self,
        words: List[Dict[str, Any]],
        created_by: Optional[str] = None,
        *,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """批量添加敏感词

        Args:
            words: 敏感词列表, 每项为 {word, category, severity, action, replacement}。
            created_by: 创建人 ID。
            tenant_id: 租户 ID。

        Returns:
            {"added": N, "skipped": N, "errors": [...]}
        """
        added = 0
        skipped = 0
        errors: List[str] = []
        for item in words:
            try:
                await self.add_word(
                    word=item.get("word", ""),
                    category=item.get("category", "custom"),
                    severity=item.get("severity", "medium"),
                    action=item.get("action", "mask"),
                    replacement=item.get("replacement"),
                    created_by=created_by,
                    tenant_id=tenant_id,
                )
                added += 1
            except ValueError as e:
                skipped += 1
                errors.append(str(e))
            except Exception as e:
                skipped += 1
                errors.append(f"添加 '{item.get('word', '?')}' 失败: {e}")
        logger.info("批量添加敏感词: 成功 %d, 跳过 %d", added, skipped)
        return {"added": added, "skipped": skipped, "errors": errors}

    async def remove_word(self, word_id: int, *, tenant_id: str = "default") -> bool:
        """删除敏感词

        Args:
            word_id: 敏感词 ID。
            tenant_id: 租户 ID。

        Returns:
            True 表示已删除, False 表示不存在。
        """
        entity = (
            await self.session.execute(
                select(SensitiveWord).where(
                    SensitiveWord.id == word_id,
                    SensitiveWord.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if entity is None:
            return False
        await self.session.delete(entity)
        await self.session.flush()
        self._automaton_dirty = True
        logger.info("删除敏感词 id=%s word=%s", word_id, entity.word)
        return True

    async def list_words(
        self,
        category: Optional[str] = None,
        page: int = 1,
        size: int = 20,
        *,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """分页查询敏感词列表

        Args:
            category: 按分类过滤 (None 表示全部)。
            page: 页码 (从 1 开始)。
            size: 每页条数。
            tenant_id: 租户 ID。

        Returns:
            {"items": [...], "total": N, "page": P, "size": S}
        """
        base = (
            select(SensitiveWord)
            .where(SensitiveWord.tenant_id == tenant_id)
            .order_by(SensitiveWord.created_at.desc())
        )
        if category:
            base = base.where(SensitiveWord.category == category)

        total = (
            await self.session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar() or 0

        offset = (page - 1) * size
        rows = (
            (await self.session.execute(base.offset(offset).limit(size)))
            .scalars()
            .all()
        )

        return {
            "items": [self._word_to_dict(w) for w in rows],
            "total": total,
            "page": page,
            "size": size,
        }

    # ===================== 导入 / 导出 =====================

    async def import_words(
        self,
        file_content: str,
        format: str = "csv",
        created_by: Optional[str] = None,
        *,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """导入敏感词表

        支持 CSV / JSON 格式。

        CSV 格式 (每行: word,category,severity,action,replacement):
            加微信,ad,medium,mask,***
            刷单,spam,high,block,

        JSON 格式:
            [{"word": "加微信", "category": "ad", "severity": "medium", "action": "mask"}]

        Args:
            file_content: 文件内容字符串。
            format: 格式 (csv / json)。
            created_by: 创建人 ID。
            tenant_id: 租户 ID。

        Returns:
            {"added": N, "skipped": N, "errors": [...]}
        """
        words: List[Dict[str, Any]] = []
        if format.lower() == "csv":
            reader = csv.DictReader(io.StringIO(file_content))
            for row in reader:
                words.append(
                    {
                        "word": (row.get("word") or "").strip(),
                        "category": (row.get("category") or "custom").strip(),
                        "severity": (row.get("severity") or "medium").strip(),
                        "action": (row.get("action") or "mask").strip(),
                        "replacement": (row.get("replacement") or "").strip() or None,
                    }
                )
        elif format.lower() == "json":
            data = json.loads(file_content)
            if not isinstance(data, list):
                raise ValueError("JSON 格式必须为数组")
            for item in data:
                if isinstance(item, dict):
                    words.append(item)
                elif isinstance(item, str):
                    words.append({"word": item})
        else:
            raise ValueError(f"不支持的格式: {format}, 可选: csv / json")

        return await self.batch_add_words(
            words, created_by=created_by, tenant_id=tenant_id
        )

    async def export_words(
        self,
        category: Optional[str] = None,
        format: str = "csv",
        *,
        tenant_id: str = "default",
    ) -> str:
        """导出敏感词表

        Args:
            category: 按分类过滤 (None 表示全部)。
            format: 格式 (csv / json)。
            tenant_id: 租户 ID。

        Returns:
            导出内容字符串。
        """
        base = (
            select(SensitiveWord)
            .where(SensitiveWord.tenant_id == tenant_id)
            .order_by(SensitiveWord.category, SensitiveWord.word)
        )
        if category:
            base = base.where(SensitiveWord.category == category)
        rows = (await self.session.execute(base)).scalars().all()

        if format.lower() == "csv":
            output = io.StringIO()
            writer = csv.DictWriter(
                output,
                fieldnames=["word", "category", "severity", "action", "replacement"],
            )
            writer.writeheader()
            for w in rows:
                writer.writerow(
                    {
                        "word": w.word,
                        "category": w.category,
                        "severity": w.severity,
                        "action": w.action,
                        "replacement": w.replacement or "",
                    }
                )
            return output.getvalue()
        elif format.lower() == "json":
            return json.dumps(
                [self._word_to_dict(w) for w in rows],
                ensure_ascii=False,
                indent=2,
            )
        else:
            raise ValueError(f"不支持的格式: {format}, 可选: csv / json")

    # ===================== 种子数据 =====================

    async def seed_default_words(
        self, created_by: Optional[str] = "system", *, tenant_id: str = "default"
    ) -> Dict[str, Any]:
        """初始化内置种子数据 (预置分类 + 常见广告/Spam 词)

        幂等操作: 已存在的分类/词会被跳过。

        Returns:
            {"categories_added": N, "words_added": N}
        """
        # 预置分类
        categories_added = 0
        for cat in SEED_CATEGORIES:
            existing = (
                await self.session.execute(
                    select(SensitiveWordCategory).where(
                        SensitiveWordCategory.name == cat["name"],
                        SensitiveWordCategory.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                self.session.add(
                    SensitiveWordCategory(
                        tenant_id=tenant_id,
                        name=cat["name"],
                        description=cat["description"],
                        is_active=True,
                    )
                )
                categories_added += 1

        # 种子词
        words_added = 0
        for word_data in SEED_WORDS:
            existing = (
                await self.session.execute(
                    select(SensitiveWord).where(
                        SensitiveWord.category == word_data["category"],
                        SensitiveWord.word == word_data["word"],
                        SensitiveWord.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                self.session.add(
                    SensitiveWord(
                        tenant_id=tenant_id,
                        word=word_data["word"],
                        category=word_data["category"],
                        severity=word_data["severity"],
                        action=word_data["action"],
                        replacement="***",
                        is_active=True,
                        created_by=created_by,
                    )
                )
                words_added += 1

        await self.session.flush()
        self._automaton_dirty = True
        logger.info("种子数据初始化: 分类 %d, 敏感词 %d", categories_added, words_added)
        return {"categories_added": categories_added, "words_added": words_added}

    # ===================== 内部方法 =====================

    async def _ensure_automaton(self, *, tenant_id: str = "default") -> None:
        """确保 AC 自动机已初始化 (懒加载, 词库变更后重建)"""
        # 若未变更且已初始化 (AC 自动机已构建 或 降级模式下 word_map 非空), 直接返回
        if not self._automaton_dirty:
            if self._automaton is not None or self._automaton_word_map:
                return

        # 从数据库加载所有启用的敏感词 (按租户过滤)
        rows = (
            (
                await self.session.execute(
                    select(SensitiveWord).where(
                        SensitiveWord.is_active == True,  # noqa: E712
                        SensitiveWord.tenant_id == tenant_id,
                    )
                )
            )
            .scalars()
            .all()
        )

        # 构建 word → info 映射
        self._automaton_word_map: Dict[str, Dict[str, Any]] = {}
        for w in rows:
            self._automaton_word_map[w.word] = {
                "category": w.category,
                "severity": w.severity,
                "action": w.action,
                "replacement": w.replacement or "***",
            }

        # 构建 AC 自动机
        if _AHOCORASICK_AVAILABLE and self._automaton_word_map:
            self._automaton = ahocorasick.Automaton()
            for word in self._automaton_word_map:
                self._automaton.add_word(word, word)
            self._automaton.make_automaton()
        else:
            self._automaton = None

        self._automaton_dirty = False
        logger.info(
            "敏感词 AC 自动机已构建, 加载 %d 个词 (ahocorasick: %s)",
            len(self._automaton_word_map),
            _AHOCORASICK_AVAILABLE,
        )

    async def _get_replacement(
        self, word: str, category: str, *, tenant_id: str = "default"
    ) -> str:
        """获取敏感词的替换文本"""
        entity = (
            await self.session.execute(
                select(SensitiveWord).where(
                    SensitiveWord.word == word,
                    SensitiveWord.category == category,
                    SensitiveWord.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if entity and entity.replacement:
            return entity.replacement
        return "***"

    @staticmethod
    def _word_to_dict(w: SensitiveWord) -> Dict[str, Any]:
        """SensitiveWord → dict"""
        return {
            "id": w.id,
            "word": w.word,
            "category": w.category,
            "severity": w.severity,
            "action": w.action,
            "replacement": w.replacement,
            "is_active": w.is_active,
            "created_by": w.created_by,
            "created_at": w.created_at.isoformat() if w.created_at else None,
        }
