"""
输入护栏：检测 Prompt 注入、恶意指令、超大输入、敏感文件、编码绕过等。
"""

import base64
import binascii
import logging
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# P1-5：误报启发式关键词。当 prompt_injection 类规则命中但内容实际是讨论/教学
# 注入防御的合法内容时（如安全培训材料），标注为疑似误报。
# 这是初版启发式，可能漏判/误判，后续可接人工回标训练分类器替代。
_FP_EDUCATIONAL_KEYWORDS = (
    "防御",
    "防范",
    "讨论",
    "教学",
    "示例",
    "检测",
    "教育",
    "培训",
    "讲解",
    "分析",
)


def record_guard_check(
    name: str, matched: bool, would_be_false_positive: bool = False
) -> None:
    """记录护栏检查指标。

    参数：
        name: 护栏类型（"input" / "output"）
        matched: 是否命中规则（True=blocked, False=clean）
        would_be_false_positive: 命中但实际为正常内容（测试 fixture 标注，
            生产环境不强制）。仅当 matched=True 时记入误报计数。

    指标埋点失败不影响护栏本身（护栏是安全关键路径，指标为辅助观测）。
    受 GUARD_RAILS_METRICS_ENABLED 开关控制，关闭时跳过。
    """
    try:
        from core.config import get_settings

        if not get_settings().guard_rails_metrics_enabled:
            return
        from core.metrics import (
            record_guard_check as _record_guard_metric,
            record_guard_false_positive,
        )

        result = "blocked" if matched else "clean"
        _record_guard_metric(name, result)
        if matched and would_be_false_positive:
            record_guard_false_positive(name)
    except Exception:
        logger.debug("护栏指标记录失败", exc_info=True)


@dataclass
class GuardResult:
    """护栏检查结果"""

    allowed: bool
    reason: str = ""
    triggered_rules: List[str] = field(default_factory=list)
    # P1-2: 命中但实际为正常内容时置 True。同步 guard 无法直接 await 异步
    # AuditService.record_guard_check，故将该标记附加到结果对象返回，由路由层
    # （异步）拿到后调 AuditService.record_guard_result 写入审计表。
    would_be_false_positive: bool = False


