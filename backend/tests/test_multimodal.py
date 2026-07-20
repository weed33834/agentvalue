"""
多模态数据清洗模块测试
"""

import base64

import pytest

from core.multimodal import MultimodalCleaner
from core.multimodal.extractors import (
    AudioExtractor,
    ImageExtractor,
    TableExtractor,
    TextExtractor,
    UnknownExtractor,
    _detect_kind,
)


def test_detect_kind():
    assert _detect_kind("image/png", "screenshot.png") == "image"
    assert _detect_kind("audio/wav", "voice.wav") == "audio"
    assert _detect_kind("text/csv", "data.csv") == "table"
    assert _detect_kind("text/plain", "notes.txt") == "text"
    assert _detect_kind("application/pdf", "report.pdf") == "pdf"
    assert _detect_kind("", "file.xyz") == "unknown"


@pytest.mark.asyncio
async def test_text_extractor_with_content():
    ext = TextExtractor()
    att = {"filename": "notes.txt", "content": "今日完成需求评审"}
    result = await ext.extract(att)
    assert "今日完成需求评审" in result
    assert "notes.txt" in result


@pytest.mark.asyncio
async def test_text_extractor_with_base64():
    ext = TextExtractor()
    raw = "周报内容".encode("utf-8")
    att = {"filename": "weekly.md", "data": base64.b64encode(raw).decode()}
    result = await ext.extract(att)
    assert "周报内容" in result


@pytest.mark.asyncio
async def test_table_extractor_csv():
    ext = TableExtractor()
    csv_bytes = "name,score,task\n张三,90,登录模块\n李四,85,接口联调\n".encode("utf-8")
    att = {"filename": "scores.csv", "data": base64.b64encode(csv_bytes).decode()}
    result = await ext.extract(att)
    assert "name" in result
    assert "张三" in result
    assert "李四" in result
    assert "|" in result  # markdown 表格


@pytest.mark.asyncio
async def test_table_extractor_truncation():
    """超长表格应被截断"""
    ext = TableExtractor()
    rows = [f"row{i},{i}\n" for i in range(100)]
    csv_bytes = "header1,header2\n" + "".join(rows)
    att = {"filename": "big.csv", "data": base64.b64encode(csv_bytes.encode()).decode()}
    result = await ext.extract(att)
    assert "已截断" in result


@pytest.mark.asyncio
async def test_image_extractor_without_vision():
    """未配置 vision 时应优雅降级"""
    ext = ImageExtractor()
    att = {"filename": "screenshot.png", "data": base64.b64encode(b"fakepng").decode()}
    result = await ext.extract(att)
    assert "未配置" in result
    assert "screenshot.png" in result


@pytest.mark.asyncio
async def test_image_extractor_with_vision():
    """配置 vision callable 后应调用（签名: prompt, image_data）"""

    async def fake_vision(prompt: str, image_data: str):
        return f"图片描述：包含一个登录界面 (prompt={prompt[:8]}...)"

    ext = ImageExtractor(vision_callable=fake_vision)
    att = {
        "filename": "ui.png",
        "mime": "image/png",
        "data": base64.b64encode(b"fakepng").decode(),
    }
    result = await ext.extract(att)
    assert "登录界面" in result
    assert "ui.png" in result


@pytest.mark.asyncio
async def test_audio_extractor_without_asr():
    ext = AudioExtractor()
    att = {"filename": "voice.mp3", "data": base64.b64encode(b"fakeaudio").decode()}
    result = await ext.extract(att)
    assert "未配置" in result


@pytest.mark.asyncio
async def test_audio_extractor_with_asr():

    async def fake_asr(payload, filename):
        return "今日完成了三个任务"

    ext = AudioExtractor(asr_callable=fake_asr)
    att = {"filename": "voice.mp3", "data": base64.b64encode(b"fakeaudio").decode()}
    result = await ext.extract(att)
    assert "今日完成了三个任务" in result


