"""文档解析（文档 04 第 5 节第 1~3 条）。

文件类型通过扩展名与内容识别双重检查；扫描 PDF 转入不支持状态，不能当空文档索引成功。
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import HarnessLabError

MARKDOWN_SUFFIXES = {".md", ".markdown", ".mdx"}
TEXT_SUFFIXES = {".txt", ".text", ".log", ".csv", ".json", ".yaml", ".yml"}
PDF_SUFFIXES = {".pdf"}

MAGIC_PDF = b"%PDF-"
MAGIC_UTF8_BOM = b"\xef\xbb\xbf"


@dataclass
class Segment:
    """可定位的解析片段。"""

    text: str
    heading_path: str = ""
    page: int | None = None
    char_start: int | None = None
    char_end: int | None = None


@dataclass
class ParsedDocument:
    title: str
    media_type: str
    segments: list[Segment] = field(default_factory=list)
    unsupported_reason: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return sum(len(segment.text) for segment in self.segments)

    @property
    def ok(self) -> bool:
        return self.unsupported_reason is None and bool(self.segments)


def detect_media_type(data: bytes, filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if data.startswith(MAGIC_PDF):
        return "application/pdf"
    if suffix in MARKDOWN_SUFFIXES:
        return "text/markdown"
    if suffix in PDF_SUFFIXES:
        return "application/pdf"
    if suffix in TEXT_SUFFIXES:
        return "text/plain"
    if data.startswith(MAGIC_UTF8_BOM) or _looks_like_text(data):
        return "text/plain"
    return "application/octet-stream"


def _looks_like_text(data: bytes) -> bool:
    sample = data[:4096]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise HarnessLabError("VALIDATION_ERROR", "文件不是有效的 UTF-8 文本，无法解析")


def parse_markdown(text: str) -> list[Segment]:
    """按标题层级切分，保留 heading_path 与字符范围。"""
    segments: list[Segment] = []
    stack: list[tuple[int, str]] = []
    buffer: list[str] = []
    start = 0
    cursor = 0
    heading_path = ""

    def flush(end: int) -> None:
        raw = "".join(buffer)
        if raw.strip():
            segments.append(
                Segment(text=raw.strip(), heading_path=heading_path, char_start=start, char_end=end)
            )

    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            title = stripped[level:].strip()
            if 1 <= level <= 6 and title:
                flush(cursor)
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, title))
                heading_path = " > ".join(item[1] for item in stack)
                buffer = []
                start = cursor + len(line)
        else:
            buffer.append(line)
        cursor += len(line)
    flush(len(text))
    if not segments and text.strip():
        segments.append(Segment(text=text.strip(), char_start=0, char_end=len(text)))
    return segments


def parse_plain_text(text: str, *, window: int = 1200) -> list[Segment]:
    """TXT 保留字符偏移；按空行聚合成片段以便定位。"""
    segments: list[Segment] = []
    position = 0
    for block in text.split("\n\n"):
        block_start = text.find(block, position)
        if block_start < 0:  # pragma: no cover
            block_start = position
        position = block_start + len(block)
        if block.strip():
            segments.append(
                Segment(text=block.strip(), char_start=block_start, char_end=block_start + len(block))
            )
    if not segments and text.strip():
        segments.append(Segment(text=text.strip(), char_start=0, char_end=len(text)))
    return segments


def parse_pdf(data: bytes, *, filename: str, max_pages: int = 200) -> ParsedDocument:
    """保留物理页码；无文本层（扫描件）返回不支持状态。"""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise HarnessLabError("VALIDATION_ERROR", "PDF 支持未安装，请安装 pypdf") from exc

    reader = PdfReader(io.BytesIO(data))
    total_pages = len(reader.pages)
    if total_pages > max_pages:
        raise HarnessLabError(
            "VALIDATION_ERROR", f"PDF 页数 {total_pages} 超过上限 {max_pages}", details={"pages": total_pages}
        )
    segments: list[Segment] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            text = ""
            logger_warning = f"第 {index} 页解析失败：{type(exc).__name__}"
            segments.append(Segment(text="", page=index, heading_path=logger_warning))
            continue
        if text.strip():
            segments.append(Segment(text=text.strip(), page=index))
    document = ParsedDocument(
        title=filename, media_type="application/pdf", segments=[s for s in segments if s.text.strip()]
    )
    if not document.segments:
        document.unsupported_reason = "PDF 没有可提取的文本层（可能是扫描件），当前不支持 OCR"
    return document


def parse_document(data: bytes, filename: str, *, max_pages: int = 200) -> ParsedDocument:
    media_type = detect_media_type(data, filename)
    if media_type == "application/pdf":
        return parse_pdf(data, filename=filename, max_pages=max_pages)
    if media_type == "application/octet-stream":
        document = ParsedDocument(title=filename, media_type=media_type)
        document.unsupported_reason = "不支持的二进制格式，仅支持 Markdown、TXT 与带文本层的 PDF"
        return document
    text = _decode(data)
    segments = parse_markdown(text) if media_type == "text/markdown" else parse_plain_text(text)
    return ParsedDocument(title=filename, media_type=media_type, segments=segments)
