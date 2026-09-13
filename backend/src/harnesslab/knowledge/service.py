"""知识库服务：导入流水线、检索、引用校验与删除（文档 04）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, get_settings
from ..errors import HarnessLabError, NotFoundError, ValidationFailure
from ..observability.events import EventSink
from ..observability.logging import get_logger
from ..storage.models import IndexStatus, ParseStatus
from ..storage.repositories import Repository
from ..utils import iso, sha256_bytes, sha256_text
from .chunker import Chunk, chunk_text
from .embeddings import EmbeddingService, build_embedding_service
from .index import VectorHit, VectorIndex, build_vector_index
from .loader import detect_media_type, parse_document
from .retrieval import get_bm25_index, reciprocal_rank_fusion

logger = get_logger("knowledge")


@dataclass
class RetrievedChunk:
    chunk: dict[str, Any]
    score: float
    vector_rank: int | None = None
    sparse_rank: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk["id"],
            "document_id": self.chunk["document_id"],
            "document_version": self.chunk["document_version"],
            "source_title": self.chunk["source_title"],
            "heading_path": self.chunk["heading_path"],
            "page": self.chunk.get("page"),
            "char_start": self.chunk.get("char_start"),
            "char_end": self.chunk.get("char_end"),
            "score": round(self.score, 6),
            "vector_rank": self.vector_rank,
            "sparse_rank": self.sparse_rank,
            "text": self.chunk["text"],
        }


@dataclass
class RetrievalResult:
    hits: list[RetrievedChunk] = field(default_factory=list)
    mode: str = "vector"
    index_version: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)
    insufficient_evidence: bool = False
    note: str = ""
    # 证据强度：本次召回中最强的向量（余弦）相似度。
    # 跨检索模式可比，用来判定「证据是否充分」；融合分（RRF）量级不同，不能用于阈值比较。
    evidence_score: float = 0.0


class KnowledgeService:
    def __init__(
        self,
        repo: Repository,
        settings: Settings | None = None,
        *,
        embeddings: EmbeddingService | None = None,
        index: VectorIndex | None = None,
        events: EventSink | None = None,
    ) -> None:
        self.repo = repo
        self.settings = settings or get_settings()
        self.embeddings = embeddings or build_embedding_service(self.settings)
        self.index = index or build_vector_index(self.settings)
        self.events = events or EventSink(repo)

    # ------------------------------------------------------------------ 索引清单
    def current_fingerprint(self) -> str:
        return self.embeddings.fingerprint()

    def ensure_active_manifest(self, project_id: str) -> dict[str, Any]:
        """确保存在活动索引；配置指纹不符时拒绝旧索引。"""
        fingerprint = self.current_fingerprint()
        active = self.repo.active_manifest(project_id)
        if active is not None:
            if active["model_fingerprint"] != fingerprint:
                raise HarnessLabError(
                    "INDEX_VERSION_MISMATCH",
                    "活动索引与当前 Embedding 配置指纹不一致，请执行 index rebuild 后切换",
                    details={"index_version": active["id"]},
                )
            self.index.ensure_collection(active["dimension"])
            return active
        probe = self.embeddings.probe()
        dimension = int(probe["dimension"])
        self.index.ensure_collection(dimension)
        manifest = self.repo.create_manifest(
            project_id=project_id,
            model_fingerprint=fingerprint,
            dimension=dimension,
            pipeline_version=self.settings.pipeline_version,
            config={
                "embedding_provider": self.settings.embedding_provider,
                "embedding_model": self.settings.embedding_model,
                "query_prefix": self.settings.embedding_query_prefix,
                "document_prefix": self.settings.embedding_document_prefix,
                "normalize": self.settings.embedding_normalize,
                "chunk_target_tokens": self.settings.chunk_target_tokens,
                "chunk_overlap_tokens": self.settings.chunk_overlap_tokens,
                "retrieval_mode": self.settings.retrieval_mode,
            },
        )
        return self.repo.activate_manifest(manifest["id"])

    def index_status(self, project_id: str) -> dict[str, Any]:
        active = self.repo.active_manifest(project_id)
        documents = self.repo.list_documents(project_id)
        not_ready = [
            {"document_id": d["id"], "title": d["title"], "status": v["parse_status"]}
            for d in documents
            for v in d.get("versions", [])[:1]
            if v["parse_status"] not in {ParseStatus.READY.value, ParseStatus.DELETED.value}
        ]
        return {
            "active_index_version": active["id"] if active else None,
            "dimension": active["dimension"] if active else None,
            "chunk_count": self.repo.count_chunks(project_id),
            "document_count": len(documents),
            "pending_documents": not_ready,
        }

    # ------------------------------------------------------------------ 导入
    def import_document(self, project_id: str, filename: str, data: bytes) -> dict[str, Any]:
        """上传 → 文件检查 → 解析 → 规范化 → 切分 → 向量化 → 写索引 → 可见性提交。"""
        if len(data) > self.settings.max_upload_bytes:
            raise ValidationFailure(
                f"文件超过 {self.settings.max_upload_bytes // (1024 * 1024)} MB 上限",
                {"size": len(data)},
            )
        content_hash = sha256_bytes(data)
        existing = self.repo.find_version_by_hash(project_id, content_hash)
        if existing is not None:
            document = self.repo.get_document(existing["document_id"])
            return {
                "document_id": document["id"],
                "version": existing["version"],
                "reused": True,
                "parse_status": existing["parse_status"],
                "chunk_count": existing["chunk_count"],
                "message": "相同内容的版本已存在，复用已完成结果",
            }

        media_type = detect_media_type(data, filename)
        document = self.repo.create_document(
            project_id=project_id, title=filename, source_path=filename, media_type=media_type
        )
        version = self.repo.next_document_version(document["id"])
        parsed = parse_document(data, filename, max_pages=self.settings.max_pdf_pages)
        self.repo.create_version(
            document_id=document["id"],
            project_id=project_id,
            version=version,
            content_hash=content_hash,
            parse_status=ParseStatus.PARSING,
            media_type=media_type,
            char_count=parsed.char_count,
        )
        if not parsed.ok:
            status = ParseStatus.UNSUPPORTED if parsed.unsupported_reason else ParseStatus.FAILED
            self.repo.update_version(
                document["id"], version, parse_status=status, error_code="UNSUPPORTED_FORMAT",
                error_message=parsed.unsupported_reason or "解析结果为空",
            )
            self.repo.set_active_version(document["id"], None)
            return {
                "document_id": document["id"],
                "version": version,
                "parse_status": status.value,
                "reused": False,
                "error_code": "UNSUPPORTED_FORMAT",
                "message": parsed.unsupported_reason or "解析失败",
            }

        manifest = self.ensure_active_manifest(project_id)
        chunks = chunk_text(
            parsed.segments,
            source_title=document["title"],
            target_tokens=self.settings.chunk_target_tokens,
            overlap_tokens=self.settings.chunk_overlap_tokens,
        )
        if not chunks:
            self.repo.update_version(
                document["id"], version, parse_status=ParseStatus.FAILED, error_code="EMPTY_CONTENT",
                error_message="切分后没有可用内容",
            )
            return {
                "document_id": document["id"],
                "version": version,
                "parse_status": ParseStatus.FAILED.value,
                "reused": False,
                "error_code": "EMPTY_CONTENT",
                "message": "切分后没有可用内容",
            }

        self.repo.update_version(document["id"], version, parse_status=ParseStatus.STAGING)
        rows = self._stage_chunks(
            chunks, project_id=project_id, document_id=document["id"], document_version=version,
            index_version=manifest["id"], embeddings=self.embeddings,
        )
        try:
            self._embed_and_upsert(
                rows, project_id=project_id, document_id=document["id"], document_version=version,
                index_version=manifest["id"],
            )
        except HarnessLabError as exc:
            self.repo.update_version(
                document["id"], version, parse_status=ParseStatus.FAILED, error_code=exc.code,
                error_message=exc.message,
            )
            self.repo.delete_chunks(project_id, document_ids=[document["id"]],
                                    index_version=manifest["id"])
            raise
        self.repo.update_version(
            document["id"], version, parse_status=ParseStatus.READY, index_version=manifest["id"],
            chunk_count=len(rows),
        )
        self.repo.set_active_version(document["id"], version)
        return {
            "document_id": document["id"],
            "version": version,
            "parse_status": ParseStatus.READY.value,
            "reused": False,
            "chunk_count": len(rows),
            "index_version": manifest["id"],
            "media_type": media_type,
            "char_count": parsed.char_count,
            "warnings": [*parsed.warnings, "token 数量为估算值"],
        }

    def _stage_chunks(
        self,
        chunks: list[Chunk],
        *,
        project_id: str,
        document_id: str,
        document_version: int,
        index_version: str,
        embeddings: EmbeddingService,
    ) -> list[dict[str, Any]]:
        now = iso()
        rows = [
            chunk.as_row(
                project_id=project_id, document_id=document_id, document_version=document_version,
                index_version=index_version, created_at=now,
            )
            for chunk in chunks
        ]
        self.repo.insert_chunks(rows)  # 暂存：版本未 ready 前查询不可见
        return rows

    def _embed_and_upsert(
        self, rows: list[dict[str, Any]], *, project_id: str, document_id: str,
        document_version: int, index_version: str,
    ) -> None:
        texts = [row["text"] for row in rows]
        vectors = self.embeddings.embed_documents(texts)
        points = [
            (
                row["id"],
                vector,
                {
                    "project_id": project_id,
                    "index_version": index_version,
                    "document_id": document_id,
                    "document_version": document_version,
                    "source_title": row["source_title"],
                    "heading_path": row["heading_path"],
                    "page": row["page"],
                    "content_hash": row["content_hash"],
                },
            )
            for row, vector in zip(rows, vectors, strict=True)
        ]
        self.index.upsert(points)

    # ------------------------------------------------------------------ 检索
    def search(
        self,
        project_id: str,
        query: str,
        *,
        top_k: int | None = None,
        document_ids: list[str] | None = None,
        index_version: str | None = None,
        mode: str | None = None,
    ) -> RetrievalResult:
        if not query.strip():
            raise ValidationFailure("检索查询不能为空")
        manifest = self._manifest_for(project_id, index_version)
        top_k = top_k or self.settings.retrieval_top_k
        mode = mode or self.settings.retrieval_mode
        index_version_value = manifest["id"]

        vector_hits: list[VectorHit] = []
        query_vector = self.embeddings.embed_query(query)
        vector_hits = self.index.search(
            query_vector,
            project_id=project_id,
            index_version=index_version_value,
            top_k=max(self.settings.retrieval_candidates, top_k),
            document_ids=document_ids,
        )

        vector_ranking = [hit.chunk_id for hit in vector_hits]
        sparse_ranking: list[str] = []
        if mode == "hybrid":
            chunks = self.repo.list_chunks(project_id, index_version_value, document_ids)
            bm25 = get_bm25_index()
            bm25.build(chunks, project_id=project_id, index_version=index_version_value)
            sparse_ranking = [hit.chunk_id for hit in bm25.search(query, self.settings.retrieval_candidates)]

        if mode == "hybrid":
            fused = reciprocal_rank_fusion(
                [vector_ranking, sparse_ranking], k=self.settings.rrf_constant, weights=[1.0, 0.8]
            )
            ordered_ids = [chunk_id for chunk_id, _ in fused]
            scores = dict(fused)
        else:
            ordered_ids = vector_ranking
            scores = {hit.chunk_id: hit.score for hit in vector_hits}

        selected = ordered_ids[:top_k]
        chunk_rows = {row["id"]: row for row in self.repo.chunks_by_ids(selected)}
        # 再次校验项目归属与文档可用性（删除后不能被引用绕过）
        hits: list[RetrievedChunk] = []
        for chunk_id in selected:
            row = chunk_rows.get(chunk_id)
            if row is None or row["project_id"] != project_id:
                continue
            document = self.repo.get_document(row["document_id"])
            if document["deleted_at"]:
                continue
            hits.append(
                RetrievedChunk(
                    chunk=row,
                    score=float(scores.get(chunk_id, 0.0)),
                    vector_rank=vector_ranking.index(chunk_id) + 1 if chunk_id in vector_ranking else None,
                    sparse_rank=sparse_ranking.index(chunk_id) + 1 if chunk_id in sparse_ranking else None,
                )
            )

        # 证据强度按向量相似度取值（RRF 融合分量级不同，不能直接套用余弦阈值）
        vector_scores = {hit.chunk_id: hit.score for hit in vector_hits}
        evidence_score = max(vector_scores.values(), default=0.0)
        insufficient = not hits or evidence_score < self.settings.min_evidence_score
        return RetrievalResult(
            hits=hits,
            mode=mode,
            index_version=index_version_value,
            evidence_score=evidence_score,
            candidates=(
                [hit.to_dict() for hit in hits]
                if mode == "vector"
                else [
                    {
                        "chunk_id": chunk_id,
                        "score": round(score, 6),
                        "vector_rank": vector_ranking.index(chunk_id) + 1 if chunk_id in vector_ranking else None,
                        "sparse_rank": sparse_ranking.index(chunk_id) + 1 if chunk_id in sparse_ranking else None,
                        "source_title": chunk_rows[chunk_id]["source_title"] if chunk_id in chunk_rows else "",
                    }
                    for chunk_id, score in list(scores.items())[: self.settings.retrieval_candidates]
                ]
            ),
            insufficient_evidence=insufficient,
            note=(
                "未检索到足够证据，请说明无法从现有资料确认"
                if insufficient
                else "检索完成；相关度阈值需用评测集校准，不跨模型复用"
            ),
        )

    def _manifest_for(self, project_id: str, index_version: str | None) -> dict[str, Any]:
        if index_version:
            manifest = self.repo.get_manifest(index_version)
            if manifest["project_id"] != project_id:
                raise HarnessLabError("FORBIDDEN", "索引不属于当前项目")
            return manifest
        manifest = self.repo.active_manifest(project_id)
        if manifest is None:
            raise HarnessLabError("INDEX_VERSION_MISMATCH", "当前项目还没有可用的索引，请先导入资料")
        if manifest["status"] != IndexStatus.ACTIVE.value:
            raise HarnessLabError("INDEX_VERSION_MISMATCH", "活动索引不可用")
        return manifest

    # ------------------------------------------------------------------ 原文与删除
    def read_source(self, chunk_id: str, *, project_id: str | None = None) -> dict[str, Any]:
        chunk = self.repo.get_chunk(chunk_id)
        if project_id and chunk["project_id"] != project_id:
            raise HarnessLabError("FORBIDDEN", "片段不属于当前项目")
        document = self.repo.get_document(chunk["document_id"])
        if document["deleted_at"]:
            raise NotFoundError("该资料已删除，原文不可访问", {"document_id": document["id"]})
        return {
            "chunk_id": chunk["id"],
            "document_id": chunk["document_id"],
            "document_version": chunk["document_version"],
            "source_title": chunk["source_title"],
            "heading_path": chunk["heading_path"],
            "page": chunk.get("page"),
            "char_start": chunk.get("char_start"),
            "char_end": chunk.get("char_end"),
            "text": chunk["text"],
            "content_hash": chunk["content_hash"],
        }

    def resolve_citations(self, labels: list[str], hits: list[RetrievedChunk]) -> tuple[list[dict[str, Any]],
                                                                                        list[str]]:
        """把答案里的 [S1] 标签映射到本次授权召回的 source；无法解析的标签记为无效。"""
        mapping = {f"S{index}": hit for index, hit in enumerate(hits, start=1)}
        resolved: list[dict[str, Any]] = []
        invalid: list[str] = []
        for label in labels:
            hit = mapping.get(label)
            if hit is None:
                invalid.append(label)
                continue
            resolved.append(
                {
                    "label": label,
                    "chunk_id": hit.chunk["id"],
                    "document_id": hit.chunk["document_id"],
                    "document_version": hit.chunk["document_version"],
                    "source_title": hit.chunk["source_title"],
                    "heading_path": hit.chunk["heading_path"],
                    "page": hit.chunk.get("page"),
                    "char_start": hit.chunk.get("char_start"),
                    "char_end": hit.chunk.get("char_end"),
                    "snippet": hit.chunk["text"][:400],
                    "score": round(hit.score, 6),
                }
            )
        return resolved, invalid

    def delete_document(self, project_id: str, document_id: str) -> dict[str, Any]:
        document = self.repo.get_document(document_id)
        if document["project_id"] != project_id:
            raise HarnessLabError("FORBIDDEN", "文档不属于当前项目")
        self.repo.delete_document(document_id)
        self.index.delete_document(project_id=project_id, document_id=document_id)
        return {"document_id": document_id, "status": "deleted"}

    def rebuild_index(self, project_id: str) -> dict[str, Any]:
        """暂存重建 → 完整性与抽样验证 → 原子切换活动索引 → 保留旧索引。"""
        old = self.repo.active_manifest(project_id)
        probe = self.embeddings.probe()
        dimension = int(probe["dimension"])
        self.index.ensure_collection(dimension)
        manifest = self.repo.create_manifest(
            project_id=project_id,
            model_fingerprint=self.current_fingerprint(),
            dimension=dimension,
            pipeline_version=self.settings.pipeline_version,
            config={"rebuild": True, "previous": old["id"] if old else None},
        )
        documents = [d for d in self.repo.list_documents(project_id) if d["active_version"]]
        total = 0
        for document in documents:
            version = document["active_version"]
            version_row = self.repo.get_version(document["id"], version)
            chunks = self.repo.list_chunks(project_id, version_row["index_version"] or manifest["id"],
                                           document_ids=[document["id"]])
            if not chunks:
                continue
            vectors = self.embeddings.embed_documents([c["text"] for c in chunks])
            points = [
                (
                    chunk["id"],
                    vector,
                    {
                        "project_id": project_id,
                        "index_version": manifest["id"],
                        "document_id": chunk["document_id"],
                        "document_version": chunk["document_version"],
                        "source_title": chunk["source_title"],
                        "heading_path": chunk["heading_path"],
                        "page": chunk["page"],
                        "content_hash": chunk["content_hash"],
                    },
                )
                for chunk, vector in zip(chunks, vectors, strict=True)
            ]
            self.index.upsert(points)
            for chunk in chunks:
                chunk["index_version"] = manifest["id"]
            self.repo.insert_chunks(
                [
                    {
                        **chunk,
                        "index_version": manifest["id"],
                    }
                    for chunk in chunks
                ]
            )
            self.repo.update_version(document["id"], version, index_version=manifest["id"])
            total += len(chunks)
        self.repo.activate_manifest(manifest["id"])
        return {
            "index_version": manifest["id"],
            "previous_index_version": old["id"] if old else None,
            "chunks": total,
            "documents": len(documents),
            "note": "旧索引保留用于回滚；活跃 run 固定其索引版本",
        }

    def staging_health(self, project_id: str, index_version: str) -> dict[str, Any]:
        indexed = self.index.count(project_id=project_id, index_version=index_version)
        stored = len(self.repo.list_chunks(project_id, index_version, limit=100000))
        return {
            "vector_points": indexed,
            "chunk_rows": stored,
            "consistent": indexed == stored,
        }


def hash_query(text: str) -> str:
    return sha256_text(text)