@pytest.mark.asyncio
async def test_unknown_extractor():
    ext = UnknownExtractor()
    att = {"filename": "data.bin", "mime": "application/octet-stream"}
    result = await ext.extract(att)
    assert "不支持" in result


@pytest.mark.asyncio
async def test_cleaner_no_attachments():
    """无附件的输入应原样返回"""
    cleaner = MultimodalCleaner()
    inputs = [{"input_id": "i1", "content": "纯文本日报", "attachments": []}]
    result = await cleaner.clean_inputs(inputs)
    assert len(result) == 1
    assert result[0]["content"] == "纯文本日报"
    assert "extracted_text" not in result[0]


@pytest.mark.asyncio
async def test_cleaner_mixed_attachments():
    """混合附件应分别抽取并合并到 content"""
    cleaner = MultimodalCleaner()
    csv_bytes = "name,score\n张三,90\n".encode("utf-8")
    inputs = [
        {
            "input_id": "i1",
            "content": "本周工作汇报",
            "attachments": [
                {
                    "filename": "scores.csv",
                    "data": base64.b64encode(csv_bytes).decode(),
                },
                {
                    "filename": "screenshot.png",
                    "data": base64.b64encode(b"fake").decode(),
                },
                {"filename": "voice.mp3", "data": base64.b64encode(b"fake").decode()},
            ],
        }
    ]
    result = await cleaner.clean_inputs(inputs)
    assert len(result) == 1
    content = result[0]["content"]
    assert "本周工作汇报" in content
    assert "附件抽取内容" in content
    assert "张三" in content  # CSV 抽取
    assert "未配置" in content  # 图片/音频降级说明
    assert "extracted_text" in result[0]


@pytest.mark.asyncio
async def test_cleaner_extract_exception_does_not_crash():
    """单个附件抽取异常不应中断整体清洗"""

    async def boom_vision(b64, mime, filename):
        raise RuntimeError("vision service down")

    cleaner = MultimodalCleaner(vision_callable=boom_vision)
    inputs = [
        {
            "input_id": "i1",
            "content": "日报",
            "attachments": [
                {"filename": "img.png", "data": base64.b64encode(b"fake").decode()},
            ],
        }
    ]
    result = await cleaner.clean_inputs(inputs)
    assert len(result) == 1
    # 异常被捕获，content 中应包含失败说明
    assert "抽取失败" in result[0]["content"] or "未配置" in result[0]["content"]


# ===== Phase 7.1 多模态真实接入补充测试 =====

from core.config import get_settings
from core.multimodal.extractors import (  # noqa: E402
    CloudOCR,
    LocalTesseractOCR,
    PdfExtractor,
    WhisperASR,
)


def _make_minimal_pdf(text="Hello AgentValue PDF"):
    """构造 pypdf 可提取文本的最小单页 PDF（无需 reportlab）。"""
    stream = f"BT /F1 12 Tf 10 180 Td ({text}) Tj ET".encode("latin-1")
    parts = [
        b"%PDF-1.4\n",
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n",
        b"4 0 obj\n<< /Length %d >>\nstream\n" % len(stream)
        + stream
        + b"\nendstream\nendobj\n",
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
        b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n0\n%%EOF\n",
    ]
    return b"".join(parts)


def _make_two_page_pdf(t1="Page One", t2="Page Two"):
    """构造两页 PDF，验证多页文本拼接。"""
    s1 = f"BT /F1 12 Tf 10 180 Td ({t1}) Tj ET".encode("latin-1")
    s2 = f"BT /F1 12 Tf 10 180 Td ({t2}) Tj ET".encode("latin-1")
    parts = [
        b"%PDF-1.4\n",
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n",
        b"4 0 obj\n<< /Length %d >>\nstream\n" % len(s1)
        + s1
        + b"\nendstream\nendobj\n",
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
        b"6 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
        b"/Contents 7 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n",
        b"7 0 obj\n<< /Length %d >>\nstream\n" % len(s2)
        + s2
        + b"\nendstream\nendobj\n",
        b"trailer\n<< /Size 8 /Root 1 0 R >>\nstartxref\n0\n%%EOF\n",
    ]
    return b"".join(parts)


