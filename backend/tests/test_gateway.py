"""工具网关测试：策略拒绝、预算扣减、幂等复用与结果不明进入人工核对。"""

from __future__ import annotations

import pytest

from harnesslab.errors import HarnessLabError
from harnesslab.harness.budget import RunBudget
from harnesslab.harness.policy import Decision, PolicyEngine, RiskLevel, ToolPolicy
from harnesslab.observability.events import EventSink
from harnesslab.storage.models import BudgetLimits, RunStatus, ToolOperationStatus
from harnesslab.tools.base import ToolContext
from harnesslab.tools.gateway import ToolGateway
from harnesslab.tools.local import calculator, create_ticket_record, write_report
from harnesslab.tools.workspace import Workspace
from harnesslab.utils import iso

PERMISSIVE = {
    **dict(PolicyEngine().policies),
    "create_demo_ticket": ToolPolicy(
        "create_demo_ticket", "1.0.0", RiskLevel.SIDE_EFFECT, Decision.ALLOW, "测试中放宽审批"
    ),
}


def _context(repo, knowledge, settings, project, run, *, budget: RunBudget) -> ToolContext:
    return ToolContext(
        run_id=run["id"],
        project_id=project["id"],
        thread_id=run["thread_id"],
        workspace=Workspace(settings.workspace_root / project["id"] / run["id"]),
        budget=budget,
        policy=PolicyEngine(PERMISSIVE),
        repo=repo,
        knowledge=knowledge,
        events=EventSink(repo),
    )


def _make_run(repo, project):
    thread = repo.create_thread(project["id"], "网关测试")
    run, _ = repo.create_run(
        thread_id=thread["id"],
        project_id=project["id"],
        user_message="创建演示工单",
        mode="research",
        config_snapshot={},
        budget=BudgetLimits(),
    )
    return run


def test_unknown_tool_is_denied(repo, knowledge, settings, project) -> None:
    run = _make_run(repo, project)
    budget = RunBudget(repo, run["id"], BudgetLimits())
    ctx = _context(repo, knowledge, settings, project, run, budget=budget)
    gateway = ToolGateway(repo, settings, policy=PolicyEngine(), events=EventSink(repo))
    result = gateway.execute(ctx, "run_shell", {"cmd": "whoami"}, handler=lambda call: None)  # type: ignore[arg-type]
    assert not result.ok and result.error_code == "TOOL_NOT_FOUND"
    assert budget.usage.tool_calls == 0


def test_side_effect_tool_is_idempotent(repo, knowledge, settings, project) -> None:
    run = _make_run(repo, project)
    budget = RunBudget(repo, run["id"], BudgetLimits())
    ctx = _context(repo, knowledge, settings, project, run, budget=budget)
    gateway = ToolGateway(repo, settings, policy=PolicyEngine(PERMISSIVE), events=EventSink(repo))

    handler = lambda call: _ticket(ctx, call)  # noqa: E731
    first = gateway.execute(
        ctx, "create_demo_ticket", {"title": "演示工单", "body": "内容"},
        handler=handler, tool_call_id="call-1",
    )
    second = gateway.execute(
        ctx, "create_demo_ticket", {"title": "演示工单", "body": "内容"},
        handler=handler, tool_call_id="call-1",
    )
    assert first.ok and second.ok
    assert second.reused is True
    assert repo.count_tickets_by_title(project["id"], "演示工单") == 1
    assert budget.usage.tool_calls == 2
    operations = repo.list_operations(run["id"])
    assert len(operations) == 1
    assert operations[0]["status"] == ToolOperationStatus.SUCCEEDED.value
    assert operations[0]["external_receipt"]


def _ticket(ctx: ToolContext, call):
    from harnesslab.storage.models import ToolResult

    outcome = create_ticket_record(ctx, call.operation_id, call.arguments["title"], call.arguments["body"])
    ticket = outcome["ticket"]
    return ToolResult(ok=True, data={"ticket_id": ticket["id"], "status": "created"})


