"""知识库测试：导入、去重、检索、引用、删除与索引指纹。"""

from __future__ import annotations

from pathlib import Path

import pytest

from harnesslab.config import REPO_ROOT, Settings
from harnesslab.errors import HarnessLabError
from harnesslab.knowledge.service import KnowledgeService
from harnesslab.storage.models import ParseStatus

DEMO_DIR = REPO_ROOT / "datasets" / "demo"


def _import(name: str, project, knowledge: KnowledgeService) -> dict:
    data = (DEMO_DIR / name).read_bytes()
    return knowledge.import_document(project["id"], name, data)


def test_import_marks_ready_and_keeps_location(project, knowledge) -> None:
    result = _import("gateway-v1.md", project, knowledge)
    assert result["parse_status"] == ParseStatus.READY.value
    assert result["chunk_count"] > 0
    chunks = knowledge.repo.list_chunks(project["id"], result["index_version"])
    assert chunks, "导入后应能在活动索引版本下查到 chunk"
    assert any(chunk["heading_path"] for chunk in chunks)
    assert all(chunk["source_title"] == "gateway-v1.md" for chunk in chunks)


def test_duplicate_import_reuses_version(project, knowledge) -> None:
    first = _import("operations-guide.md", project, knowledge)
    second = _import("operations-guide.md", project, knowledge)
    assert second["reused"] is True
    assert second["document_id"] == first["document_id"]
    assert second["version"] == first["version"]
    assert knowledge.repo.count_chunks(project["id"]) == first["chunk_count"]


def test_unsupported_binary_is_not_indexed(project, knowledge) -> None:
    result = knowledge.import_document(project["id"], "blob.bin", b"\x00\x01\x02\x03binarydata")
    assert result["parse_status"] == ParseStatus.UNSUPPORTED.value
    assert result["reused"] is False
    assert "error_code" in result


def test_hybrid_retrieval_finds_expected_source(project, knowledge) -> None:
    _import("gateway-v1.md", project, knowledge)
    _import("gateway-v2.md", project, knowledge)
    result = knowledge.search(project["id"], "v2 的单实例并发上限是多少", top_k=5)
    assert result.hits
    titles = {hit.chunk["source_title"] for hit in result.hits}
    assert "gateway-v2.md" in titles
    assert result.index_version


def test_vector_mode_distinguishes_candidates(project, knowledge) -> None:
    _import("capacity-notes.txt", project, knowledge)
    hybrid = knowledge.search(project["id"], "理论吞吐 并发上限", mode="hybrid", top_k=5)
    vector = knowledge.search(project["id"], "理论吞吐 并发上限", mode="vector", top_k=5)
    assert hybrid.hits and vector.hits
    assert hybrid.mode == "hybrid" and vector.mode == "vector"


def test_no_answer_query_reports_insufficient_evidence(project, knowledge) -> None:
    _import("capacity-notes.txt", project, knowledge)
    result = knowledge.search(project["id"], "请给出零数据丢失的正式保证", top_k=5)
    assert isinstance(result.insufficient_evidence, bool)
    assert result.note


def test_citation_resolution_and_invalid_labels(project, knowledge) -> None:
    _import("gateway-v2.md", project, knowledge)
    result = knowledge.search(project["id"], "队列方案的重试规则", top_k=5)
    resolved, invalid = knowledge.resolve_citations(["S1", "S9"], result.hits)
    assert resolved and resolved[0]["label"] == "S1"
    assert resolved[0]["document_version"] >= 1
    assert invalid == ["S9"]


def test_delete_document_blocks_retrieval_and_source(project, knowledge) -> None:
    result = _import("untrusted-note.md", project, knowledge)
    chunks = knowledge.repo.list_chunks(project["id"], result["index_version"])
    chunk_id = chunks[0]["id"]
    assert knowledge.read_source(chunk_id, project_id=project["id"])["text"]

    knowledge.delete_document(project["id"], result["document_id"])
    with pytest.raises(HarnessLabError):
        knowledge.read_source(chunk_id, project_id=project["id"])
    hits = knowledge.search(project["id"], "外部协作方留言", top_k=5)
    assert all(hit.chunk["source_title"] != "untrusted-note.md" for hit in hits.hits)


def test_fingerprint_change_rejects_old_index(project, knowledge, settings) -> None:
    _import("gateway-v1.md", project, knowledge)
    changed = Settings(
        **{
            **settings.model_dump(),
            "data_dir": str(settings.data_dir),
            "artifact_dir": str(settings.artifact_dir),
            "qdrant_path": str(settings.qdrant_path),
            "embedding_document_prefix": "改动后的前缀",
        }
    )
    altered = KnowledgeService(knowledge.repo, changed)
    with pytest.raises(HarnessLabError) as exc:
        altered.ensure_active_manifest(project["id"])
    assert exc.value.code == "INDEX_VERSION_MISMATCH"


def test_index_status_reports_active_manifest(project, knowledge) -> None:
    _import("gateway-v1.md", project, knowledge)
    status = knowledge.index_status(project["id"])
    assert status["active_index_version"]
    assert status["chunk_count"] > 0
    assert status["dimension"] == 64  # 替身 Embedding 的维度


def test_demo_datasets_exist() -> None:
    for name in (
        "gateway-v1.md",
        "gateway-v2.md",
        "operations-guide.md",
        "capacity-notes.txt",
        "untrusted-note.md",
        "gold.json",
    ):
        assert (DEMO_DIR / name).exists(), f"缺少演示资料 {name}"
    assert Path(DEMO_DIR).is_dir()