def _make_blank_pdf():
    """无文本内容的空白页 PDF（模拟扫描件）。"""
    parts = [
        b"%PDF-1.4\n",
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << >> >>\nendobj\n",
        b"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n0\n%%EOF\n",
    ]
    return b"".join(parts)


# ---------- PDF 抽取（真实 pypdf） ----------


@pytest.mark.asyncio
async def test_pdf_extractor_real_pypdf_text():
    """真实 PDF 文本提取：pypdf 解析手写最小 PDF。"""
    pdf_bytes = _make_minimal_pdf("Hello AgentValue PDF")
    ext = PdfExtractor()
    att = {"filename": "report.pdf", "data": base64.b64encode(pdf_bytes).decode()}
    result = await ext.extract(att)
    assert "Hello AgentValue PDF" in result
    assert "report.pdf" in result
    assert "第 1 页" in result


@pytest.mark.asyncio
async def test_pdf_extractor_two_pages_concat():
    """多页 PDF：每页文本按页拼接。"""
    pdf_bytes = _make_two_page_pdf("Page One", "Page Two")
    ext = PdfExtractor()
    att = {"filename": "multi.pdf", "data": base64.b64encode(pdf_bytes).decode()}
    result = await ext.extract(att)
    assert "Page One" in result
    assert "Page Two" in result
    assert "第 1 页" in result
    assert "第 2 页" in result


@pytest.mark.asyncio
async def test_pdf_extractor_blank_pdf_no_text():
    """空白/扫描件 PDF：无可提取文本时应提示。"""
    pdf_bytes = _make_blank_pdf()
    ext = PdfExtractor()
    att = {"filename": "scan.pdf", "data": base64.b64encode(pdf_bytes).decode()}
    result = await ext.extract(att)
    assert "无可提取文本" in result


@pytest.mark.asyncio
async def test_pdf_extractor_encrypted_detected(monkeypatch):
    """加密 PDF 应被检测并给出清晰提示。"""
    import pypdf

    class _FakeEncryptedReader:
        is_encrypted = True
        pages = []

    monkeypatch.setattr(pypdf, "PdfReader", lambda stream: _FakeEncryptedReader())
    ext = PdfExtractor()
    att = {"filename": "secret.pdf", "data": base64.b64encode(b"%PDF-1.4").decode()}
    result = await ext.extract(att)
    assert "已加密" in result


@pytest.mark.asyncio
async def test_pdf_extractor_corrupt_degrades_with_hint():
    """损坏 PDF：pypdf 解析失败时应降级提示（含 pdfplumber 兼容性引导）。"""
    ext = PdfExtractor()
    att = {"filename": "bad.pdf", "data": base64.b64encode(b"%PDF-1.4 fake").decode()}
    result = await ext.extract(att)
    assert "解析失败" in result
    assert "pdfplumber" in result


# ---------- Image OCR ----------


@pytest.mark.asyncio
async def test_image_ocr_tesseract_missing_degrades():
    """tesseract 系统二进制缺失时降级提示，不抛异常。"""
    pytest.importorskip("pytesseract")
    ext = LocalTesseractOCR()
    att = {"filename": "shot.png", "data": base64.b64encode(b"\x89PNGfake").decode()}
    result = await ext.extract(att)
    assert "OCR 不可用" in result
    assert "tesseract" in result


@pytest.mark.asyncio
async def test_image_ocr_mocked_text(monkeypatch):
    """mock pytesseract.image_to_string：应返回 OCR 文本。"""
    pytesseract = pytest.importorskip("pytesseract")
    monkeypatch.setattr(
        pytesseract, "image_to_string", lambda img, lang=None: "登录界面截图"
    )
    ext = LocalTesseractOCR()
    att = {"filename": "ui.png", "data": base64.b64encode(b"\x89PNGfake").decode()}
    result = await ext.extract(att)
    assert "登录界面截图" in result
    assert "ui.png" in result


