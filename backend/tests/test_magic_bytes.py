"""
H9: magic bytes 校验测试

验证：
- _validate_magic_bytes 对 PNG / JPEG / WebP / MP3 / WAV / MP4 / M4A 各类型
  的正例（合法签名）和反例（错签名 / 空数据 / 类型不匹配）；
- CloudOCR / WhisperASR 入口校验：合法 magic bytes 放行，非法 magic bytes
  抛 ValueError 含 mime 信息。
"""

import base64

import pytest

from core.multimodal.extractors import (
    CloudOCR,
    WhisperASR,
    _validate_magic_bytes,
)

# ---------------- 各类型真实 magic bytes 样本 ----------------

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
WEBP_BYTES = b"RIFF\x00\x00\x00\x00WEBPVP8 "
MP3_ID3_BYTES = b"ID3\x00\x00\x00\x00"
MP3_FB_BYTES = b"\xff\xfb\x90\x00"
WAV_BYTES = b"RIFF\x00\x00\x00\x00WAVEfmt "
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42"
M4A_BYTES = b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00M4A "


# ---------------- PNG ----------------


def test_validate_png_positive():
    assert _validate_magic_bytes(PNG_MAGIC + b"image data", ["png"]) is True


def test_validate_png_case_insensitive():
    assert _validate_magic_bytes(PNG_MAGIC + b"x", ["PNG", "Png"]) is True


def test_validate_png_negative_wrong_signature():
    assert _validate_magic_bytes(b"not a png", ["png"]) is False


def test_validate_png_negative_empty_data():
    assert _validate_magic_bytes(b"", ["png"]) is False


# ---------------- JPEG ----------------


def test_validate_jpeg_positive():
    assert _validate_magic_bytes(JPEG_MAGIC + b"\xe0jpegdata", ["jpeg"]) is True


def test_validate_jpeg_negative_wrong_signature():
    assert _validate_magic_bytes(b"abcdef", ["jpeg"]) is False


# ---------------- WebP ----------------


def test_validate_webp_positive():
    assert _validate_magic_bytes(WEBP_BYTES, ["webp"]) is True


def test_validate_webp_negative_riff_but_not_webp():
    """RIFF 开头但偏移 8 处不是 WEBP（如 WAV）不应通过 webp 校验。"""
    assert _validate_magic_bytes(WAV_BYTES, ["webp"]) is False


def test_validate_webp_negative_too_short():
    """RIFF 数据过短（<12 字节）不应通过 webp 校验。"""
    assert _validate_magic_bytes(b"RIFF\x00\x00", ["webp"]) is False


# ---------------- MP3 ----------------


def test_validate_mp3_id3_positive():
    assert _validate_magic_bytes(MP3_ID3_BYTES + b"audio", ["mp3"]) is True


def test_validate_mp3_fb_positive():
    assert _validate_magic_bytes(MP3_FB_BYTES + b"audio", ["mp3"]) is True


def test_validate_mp3_negative_wrong_signature():
    assert _validate_magic_bytes(b"notmp3", ["mp3"]) is False


# ---------------- WAV ----------------


def test_validate_wav_positive():
    assert _validate_magic_bytes(WAV_BYTES + b"data", ["wav"]) is True


def test_validate_wav_negative_riff_but_not_wave():
    """RIFF 开头但偏移 8 处不是 WAVE（如 WebP）不应通过 wav 校验。"""
    assert _validate_magic_bytes(WEBP_BYTES, ["wav"]) is False


# ---------------- MP4 / M4A (ftyp) ----------------


def test_validate_mp4_positive():
    assert _validate_magic_bytes(MP4_BYTES, ["mp4"]) is True


def test_validate_m4a_positive():
    assert _validate_magic_bytes(MP4_BYTES, ["m4a"]) is True


def test_validate_m4a_with_m4a_bytes_positive():
    assert _validate_magic_bytes(M4A_BYTES, ["m4a"]) is True


def test_validate_mp4_negative_no_ftyp():
    """没有 ftyp box 的字节流不应通过 mp4 校验。"""
    assert _validate_magic_bytes(b"plain bytes here", ["mp4"]) is False


def test_validate_mp4_negative_ftyp_at_wrong_offset():
    """ftyp 在偏移 0 处不应通过（必须在偏移 4~8）。"""
    assert _validate_magic_bytes(b"ftyp\x00\x00", ["mp4"]) is False


