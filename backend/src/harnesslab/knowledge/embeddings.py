"""Embedding 适配（文档 04 第 4 节）。

优先使用自研薄适配器：直接发送原始字符串与 encoding_format=float，按返回 index 排序，
并显式校验长度、维度与数值有限性。本地模型的 tokenizer / 前缀与云端不同，需要掌控预处理。
"""

from __future__ import annotations

import hashlib
import math
import time
from typing import Any, Protocol

import httpx
from langchain_core.embeddings import Embeddings

from ..config import Settings, get_settings
from ..errors import HarnessLabError
from ..observability.logging import get_logger
from ..utils import hash_payload

logger = get_logger("embeddings")


class EmbeddingsBackend(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def _validate(vectors: list[list[float]], expected: int, dimension: int | None) -> int:
    if len(vectors) != expected:
        raise HarnessLabError(
            "EMBEDDING_UNAVAILABLE",
            f"向量条数与输入不一致：期望 {expected}，实际 {len(vectors)}",
            details={"expected": expected, "actual": len(vectors)},
        )
    dims = {len(v) for v in vectors}
    if len(dims) != 1:
        raise HarnessLabError("EMBEDDING_UNAVAILABLE", "同一批次返回的向量维度不一致",
                              details={"dimensions": sorted(dims)})
    dim = dims.pop()
    if dim <= 0:
        raise HarnessLabError("EMBEDDING_UNAVAILABLE", "返回了空向量")
    for vector in vectors:
        for value in vector:
            if not math.isfinite(value):
                raise HarnessLabError("EMBEDDING_UNAVAILABLE", "向量包含非有限数值")
        if not any(value != 0.0 for value in vector):
            raise HarnessLabError("EMBEDDING_UNAVAILABLE", "向量全为零，来源模型或输入可能异常")
    if dimension is not None and dim != dimension:
        raise HarnessLabError(
            "INDEX_VERSION_MISMATCH",
            f"实测维度 {dim} 与索引维度 {dimension} 不一致，需要重建索引",
            details={"measured": dim, "expected": dimension},
        )
    return dim


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0:
        return vector
    return [v / norm for v in vector]


class OpenAICompatibleEmbeddings:
    """直接调用 OpenAI 兼容 /embeddings（LM Studio 等）。"""

    def __init__(self, settings: Settings, dimension: int | None = None) -> None:
        self.settings = settings
        self.dimension = dimension
        self._client = httpx.Client(
            base_url=settings.embedding_base_url.rstrip("/"),
            timeout=settings.embedding_timeout_seconds,
            headers=self._headers(),
        )
        self.measured_dimension: int | None = dimension

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.embedding_api_key:
            headers["Authorization"] = f"Bearer {self.settings.embedding_api_key}"
        return headers

    # ------------------------------------------------------------------
    def embed(self, texts: list[str], *, prefix: str = "") -> list[list[float]]:
        if not texts:
            return []
        prepared = [f"{prefix}{text}" if prefix else text for text in texts]
        batches = max(1, self.settings.embedding_batch_size)
        output: list[list[float]] = []
        for start in range(0, len(prepared), batches):
            output.extend(self._embed_batch(prepared[start : start + batches]))
        return output

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload = {"model": self.settings.embedding_model, "input": texts, "encoding_format": "float"}
        last_error: Exception | None = None
        for attempt in range(self.settings.embedding_max_retries + 1):
            try:
                response = self._client.post("/embeddings", json=payload)
                if response.status_code == 401:
                    raise HarnessLabError("EMBEDDING_UNAVAILABLE", "向量服务认证失败，请检查 EMBEDDING_API_KEY")
                if response.status_code == 404:
                    raise HarnessLabError(
                        "EMBEDDING_UNAVAILABLE",
                        "向量接口 404：请确认 base URL 以 /v1 结尾且模型已加载",
                    )
                if response.status_code >= 400:
                    raise HarnessLabError(
                        "EMBEDDING_UNAVAILABLE",
                        f"向量服务返回 {response.status_code}",
                        details={"body": response.text[:300]},
                    )
                body: dict[str, Any] = response.json()
                data = sorted(body.get("data", []), key=lambda item: item.get("index", 0))
                vectors = [[float(x) for x in item.get("embedding", [])] for item in data]
                dimension = _validate(vectors, len(texts), self.measured_dimension)
                if self.settings.embedding_normalize:
                    vectors = [_normalize(v) for v in vectors]
                self.measured_dimension = dimension
                return vectors
            except HarnessLabError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt < self.settings.embedding_max_retries:
                    time.sleep(min(2**attempt * 0.5, 4))
                    continue
        raise HarnessLabError(
            "EMBEDDING_UNAVAILABLE",
            "本地向量服务不可用，请检查 LM Studio 服务状态",
            retryable=True,
            details={"reason": type(last_error).__name__ if last_error else "unknown"},
        )

    def probe(self) -> dict[str, Any]:
        vectors = self.embed(["本地向量检索测试"])
        return {
            "model": self.settings.embedding_model,
            "dimension": len(vectors[0]),
            "normalize": self.settings.embedding_normalize,
            "batch_size": self.settings.embedding_batch_size,
        }

    def list_models(self) -> list[str]:
        response = self._client.get("/models")
        response.raise_for_status()
        return [item.get("id", "") for item in response.json().get("data", [])]

    def close(self) -> None:
        self._client.close()


class HashEmbeddings:
    """确定性替身：仅用于测试与离线契约验证，界面必须显式标注为替身。"""

    def __init__(self, dimension: int = 64) -> None:
        self.dimension = dimension

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in text.lower().split() or [text.lower()]:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            for index in range(self.dimension):
                vector[index] += (digest[index % len(digest)] - 128) / 128.0
        return _normalize(vector)

    def embed(self, texts: list[str], *, prefix: str = "") -> list[list[float]]:
        return [self._vector(f"{prefix}{t}" if prefix else t) for t in texts]

    def probe(self) -> dict[str, Any]:
        return {"model": "fake-hash-embeddings", "dimension": self.dimension, "normalize": True,
                "batch_size": self.dimension}


class EmbeddingService(Embeddings):
    """LangChain Embeddings 接口 + 项目内使用的原始接口。"""

    def __init__(self, backend: OpenAICompatibleEmbeddings | HashEmbeddings, settings: Settings) -> None:
        self.backend = backend
        self.settings = settings

    # LangChain 接口 ---------------------------------------------------
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.backend.embed(texts, prefix=self.settings.embedding_document_prefix)

    def embed_query(self, text: str) -> list[float]:
        return self.backend.embed([text], prefix=self.settings.embedding_query_prefix)[0]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed_documents(texts)

    async def aembed_query(self, text: str) -> list[float]:
        return self.embed_query(text)

    # 项目接口 ---------------------------------------------------------
    def embed_query_detailed(self, text: str) -> list[float]:
        return self.embed_query(text)

    def probe(self) -> dict[str, Any]:
        return self.backend.probe()

    def fingerprint(self) -> str:
        """索引指纹：provider + 模型 + 维度 + 前缀 + 归一化（文档 04 第 6 节）。"""
        dimension = self.backend.probe()["dimension"] if isinstance(self.backend, HashEmbeddings) else None
        return hash_payload(
            {
                "provider": self.settings.embedding_provider,
                "model": self.settings.embedding_model,
                "dimension": (
                    dimension if dimension is not None
                    else (self.settings.embedding_dimension if self.settings.embedding_dimension != "auto"
                          else "auto")
                ),
                "query_prefix": self.settings.embedding_query_prefix,
                "document_prefix": self.settings.embedding_document_prefix,
                "normalize": self.settings.embedding_normalize,
                "pipeline_version": self.settings.pipeline_version,
            }
        )


def build_embedding_service(settings: Settings | None = None,
                            dimension: int | None = None) -> EmbeddingService:
    settings = settings or get_settings()
    if settings.embedding_provider == "fake":
        backend: OpenAICompatibleEmbeddings | HashEmbeddings = HashEmbeddings()
    else:
        backend = OpenAICompatibleEmbeddings(settings, dimension=dimension)
    return EmbeddingService(backend, settings)