@pytest.mark.asyncio
async def test_image_ocr_low_confidence_flag(monkeypatch):
    """OCR 置信度低于阈值时应标记需人工复核。"""
    pytesseract = pytest.importorskip("pytesseract")
    monkeypatch.setattr(
        pytesseract, "image_to_string", lambda img, lang=None: "模糊文字"
    )
    monkeypatch.setattr(pytesseract, "image_to_data", lambda *a, **k: {"conf": ["50"]})
    ext = LocalTesseractOCR()
    att = {"filename": "blur.png", "data": base64.b64encode(b"\x89PNGfake").decode()}
    result = await ext.extract(att)
    assert "模糊文字" in result
    assert "人工复核" in result


@pytest.mark.asyncio
async def test_image_ocr_high_confidence_no_flag(monkeypatch):
    """置信度高于阈值时不应出现复核标记。"""
    pytesseract = pytest.importorskip("pytesseract")
    monkeypatch.setattr(
        pytesseract, "image_to_string", lambda img, lang=None: "清晰文字"
    )
    monkeypatch.setattr(pytesseract, "image_to_data", lambda *a, **k: {"conf": ["95"]})
    ext = LocalTesseractOCR()
    att = {"filename": "clear.png", "data": base64.b64encode(b"\x89PNGfake").decode()}
    result = await ext.extract(att)
    assert "清晰文字" in result
    assert "人工复核" not in result


@pytest.mark.asyncio
async def test_cloud_ocr_no_api_key_degrades():
    """云端 OCR 未配置 API Key 时降级，不崩。"""
    ext = CloudOCR(provider="aliyun", api_key=None)
    att = {"filename": "x.png", "data": base64.b64encode(b"fake").decode()}
    result = await ext.extract(att)
    assert "未配置 API Key" in result


@pytest.mark.asyncio
async def test_cloud_ocr_disabled_returns_placeholder():
    """云端 OCR 未配置 api_key 且未注入 vision_callable 时返回需人工复核占位。"""
    ext = CloudOCR(api_key=None, vision_callable=None)
    att = {"filename": "x.png", "data": base64.b64encode(b"fake").decode()}
    result = await ext.extract(att)
    assert "未配置" in result
    assert "建议人工复核" in result  # 等价于 needs_human_review=True
    assert "x.png" in result


@pytest.mark.asyncio
async def test_cloud_ocr_with_mock_vision_callable():
    """注入 vision_callable 后应调用并返回其结果，不走 OpenAI client。"""

    captured = {}

    async def fake_vision(prompt, image_data):
        captured["prompt"] = prompt
        captured["image_data"] = image_data
        return "登录界面 用户名 密码 登录按钮"

    # 使用真实 PNG magic bytes 以通过 _validate_magic_bytes 校验
    png_bytes = b"\x89PNG\r\n\x1a\nfakepng"
    ext = CloudOCR(vision_callable=fake_vision)
    att = {
        "filename": "ui.png",
        "mime": "image/png",
        "data": base64.b64encode(png_bytes).decode(),
    }
    result = await ext.extract(att)
    assert "登录界面" in result
    assert "ui.png" in result
    # 验证 vision_callable 被调用且参数正确
    assert "prompt" in captured
    assert "image_data" in captured
    assert captured["image_data"] == base64.b64encode(png_bytes).decode()