# ---------------- 多类型联合校验 ----------------


def test_validate_multiple_types_any_match():
    """任意一个期望类型命中即返回 True。"""
    assert _validate_magic_bytes(PNG_MAGIC + b"x", ["jpeg", "png"]) is True
    assert _validate_magic_bytes(JPEG_MAGIC + b"x", ["jpeg", "png"]) is True


def test_validate_multiple_types_none_match():
    assert _validate_magic_bytes(b"unknown", ["png", "jpeg", "webp"]) is False


def test_validate_empty_expected_types_returns_false():
    assert _validate_magic_bytes(b"some data", []) is False


def test_validate_none_data_returns_false():
    assert _validate_magic_bytes(None, ["png"]) is False  # type: ignore[arg-type]


def test_validate_unknown_type_in_list_skipped():
    """列表中包含未知类型名时，应跳过该项不报错。"""
    assert _validate_magic_bytes(PNG_MAGIC + b"x", ["unknown_type", "png"]) is True


# ---------------- CloudOCR 集成校验 ----------------


@pytest.mark.asyncio
async def test_cloud_ocr_valid_png_passes_validation():
    """CloudOCR 收到合法 PNG magic bytes 应放行调用 vision_callable。"""
    captured = {}

    async def fake_vision(prompt, image_data):
        captured["called"] = True
        return "OCR text"

    ext = CloudOCR(vision_callable=fake_vision)
    png_bytes = PNG_MAGIC + b"fake-image-data"
    att = {
        "filename": "ui.png",
        "mime": "image/png",
        "data": base64.b64encode(png_bytes).decode(),
    }
    result = await ext.extract(att)
    assert captured.get("called") is True
    assert "OCR text" in result


@pytest.mark.asyncio
async def test_cloud_ocr_invalid_bytes_raises_value_error():
    """CloudOCR 收到非图片字节流应抛 ValueError 且含 mime 信息。"""
    ext = CloudOCR(vision_callable=lambda **kw: None)
    att = {
        "filename": "evil.png",
        "mime": "image/png",
        "data": base64.b64encode(b"not-an-image").decode(),
    }
    with pytest.raises(ValueError, match="PNG/JPEG/WebP") as exc_info:
        await ext.extract(att)
    # 错误信息应包含 mime 便于排查
    assert "image/png" in str(exc_info.value)
    assert "evil.png" in str(exc_info.value)


@pytest.mark.asyncio
async def test_cloud_ocr_invalid_bytes_does_not_call_vision():
    """校验失败时不应调用 vision_callable，避免无效请求。"""
    called = False

    async def fake_vision(prompt, image_data):
        nonlocal called
        called = True
        return "should not be reached"

    ext = CloudOCR(vision_callable=fake_vision)
    att = {"filename": "x.png", "data": base64.b64encode(b"bad").decode()}
    with pytest.raises(ValueError):
        await ext.extract(att)
    assert called is False


# ---------------- WhisperASR 集成校验 ----------------


@pytest.mark.asyncio
async def test_whisper_asr_invalid_bytes_raises_value_error():
    """WhisperASR 收到非音频字节流应抛 ValueError 且含 mime 信息。"""
    ext = WhisperASR(api_key="sk-test")
    att = {
        "filename": "voice.mp3",
        "mime": "audio/mpeg",
        "data": base64.b64encode(b"not-audio").decode(),
    }
    with pytest.raises(ValueError, match="MP3/WAV/MP4/M4A") as exc_info:
        await ext.extract(att)
    assert "audio/mpeg" in str(exc_info.value)
    assert "voice.mp3" in str(exc_info.value)


@pytest.mark.asyncio
async def test_whisper_asr_valid_mp3_passes_validation(monkeypatch):
    """WhisperASR 收到合法 MP3 magic bytes 应放行调用 whisper API。"""
    import sys
    import types as _types

    captured = {}

    class _FakeResponse:
        text = "transcription text"

    class _FakeTranscriptions:
        async def create(self, **kwargs):
            captured["called"] = True
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

    ext = WhisperASR(api_key="sk-test")
    mp3_bytes = MP3_ID3_BYTES + b"audio-data"
    att = {"filename": "voice.mp3", "data": base64.b64encode(mp3_bytes).decode()}
    result = await ext.extract(att)
    assert captured.get("called") is True
    assert "transcription text" in result