class InputGuard:
    """输入内容安全护栏"""

    # Prompt 注入常见特征词与模式
    INJECTION_PATTERNS = [
        r"忽略.{0,10}(提示|指令|prompt|instruction)",
        r"ignore\s+.{0,20}(prompt|instruction|above|previous|all)",
        r"你.{0,8}是.{0,15}(助手|AI|模型|model|没有限制|无限制)",
        r"you\s+are\s+.{0,20}(an?\s+\w+|not\s+|unrestricted|different)",
        r"系统提示",
        r"係統提示",
        r"system\s+prompt",
        r"不要.{0,10}(评估|打分|判断|遵守|遵循)",
        r"输出.{0,10}(代码|密码|密钥|key|token)",
        r"```\s*(system|yaml|json)",
        r"system_prompt",
        r"system\s*:\s*you\s+are",
        r"开发者模式|開發者模式|developer\s+mode",
        r"DAN\s*模式|do\s+anything\s+now",
        r"越狱|jailbreak",
        r"管理员模式|root\s+用户|sudo\s+ignore",
        r"以管理员身份|切换到\s*root",
        r"无限制|無限制|unrestricted",
        r"不再受任何规则约束|不受任何限制",
        r"直接给?(所有员工|大家|全员).{0,5}(满分|最高分|通过)",
        r"(qing\s+wu\s+lue|hu\s+lue|xi\s+tong\s+ti\s+shi|zhi\s+ling)",
        r"制限有没你\s*，\s*令指的前之略忽",
        r"示提的前之略忽",
    ]

    # 恶意指令模式
    MALICIOUS_PATTERNS = [
        r"删除.{0,10}(数据库|数据|文件)",
        r"drop\s+table",
        r"rm\s+-rf",
        r"exec\s*\(",
        r"清空服务器|删除所有数据",
        r"open\s*\(\s*['\"]/etc/passwd",
    ]

    # 附件名危险模式（与扩展名校验并行）
    # 注意：与 INJECTION_PATTERNS/MALICIOUS_PATTERNS 中的对应模式保持一致，
    # 但独立维护以避免对文件名过度严格（如"你是AI"在文件名中可能误报）。
    MALICIOUS_FILENAME_PATTERNS = [
        r"drop\s+table",
        r"rm\s+-rf",
        r"exec\s*\(",
        r"delete\s+(from|table)",
        # 与 INJECTION_PATTERNS 中的 "ignore\s+.{0,20}(prompt|instruction|above|previous|all)" 对齐：
        # 允许 "ignore" 与目标词之间存在修饰词（如 previous/above/all），覆盖
        # "ignore previous instruction"、"ignore all previous instructions" 等变体。
        r"ignore\s+.{0,20}(prompt|instruction|above|previous|all)",
        r"system\s+prompt",
        # 中文"忽略...指令/提示"变体（与 INJECTION_PATTERNS 对齐）
        r"忽略.{0,10}(提示|指令|prompt|instruction)",
    ]

    MAX_INPUT_LENGTH = 10000
    MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024  # 10MB

    # 零宽及控制类字符（常被用于分隔关键词绕过）
    BYPASS_CHARS = "".join(
        chr(c)
        for c in [
            0x200B,  # ZERO WIDTH SPACE
            0x200C,  # ZERO WIDTH NON-JOINER
            0x200D,  # ZERO WIDTH JOINER
            0x2060,  # WORD JOINER
            0xFEFF,  # ZERO WIDTH NO-BREAK SPACE / BOM
            0x00AD,  # SOFT HYPHEN
            0x180E,  # MONGOLIAN VOWEL SEPARATOR
        ]
    )

    def __init__(self, max_input_length: Optional[int] = None):
        self.max_input_length = max_input_length or self.MAX_INPUT_LENGTH

    def _normalize(self, text: str) -> str:
        """统一 Unicode 形态并去除常见绕过字符"""
        # NFKC 将全角字符、兼容字符统一为常规形态
        normalized = unicodedata.normalize("NFKC", text)
        # 将零宽字符替换为空格而非直接删除：英文绕过载荷（如
        # "ignore\u200bprevious\u200binstruction"）若直接删除会把多个英文
        # 单词粘成 "ignorepreviousinstruction"，导致依赖词边界的正则
        # (ignore\s+...) 失配。替换为空格后保留词边界；中文场景下的多余
        # 空格由后续"中文字符间去空格"规则清理。
        for ch in self.BYPASS_CHARS:
            normalized = normalized.replace(ch, " ")
        # 将空字节替换为空格，避免截断攻击
        normalized = normalized.replace("\x00", " ")
        # 压缩空白并转为小写用于模式匹配（原始内容仍保留）
        normalized = re.sub(r"\s+", " ", normalized).strip().lower()
        # 去除中文字符之间的空格（对抗换行/空格分隔绕过）
        normalized = re.sub(
            r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", normalized
        )
        return normalized

    def _extract_candidate_decodings(self, text: str) -> List[str]:
        """尝试提取编码负载（base64/hex/url）的解码结果"""
        candidates = []

        # Base64：寻找符合 base64 字符集且长度为 4 的倍数的子串
        for candidate in re.findall(r"[A-Za-z0-9+/]{20,}={0,2}", text):
            if len(candidate) % 4 != 0:
                continue
            try:
                decoded = base64.b64decode(candidate).decode("utf-8", errors="strict")
                candidates.append(decoded)
            except (binascii.Error, UnicodeDecodeError):
                continue

        # Hex：匹配 hex: 前缀或连续十六进制串（长度>=20 且为偶数）
        hex_prefix = re.search(r"hex\s*[:\s]\s*([0-9a-fA-F]{20,})", text)
        if hex_prefix:
            try:
                decoded = bytes.fromhex(hex_prefix.group(1)).decode(
                    "utf-8", errors="strict"
                )
                candidates.append(decoded)
            except (ValueError, UnicodeDecodeError):
                pass

        for candidate in re.findall(r"[0-9a-fA-F]{20,}", text):
            if len(candidate) % 2 != 0:
                continue
            try:
                decoded = bytes.fromhex(candidate).decode("utf-8", errors="strict")
                candidates.append(decoded)
            except (ValueError, UnicodeDecodeError):
                continue

        # URL 编码
        if "%" in text:
            try:
                decoded = urllib.parse.unquote(text)
                if decoded != text:
                    candidates.append(decoded)
            except Exception:
                pass

        return candidates

    def _check_text(self, text: str, rules_prefix: str) -> List[str]:
        """对单段文本执行所有模式匹配，返回触发的规则列表"""
        triggered = []
        variants = [text, self._normalize(text)]
        variants.extend(self._extract_candidate_decodings(text))

        for variant in variants:
            variant_lower = variant.lower()
            for pattern in self.INJECTION_PATTERNS:
                if re.search(pattern, variant_lower, re.IGNORECASE | re.DOTALL):
                    triggered.append(f"{rules_prefix}:injection_pattern:{pattern}")
            for pattern in self.MALICIOUS_PATTERNS:
                if re.search(pattern, variant_lower, re.IGNORECASE | re.DOTALL):
                    triggered.append(f"{rules_prefix}:malicious_pattern:{pattern}")
        return triggered

    @staticmethod
    def _detect_false_positive(
        raw_inputs: List[Dict], triggered_rules: List[str]
    ) -> bool:
        """启发式判定：命中的 injection 规则是否疑似误报。

        P1-5：仅当触发的规则包含 prompt_injection 类（injection_pattern）且输入内容
        含教学/防御性词（如"防御/讨论/教学/示例"）时，标注为疑似误报。
        这是初版启发式，可能漏判/误判，后续可接人工回标训练分类器替代。
        """
        if not triggered_rules:
            return False
        if not any("injection_pattern" in r for r in triggered_rules):
            return False
        for inp in raw_inputs:
            content = str(inp.get("content", ""))
            if any(kw in content for kw in _FP_EDUCATIONAL_KEYWORDS):
                return True
        return False

    def check(self, raw_inputs: List[Dict]) -> GuardResult:
        """检查输入列表（文本内容 + 附件），记录护栏检查指标。"""
        result = self._check_impl(raw_inputs)
        # P1-5：命中时按启发式判定是否疑似误报，便于误报率统计
        would_be_fp = False
        if not result.allowed:
            would_be_fp = self._detect_false_positive(
                raw_inputs, result.triggered_rules
            )
        # P1-2：将误报标记附加到结果对象，供路由层调 AuditService.record_guard_result
        result.would_be_false_positive = would_be_fp
        record_guard_check(
            "input", matched=not result.allowed, would_be_false_positive=would_be_fp
        )
        return result

    def _check_impl(self, raw_inputs: List[Dict]) -> GuardResult:
        """检查输入列表（文本内容 + 附件）"""
        triggered = []

        if not isinstance(raw_inputs, list) or len(raw_inputs) == 0:
            return GuardResult(
                allowed=False,
                reason="输入不能为空",
                triggered_rules=["empty_input"],
            )

        total_length = 0
        for idx, inp in enumerate(raw_inputs):
            content = str(inp.get("content", ""))
            total_length += len(content)

            if total_length > self.max_input_length:
                return GuardResult(
                    allowed=False,
                    reason=f"输入总长度超过限制 {self.max_input_length} 字符",
                    triggered_rules=["input_size_limit"],
                )

            # 空内容/空白内容拦截
            if not content.strip():
                return GuardResult(
                    allowed=False,
                    reason="输入内容不能为空或仅含空白字符",
                    triggered_rules=["empty_content"],
                )

            triggered.extend(self._check_text(content, f"input[{idx}]"))

            # 校验附件类型与大小
            for att in inp.get("attachments", []) or []:
                att_result = self.check_attachment(
                    filename=att.get("filename", ""),
                    size=att.get(
                        "size",
                        (
                            len(att.get("data", ""))
                            if isinstance(att.get("data"), str)
                            else 0
                        ),
                    ),
                    mime=att.get("mime", ""),
                )
                if not att_result.allowed:
                    return att_result

        if triggered:
            # 去重并保持顺序
            seen = set()
            unique_triggered = []
            for rule in triggered:
                if rule not in seen:
                    seen.add(rule)
                    unique_triggered.append(rule)
            return GuardResult(
                allowed=False,
                reason="检测到潜在 Prompt 注入或恶意指令",
                triggered_rules=unique_triggered,
            )

        return GuardResult(allowed=True)

    def check_attachment(self, filename: str, size: int, mime: str) -> GuardResult:
        """检查附件"""
        # 扩展名白名单与 magic bytes 校验(routes.py)、抽取器(extractors.py)支持列表对齐
        allowed_exts = {
            ".txt",
            ".md",
            ".pdf",
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".wav",
            ".mp3",
            ".mp4",
            ".m4a",
        }
        ext = filename.lower()[filename.rfind(".") :]
        if ext not in allowed_exts:
            return GuardResult(
                allowed=False,
                reason=f"不支持的附件类型: {ext}",
                triggered_rules=["unsupported_attachment_type"],
            )
        if size > self.MAX_ATTACHMENT_SIZE:
            return GuardResult(
                allowed=False,
                reason="附件大小超过 10MB 限制",
                triggered_rules=["attachment_size_limit"],
            )

        # 附件名本身也可能携带注入/恶意指令
        name_lower = self._normalize(filename)
        for pattern in self.MALICIOUS_FILENAME_PATTERNS:
            if re.search(pattern, name_lower, re.IGNORECASE | re.DOTALL):
                return GuardResult(
                    allowed=False,
                    reason=f"附件名包含恶意特征: {filename}",
                    triggered_rules=[f"malicious_filename:{pattern}"],
                )

        return GuardResult(allowed=True)