@pytest.mark.asyncio
async def test_cloud_ocr_with_api_key_calls_openai(monkeypatch):
    """配置 api_key 后通过 AsyncOpenAI 调用 vision chat completions。"""
    import sys
    import types as _types

    captured = {}

    class _FakeMessage:
        content = "OCR 文本结果"

    class _FakeChoice:
        message = _FakeMessage()

    class _FakeResponse:
        choices = [_FakeChoice()]

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured["model"] = kwargs.get("model")
            captured["messages"] = kwargs.get("messages")
            return _FakeResponse()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        def __init__(self, **kwargs):
            captured["api_key"] = kwargs.get("api_key")
            captured["base_url"] = kwargs.get("base_url")
            self.chat = _FakeChat()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    fake_openai = _types.ModuleType("openai")
    fake_openai.AsyncOpenAI = _FakeClient
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    ext = CloudOCR(
        api_key="sk-test",
        base_url="https://api.example.com/v1",
        model="gpt-4o-mini",
    )
    # 使用真实 PNG magic bytes 以通过 _validate_magic_bytes 校验
    png_bytes = b"\x89PNG\r\n\x1a\nfakepng"
    att = {"filename": "x.png", "data": base64.b64encode(png_bytes).decode()}
    result = await ext.extract(att)
    assert "OCR 文本结果" in result
    assert "x.png" in result
    # 验证 client 用对了 key/base_url/model
    assert captured["api_key"] == "sk-test"
    assert captured["base_url"] == "https://api.example.com/v1"
    assert captured["model"] == "gpt-4o-mini"
    # 验证 messages 含图片 url
    msg_content = captured["messages"][0]["content"]
    assert any(part.get("type") == "image_url" for part in msg_content)


@pytest.mark.asyncio
async def test_cloud_ocr_short_output_flagged():
    """vision 输出过短时应标记需人工复核。"""

    async def short_vision(prompt, image_data):
        return "短"  # 长度 < 20

    ext = CloudOCR(vision_callable=short_vision)
    png_bytes = b"\x89PNG\r\n\x1a\nfake"
    att = {"filename": "x.png", "data": base64.b64encode(png_bytes).decode()}
    result = await ext.extract(att)
    assert "建议人工复核" in result


@pytest.mark.asyncio
async def test_cloud_ocr_failure_degrades():
    """vision_callable 抛异常时应降级不崩，标记人工复核。"""

    async def boom_vision(prompt, image_data):
        raise RuntimeError("vision service down")

    ext = CloudOCR(vision_callable=boom_vision)
    png_bytes = b"\x89PNG\r\n\x1a\nfake"
    att = {"filename": "x.png", "data": base64.b64encode(png_bytes).decode()}
    result = await ext.extract(att)
    assert "调用失败" in result
    assert "建议人工复核" in result


@pytest.mark.asyncio
async def test_image_extractor_delegates_to_ocr():
    """ImageExtractor 注入 ocr_extractor 后应委托给 OCR。"""

    class _StubOCR(LocalTesseractOCR):
        async def extract(self, attachment):
            return f"[图片附件 {attachment.get('filename')}] stub ocr text"

    ext = ImageExtractor(ocr_extractor=_StubOCR())
    att = {"filename": "x.png", "data": base64.b64encode(b"fake").decode()}
    result = await ext.extract(att)
    assert "stub ocr text" in result


# ---------- Audio ASR ----------


@pytest.mark.asyncio
async def test_whisper_asr_placeholder_no_crash():
    """WhisperASR 占位：未配置 api_key 时不崩，返回提示。"""
    ext = WhisperASR()
    att = {"filename": "v.mp3", "data": base64.b64encode(b"fake").decode()}
    result = await ext.extract(att)
    assert "Whisper" in result


@pytest.mark.asyncio
async def test_cloud_asr_disabled_returns_placeholder():
    """云端 ASR 未配置 api_key 时返回需人工复核占位。"""
    ext = WhisperASR(api_key=None)
    att = {"filename": "v.mp3", "data": base64.b64encode(b"fake").decode()}
    result = await ext.extract(att)
    assert "未配置" in result
    assert "建议人工复核" in result  # 等价于 needs_human_review=True
    assert "v.mp3" in result


