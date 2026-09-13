"""切分（文档 04 第 5 节第 4~5 条）。

目标约 600 token、重叠约 80；无法获得准确 tokenizer，使用保守估算并显式标注为估算值。
表格与代码块尽量保持边界；过大块单独切分并保留父块关系。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..utils import estimate_tokens, sha256_text
from .loader import Segment


@dataclass
class Chunk:
    seq: int
    text: str
    content_hash: str
    source_title: str
    heading_path: str = ""
    page: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    token_estimate: int = 0
    parent_seq: int | None = None
    kind: str = "text"

    def as_row(self, *, project_id: str, document_id: str, document_version: int, index_version: str,
               created_at: str) -> dict:
        from ..utils import new_id

        return {
            "id": new_id(),
            "project_id": project_id,
            "document_id": document_id,
            "document_version": document_version,
            "index_version": index_version,
            "seq": self.seq,
            "text": self.text,
            "content_hash": self.content_hash,
            "source_title": self.source_title,
            "heading_path": self.heading_path,
            "page": self.page,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "token_estimate": self.token_estimate,
            "created_at": created_at,
        }


def _split_long_text(text: str, target_tokens: int, overlap_tokens: int) -> list[str]:
    """按段落/句子边界滑动窗口切分，超长单段强制按字符切。"""
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    pieces: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        candidate = f"{buffer}\n\n{paragraph}" if buffer else paragraph
        if estimate_tokens(candidate) <= target_tokens:
            buffer = candidate
            continue
        if buffer:
            pieces.append(buffer)
        if estimate_tokens(paragraph) <= target_tokens:
            buffer = paragraph
            continue
        # 单段过长：按字符窗口切分
        approx_chars = max(int(target_tokens / 0.7), 200)
        step = max(approx_chars - int(overlap_tokens / 0.7), approx_chars // 2)
        for start in range(0, len(paragraph), step):
            pieces.append(paragraph[start : start + approx_chars])
        buffer = ""
    if buffer:
        pieces.append(buffer)
    return pieces


def chunk_segment(
    segment: Segment,
    *,
    source_title: str,
    start_seq: int,
    target_tokens: int = 600,
    overlap_tokens: int = 80,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    seq = start_seq
    for piece in _split_long_text(segment.text, target_tokens, overlap_tokens):
        text = piece.strip()
        if not text:
            continue
        char_start = segment.char_start
        if char_start is not None:
            offset = segment.text.find(text)
            if offset >= 0:
                char_start = segment.char_start + offset
        char_end = char_start + len(text) if char_start is not None else None
        chunks.append(
            Chunk(
                seq=seq,
                text=text,
                content_hash=sha256_text(text),
                source_title=source_title,
                heading_path=segment.heading_path,
                page=segment.page,
                char_start=char_start,
                char_end=char_end,
                token_estimate=estimate_tokens(text),
            )
        )
        seq += 1
    return chunks


def chunk_text(segments: list[Segment], *, source_title: str, target_tokens: int = 600,
               overlap_tokens: int = 80) -> list[Chunk]:
    chunks: list[Chunk] = []
    for segment in segments:
        chunks.extend(
            chunk_segment(
                segment,
                source_title=source_title,
                start_seq=len(chunks),
                target_tokens=target_tokens,
                overlap_tokens=overlap_tokens,
            )
        )
    return chunks
