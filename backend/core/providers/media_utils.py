"""多模态媒体工具函数

提供 data URL 解析等跨 Provider 共用的辅助函数，
避免在 Anthropic / Gemini / OpenAI 等 Provider 中重复实现。
"""

from typing import Tuple


def parse_data_url(url: str) -> Tuple[str, str]:
    """从 data:image/...;base64,... 中解析出 (mimeType, base64Data)。

    支持 data URI scheme (RFC 2397)：
        data:[<mediatype>][;base64],<data>

    Args:
        url: data URI 字符串，如 "data:image/png;base64,iVBORw0KGgo..."

    Returns:
        (mimeType, base64Data) 元组。
        若 url 不是有效的 data URI，返回 ("image/jpeg", "")。
    """
    if not url or not url.startswith("data:"):
        return "image/jpeg", ""
    try:
        header, _, data = url.partition(",")
        # header 形如 "data:image/png;base64"
        mime = "image/jpeg"
        if ";" in header and "/" in header:
            mime = header[5:].split(";", 1)[0]
        return mime, data
    except Exception:
        return "image/jpeg", ""