@pytest.mark.asyncio
async def test_cloud_asr_with_api_key_calls_openai(monkeypatch):
    """配置 api_key 后通过 AsyncOpenAI 调用 whisper-1 audio.transcriptions。"""
    import sys
    import types as _types

    captured = {}

    class _FakeResponse:
        text = "今日完成了三个任务"

    class _FakeTranscriptions:
        async def create(self, **kwargs):
            captured["model"] = kwargs.get("model")
            captured["file"] = kwargs.get("file")
            return _FakeResponse()

    class _FakeAudio:
        transcriptions = _FakeTranscriptions()

    class _FakeClient:
        def __init__(self, **kwargs):
            captured["api_key"] = kwargs.get("api_key")
            captured["base_url"] = kwargs.get("base_url")
            self.audio = _FakeAudio()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    fake_openai = _types.ModuleType("openai")
    fake_openai.AsyncOpenAI = _FakeClient
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    ext = WhisperASR(
        api_key="sk-test",
        base_url="https://api.example.com/v1",
        model="whisper-1",
    )
    # 使用真实 MP3 magic bytes (ID3) 以通过 _validate_magic_bytes 校验
    mp3_bytes = b"ID3fakeaudio"
    att = {"filename": "voice.mp3", "data": base64.b64encode(mp3_bytes).decode()}
    result = await ext.extract(att)
    assert "今日完成了三个任务" in result
    assert "voice.mp3" in result
    assert captured["api_key"] == "sk-test"
    assert captured["base_url"] == "https://api.example.com/v1"
    assert captured["model"] == "whisper-1"
    # file 应是 BytesIO 且 name 设置为附件文件名
    assert hasattr(captured["file"], "read")
    assert captured["file"].name == "voice.mp3"


@pytest.mark.asyncio
async def test_cloud_asr_short_output_flagged(monkeypatch):
    """Whisper 输出过短时应标记需人工复核。"""

    captured = {}

    class _FakeResponse:
        text = "短"  # 长度 < 10

    class _FakeTranscriptions:
        async def create(self, **kwargs):
            return _FakeResponse()

    class _FakeAudio:
        transcriptions = _FakeTranscriptions()

    class _FakeClient:
        def __init__(self, **kwargs):
            self.audio = _FakeAudio()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    import sys
    import types as _types

    fake_openai = _types.ModuleType("openai")
    fake_openai.AsyncOpenAI = _FakeClient
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    ext = WhisperASR(api_key="sk-test")
    mp3_bytes = b"ID3fakeaudio"
    att = {"filename": "v.mp3", "data": base64.b64encode(mp3_bytes).decode()}
    result = await ext.extract(att)
    assert "建议人工复核" in result


@pytest.mark.asyncio
async def test_cloud_asr_failure_degrades(monkeypatch):
    """Whisper API 调用失败时应降级不崩，标记人工复核。"""

    class _FakeTranscriptions:
        async def create(self, **kwargs):
            raise RuntimeError("whisper service down")

    class _FakeAudio:
        transcriptions = _FakeTranscriptions()

    class _FakeClient:
        def __init__(self, **kwargs):
            self.audio = _FakeAudio()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    import sys
    import types as _types

    fake_openai = _types.ModuleType("openai")
    fake_openai.AsyncOpenAI = _FakeClient
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    ext = WhisperASR(api_key="sk-test")
    mp3_bytes = b"ID3fakeaudio"
    att = {"filename": "v.mp3", "data": base64.b64encode(mp3_bytes).decode()}
    result = await ext.extract(att)
    assert "调用失败" in result
    assert "建议人工复核" in result


# ---------- Cleaner 编排与防护 ----------


