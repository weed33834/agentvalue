"""
多模态数据清洗模块
对原始输入中的附件（图片/音频/表格/文本）进行抽取，统一转为文本，供 LLM 评估。
"""

from core.multimodal.cleaner import MultimodalCleaner

__all__ = ["MultimodalCleaner"]
