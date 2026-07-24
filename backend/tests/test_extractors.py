"""
core/multimodal/extractors.py 补充测试
覆盖 _get_attachment_payload 的路径/bytes/降级分支、_detect_kind 剩余类型、
各抽取器的 None payload / 异常 / ImportError 降级 / 空数据分支，以及 _rows_to_markdown 边界。
"""

import base64
import sys
import types

import pytest

from core.config import get_settings
from core.multimodal.extractors import (
    AudioExtractor,
    ImageExtractor,
    PdfExtractor,
    TableExtractor,
    TextExtractor,
    _detect_kind,
    _get_attachment_payload,
)


# ---------------- _get_attachment_payload ----------------


def test_payload_bytes_data():
    assert _get_attachment_payload({"data": b"hello"}) == b"hello"


def test_payload_bytearray_data():
    assert _get_attachment_payload({"data": bytearray(b"abc")}) == b"abc"


def test_payload_invalid_base64_returns_none():
    assert _get_attachment_payload({"data": "不是合法base64!!!"}) is None


def test_payload_key_downloads_from_storage(monkeypatch):
    """有 key 字段时,通过 storage 抽象下载二进制内容"""
    import core.storage as storage_mod

    class _FakeStorage:
        def download(self, key):
            assert key == "attachments/test.png"
            return b"downloaded-bytes"

    monkeypatch.setattr(storage_mod, "get_storage", lambda: _FakeStorage())

    result = _get_attachment_payload({"key": "attachments/test.png"})
    assert result == b"downloaded-bytes"


def test_payload_key_download_failure_returns_none(monkeypatch):
    """对象存储下载失败时返回 None,不抛异常"""
    import core.storage as storage_mod

    class _FailStorage:
        def download(self, key):
            raise FileNotFoundError("object not found")

    monkeypatch.setattr(storage_mod, "get_storage", lambda: _FailStorage())

    assert _get_attachment_payload({"key": "missing-key"}) is None


def test_payload_url_returns_none():
    assert _get_attachment_payload({"url": "https://example.com/a.png"}) is None


def test_payload_empty_returns_none():
    assert _get_attachment_payload({}) is None


def test_payload_valid_path_read(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "attachment_dir", str(tmp_path))
    f = tmp_path / "note.txt"
    f.write_bytes(b"file content")

    assert _get_attachment_payload({"path": str(f)}) == b"file content"


def test_payload_path_traversal_denied(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "attachment_dir", str(tmp_path))
    # 路径在白名单目录之外
    outside = tmp_path.parent / "secret.txt"
    outside.write_bytes(b"secret")  # tmp_path.parent 是 /tmp 之类
    try:
        assert _get_attachment_payload({"path": str(outside)}) is None
    finally:
        outside.unlink(missing_ok=True)


def test_payload_path_inside_dir_but_missing_returns_none(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "attachment_dir", str(tmp_path))
    missing = tmp_path / "nope.txt"
    assert _get_attachment_payload({"path": str(missing)}) is None


# ---------------- _detect_kind ----------------


def test_detect_kind_video():
    assert _detect_kind("video/mp4", "clip.mp4") == "video"


def test_detect_kind_xlsx_mime():
    assert (
        _detect_kind(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "data.xlsx",
        )
        == "table"
    )


def test_detect_kind_tsv():
    assert _detect_kind("", "data.tsv") == "table"


def test_detect_kind_csv_mime_priority_over_text():
    # text/csv 应识别为 table 而非 text
    assert _detect_kind("text/csv", "data.csv") == "table"


# ---------------- TextExtractor ----------------


@pytest.mark.asyncio
async def test_text_extractor_no_payload_no_content():
    ext = TextExtractor()
    result = await ext.extract({"filename": "empty.txt"})
    assert "无法读取内容" in result


# ---------------- TableExtractor ----------------


@pytest.mark.asyncio
async def test_table_extractor_no_payload():
    ext = TableExtractor()
    result = await ext.extract({"filename": "no_data.csv"})
    assert "无法读取内容" in result


@pytest.mark.asyncio
async def test_table_extractor_tsv():
    ext = TableExtractor()
    tsv_bytes = "name\tscore\n张三\t90\n".encode("utf-8")
    att = {"filename": "data.tsv", "data": base64.b64encode(tsv_bytes).decode()}
    result = await ext.extract(att)
    assert "张三" in result
    assert "name" in result


@pytest.mark.asyncio
async def test_table_extractor_xlsx_without_openpyxl_degrades(monkeypatch):
    """openpyxl 未安装时应降级提示"""
    # 模拟 openpyxl 未安装：置 sys.modules 为 None 使 `from openpyxl import ...` 触发 ImportError
    monkeypatch.setitem(sys.modules, "openpyxl", None)
    ext = TableExtractor()
    att = {"filename": "data.xlsx", "data": base64.b64encode(b"fakexlsx").decode()}
    result = await ext.extract(att)
    assert "openpyxl" in result


