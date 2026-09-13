"""向量索引（文档 02 / 04 第 6 节）。

P0 使用 Qdrant Local 独立持久化目录，只由工作进程访问；API 不另开实例竞争该目录。
所有召回都在存储层应用项目与索引版本过滤，不做“先检索全部再交给模型过滤”。
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

from ..config import Settings, get_settings
from ..errors import HarnessLabError
from ..observability.logging import get_logger

logger = get_logger("index")


@dataclass
class VectorHit:
    chunk_id: str
    score: float
    payload: dict[str, Any]


class VectorIndex:
    """Qdrant 封装：collection 内按 project_id + index_version 过滤。"""

    def __init__(self, settings: Settings) -> None:
        from qdrant_client import QdrantClient

        self.settings = settings
        self._dimension: int | None = None
        if settings.vector_mode == "service":
            self.client = QdrantClient(url=settings.embedding_base_url)  # 服务模式由后续阶段配置
        else:
            self.client = QdrantClient(path=str(settings.qdrant_path))
        self.collection = settings.vector_collection

    # ------------------------------------------------------------------
    def ensure_collection(self, dimension: int) -> None:
        from qdrant_client import models

        if self.client.collection_exists(self.collection):
            self._dimension = self._detect_dimension()
            if self._dimension is not None and self._dimension != dimension:
                raise HarnessLabError(
                    "INDEX_VERSION_MISMATCH",
                    f"向量库维度 {self._dimension} 与当前配置 {dimension} 不一致，请执行 index rebuild",
                    details={"stored": self._dimension, "configured": dimension},
                )
            return
        self.client.create_collection(
            collection_name=self.collection,
            vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE),
        )
        self._dimension = dimension

    def _detect_dimension(self) -> int | None:
        try:
            info = self.client.get_collection(self.collection)
            vectors = info.config.params.vectors
            size = getattr(vectors, "size", None)
            return int(size) if size else None
        except Exception:
            return None

    @property
    def dimension(self) -> int | None:
        return self._dimension

    # ------------------------------------------------------------------
    def upsert(self, points: list[tuple[str, list[float], dict[str, Any]]]) -> int:
        if not points:
            return 0
        from qdrant_client import models

        payload = [
            models.PointStruct(id=chunk_id, vector=vector, payload=metadata | {"chunk_id": chunk_id})
            for chunk_id, vector, metadata in points
        ]
        self.client.upsert(collection_name=self.collection, points=payload, wait=True)
        return len(payload)

    def _filter(self, project_id: str, index_version: str, extra: dict[str, Any] | None = None):
        from qdrant_client import models

        must: list[Any] = [
            models.FieldCondition(key="project_id", match=models.MatchValue(value=project_id)),
            models.FieldCondition(key="index_version", match=models.MatchValue(value=index_version)),
        ]
        for key, value in (extra or {}).items():
            must.append(models.FieldCondition(key=key, match=models.MatchValue(value=value)))
        return models.Filter(must=must)

    def search(
        self,
        vector: list[float],
        *,
        project_id: str,
        index_version: str,
        top_k: int,
        document_ids: list[str] | None = None,
    ) -> list[VectorHit]:
        if not self.client.collection_exists(self.collection):
            return []
        query_filter = self._filter(project_id, index_version)
        if document_ids:
            from qdrant_client import models

            query_filter = models.Filter(
                must=[
                    models.FieldCondition(key="project_id", match=models.MatchValue(value=project_id)),
                    models.FieldCondition(key="index_version", match=models.MatchValue(value=index_version)),
                    models.FieldCondition(key="document_id", match=models.MatchAny(any=document_ids)),
                ]
            )
        response = self.client.query_points(
            collection_name=self.collection,
            query=vector,
            query_filter=query_filter,
            limit=top_k,
            with_payload=True,
        )
        return [
            VectorHit(chunk_id=str(point.id), score=float(point.score or 0.0), payload=dict(point.payload or {}))
            for point in response.points
        ]

    def delete_document(self, *, project_id: str, document_id: str) -> None:
        if not self.client.collection_exists(self.collection):
            return
        from qdrant_client import models

        self.client.delete(
            collection_name=self.collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(key="project_id", match=models.MatchValue(value=project_id)),
                        models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id)),
                    ]
                )
            ),
            wait=True,
        )

    def delete_index_version(self, *, project_id: str, index_version: str) -> None:
        if not self.client.collection_exists(self.collection):
            return
        from qdrant_client import models

        self.client.delete(
            collection_name=self.collection,
            points_selector=models.FilterSelector(filter=self._filter(project_id, index_version)),
            wait=True,
        )

    def count(self, *, project_id: str, index_version: str) -> int:
        if not self.client.collection_exists(self.collection):
            return 0
        result = self.client.count(
            collection_name=self.collection,
            count_filter=self._filter(project_id, index_version),
            exact=True,
        )
        return int(result.count)

    def close(self) -> None:
        with contextlib.suppress(Exception):  # 关闭失败不影响退出
            self.client.close()


_index: VectorIndex | None = None


def build_vector_index(settings: Settings | None = None, *, force_new: bool = False) -> VectorIndex:
    global _index
    if _index is None or force_new:
        _index = VectorIndex(settings or get_settings())
    return _index