def test_uncertain_operation_requires_manual_reconciliation(repo, knowledge, settings, project) -> None:
    run = _make_run(repo, project)
    budget = RunBudget(repo, run["id"], BudgetLimits())
    ctx = _context(repo, knowledge, settings, project, run, budget=budget)
    from harnesslab.utils import hash_payload

    key = hash_payload(
        {
            "run_id": run["id"],
            "logical_action_id": "call-9",
            "tool_version": "1.0.0",
            "approval_revision": 0,
        }
    )
    operation = repo.create_operation(
        run_id=run["id"],
        project_id=project["id"],
        logical_action_id="call-9",
        tool_name="create_demo_ticket",
        tool_version="1.0.0",
        idempotency_key=key,
        request={"title": "t", "body": "b"},
    )
    repo.update_operation(key, status=ToolOperationStatus.EXECUTING)
    assert operation["status"] == ToolOperationStatus.PREPARED.value

    # 崩溃发生在操作执行后：用相同逻辑动作重入，网关不得自动重复执行
    gateway = ToolGateway(repo, settings, policy=PolicyEngine(PERMISSIVE), events=EventSink(repo))
    result = gateway.execute(
        ctx, "create_demo_ticket", {"title": "t", "body": "b"},
        handler=lambda call: _ticket(ctx, call),  # type: ignore[arg-type]
        tool_call_id="call-9",
    )
    assert not result.ok and result.error_code == "SIDE_EFFECT_UNCERTAIN"
    assert repo.find_operation(key)["status"] == ToolOperationStatus.UNCERTAIN.value
    assert repo.count_tickets_by_title(project["id"], "t") == 0


def test_denied_tool_has_no_side_effect(repo, knowledge, settings, project) -> None:
    run = _make_run(repo, project)
    budget = RunBudget(repo, run["id"], BudgetLimits())
    ctx = _context(repo, knowledge, settings, project, run, budget=budget)
    called: list[str] = []
    gateway = ToolGateway(repo, settings, policy=PolicyEngine(), events=EventSink(repo))
    result = gateway.execute(
        ctx, "run_python_sandbox", {"code": "print(1)"}, handler=lambda call: called.append("x")  # type: ignore[arg-type,return-value]
    )
    assert not result.ok and result.error_code == "FORBIDDEN"
    assert called == []
    assert repo.count_tickets_by_title(project["id"], "t") == 0
    assert repo.list_operations(run["id"]) == []


def test_citation_labels_are_global_and_stable(repo, knowledge, settings, project) -> None:
    """引用标签在全 run 内唯一：多次检索续号，重复片段复用原标签。"""
    from harnesslab.config import REPO_ROOT
    from harnesslab.tools.local import search_knowledge

    demo = REPO_ROOT / "datasets" / "demo"
    for name in ("gateway-v1.md", "gateway-v2.md", "operations-guide.md"):
        knowledge.import_document(project["id"], name, (demo / name).read_bytes())

    run = _make_run(repo, project)
    budget = RunBudget(repo, run["id"], BudgetLimits())
    ctx = _context(repo, knowledge, settings, project, run, budget=budget)

    first = search_knowledge(ctx, "并发上限", top_k=5)
    first_labels = [hit["label"] for hit in first.data["hits"]]
    assert first_labels[0] == "S1"
    assert len(set(first_labels)) == len(first_labels)

    second = search_knowledge(ctx, "故障恢复要求", top_k=5)
    second_labels = [hit["label"] for hit in second.data["hits"]]
    max_first = max(int(label[1:]) for label in first_labels)
    new_labels = [label for label in second_labels if label not in first_labels]
    assert new_labels, "第二次检索命中的是新文档，应当产生新标签"
    assert all(int(label[1:]) > max_first for label in new_labels), "新标签必须接续已有最大编号"
    assert len({*first_labels, *second_labels}) == len(first_labels) + len(new_labels)

    repeated = search_knowledge(ctx, "并发上限", top_k=5)
    repeated_labels = [hit["label"] for hit in repeated.data["hits"]]
    assert repeated_labels == first_labels, "再次检索同一内容应复用原标签"


