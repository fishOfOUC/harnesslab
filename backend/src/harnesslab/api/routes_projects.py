"""项目、知识库、后台任务与记忆接口（文档 05 第 3 节）。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Query, UploadFile
from pydantic import BaseModel, Field

from ..errors import ValidationFailure
from ..runtime.worker import JOB_DOCUMENT_IMPORT, JOB_EVAL, JOB_INDEX_REBUILD
from ..storage.models import MemoryStatus
from ..utils import new_id
from .deps import IdentityDep, KnowledgeDep, RepoDep, SettingsDep, require_project

router = APIRouter(tags=["projects"])


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class RetrievalPreview(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = 8
    mode: str | None = None
    document_ids: list[str] = Field(default_factory=list)


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1)
    status: MemoryStatus = MemoryStatus.CANDIDATE
    source_ref: str = ""


class MemoryUpdate(BaseModel):
    content: str | None = None
    status: MemoryStatus | None = None
    revision: int


# --------------------------------------------------------------------- 项目
@router.post("/projects", status_code=201)
def create_project(payload: ProjectCreate, repo: RepoDep, identity: IdentityDep) -> dict[str, Any]:
    return repo.create_project(payload.name, identity.user_id)


@router.get("/projects")
def list_projects(repo: RepoDep) -> dict[str, Any]:
    return {"projects": repo.list_projects()}


# --------------------------------------------------------------------- 文档
@router.post("/projects/{project_id}/documents", status_code=202)
def upload_document(
    project_id: str,
    repo: RepoDep,
    settings: SettingsDep,
    identity: IdentityDep,
    file: Annotated[UploadFile, File()],
    title: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    require_project(repo, project_id, identity)
    data = file.file.read()
    if not data:
        raise ValidationFailure("上传文件为空")
    if len(data) > settings.max_upload_bytes:
        raise ValidationFailure(
            f"文件超过 {settings.max_upload_bytes // (1024 * 1024)} MB 上限", {"size": len(data)}
        )
    filename = title or file.filename or "upload"
    stored = settings.upload_root / f"{new_id()}-{filename}"
    stored.write_bytes(data)
    job = repo.create_job(
        project_id=project_id,
        kind=JOB_DOCUMENT_IMPORT,
        payload={"stored_path": str(stored), "filename": filename, "size": len(data)},
    )
    return {
        "job_id": job["id"],
        "job_url": f"/api/v1/jobs/{job['id']}",
        "status": job["status"],
        "note": "解析、切分与向量化由工作进程执行；全部 chunk 完成后文档版本才可见",
    }


@router.get("/projects/{project_id}/documents")
def list_documents(
    project_id: str, repo: RepoDep, knowledge: KnowledgeDep, identity: IdentityDep
) -> dict[str, Any]:
    require_project(repo, project_id, identity)
    return {
        "documents": repo.list_documents(project_id),
        "index": knowledge.index_status(project_id),
    }


@router.delete("/projects/{project_id}/documents/{document_id}", status_code=202)
def delete_document(
    project_id: str,
    document_id: str,
    repo: RepoDep,
    knowledge: KnowledgeDep,
    identity: IdentityDep,
) -> dict[str, Any]:
    require_project(repo, project_id, identity)
    return knowledge.delete_document(project_id, document_id)


@router.post("/projects/{project_id}/retrieval/preview")
def retrieval_preview(
    project_id: str,
    payload: RetrievalPreview,
    repo: RepoDep,
    knowledge: KnowledgeDep,
    identity: IdentityDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """检索调试：返回各阶段候选。需要 API 与工作进程共用同一向量库实例。"""
    require_project(repo, project_id, identity)
    result = knowledge.search(
        project_id,
        payload.query,
        top_k=payload.top_k,
        document_ids=payload.document_ids or None,
        mode=payload.mode,
    )
    return {
        "mode": result.mode,
        "index_version": result.index_version,
        "evidence_score": round(result.evidence_score, 4),
        "evidence_score_kind": "vector_cosine_max",
        "min_evidence_score": settings.min_evidence_score,
        "insufficient_evidence": result.insufficient_evidence,
        "note": result.note,
        "candidates": result.candidates,
        "hits": [hit.to_dict() for hit in result.hits],
    }


@router.post("/projects/{project_id}/index/rebuild", status_code=202)
def rebuild_index(project_id: str, repo: RepoDep, identity: IdentityDep) -> dict[str, Any]:
    require_project(repo, project_id, identity)
    job = repo.create_job(project_id=project_id, kind=JOB_INDEX_REBUILD, payload={})
    return {
        "job_id": job["id"],
        "status": job["status"],
        "note": "重建使用新清单暂存，验证后原子切换，旧索引保留用于回滚",
    }


@router.get("/jobs/{job_id}")
def get_job(job_id: str, repo: RepoDep, identity: IdentityDep) -> dict[str, Any]:
    job = repo.get_job(job_id)
    require_project(repo, job["project_id"], identity)
    return job


# --------------------------------------------------------------------- 记忆
@router.get("/projects/{project_id}/memories")
def list_memories(
    project_id: str,
    repo: RepoDep,
    identity: IdentityDep,
    statuses: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    require_project(repo, project_id, identity)
    status_list = [item for item in (statuses or "").split(",") if item]
    return {
        "memories": repo.list_memories(project_id, identity.user_id, status_list or None),
        "note": "长期记忆默认需用户确认后生效；敏感凭证禁止写入",
    }


@router.post("/projects/{project_id}/memories", status_code=201)
def create_memory(
    project_id: str, payload: MemoryCreate, repo: RepoDep, identity: IdentityDep
) -> dict[str, Any]:
    require_project(repo, project_id, identity)
    return repo.create_memory(
        project_id=project_id,
        user_id=identity.user_id,
        content=payload.content,
        status=payload.status.value,
        source_ref=payload.source_ref,
    )


@router.patch("/memories/{memory_id}")
def update_memory(
    memory_id: str, payload: MemoryUpdate, repo: RepoDep, identity: IdentityDep
) -> dict[str, Any]:
    memory = repo.get_memory(memory_id)
    require_project(repo, memory["project_id"], identity)
    return repo.update_memory(
        memory_id,
        content=payload.content,
        status=payload.status.value if payload.status else None,
        revision=payload.revision,
    )


@router.delete("/memories/{memory_id}", status_code=202)
def delete_memory(memory_id: str, repo: RepoDep, identity: IdentityDep) -> dict[str, Any]:
    memory = repo.get_memory(memory_id)
    require_project(repo, memory["project_id"], identity)
    repo.delete_memory(memory_id)
    return {
        "memory_id": memory_id,
        "status": "deleted",
        "note": "已立即阻断读取；派生索引与后台摘要写回随后清理",
    }


class EvalRequest(BaseModel):
    mode: str | None = None
    dataset: str | None = None


@router.post("/projects/{project_id}/eval-runs", status_code=202)
def create_eval_run(
    project_id: str, payload: EvalRequest, repo: RepoDep, identity: IdentityDep
) -> dict[str, Any]:
    require_project(repo, project_id, identity)
    job = repo.create_job(
        project_id=project_id,
        kind=JOB_EVAL,
        payload={"mode": payload.mode, "dataset": payload.dataset},
    )
    return {
        "eval_id": job["id"],
        "status": job["status"],
        "note": "检索质量评测由工作进程执行；生成质量与任务成功率需真实模型端到端运行",
    }


@router.get("/eval-runs/{eval_id}")
def get_eval_run(eval_id: str, repo: RepoDep, identity: IdentityDep) -> dict[str, Any]:
    job = repo.get_job(eval_id)
    require_project(repo, job["project_id"], identity)
    if job["kind"] != JOB_EVAL:
        raise ValidationFailure("该 ID 不是评测任务")
    report = job["result"] or {}
    return {
        "eval_id": job["id"],
        "status": job["status"],
        "error_code": job["error_code"],
        "metrics": report.get("metrics"),
        "environment": report.get("environment"),
        "case_results": report.get("case_results", []),
        "dataset_version": report.get("dataset_version"),
        "created_at": job["created_at"],
    }


# --------------------------------------------------------------------- 演示数据
@router.post("/projects/{project_id}/demo/seed", status_code=202)
def seed_demo(
    project_id: str, repo: RepoDep, settings: SettingsDep, identity: IdentityDep
) -> dict[str, Any]:
    """幂等创建合成演示资料（datasets/demo）。"""
    from ..demo import enqueue_demo_seed

    require_project(repo, project_id, identity)
    return enqueue_demo_seed(repo, settings, project_id)


@router.get("/projects/{project_id}/demo/tickets")
def list_demo_tickets(
    project_id: str, repo: RepoDep, identity: IdentityDep
) -> dict[str, Any]:
    """演示用：列出本项目模拟工单，用于验证“只有一份工单”。"""
    require_project(repo, project_id, identity)
    rows = repo.db.query(
        "SELECT * FROM demo_tickets WHERE project_id=? ORDER BY created_at", (project_id,)
    )
    return {"tickets": [dict(row) for row in rows]}
