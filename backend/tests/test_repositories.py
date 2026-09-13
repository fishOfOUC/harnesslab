"""仓储与一致性测试：幂等、同 thread 互斥、租约 CAS、审批竞态、工具账本。"""

from __future__ import annotations

import pytest

from harnesslab.errors import ConflictError
from harnesslab.storage.models import (
    ApprovalStatus,
    BudgetLimits,
    RunStatus,
    ToolOperationStatus,
)
from harnesslab.utils import iso, utcnow


def _new_run(repo, project, message: str = "问题", idempotency_key: str | None = None):
    thread = repo.create_thread(project["id"], "会话")
    return _run_in_thread(repo, project, thread, message, idempotency_key)


def _run_in_thread(repo, project, thread, message: str, idempotency_key: str | None = None):
    return repo.create_run(
        thread_id=thread["id"],
        project_id=project["id"],
        user_message=message,
        mode="research",
        config_snapshot={},
        budget=BudgetLimits(),
        idempotency_key=idempotency_key,
    )


def _drain_queue(repo) -> None:
    """把已存在的排队运行清空，避免测试之间相互影响。"""
    from harnesslab.storage.models import RunStatus

    while True:
        run = repo.claim_next_run("queue-drain", 60)
        if run is None:
            return
        repo.update_run(run["id"], status=RunStatus.COMPLETED.value, finished_at=iso())


def test_create_run_idempotency(repo, project) -> None:
    run, created = _new_run(repo, project, "同一个请求", idempotency_key="k-1")
    again, created_again = _new_run(repo, project, "同一个请求", idempotency_key="k-1")
    assert created and not created_again
    assert run["id"] == again["id"]

    with pytest.raises(ConflictError):
        _new_run(repo, project, "不同的请求体", idempotency_key="k-1")


def test_thread_single_active_run(repo, project) -> None:
    thread = repo.create_thread(project["id"], "会话")
    _run_in_thread(repo, project, thread, "第一个运行")
    with pytest.raises(ConflictError) as exc:
        _run_in_thread(repo, project, thread, "第二个运行")
    assert exc.value.code == "RUN_CONFLICT"


def test_thread_allows_new_run_after_terminal(repo, project) -> None:
    run, _ = _new_run(repo, project, "第一个运行")
    repo.update_run(run["id"], status=RunStatus.COMPLETED.value, finished_at=iso())
    second, created = _new_run(repo, project, "第二个运行")
    assert created and second["status"] == RunStatus.QUEUED.value


def test_lease_claim_is_single_owner(repo, project) -> None:
    _drain_queue(repo)
    run, _ = _new_run(repo, project, "租约测试")
    claimed = repo.claim_next_run("worker-a", 60)
    assert claimed is not None and claimed["id"] == run["id"]
    assert claimed["status"] == RunStatus.RUNNING.value
    assert claimed["fencing_token"] == 1
    # 已被领取的运行不会被第二个工作进程重复领取
    assert repo.claim_next_run("worker-b", 60) is None
    assert repo.renew_lease(run["id"], "worker-a", 60) is True
    assert repo.renew_lease(run["id"], "worker-b", 60) is False


def test_recover_expired_lease(repo, project) -> None:
    _drain_queue(repo)
    run, _ = _new_run(repo, project, "恢复测试")
    claimed = repo.claim_next_run("worker-a", 60)
    assert claimed is not None and claimed["id"] == run["id"]
    repo.update_run(run["id"], lease_expiry=iso(utcnow().replace(year=2000)))
    recovered = repo.recover_expired_runs()
    assert run["id"] in [item["id"] for item in recovered]
    assert repo.get_run(run["id"])["status"] == RunStatus.RECOVERING.value


def test_approval_double_decision_conflict(repo, project) -> None:
    run, _ = _new_run(repo, project, "审批测试")
    approval = repo.create_approval(
        run_id=run["id"],
        project_id=project["id"],
        thread_id=run["thread_id"],
        tool_name="create_demo_ticket",
        arguments={"title": "t", "body": "b"},
        expected_effect="创建模拟工单",
    )
    decided = repo.decide_approval(
        approval["id"],
        decision="approve",
        expected_revision=1,
        arguments_hash=approval["arguments_hash"],
        reviewer_id="local-dev-user",
    )
    assert decided["status"] == ApprovalStatus.APPROVED.value
    with pytest.raises(ConflictError) as exc:
        repo.decide_approval(
            approval["id"],
            decision="approve",
            expected_revision=1,
            arguments_hash=approval["arguments_hash"],
            reviewer_id="local-dev-user",
        )
    assert exc.value.code == "APPROVAL_CONFLICT"