def test_write_report_creates_artifact_and_download_ref(repo, knowledge, settings, project) -> None:
    run = _make_run(repo, project)
    budget = RunBudget(repo, run["id"], BudgetLimits())
    ctx = _context(repo, knowledge, settings, project, run, budget=budget)
    gateway = ToolGateway(repo, settings, policy=PolicyEngine(), events=EventSink(repo))
    result = gateway.execute(
        ctx,
        "write_report",
        {"title": "选型报告", "content_markdown": "# 报告\n\n结论 [S1]", "filename": "report.md"},
        handler=lambda call: write_report(ctx, **call.arguments),
        tool_call_id="call-report",
    )
    assert result.ok and result.artifact_ref
    artifact = repo.get_artifact(result.artifact_ref)
    assert artifact["run_id"] == run["id"]
    assert artifact["relative_path"] == "reports/report.md"
    events = [event["type"] for event in repo.list_events(run["id"])]
    assert "artifact.created" in events and "tool.completed" in events


def test_tool_timeout_is_recorded(repo, knowledge, settings, project) -> None:
    run = _make_run(repo, project)
    budget = RunBudget(repo, run["id"], BudgetLimits())
    ctx = _context(repo, knowledge, settings, project, run, budget=budget)
    fast_timeout = {
        **PERMISSIVE,
        "calculator": ToolPolicy(
            "calculator", "1.0.0", RiskLevel.READ, Decision.ALLOW, "测试超时", timeout_seconds=0
        ),
    }
    gateway = ToolGateway(repo, settings, policy=PolicyEngine(fast_timeout), events=EventSink(repo))

    import time

    result = gateway.execute(
        ctx, "calculator", {"expression": "1+1"},
        handler=lambda call: (time.sleep(0.4), calculator(ctx, "1+1"))[1],
        tool_call_id="call-slow",
    )
    assert not result.ok and result.error_code == "TOOL_TIMEOUT"
    operations = repo.list_operations(run["id"])
    assert operations[0]["status"] == ToolOperationStatus.FAILED.value


def test_project_scope_arguments_are_ignored(repo, knowledge, settings, project) -> None:
    run = _make_run(repo, project)
    budget = RunBudget(repo, run["id"], BudgetLimits())
    ctx = _context(repo, knowledge, settings, project, run, budget=budget)
    gateway = ToolGateway(repo, settings, policy=PolicyEngine(PERMISSIVE), events=EventSink(repo))
    captured: dict = {}

    def handler(call):
        captured.update(call.arguments)
        from harnesslab.storage.models import ToolResult

        return ToolResult(ok=True, data={})

    gateway.execute(
        ctx, "create_demo_ticket",
        {"title": "t", "body": "b", "project_id": "另一个项目"},
        handler=handler, tool_call_id="call-scope",
    )
    assert "project_id" not in captured


def test_budget_exhaustion_blocks_tool(repo, knowledge, settings, project) -> None:
    run = _make_run(repo, project)
    budget = RunBudget(repo, run["id"], BudgetLimits(max_tool_calls=1))
    ctx = _context(repo, knowledge, settings, project, run, budget=budget)
    gateway = ToolGateway(repo, settings, policy=PolicyEngine(), events=EventSink(repo))
    first = gateway.execute(ctx, "calculator", {"expression": "1+1"},
                            handler=lambda call: calculator(ctx, **call.arguments), tool_call_id="c1")
    assert first.ok
    with pytest.raises(HarnessLabError):
        gateway.execute(ctx, "calculator", {"expression": "1+1"},
                        handler=lambda call: calculator(ctx, **call.arguments), tool_call_id="c2")


def test_cancel_signal_blocks_new_side_effect(repo, knowledge, settings, project) -> None:
    run = _make_run(repo, project)
    budget = RunBudget(repo, run["id"], BudgetLimits())
    ctx = _context(repo, knowledge, settings, project, run, budget=budget)
    gateway = ToolGateway(repo, settings, policy=PolicyEngine(PERMISSIVE), events=EventSink(repo))
    repo.update_run(run["id"], status=RunStatus.RUNNING.value, cancel_requested=1, updated_at=iso())
    result = gateway.execute(
        ctx, "create_demo_ticket", {"title": "t", "body": "b"},
        handler=lambda call: _ticket(ctx, call), tool_call_id="call-cancel",  # type: ignore[arg-type]
    )
    assert not result.ok and result.error_code == "CANCELLED"
    assert repo.count_tickets_by_title(project["id"], "t") == 0
