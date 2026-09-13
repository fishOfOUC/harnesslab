"""API 契约测试：上传导入、检索、运行、SSE、审批、产物下载与记忆修订。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harnesslab.api.app import create_app
from harnesslab.config import REPO_ROOT
from harnesslab.runtime.worker import Worker
from harnesslab.storage.models import BudgetLimits, RunStatus

DEMO_DIR = REPO_ROOT / "datasets" / "demo"
PREFIX = "/api/v1"


@pytest.fixture()
def client(settings):
    app = create_app(settings, with_worker=False)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def api_project(client) -> str:
    response = client.post(f"{PREFIX}/projects", json={"name": "API 测试项目"})
    assert response.status_code == 201
    return response.json()["id"]


def _import_via_api(client, repo, settings, project_id: str, filename: str) -> str:
    data = (DEMO_DIR / filename).read_bytes()
    response = client.post(
        f"{PREFIX}/projects/{project_id}/documents",
        files={"file": (filename, data, "text/markdown" if filename.endswith(".md") else "text/plain")},
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    worker = Worker(repo, settings)
    job = repo.get_job(job_id)
    assert repo.claim_job(job["id"])
    worker._handle_job_sync(job)
    assert repo.get_job(job_id)["status"] == "succeeded"
    return job_id


# --------------------------------------------------------------------- 健康
def test_health_endpoints(client) -> None:
    assert client.get(f"{PREFIX}/health/live").json()["status"] == "alive"
    ready = client.get(f"{PREFIX}/health/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"]["business_db"]["ok"] is True


def test_model_profiles_are_redacted(client) -> None:
    body = client.get(f"{PREFIX}/model-profiles").json()
    assert len(body["profiles"]) == 2
    dumped = str(body)
    assert "api_key_configured" in dumped
    assert "replace-with-real-chat-model-id" in dumped or "stub" in dumped


def test_policy_snapshot_lists_risks(client) -> None:
    policies = {item["name"]: item for item in client.get(f"{PREFIX}/policy").json()["policies"]}
    assert policies["search_knowledge"]["decision"] == "allow"
    assert policies["create_demo_ticket"]["decision"] == "require_approval"
    assert policies["run_python_sandbox"]["decision"] == "deny"


# --------------------------------------------------------------------- 知识与任务
def test_upload_import_and_preview(client, repo, settings, api_project) -> None:
    _import_via_api(client, repo, settings, api_project, "gateway-v1.md")
    _import_via_api(client, repo, settings, api_project, "gateway-v2.md")

    documents = client.get(f"{PREFIX}/projects/{api_project}/documents").json()
    assert documents["index"]["chunk_count"] > 0
    assert len(documents["documents"]) == 2
    assert documents["documents"][0]["active_version"] == 1

    preview = client.post(
        f"{PREFIX}/projects/{api_project}/retrieval/preview",
        json={"query": "v2 的并发上限", "top_k": 5},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["hits"] and body["index_version"]
    assert all(hit["document_version"] == 1 for hit in body["hits"])


def test_demo_seed_is_idempotent(client, repo, settings, api_project) -> None:
    first = client.post(f"{PREFIX}/projects/{api_project}/demo/seed")
    assert first.status_code == 202
    for item in first.json()["queued"]:
        worker = Worker(repo, settings)
        job = repo.get_job(item["job_id"])
        repo.claim_job(job["id"])
        worker._handle_job_sync(job)
    second = client.post(f"{PREFIX}/projects/{api_project}/demo/seed")
    assert second.status_code == 202
    assert second.json()["queued"] == []
    assert len(second.json()["already_indexed"]) >= 5


def test_job_not_found(client) -> None:
    response = client.get(f"{PREFIX}/jobs/00000000-0000-4000-8000-000000000000")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_upload_rejects_empty_file(client, api_project) -> None:
    response = client.post(
        f"{PREFIX}/projects/{api_project}/documents",
        files={"file": ("empty.md", b"", "text/markdown")},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# --------------------------------------------------------------------- 运行
def _create_run(client, thread_id: str, message: str, key: str | None = None, **limits) -> dict:
    headers = {"Idempotency-Key": key} if key else {}
    body = {"message": message, "mode": "research"}
    if limits:
        body["limits"] = limits
    response = client.post(f"{PREFIX}/threads/{thread_id}/runs", json=body, headers=headers)
    assert response.status_code == 202, response.text
    return response.json()


def test_create_run_is_idempotent(client, api_project) -> None:
    thread = client.post(f"{PREFIX}/projects/{api_project}/threads", json={"title": "会话"}).json()
    first = _create_run(client, thread["id"], "比较两个方案", key="demo-request-001")
    second = _create_run(client, thread["id"], "比较两个方案", key="demo-request-001")
    assert first["run_id"] == second["run_id"]
    assert second["created"] is False

    conflict = client.post(
        f"{PREFIX}/threads/{thread['id']}/runs",
        json={"message": "完全不同的请求体"},
        headers={"Idempotency-Key": "demo-request-001"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "RUN_CONFLICT"


def test_second_run_in_same_thread_conflicts(client, api_project) -> None:
    thread = client.post(f"{PREFIX}/projects/{api_project}/threads", json={}).json()
    _create_run(client, thread["id"], "第一个")
    response = client.post(f"{PREFIX}/threads/{thread['id']}/runs", json={"message": "第二个"})
    assert response.status_code == 409


def test_limits_can_only_be_lowered_for_admin_scope(client, api_project) -> None:
    thread = client.post(f"{PREFIX}/projects/{api_project}/threads", json={}).json()
    created = _create_run(client, thread["id"], "限制预算", max_tool_calls=3)
    run = client.get(f"{PREFIX}/runs/{created['run_id']}").json()
    assert run["run"]["budget_limits"]["max_tool_calls"] == 3
    assert run["run"]["budget_limits"]["max_model_calls"] > 3


def test_run_detail_events_and_cursor(client, api_project) -> None:
    thread = client.post(f"{PREFIX}/projects/{api_project}/threads", json={}).json()
    created = _create_run(client, thread["id"], "事件测试")
    run_id = created["run_id"]

    detail = client.get(f"{PREFIX}/runs/{run_id}").json()
    assert detail["run"]["status"] == RunStatus.QUEUED.value
    assert detail["last_seq"] >= 1
    assert detail["snapshot"]["knowledge_scope"] == {"document_ids": []}

    events = client.get(f"{PREFIX}/runs/{run_id}/events", params={"stream": "false"}).json()
    assert events["events"][0]["type"] == "run.queued"
    assert events["events"][0]["seq"] == 1

    timeline = client.get(f"{PREFIX}/runs/{run_id}/timeline").json()
    assert timeline["events"] and timeline["operations"] == []

    too_old = client.get(f"{PREFIX}/runs/{run_id}/events", params={"stream": "false", "after": 0})
    assert too_old.status_code == 200


def test_cancel_and_retry_flow(client, repo, api_project) -> None:
    thread = client.post(f"{PREFIX}/projects/{api_project}/threads", json={}).json()
    run_id = _create_run(client, thread["id"], "取消测试")["run_id"]
    cancel = client.post(f"{PREFIX}/runs/{run_id}/cancel")
    assert cancel.status_code == 202
    assert cancel.json()["status"] == RunStatus.CANCELLED.value

    retry = client.post(f"{PREFIX}/runs/{run_id}/retry")
    assert retry.status_code == 202
    assert retry.json()["parent_run_id"] == run_id

    conflict = client.post(f"{PREFIX}/runs/{run_id}/retry")
    assert conflict.status_code == 202  # 原终态不变，仍可再次派生新 run


def test_fork_creates_new_thread(client, api_project) -> None:
    thread = client.post(f"{PREFIX}/projects/{api_project}/threads", json={}).json()
    run_id = _create_run(client, thread["id"], "分叉测试")["run_id"]
    client.post(f"{PREFIX}/runs/{run_id}/cancel")
    forked = client.post(f"{PREFIX}/runs/{run_id}/fork").json()
    assert forked["thread_id"] != thread["id"]
    assert forked["run_id"] != run_id


# --------------------------------------------------------------------- 审批
def test_approval_decision_and_conflict(client, repo, api_project) -> None:
    thread = client.post(f"{PREFIX}/projects/{api_project}/threads", json={}).json()
    run_id = _create_run(client, thread["id"], "审批接口测试")["run_id"]
    approval = repo.create_approval(
        run_id=run_id,
        project_id=api_project,
        thread_id=thread["id"],
        tool_name="create_demo_ticket",
        arguments={"title": "演示工单", "body": "b"},
        expected_effect="创建本地模拟工单",
    )
    repo.update_run(run_id, status=RunStatus.WAITING_APPROVAL.value)
    listed = client.get(f"{PREFIX}/runs/{run_id}/approvals").json()["approvals"]
    assert listed and listed[0]["status"] == "pending"

    approved = client.post(
        f"{PREFIX}/approvals/{approval['id']}/decision",
        json={
            "decision": "approve",
            "expected_revision": 1,
            "arguments_hash": approval["arguments_hash"],
            "comment": "确认创建本地演示工单",
        },
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["run_status"] == RunStatus.QUEUED.value
    assert repo.get_run(run_id)["interrupt_payload"]["decision"] == "approve"

    again = client.post(
        f"{PREFIX}/approvals/{approval['id']}/decision",
        json={"decision": "approve", "expected_revision": 1, "arguments_hash": approval["arguments_hash"]},
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "APPROVAL_CONFLICT"


def test_approval_revision_supersedes(client, repo, api_project) -> None:
    thread = client.post(f"{PREFIX}/projects/{api_project}/threads", json={}).json()
    run_id = _create_run(client, thread["id"], "修订参数")["run_id"]
    approval = repo.create_approval(
        run_id=run_id,
        project_id=api_project,
        thread_id=thread["id"],
        tool_name="create_demo_ticket",
        arguments={"title": "原参数", "body": "b"},
        expected_effect="创建本地模拟工单",
    )
    revised = client.post(
        f"{PREFIX}/approvals/{approval['id']}/revision",
        json={"arguments": {"title": "修改后的标题", "body": "b2"}},
    )
    assert revised.status_code == 201, revised.text
    body = revised.json()
    assert body["revision"] == 2 and body["arguments"]["title"] == "修改后的标题"

    rejected = client.post(
        f"{PREFIX}/approvals/{approval['id']}/decision",
        json={"decision": "approve", "expected_revision": 1, "arguments_hash": approval["arguments_hash"]},
    )
    assert rejected.status_code == 409


# --------------------------------------------------------------------- 产物与记忆
def test_artifact_download_and_path_safety(client, repo, settings, api_project) -> None:
    thread = client.post(f"{PREFIX}/projects/{api_project}/threads", json={}).json()
    run_id = _create_run(client, thread["id"], "产物下载")["run_id"]
    workspace = settings.workspace_root / api_project / run_id
    (workspace / "reports").mkdir(parents=True, exist_ok=True)
    (workspace / "reports" / "r.md").write_text("# 报告内容", encoding="utf-8")
    artifact = repo.create_artifact(
        run_id=run_id,
        project_id=api_project,
        title="报告",
        relative_path="reports/r.md",
        media_type="text/markdown",
        content_hash="sha256:x",
        size=12,
    )
    response = client.get(f"{PREFIX}/artifacts/{artifact['id']}/download")
    assert response.status_code == 200
    assert "报告内容" in response.text

    escaping = repo.create_artifact(
        run_id=run_id,
        project_id=api_project,
        title="越界",
        relative_path="../../escape.md",
        media_type="text/markdown",
        content_hash="sha256:y",
        size=1,
    )
    assert client.get(f"{PREFIX}/artifacts/{escaping['id']}/download").status_code == 404


def test_source_lookup_returns_location(client, repo, settings, api_project) -> None:
    _import_via_api(client, repo, settings, api_project, "operations-guide.md")
    preview = client.post(
        f"{PREFIX}/projects/{api_project}/retrieval/preview",
        json={"query": "恢复顺序 租约 fencing", "top_k": 3},
    ).json()
    chunk_id = preview["hits"][0]["chunk_id"]
    source = client.get(f"{PREFIX}/sources/{chunk_id}").json()
    assert source["chunk_id"] == chunk_id
    assert source["source_title"] == "operations-guide.md"
    assert source["document_version"] == 1


def test_memory_revision_conflict(client, api_project) -> None:
    created = client.post(
        f"{PREFIX}/projects/{api_project}/memories",
        json={"content": "用户偏好中文报告", "status": "candidate"},
    )
    assert created.status_code == 201
    memory = created.json()

    confirmed = client.patch(
        f"{PREFIX}/memories/{memory['id']}", json={"status": "confirmed", "revision": 1}
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["revision"] == 2

    stale = client.patch(
        f"{PREFIX}/memories/{memory['id']}", json={"content": "改动", "revision": 1}
    )
    assert stale.status_code == 422

    deleted = client.delete(f"{PREFIX}/memories/{memory['id']}")
    assert deleted.status_code == 202
    listed = client.get(f"{PREFIX}/projects/{api_project}/memories").json()["memories"]
    assert all(item["id"] != memory["id"] for item in listed)


def test_unknown_project_returns_404(client) -> None:
    response = client.get(f"{PREFIX}/projects/00000000-0000-4000-8000-000000000000/documents")
    assert response.status_code == 404


def test_missing_upload_field_is_validation_error(client, api_project) -> None:
    response = client.post(f"{PREFIX}/projects/{api_project}/documents", data={})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_default_dataset_is_present() -> None:
    assert Path(DEMO_DIR / "gold.json").exists()
    assert BudgetLimits().max_model_calls == 20


# --------------------------------------------------------------------- 评测
def test_eval_run_reports_retrieval_metrics(client, repo, settings, api_project) -> None:
    for name in ("gateway-v1.md", "gateway-v2.md", "operations-guide.md", "capacity-notes.txt"):
        _import_via_api(client, repo, settings, api_project, name)

    created = client.post(f"{PREFIX}/projects/{api_project}/eval-runs", json={"mode": "hybrid"})
    assert created.status_code == 202
    eval_id = created.json()["eval_id"]

    pending = client.get(f"{PREFIX}/eval-runs/{eval_id}").json()
    assert pending["status"] == "queued"

    worker = Worker(repo, settings)
    job = repo.get_job(eval_id)
    assert repo.claim_job(job["id"])
    worker._handle_job_sync(job)

    result = client.get(f"{PREFIX}/eval-runs/{eval_id}").json()
    assert result["status"] == "succeeded", result
    assert result["dataset_version"] == "demo-dataset-v1"
    assert result["metrics"]["samples"]["total"] == 5
    assert result["metrics"]["recall_at_8"] is not None
    assert result["metrics"]["task_success_rate"].startswith("未执行")
    assert result["environment"]["notice"].startswith("容量与延迟指标未测量")
