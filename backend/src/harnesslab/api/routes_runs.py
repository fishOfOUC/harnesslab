"""会话、运行、事件流、审批与产物接口（文档 05 第 3、4、5、6 节）。"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..errors import ConflictError, HarnessLabError, NotFoundError, ValidationFailure
from ..observability.events import EventSink
from ..storage.models import BudgetLimits, EventType, RunStatus
from ..utils import hash_payload, iso
from .deps import IdentityDep, KnowledgeDep, RepoDep, SettingsDep, require_project, require_thread
from .sse import event_stream

router = APIRouter(tags=["runs"])


class ThreadCreate(BaseModel):
    title: str = ""


class RunLimits(BaseModel):
    max_model_calls: int | None = None
    max_tool_calls: int | None = None
    active_timeout_seconds: int | None = None


class KnowledgeScope(BaseModel):
    document_ids: list[str] = Field(default_factory=list)


class RunCreate(BaseModel):
    message: str = Field(min_length=1)
    mode: str = "research"
    model_profile_id: str = "local-chat"
    knowledge_scope: KnowledgeScope = Field(default_factory=KnowledgeScope)
    limits: RunLimits = Field(default_factory=RunLimits)


class DecisionPayload(BaseModel):
    decision: str
    expected_revision: int
    arguments_hash: str
    comment: str = ""


class RevisionPayload(BaseModel):
    arguments: dict[str, Any]


# --------------------------------------------------------------------- 会话
@router.post("/projects/{project_id}/threads", status_code=201)
def create_thread(
    project_id: str, payload: ThreadCreate, repo: RepoDep, identity: IdentityDep
) -> dict[str, Any]:
    require_project(repo, project_id, identity)
    return repo.create_thread(project_id, payload.title)


@router.get("/projects/{project_id}/threads")
def list_threads(project_id: str, repo: RepoDep, identity: IdentityDep) -> dict[str, Any]:
    require_project(repo, project_id, identity)
    return {"threads": repo.list_threads(project_id)}


@router.get("/threads/{thread_id}")
def get_thread(thread_id: str, repo: RepoDep, identity: IdentityDep) -> dict[str, Any]:
    thread = require_thread(repo, thread_id, identity)
    runs = repo.list_runs(thread_id=thread_id, limit=20)
    return {"thread": thread, "runs": runs}


# --------------------------------------------------------------------- 运行
@router.post("/threads/{thread_id}/runs", status_code=202)
def create_run(
    thread_id: str,
    payload: RunCreate,
    repo: RepoDep,
    settings: SettingsDep,
    identity: IdentityDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    thread = require_thread(repo, thread_id, identity)
    if payload.model_profile_id not in {"local-chat"}:
        raise ValidationFailure(f"未知 model_profile_id：{payload.model_profile_id}")
    limits = BudgetLimits(
        max_model_calls=settings.budget_max_model_calls,
        max_tool_calls=settings.budget_max_tool_calls,
        active_timeout_seconds=settings.budget_active_timeout_seconds,
        tool_timeout_seconds=settings.budget_tool_timeout_seconds,
        max_retries=settings.budget_max_retries,
        max_replans=settings.budget_max_replans,
        max_subtasks=settings.budget_max_subtasks,
        max_depth=settings.budget_max_depth,
        input_tokens=settings.budget_input_tokens,
        output_tokens=settings.budget_output_tokens,
    )
    # 调用者只能降低普通运行限制；提高上限需要策略管理权限
    overrides = {
        "max_model_calls": payload.limits.max_model_calls,
        "max_tool_calls": payload.limits.max_tool_calls,
        "active_timeout_seconds": payload.limits.active_timeout_seconds,
    }
    for field_name, value in overrides.items():
        if value is None:
            continue
        current = getattr(limits, field_name)
        if value > current and not identity.is_admin:
            raise HarnessLabError("FORBIDDEN", f"不允许提高 {field_name} 上限")
        setattr(limits, field_name, value)

    run, created = repo.create_run(
        thread_id=thread_id,
        project_id=thread["project_id"],
        user_message=payload.message,
        mode=payload.mode,
        config_snapshot={
            "knowledge_scope": payload.knowledge_scope.model_dump(),
            "model_profile_id": payload.model_profile_id,
            "requested_limits": payload.limits.model_dump(exclude_none=True),
            "request_hash": hash_payload(payload.model_dump()),
            "requested_at": iso(),
        },
        budget=limits,
        idempotency_key=idempotency_key,
    )
    if created:
        EventSink(repo).emit(
            run["id"],
            EventType.RUN_QUEUED,
            {"mode": run["mode"], "message": payload.message[:200], "limits": limits.model_dump()},
        )
    return {
        "run_id": run["id"],
        "status": run["status"],
        "created": created,
        "events_url": f"/api/v1/runs/{run['id']}/events",
    }


@router.get("/runs/{run_id}")
def get_run(run_id: str, repo: RepoDep, identity: IdentityDep) -> dict[str, Any]:
    run = repo.get_run(run_id)
    require_project(repo, run["project_id"], identity)
    from ..utils import json_loads

    pending = repo.pending_approval(run_id)
    return {
        "run": _serialize_run(run),
        "events_url": f"/api/v1/runs/{run_id}/events",
        "pending_approval": pending,
        "artifacts": repo.list_artifacts(run_id=run_id),
        "operations": repo.list_operations(run_id),
        "snapshot": json_loads(run["config_snapshot"], {}),
        "last_seq": repo.max_event_seq(run_id),
    }


@router.get("/runs/{run_id}/timeline")
def run_timeline(run_id: str, repo: RepoDep, identity: IdentityDep) -> dict[str, Any]:
    run = repo.get_run(run_id)
    require_project(repo, run["project_id"], identity)
    return {
        "events": repo.list_events(run_id),
        "operations": repo.list_operations(run_id),
        "approvals": repo.list_approvals(run_id),
        "artifacts": repo.list_artifacts(run_id=run_id),
    }


@router.get("/runs/{run_id}/events")
def run_events(
    run_id: str,
    request: Request,
    repo: RepoDep,
    settings: SettingsDep,
    identity: IdentityDep,
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    stream: Annotated[bool, Query()] = True,
):
    run = repo.get_run(run_id)
    require_project(repo, run["project_id"], identity)
    cursor = int(last_event_id) if last_event_id and last_event_id.isdigit() else after
    min_seq = repo.min_event_seq(run_id)
    if cursor and min_seq and cursor < min_seq - 1:
        return JSONResponse(
            status_code=410,
            content={
                "error": {
                    "code": "NOT_FOUND",
                    "message": "事件游标过旧，请用 GET /runs/{id} 重建后再订阅",
                    "retryable": False,
                    "details": {"min_seq": min_seq},
                }
            },
        )
    accepts = request.headers.get("accept", "")
    if not stream or "text/event-stream" not in accepts:
        return {"events": repo.list_events(run_id, cursor), "last_seq": repo.max_event_seq(run_id)}
    return StreamingResponse(
        event_stream(repo, settings, run_id, cursor),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/runs/{run_id}/cancel", status_code=202)
def cancel_run(run_id: str, repo: RepoDep, identity: IdentityDep) -> dict[str, Any]:
    run = repo.get_run(run_id)
    require_project(repo, run["project_id"], identity)
    updated = repo.request_cancel(run_id)
    EventSink(repo).emit(
        run_id,
        EventType.RUN_CANCELLED if updated["status"] == "cancelled" else EventType.RUN_QUEUED,
        {"reason": "user_cancel", "status": updated["status"]},
    )
    return {
        "run_id": run_id,
        "status": updated["status"],
        "note": "取消后保留已有答案、产物与已发生操作；已提交的副作用不会被声称撤回",
    }


@router.post("/runs/{run_id}/retry", status_code=202)
def retry_run(run_id: str, repo: RepoDep, identity: IdentityDep) -> dict[str, Any]:
    run = repo.get_run(run_id)
    require_project(repo, run["project_id"], identity)
    if not RunStatus(run["status"]).terminal:
        raise ConflictError("RUN_CONFLICT", "只有终态运行可以重跑")
    thread = repo.create_thread(run["project_id"], f"重跑：{run['user_message'][:30]}")
    new_run, _ = repo.create_run(
        thread_id=thread["id"],
        project_id=run["project_id"],
        user_message=run["user_message"],
        mode=run["mode"],
        config_snapshot={"retry_of": run_id},
        budget=BudgetLimits(**_load_limits(run)),
        parent_run_id=run_id,
    )
    EventSink(repo).emit(new_run["id"], EventType.RUN_QUEUED, {"parent_run_id": run_id})
    return {"run_id": new_run["id"], "parent_run_id": run_id, "status": new_run["status"],
            "note": "重跑创建新 run，不复活原终态"}


@router.post("/runs/{run_id}/fork", status_code=202)
def fork_run(run_id: str, repo: RepoDep, identity: IdentityDep) -> dict[str, Any]:
    run = repo.get_run(run_id)
    require_project(repo, run["project_id"], identity)
    thread = repo.create_thread(run["project_id"], f"分叉：{run['user_message'][:30]}")
    new_run, _ = repo.create_run(
        thread_id=thread["id"],
        project_id=run["project_id"],
        user_message=run["user_message"],
        mode=run["mode"],
        config_snapshot={"forked_from": run_id},
        budget=BudgetLimits(**_load_limits(run)),
        parent_run_id=run_id,
    )
    EventSink(repo).emit(new_run["id"], EventType.RUN_QUEUED, {"forked_from": run_id})
    return {
        "run_id": new_run["id"],
        "thread_id": thread["id"],
        "status": new_run["status"],
        "note": "分叉使用新分支标识；副作用需要在新的分支中重新审批",
    }


# --------------------------------------------------------------------- 审批
@router.get("/runs/{run_id}/approvals")
def list_approvals(run_id: str, repo: RepoDep, identity: IdentityDep) -> dict[str, Any]:
    run = repo.get_run(run_id)
    require_project(repo, run["project_id"], identity)
    return {"approvals": repo.list_approvals(run_id)}


@router.get("/projects/{project_id}/approvals")
def list_project_approvals(
    project_id: str,
    repo: RepoDep,
    identity: IdentityDep,
    status: Annotated[str | None, Query()] = "pending",
) -> dict[str, Any]:
    """审批中心：跨运行列出待审批项（文档 06 第 1 节页面需求）。"""
    require_project(repo, project_id, identity)
    sql = "SELECT * FROM approvals WHERE project_id=?"
    params: list[Any] = [project_id]
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT 100"
    rows = repo.db.query(sql, params)
    from ..utils import json_loads

    approvals = []
    for row in rows:
        item = dict(row)
        item["arguments"] = json_loads(item["arguments"], {})
        approvals.append(item)
    return {"approvals": approvals}


@router.post("/approvals/{approval_id}/decision")
def decide(
    approval_id: str,
    payload: DecisionPayload,
    repo: RepoDep,
    identity: IdentityDep,
) -> dict[str, Any]:
    if payload.decision not in {"approve", "reject"}:
        raise ValidationFailure("decision 只能是 approve 或 reject")
    approval = repo.get_approval(approval_id)
    require_project(repo, approval["project_id"], identity)
    updated = repo.decide_approval(
        approval_id,
        decision=payload.decision,
        expected_revision=payload.expected_revision,
        arguments_hash=payload.arguments_hash,
        reviewer_id=identity.user_id,
        comment=payload.comment,
    )
    run = repo.get_run(updated["run_id"])
    if RunStatus(run["status"]).terminal:
        raise ConflictError("APPROVAL_CONFLICT", "运行已结束，审批无效")

    if updated["kind"] == "manual_reconcile" and payload.decision == "reject":
        repo.update_run(
            run["id"],
            status=RunStatus.FAILED.value,
            error_code="SIDE_EFFECT_UNCERTAIN",
            error_message="人工核对后决定不继续：副作用结果不明，需要外部对账",
            finished_at=iso(),
        )
        EventSink(repo).emit(
            run["id"],
            EventType.RUN_FAILED,
            {"error_code": "SIDE_EFFECT_UNCERTAIN", "approval_id": approval_id},
        )
    else:
        resume_payload: dict[str, Any] = {
            "decision": payload.decision,
            "approval_id": updated["id"],
            "revision": updated["revision"],
            "arguments": updated["arguments"],
            "comment": payload.comment,
        }
        if updated["kind"] == "manual_reconcile":
            resume_payload = {"kind": "manual_reconcile", "decision": payload.decision,
                              "approval_id": updated["id"]}
        repo.update_run(
            run["id"],
            status=RunStatus.QUEUED.value,
            interrupt_payload=resume_payload,
            lease_owner=None,
            lease_expiry=None,
        )
    EventSink(repo).emit(
        run["id"],
        EventType.APPROVAL_DECIDED,
        {
            "approval_id": approval_id,
            "decision": payload.decision,
            "revision": updated["revision"],
            "reviewer_id": identity.user_id,
            "comment": payload.comment,
        },
    )
    return {
        "approval_id": approval_id,
        "status": updated["status"],
        "run_status": repo.get_run(run["id"])["status"],
        "note": "决策已保存；实际执行结果通过 run 事件查询",
    }


@router.post("/approvals/{approval_id}/revision", status_code=201)
def revise(approval_id: str, payload: RevisionPayload, repo: RepoDep, identity: IdentityDep) -> dict[str, Any]:
    approval = repo.get_approval(approval_id)
    require_project(repo, approval["project_id"], identity)
    revised = repo.revise_approval(approval_id, payload.arguments)
    EventSink(repo).emit(
        revised["run_id"],
        EventType.APPROVAL_REQUESTED,
        {
            "approval_id": revised["id"],
            "revision": revised["revision"],
            "tool_name": revised["tool_name"],
            "arguments": revised["arguments"],
            "arguments_hash": revised["arguments_hash"],
            "expected_effect": revised["expected_effect"],
            "expires_at": revised["expires_at"],
            "supersedes": approval_id,
        },
    )
    return revised


# --------------------------------------------------------------------- 产物与原文
@router.get("/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: str, repo: RepoDep, settings: SettingsDep, identity: IdentityDep):
    artifact = repo.get_artifact(artifact_id)
    require_project(repo, artifact["project_id"], identity)
    path = settings.workspace_root / artifact["project_id"] / artifact["run_id"] / artifact["relative_path"]
    root = (settings.workspace_root / artifact["project_id"] / artifact["run_id"]).resolve()
    if not path.resolve().is_relative_to(root) or not path.exists():
        raise NotFoundError("产物文件不存在或已被清理", {"artifact_id": artifact_id})
    # 非 ASCII 文件名按 RFC 5987 编码，避免响应头 latin-1 编码失败
    encoded_name = quote(artifact["title"], safe="")
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"}
    media_type = "application/octet-stream" if artifact["media_type"] == "text/html" else artifact["media_type"]
    return FileResponse(path, media_type=media_type, headers=headers)


@router.get("/sources/{chunk_id}")
def read_source(
    chunk_id: str, repo: RepoDep, knowledge: KnowledgeDep, identity: IdentityDep
) -> dict[str, Any]:
    chunk = repo.get_chunk(chunk_id)
    require_project(repo, chunk["project_id"], identity)
    return knowledge.read_source(chunk_id, project_id=chunk["project_id"])


@router.get("/artifacts")
def list_artifacts(
    repo: RepoDep,
    identity: IdentityDep,
    project_id: Annotated[str, Query()],
) -> dict[str, Any]:
    require_project(repo, project_id, identity)
    return {"artifacts": repo.list_artifacts(project_id=project_id)}


def _serialize_run(run: dict[str, Any]) -> dict[str, Any]:
    from ..utils import json_loads

    serialized = dict(run)
    serialized["config_snapshot"] = json_loads(run["config_snapshot"], {})
    serialized["budget_limits"] = json_loads(run["budget_limits"], {})
    serialized["budget_usage"] = json_loads(run["budget_usage"], {})
    serialized["result"] = json_loads(run["result"], None)
    serialized["interrupt_payload"] = json_loads(run["interrupt_payload"], None)
    return serialized


def _load_limits(run: dict[str, Any]) -> dict[str, Any]:
    from ..utils import json_loads

    return json_loads(run["budget_limits"], BudgetLimits().model_dump())
