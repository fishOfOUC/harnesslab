"""稀疏检索与融合（文档 04 第 7 节）。

中文分词 + BM25 可重建索引；RRF 参数与分词配置进入索引指纹版本。
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any

from ..observability.logging import get_logger

logger = get_logger("retrieval")

_LATIN = re.compile(r"[A-Za-z0-9_]+")
_CJK = re.compile(r"[\u3000-\u9fff\uf900-\ufaff]+")
STOPWORDS = {"的", "了", "是", "和", "与", "在", "对", "及", "或", "一个", "the", "a", "an", "of", "and", "to"}


def tokenize(text: str) -> list[str]:
    """中文使用 jieba 分词；不可用时退化为字级二元切分，避免默认英文分词影响中文召回。"""
    tokens: list[str] = []
    for word in _LATIN.findall(text.lower()):
        tokens.append(word)
    for block in _CJK.findall(text):
        try:
            import jieba

            cut = [token.strip() for token in jieba.cut(block)]
        except ImportError:  # pragma: no cover - 可选依赖
            cut = [block[i : i + 2] for i in range(max(len(block) - 1, 1))]
        tokens.extend(token for token in cut if token and token not in STOPWORDS)
    return tokens or [text.lower()]


@dataclass
class SparseHit:
    chunk_id: str
    score: float


class BM25Index:
    """按 (project_id, index_version) 构建的内存 BM25，可整体重建。

    这是进程级单例：多线程（API 检索预览与工作进程执行）可能同时进来，
    因此重建与查询都持同一把锁，避免读到「旧的词表 + 新的 id 列表」这种错配。
    """

    def __init__(self) -> None:
        self._version_key: tuple[str, str, int] | None = None
        self._bm25 = None
        self._ids: list[str] = []
        self._lock = threading.RLock()

    def build(self, chunks: list[dict[str, Any]], *, project_id: str, index_version: str) -> None:
        key = (project_id, index_version, len(chunks))
        with self._lock:
            if self._version_key == key:
                return
            from rank_bm25 import BM25Okapi

            corpus = [tokenize(chunk["text"]) for chunk in chunks]
            ids = [chunk["id"] for chunk in chunks]
            bm25 = BM25Okapi(corpus) if corpus else None
            # 先算完再整体替换，避免中途状态被其他线程读到
            self._ids = ids
            self._bm25 = bm25
            self._version_key = key
        logger.info("BM25 索引重建完成", extra={"extra_fields": {"chunks": len(chunks)}})

    def search(self, query: str, top_k: int) -> list[SparseHit]:
        with self._lock:
            if self._bm25 is None or not self._ids:
                return []
            ids = list(self._ids)
            scores = self._bm25.get_scores(tokenize(query))
        ranked = sorted(zip(ids, scores, strict=True), key=lambda item: item[1], reverse=True)
        return [SparseHit(chunk_id=cid, score=float(score)) for cid, score in ranked[:top_k] if score > 0]


def reciprocal_rank_fusion(
    rankings: list[list[str]], *, k: int = 60, weights: list[float] | None = None
) -> list[tuple[str, float]]:
    """RRF 合并多路召回；相同 chunk 只保留最高融合分。"""
    scores: dict[str, float] = {}
    for index, ranking in enumerate(rankings):
        weight = 1.0 if weights is None else weights[index]
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (k + rank)
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)


_bm25 = BM25Index()


def get_bm25_index() -> BM25Index:
    return _bm25
