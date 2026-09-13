"""工作进程测试：后台导入任务、排队运行领取、排队阶段取消与事件落库。"""

from __future__ import annotations

import asyncio

from harnesslab.config import REPO_ROOT
from harnesslab.runtime.worker import JOB_DOCUMENT_IMPORT, Worker
from harnesslab.storage.models import BudgetLimits, EventType, RunStatus

DEMO_DIR = REPO_ROOT / "datasets" / "demo"


def _drain_queue(repo) -> None:
    """清空其他用例残留的排队运行，保证工作进程只处理本用例的任务。"""
    from harnesslab.utils import iso

    while True:
        run = repo.claim_next_run("queue-drain", 60)
        if run is None:
            return
        if run["status"] == RunStatus.RUNNING.value:
            repo.update_run(run["id"], status=RunStatus.COMPLETED.value, finished_at=iso())


def _enqueue_import(repo, settings, project, filename: str) -> dict:
    data = (DEMO_DIR / filename).read_bytes()
    stored = settings.upload_root / f"worker-{filename}"
    stored.write_bytes(data)
    return repo.create_job(
        project_id=project["id"],
        kind=JOB_DOCUMENT_IMPORT,
        payload={"stored_path": str(stored), "filename": filename, "size": len(data)},
    )


def test_worker_processes_import_job(repo, settings, project) -> None:
    _drain_queue(repo)
    job = _enqueue_import(repo, settings, project, "gateway-v2.md")
    worker = Worker(repo, settings)
    assert asyncio.run(worker.run_once()) is True
    processed = repo.get_job(job["id"])
    assert processed["status"] == "succeeded"
    assert processed["result"]["parse_status"] == "ready"
    assert repo.count_chunks(project["id"]) > 0


def test_worker_reports_failed_job(repo, settings, project) -> None:
    _drain_queue(repo)
    stored = settings.upload_root / "broken.bin"
    stored.write_bytes(b"\x00\x01\x02blob")
    job = repo.create_job(
        project_id=project["id"],
        kind=JOB_DOCUMENT_IMPORT,
        payload={"stored_path": str(stored), "filename": "broken.bin", "size": 7},
    )
    worker = Worker(repo, settings)
    asyncio.run(worker.run_once())
    processed = repo.get_job(job["id"])
    assert processed["status"] == "succeeded"
    assert processed["result"]["parse_status"] == "unsupported"
    docs = repo.list_documents(project["id"])
    assert all(doc["active_version"] is None for doc in docs)


def test_worker_claims_and_executes_queued_run(repo, settings, project) -> None:
    _drain_queue(repo)
    thread = repo.create_thread(project["id"], "worker 会话")
    run, _ = repo.create_run(
        thread_id=thread["id"],
        project_id=project["id"],
        user_message="工作进程执行的任务",
        mode="research",
        config_snapshot={},
        budget=BudgetLimits(),
    )
    repo.append_event(run["id"], EventType.RUN_QUEUED, {"mode": "research"})
    worker = Worker(repo, settings)
    assert asyncio.run(worker.run_once()) is True

    finished = repo.get_run(run["id"])
    assert finished["fencing_token"] >= 1
    assert finished["lease_owner"] is None
    assert finished["status"] in {RunStatus.COMPLETED.value, RunStatus.FAILED.value}


def test_worker_cancels_queued_run(repo, settings, project) -> None:
    _drain_queue(repo)
    thread = repo.create_thread(project["id"], "取消会话")
    run, _ = repo.create_run(
        thread_id=thread["id"],
        project_id=project["id"],
        user_message="排队时取消",
        mode="research",
        config_snapshot={},
        budget=BudgetLimits(),
    )
    repo.update_run(run["id"], cancel_requested=1)
    worker = Worker(repo, settings)
    asyncio.run(worker.run_once())
    assert repo.get_run(run["id"])["status"] == RunStatus.CANCELLED.value
    assert any(
        event["type"] == EventType.RUN_CANCELLED.value for event in repo.list_events(run["id"])
    )


def test_worker_recovers_expired_lease(repo, settings, project) -> None:
    _drain_queue(repo)
    thread = repo.create_thread(project["id"], "恢复会话")
    run, _ = repo.create_run(
        thread_id=thread["id"],
        project_id=project["id"],
        user_message="恢复中的任务",
        mode="research",
        config_snapshot={},
        budget=BudgetLimits(),
    )
    claimed = repo.claim_next_run("dead-worker", 60)
    assert claimed is not None and claimed["id"] == run["id"]
    repo.update_run(run["id"], lease_expiry="2000-01-01T00:00:00Z")
    worker = Worker(repo, settings)
    asyncio.run(worker.run_once())
    status = repo.get_run(run["id"])["status"]
    assert status in {RunStatus.COMPLETED.value, RunStatus.FAILED.value,
                      RunStatus.WAITING_APPROVAL.value, RunStatus.RECOVERING.value}
