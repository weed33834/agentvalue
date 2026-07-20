"""
输出护栏：PII 脱敏、歧视/偏见检测、员工视图负面词过滤、幻觉标记检测。
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from core.guards.input_guard import record_guard_check
from core.utils.pii import PII_PATTERNS as _PII_REGISTRY

# P1-5：误报启发式关键词。偏见词命中但内容实际是讨论如何避免偏见
# （如"禁止性别歧视""避免年龄偏见"）时，标注为疑似误报。
# 这是初版启发式，可能漏判/误判，后续可接人工回标训练分类器替代。
_FP_PREVENTION_KEYWORDS = (
    "禁止",
    "避免",
    "防范",
    "防止",
    "不应",
    "杜绝",
    "消除",
    "反对",
)


@dataclass
class OutputGuardResult:
    """输出护栏结果"""

    clean_text: str
    violations: List[str]
    redacted_entities: List[str]
    # P1-2: 命中但实际为正常内容时置 True，供路由层调 AuditService.record_guard_result
    would_be_false_positive: bool = False


class OutputGuard:
    """输出内容安全护栏"""

    # 员工视图禁用负面词（避免误命中“差距”“信息差”等中性复合词）。
    # 此处为护栏检测专用,刻意为 eval.constants.NEGATIVE_WORDS 的超集
    # （额外含“废柴”“混日子”），语境不同故独立维护,不从 eval.constants 导入。
    NEGATIVE_WORDS = [
        "很差",
        "太差",
        "较差",
        "差劲",
        "表现差",
        "态度差",
        "质量差",
        "能力差",
        "水平差",
        "懒",
        "慢",
        "拖沓",
        "消极",
        "不合格",
        "无能",
        "没用",
        "糟糕",
        "失败",
        "失职",
        "敷衍",
        "逃避",
        "推卸",
        "废柴",
        "混日子",
    ]

    # PII 模式：从 core.utils.pii 的注册表导入(单一来源),仅用于展示视图的占位符
    # 脱敏(替换为 "[{name}已脱敏]"),与 utils/pii 的掩码策略不同。展示视图不脱敏
    # 银行卡号,故排除该项。
    # 注意：不包含日期模式，因为评估上下文中日期（如周期、截止日）是合法信息，
    # 误脱敏会破坏输出可读性。生日等敏感日期应由上游输入护栏过滤。
    PII_PATTERNS = [
        (entry["pattern"], entry["name"])
        for entry in _PII_REGISTRY
        if entry["name"] != "银行卡号"
    ]

    # 歧视/偏见敏感词（覆盖年龄/性别/地域/星座/血型等维度）
    BIASED_WORDS = [
        "性别",
        "年龄",
        "籍贯",
        "星座",
        "血型",
        "属相",
        "生肖",
        "剩女",
        "大龄",
        "外地",
        "农村",
        "乡下",
        # 扩展场景词
        "女员工",
        "男员工",
        "35岁",
        "90后",
        "00后",
        "处女座",
        "天蝎座",
        "摩羯座",
        "O型血",
        "A型血",
        "B型血",
        "AB型血",
        "该群体",
        "本地人优先",
        "外地人稳定性",
    ]

    # 场景词到偏见维度的映射（便于上层按维度统计）
    BIAS_CATEGORY_MAP = {
        "性别": ["性别", "女员工", "男员工", "男女"],
        "年龄": ["年龄", "剩女", "大龄", "35岁", "90后", "00后"],
        "籍贯": ["籍贯", "外地", "农村", "乡下"],
        "地域": ["外地", "农村", "乡下", "本地人优先", "外地人稳定性"],
        "星座": [
            "星座",
            "处女座",
            "天蝎座",
            "摩羯座",
            "双子座",
            "巨蟹座",
            "狮子座",
            "天秤座",
            "射手座",
            "水瓶座",
            "双鱼座",
            "白羊座",
            "金牛座",
        ],
        "血型": ["血型", "O型血", "A型血", "B型血", "AB型血"],
        "生肖": ["属相", "生肖"],
    }

    # 偏见模式（比关键词更灵活，覆盖委婉表达）
    BIAS_PATTERNS = [
        r"(女|男)员工.{0,10}(容易|不适合|优先|差|弱|分心)",
        r"(35|40|50)岁.{0,10}(以上|以下).{0,10}(能力|学习|创新|精力|体力)",
        r"(外地|本地|农村|乡下).{0,15}(稳定性|素质|能力|优先|不好|差)",
        r"(处女座|天蝎座|摩羯座|双子座|巨蟹座|狮子座|天秤座|射手座|水瓶座|双鱼座|白羊座|金牛座).{0,10}(挑剔|固执|情绪化|懒散|完美主义)",
        r"(O|A|B|AB)型血.{0,10}(细心|粗心|性格|能力|适合)",
        r"该群体.{0,10}(离职率|稳定性|能力|素质)",
        r"本地人优先",
        r"这个年纪.{0,10}(员工|人).{0,10}(不好管|能力差|不稳定)",
    ]

    # 幻觉 / 无证据夸张表述标记
    HALLUCINATION_PATTERNS = [
        r"史上最佳|有史以来最|史上最强",
        r"从来没有人|从未有过|绝无仅有",
        r"100%完美|完美无缺|无可挑剔|毫无缺点",
        r"所有人都认为|大家一致认为|公认最",
        r"绝对第一|当之无愧的第一",
    ]

    def redact_pii(self, text: str) -> tuple[str, List[str]]:
        """对文本中的 PII 进行脱敏"""
        redacted = []
        result = text
        for pattern, label in self.PII_PATTERNS:
            matches = re.findall(pattern, result)
            for m in matches:
                redacted.append(f"{label}:{m}")
            result = re.sub(pattern, f"[{label}已脱敏]", result)
        return result, redacted

    def check_negative_words(self, text: str) -> List[str]:
        """检查员工视图是否包含负面词"""
        return [w for w in self.NEGATIVE_WORDS if w in text]

    def check_bias(self, text: str) -> List[str]:
        """检查是否存在偏见表述（关键词 + 模式 + 维度标签）"""
        hits = [w for w in self.BIASED_WORDS if w in text]
        for pattern in self.BIAS_PATTERNS:
            if re.search(pattern, text):
                hits.append(f"bias_pattern:{pattern}")
        # 若命中某维度下的场景词，同时返回该维度标签
        for category, keywords in self.BIAS_CATEGORY_MAP.items():
            if category in hits:
                continue
            if any(kw in text for kw in keywords):
                hits.append(category)
        return hits

    def check_hallucination_markers(self, text: str) -> List[str]:
        """检查是否存在无证据的夸张/幻觉表述"""
        hits = []
        for pattern in self.HALLUCINATION_PATTERNS:
            if re.search(pattern, text):
                hits.append(f"hallucination:{pattern}")
        return hits

    @staticmethod
    def _detect_bias_false_positive(text: str, violations: List[str]) -> bool:
        """启发式判定：偏见词命中是否疑似误报。

        P1-5：仅当命中 biased_words 类违规且内容含偏见防范性词
        （如"禁止性别歧视""避免年龄偏见"）时，标注为疑似误报——这类内容
        实际是正确地反对偏见，而非表达偏见。初版启发式，后续可接人工回标。
        """
        if not violations:
            return False
        if not any(v.startswith("biased_words") for v in violations):
            return False
        return any(kw in text for kw in _FP_PREVENTION_KEYWORDS)

    def sanitize_employee_view(self, employee_view: Dict) -> OutputGuardResult:
        """对员工视图进行安全处理"""
        violations = []
        redacted_all = []

        text_fields = ["summary", "strengths"]
        for field in text_fields:
            value = employee_view.get(field, "")
            if isinstance(value, list):
                cleaned = []
                for item in value:
                    c, r = self.redact_pii(item)
                    cleaned.append(c)
                    redacted_all.extend(r)
                employee_view[field] = cleaned
            elif isinstance(value, str):
                cleaned, r = self.redact_pii(value)
                employee_view[field] = cleaned
                redacted_all.extend(r)

        # 检查成长维度
        for area in employee_view.get("growth_areas", []):
            for key in ["evidence", "improvement_actions"]:
                value = area.get(key, [])
                cleaned = []
                for item in value:
                    c, r = self.redact_pii(item)
                    cleaned.append(c)
                    redacted_all.extend(r)
                area[key] = cleaned

        # 负面词检查
        view_text = str(employee_view)
        negatives = self.check_negative_words(view_text)
        if negatives:
            violations.append(f"employee_view_negative_words:{','.join(negatives)}")

        # 偏见检查
        biased = self.check_bias(view_text)
        if biased:
            violations.append(f"biased_words:{','.join(biased)}")

        # 幻觉 / 过度自信检查
        hallucination = self.check_hallucination_markers(view_text)
        if hallucination:
            violations.append(f"hallucination:{','.join(hallucination)}")

        # P1-5：命中时按启发式判定是否疑似误报
        would_be_fp = self._detect_bias_false_positive(view_text, violations)
        record_guard_check(
            "output", matched=bool(violations), would_be_false_positive=would_be_fp
        )
        return OutputGuardResult(
            clean_text=str(employee_view),
            violations=violations,
            redacted_entities=redacted_all,
            would_be_false_positive=would_be_fp,
        )

    def sanitize_manager_view(self, manager_view: Dict) -> OutputGuardResult:
        """对管理视图进行 PII 脱敏（允许尖锐判断，但脱敏敏感信息）"""
        redacted_all = []
        violations = []

        def _redact_inplace(obj):
            """递归遍历 dict/list，对每个字符串字段 in-place 脱敏"""
            if isinstance(obj, str):
                cleaned, r = self.redact_pii(obj)
                redacted_all.extend(r)
                return cleaned
            elif isinstance(obj, dict):
                for k, v in obj.items():
                    obj[k] = _redact_inplace(v)
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    obj[i] = _redact_inplace(item)
            return obj

        _redact_inplace(manager_view)

        # 管理视图同样应检测偏见与幻觉（仅记录违规，不阻断）
        view_text = str(manager_view)
        biased = self.check_bias(view_text)
        if biased:
            violations.append(f"biased_words:{','.join(biased)}")
        hallucination = self.check_hallucination_markers(view_text)
        if hallucination:
            violations.append(f"hallucination:{','.join(hallucination)}")

        # P1-5：命中时按启发式判定是否疑似误报
        would_be_fp = self._detect_bias_false_positive(view_text, violations)
        record_guard_check(
            "output", matched=bool(violations), would_be_false_positive=would_be_fp
        )
        return OutputGuardResult(
            clean_text=str(manager_view),
            violations=violations,
            redacted_entities=redacted_all,
            would_be_false_positive=would_be_fp,
        )
