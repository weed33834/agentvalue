"""
多模态清洗器：编排各抽取器，将原始输入中的附件统一转为文本。
"""

import logging
from typing import Any, Dict, List, Optional

from core.multimodal.extractors import (
    AudioExtractor,
    BaseExtractor,
    CloudOCR,
    ImageExtractor,
    PdfExtractor,
    TableExtractor,
    TextExtractor,
    UnknownExtractor,
    WhisperASR,
    _detect_kind,
)

logger = logging.getLogger(__name__)


class MultimodalCleaner:
    """
    多模态清洗器。
    负责遍历 raw_inputs，对每条输入的 attachments 调用对应抽取器，
    将抽取出的文本拼接到输入 content 之后，形成 cleaned_content。
    """

    def __init__(
        self,
        vision_callable=None,
        asr_callable=None,
        ocr_extractor=None,
        ocr_api_key: Optional[str] = None,
        ocr_base_url: Optional[str] = None,
        ocr_model: str = "gpt-4o-mini",
        asr_api_key: Optional[str] = None,
        asr_base_url: Optional[str] = None,
        asr_model: str = "whisper-1",
    ):
        # 装配 OCR：显式 ocr_extractor 优先；未传但配置了 api_key 则构造 CloudOCR
        if ocr_extractor is None and ocr_api_key:
            ocr_extractor = CloudOCR(
                api_key=ocr_api_key,
                base_url=ocr_base_url,
                model=ocr_model,
            )

        # 装配 ASR：显式 asr_callable 优先；未传但配置了 api_key 则构造 WhisperASR
        if asr_callable is not None:
            audio_extractor: BaseExtractor = AudioExtractor(asr_callable=asr_callable)
        elif asr_api_key:
            audio_extractor = WhisperASR(
                api_key=asr_api_key,
                base_url=asr_base_url,
                model=asr_model,
            )
        else:
            audio_extractor = AudioExtractor()

        self._extractors: Dict[str, BaseExtractor] = {
            "text": TextExtractor(),
            "table": TableExtractor(),
            "image": ImageExtractor(
                vision_callable=vision_callable, ocr_extractor=ocr_extractor
            ),
            "audio": audio_extractor,
            "pdf": PdfExtractor(),
            "unknown": UnknownExtractor(),
        }

    async def clean_inputs(
        self, raw_inputs: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        清洗整批输入，返回 enriched inputs：
        每条输入新增 extracted_text 字段（附件抽取汇总），content 保持不变。
        """
        cleaned: List[Dict[str, Any]] = []
        for inp in raw_inputs:
            enriched = dict(inp)
            attachments = inp.get("attachments") or []
            if not attachments:
                cleaned.append(enriched)
                continue

            extracted_parts: List[str] = []
            for att in attachments:
                kind = _detect_kind(att.get("mime", ""), att.get("filename", ""))
                extractor = self._extractors.get(kind) or self._extractors["unknown"]
                try:
                    text = await extractor.extract(att)
                    extracted_parts.append(text)
                except Exception as e:
                    logger.exception("附件抽取异常")
                    extracted_parts.append(
                        f"[附件 {att.get('filename', '?')}] 抽取异常: {e}"
                    )

            enriched["extracted_text"] = "\n\n".join(extracted_parts)
            # 合并到 content 末尾，便于 LLM 直接消费
            base_content = inp.get("content", "") or ""
            if extracted_parts:
                enriched["content"] = (
                    f"{base_content}\n\n--- 附件抽取内容 ---\n"
                    + "\n\n".join(extracted_parts)
                ).strip()
            cleaned.append(enriched)
        return cleaned