def test_approval_revision_invalidates_previous(repo, project) -> None:
    run, _ = _new_run(repo, project, "审批版本测试")
    approval = repo.create_approval(
        run_id=run["id"],
        project_id=project["id"],
        thread_id=run["thread_id"],
        tool_name="create_demo_ticket",
        arguments={"title": "a", "body": "b"},
        expected_effect="创建模拟工单",
    )
    revised = repo.revise_approval(approval["id"], {"title": "a2", "body": "b"})
    assert revised["revision"] == 2
    assert repo.get_approval(approval["id"])["status"] == ApprovalStatus.SUPERSEDED.value
    with pytest.raises(ConflictError):
        repo.decide_approval(
            approval["id"],
            decision="approve",
            expected_revision=1,
            arguments_hash=approval["arguments_hash"],
            reviewer_id="local-dev-user",
        )


def test_approval_expiry(repo, project) -> None:
    run, _ = _new_run(repo, project, "过期审批")
    approval = repo.create_approval(
        run_id=run["id"],
        project_id=project["id"],
        thread_id=run["thread_id"],
        tool_name="create_demo_ticket",
        arguments={"title": "t"},
        expected_effect="创建模拟工单",
        ttl_hours=-1,
    )
    with pytest.raises(ConflictError):
        repo.decide_approval(
            approval["id"],
            decision="approve",
            expected_revision=1,
            arguments_hash=approval["arguments_hash"],
            reviewer_id="local-dev-user",
        )
    assert repo.get_approval(approval["id"])["status"] == ApprovalStatus.EXPIRED.value


def test_tool_ledger_records_and_recovery_report(repo, project) -> None:
    run, _ = _new_run(repo, project, "账本测试")
    operation = repo.create_operation(
        run_id=run["id"],
        project_id=project["id"],
        logical_action_id="call-1",
        tool_name="create_demo_ticket",
        tool_version="1.0.0",
        idempotency_key="key-1",
        request={"title": "t"},
    )
    repo.update_operation(
        "key-1", status=ToolOperationStatus.SUCCEEDED, result={"ok": True}, external_receipt="ticket-1"
    )
    stored = repo.find_operation("key-1")
    assert stored["status"] == ToolOperationStatus.SUCCEEDED.value
    assert stored["external_receipt"] == "ticket-1"
    report = repo.recovery_report(run["id"])
    assert len(report["succeeded"]) == 1
    assert operation["idempotency_key"] == "key-1"


def test_ticket_unique_per_operation(repo, project) -> None:
    first = repo.create_ticket(project_id=project["id"], title="演示工单", body="b",
                               tool_operation_id="op-1")
    second = repo.create_ticket(project_id=project["id"], title="演示工单", body="b",
                                tool_operation_id="op-1")
    assert first["id"] == second["id"]
    assert repo.count_tickets_by_title(project["id"], "演示工单") == 1


def test_events_seq_is_monotonic_and_unique(repo, project) -> None:
    run, _ = _new_run(repo, project, "事件测试")
    from harnesslab.storage.models import EventType

    events = [repo.append_event(run["id"], EventType.MESSAGE_DELTA, {"i": index}) for index in range(5)]
    assert [event["seq"] for event in events] == [1, 2, 3, 4, 5]
    assert repo.max_event_seq(run["id"]) == 5
    assert len(repo.list_events(run["id"], after_seq=2)) == 3


def test_cross_project_scope_isolation(repo, project) -> None:
    other = repo.create_project("另一个项目", "local-dev-user")
    document = repo.create_document(
        project_id=other["id"], title="a.md", source_path="a.md", media_type="text/markdown"
    )
    assert document["project_id"] == other["id"]
    assert all(item["id"] != document["id"] for item in repo.list_documents(project["id"]))