# ---------------- _rows_to_markdown 边界 ----------------


def test_rows_to_markdown_empty():
    ext = TableExtractor()
    assert "空表格" in ext._rows_to_markdown([], "f.csv")


def test_rows_to_markdown_none_cell_and_no_header():
    ext = TableExtractor()
    # header=False 分支 + None 单元格
    md = ext._rows_to_markdown([["a", None], ["b", "c"]], "f.csv", header=False)
    assert "列" in md  # 无表头时用“列”占位
    assert "a" in md
    assert "b" in md
    assert "c" in md


def test_rows_to_markdown_truncates_long_table():
    ext = TableExtractor()
    rows = [[f"r{i}"] for i in range(100)]
    md = ext._rows_to_markdown(rows, "big.csv")
    assert "已截断" in md


# ---------------- ImageExtractor ----------------


@pytest.mark.asyncio
async def test_image_extractor_vision_but_no_payload():
    async def vision(b64, mime, filename):
        return "desc"

    ext = ImageExtractor(vision_callable=vision)
    result = await ext.extract({"filename": "x.png"})  # 无 data
    assert "无法读取图片数据" in result


@pytest.mark.asyncio
async def test_image_extractor_vision_exception():
    """vision callable 抛异常时应降级到 OCR，无 OCR 时返回未配置提示（不丢失图片内容）"""

    async def vision(prompt: str, image_data: str):
        raise RuntimeError("vision down")

    ext = ImageExtractor(vision_callable=vision)
    att = {"filename": "x.png", "data": base64.b64encode(b"fake").decode()}
    result = await ext.extract(att)
    # vision 失败后降级：无 ocr_extractor 时返回未配置提示
    assert "未配置" in result


# ---------------- AudioExtractor ----------------


@pytest.mark.asyncio
async def test_audio_extractor_asr_but_no_payload():
    async def asr(payload, filename):
        return "text"

    ext = AudioExtractor(asr_callable=asr)
    result = await ext.extract({"filename": "x.mp3"})  # 无 data
    assert "无法读取音频数据" in result


@pytest.mark.asyncio
async def test_audio_extractor_asr_exception():
    async def asr(payload, filename):
        raise RuntimeError("asr down")

    ext = AudioExtractor(asr_callable=asr)
    att = {"filename": "x.mp3", "data": base64.b64encode(b"fake").decode()}
    result = await ext.extract(att)
    assert "抽取失败" in result


# ---------------- PdfExtractor ----------------


@pytest.mark.asyncio
async def test_pdf_extractor_no_payload():
    ext = PdfExtractor()
    result = await ext.extract({"filename": "x.pdf"})
    assert "无法读取数据" in result


@pytest.mark.asyncio
async def test_pdf_extractor_without_pdfplumber_degrades():
    """pdfplumber/PyPDF2 未安装时应降级提示"""
    ext = PdfExtractor()
    att = {"filename": "x.pdf", "data": base64.b64encode(b"%PDF-1.4 fake").decode()}
    result = await ext.extract(att)
    assert "pdfplumber" in result


@pytest.mark.asyncio
async def test_pdf_extractor_with_mocked_pdfplumber():
    """注入假 pdfplumber 模块，覆盖正常抽取路径"""
    fake_pdf = types.ModuleType("pdfplumber")

    class _FakePage:
        def extract_text(self):
            return "第 1 页文本内容"

    class _FakePdf:
        def __init__(self, payload):
            self.pages = [_FakePage()]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _open(payload):
        return _FakePdf(payload)

    fake_pdf.open = _open
    # 临时注入模块，测试后还原
    sys.modules["pdfplumber"] = fake_pdf
    try:
        ext = PdfExtractor()
        att = {"filename": "doc.pdf", "data": base64.b64encode(b"%PDF-1.4").decode()}
        result = await ext.extract(att)
        assert "第 1 页文本内容" in result
        assert "doc.pdf" in result
    finally:
        del sys.modules["pdfplumber"]


@pytest.mark.asyncio
async def test_pdf_extractor_no_extractable_text_with_mock():
    """注入假 pdfplumber 返回空文本，应提示无可提取文本"""
    fake_pdf = types.ModuleType("pdfplumber")

    class _FakePage:
        def extract_text(self):
            return ""

    class _FakePdf:
        def __init__(self, payload):
            self.pages = [_FakePage()]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake_pdf.open = lambda payload: _FakePdf(payload)
    sys.modules["pdfplumber"] = fake_pdf
    try:
        ext = PdfExtractor()
        att = {"filename": "scan.pdf", "data": base64.b64encode(b"%PDF-1.4").decode()}
        result = await ext.extract(att)
        assert "无可提取文本" in result
    finally:
        del sys.modules["pdfplumber"]