@pytest.mark.asyncio
async def test_cleaner_mixed_text_image_pdf_real():
    """Text+Image+Pdf 混合：按顺序拼接，content 末尾追加附件文本。"""
    # PDF 用 ASCII 文本（标准 Helvetica 字体不支持 CJK，此处验证提取机制）
    pdf_bytes = _make_minimal_pdf("PDF weekly report")
    cleaner = MultimodalCleaner()
    inputs = [
        {
            "input_id": "i1",
            "content": "本周工作汇报",
            "attachments": [
                {"filename": "note.txt", "content": "文本附件内容A"},
                {"filename": "shot.png", "data": base64.b64encode(b"fakepng").decode()},
                {
                    "filename": "report.pdf",
                    "data": base64.b64encode(pdf_bytes).decode(),
                },
            ],
        }
    ]
    result = await cleaner.clean_inputs(inputs)
    content = result[0]["content"]
    assert "本周工作汇报" in content
    assert "附件抽取内容" in content
    assert "文本附件内容A" in content
    assert "未配置" in content  # 图片降级
    assert "PDF weekly report" in content  # PDF 真实提取
    # 顺序：text -> image -> pdf
    assert content.index("文本附件内容A") < content.index("PDF weekly report")


@pytest.mark.asyncio
async def test_cleaner_path_traversal_blocked(monkeypatch, tmp_path):
    """附件路径越权访问应被拦截，敏感内容不得进入 content。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "attachment_dir", str(tmp_path))
    secret = tmp_path.parent / "secret_leak.txt"
    secret.write_bytes(b"TOP_SECRET_CONTENT")
    try:
        cleaner = MultimodalCleaner()
        inputs = [
            {
                "input_id": "i1",
                "content": "日报",
                "attachments": [{"filename": "evil.txt", "path": str(secret)}],
            }
        ]
        result = await cleaner.clean_inputs(inputs)
        content = result[0]["content"]
        assert "TOP_SECRET_CONTENT" not in content
        assert "无法读取" in content  # 越权 -> payload None -> 文本附件无法读取
    finally:
        secret.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_text_table_unknown_regression():
    """回归：Text/Table/Unknown 类型仍正常工作。"""
    cleaner = MultimodalCleaner()
    csv_bytes = "a,b\n1,2\n".encode("utf-8")
    inputs = [
        {
            "input_id": "i1",
            "content": "base",
            "attachments": [
                {"filename": "n.txt", "content": "纯文本"},
                {"filename": "d.csv", "data": base64.b64encode(csv_bytes).decode()},
                {"filename": "u.bin", "mime": "application/octet-stream"},
            ],
        }
    ]
    result = await cleaner.clean_inputs(inputs)
    content = result[0]["content"]
    assert "纯文本" in content
    assert "a" in content and "1" in content  # table
    assert "不支持" in content  # unknown


# ---------- Cleaner 装配云端 OCR/ASR（Phase 10）----------


def test_cleaner_default_no_cloud_wiring():
    """不传新参数时 cleaner 行为与原来一致：image/audio 走占位降级。"""
    cleaner = MultimodalCleaner()
    image_ext = cleaner._extractors["image"]
    audio_ext = cleaner._extractors["audio"]
    # 默认 ImageExtractor 无 vision_callable / 无 ocr_extractor
    assert image_ext._vision is None
    assert image_ext._ocr is None
    # 默认 AudioExtractor 无 asr_callable
    assert audio_ext._asr is None


def test_cleaner_wires_cloud_ocr_from_api_key():
    """传 ocr_api_key 但不传 ocr_extractor 时,自动构造 CloudOCR 注入到 ImageExtractor。"""
    cleaner = MultimodalCleaner(
        ocr_api_key="sk-ocr",
        ocr_base_url="https://api.example.com/v1",
        ocr_model="gpt-4o-mini",
    )
    image_ext = cleaner._extractors["image"]
    assert isinstance(image_ext._ocr, CloudOCR)
    assert image_ext._ocr._api_key == "sk-ocr"
    assert image_ext._ocr._base_url == "https://api.example.com/v1"
    assert image_ext._ocr._model == "gpt-4o-mini"


def test_cleaner_explicit_ocr_extractor_wins():
    """传 ocr_extractor 时不再自动构造 CloudOCR。"""

    class _StubOCR(LocalTesseractOCR):
        async def extract(self, attachment):
            return "stub"

    stub = _StubOCR()
    cleaner = MultimodalCleaner(
        ocr_extractor=stub,
        ocr_api_key="should-be-ignored",
    )
    image_ext = cleaner._extractors["image"]
    # 显式 extractor 优先,不会被 CloudOCR 覆盖
    assert image_ext._ocr is stub


def test_cleaner_wires_cloud_asr_from_api_key():
    """传 asr_api_key 但不传 asr_callable 时,自动构造 WhisperASR 注入到 audio。"""
    cleaner = MultimodalCleaner(
        asr_api_key="sk-asr",
        asr_base_url="https://api.example.com/v1",
        asr_model="whisper-1",
    )
    audio_ext = cleaner._extractors["audio"]
    assert isinstance(audio_ext, WhisperASR)
    assert audio_ext._api_key == "sk-asr"
    assert audio_ext._base_url == "https://api.example.com/v1"
    assert audio_ext._model == "whisper-1"


def test_cleaner_explicit_asr_callable_wins():
    """传 asr_callable 时不再自动构造 WhisperASR。"""

    async def fake_asr(payload, filename):
        return "stub"

    cleaner = MultimodalCleaner(
        asr_callable=fake_asr,
        asr_api_key="should-be-ignored",
    )
    audio_ext = cleaner._extractors["audio"]
    # 显式 callable 优先,不会被 WhisperASR 覆盖
    assert not isinstance(audio_ext, WhisperASR)
    assert audio_ext._asr is fake_asr


@pytest.mark.asyncio
async def test_cleaner_with_cloud_ocr_end_to_end():
    """cleaner 装配 CloudOCR 后,图片附件走 vision_callable 路径产出文本。"""

    async def fake_vision(prompt, image_data):
        return "云端 OCR 识别结果"

    # 这里直接注入 vision_callable 到 CloudOCR,验证 cleaner 编排能正确串联
    cloud_ocr = CloudOCR(vision_callable=fake_vision)
    cleaner = MultimodalCleaner(ocr_extractor=cloud_ocr)
    # 使用真实 PNG magic bytes 以通过 _validate_magic_bytes 校验
    png_bytes = b"\x89PNG\r\n\x1a\nfake"
    inputs = [
        {
            "input_id": "i1",
            "content": "日报",
            "attachments": [
                {"filename": "shot.png", "data": base64.b64encode(png_bytes).decode()},
            ],
        }
    ]
    result = await cleaner.clean_inputs(inputs)
    content = result[0]["content"]
    assert "云端 OCR 识别结果" in content
    assert "附件抽取内容" in content


@pytest.mark.asyncio
async def test_cleaner_with_cloud_asr_end_to_end(monkeypatch):
    """cleaner 装配 WhisperASR 后,音频附件走云端 whisper-1 路径产出文本。"""
    import sys
    import types as _types

    class _FakeResponse:
        text = "今日工作汇报音频转写"

    class _FakeTranscriptions:
        async def create(self, **kwargs):
            return _FakeResponse()

    class _FakeAudio:
        transcriptions = _FakeTranscriptions()

    class _FakeClient:
        def __init__(self, **kwargs):
            self.audio = _FakeAudio()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    fake_openai = _types.ModuleType("openai")
    fake_openai.AsyncOpenAI = _FakeClient
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    cleaner = MultimodalCleaner(
        asr_api_key="sk-asr",
        asr_base_url="https://api.example.com/v1",
    )
    # 使用真实 MP3 magic bytes (ID3) 以通过 _validate_magic_bytes 校验
    mp3_bytes = b"ID3fakeaudio"
    inputs = [
        {
            "input_id": "i1",
            "content": "日报",
            "attachments": [
                {"filename": "voice.mp3", "data": base64.b64encode(mp3_bytes).decode()},
            ],
        }
    ]
    result = await cleaner.clean_inputs(inputs)
    content = result[0]["content"]
    assert "今日工作汇报音频转写" in content
    assert "附件抽取内容" in content
