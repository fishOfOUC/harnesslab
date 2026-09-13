"""运行执行器测试：图驱动、引用校验、审批中断/恢复、预算与人工核对。

使用脚本化替身模型驱动确定性控制流；结论不得当作真实模型成绩。
"""

from __future__ import annotations

from harnesslab.config import REPO_ROOT
from harnesslab.runtime.executor import RunExecutor
from harnesslab.storage.models import (
    ApprovalStatus,
    BudgetLimits,
    RunStatus,
    ToolOperationStatus,
)
from harnesslab.utils import iso

from .fakes import ScriptedChatModel, final, tool_call

DEMO_DIR = REPO_ROOT / "datasets" / "demo"


def _import_demo(knowledge, project) -> None:
    for name in ("gateway-v1.md", "gateway-v2.md", "capacity-notes.txt", "operations-guide.md"):
        knowledge.import_document(project["id"], name, (DEMO_DIR / name).read_bytes())


def _make_executor(repo, settings, model: ScriptedChatModel) -> RunExecutor:
    """注入替身模型与固定计划，使控制流完全确定。"""
    executor = RunExecutor(repo, settings, model_factory=lambda: model)
    executor.agent_factory.generate_plan = lambda model, goal, mode="research": _plan(goal)
    return executor


def _plan(goal: str):
    from harnesslab.storage.models import Plan, PlanStep

    return Plan(goal=goal, steps=[PlanStep(id="step-1", title="检索资料并给出带引用的回答")])


def _new_run(repo, project, message: str, limits: BudgetLimits | None = None) -> dict:
    thread = repo.create_thread(project["id"], "执行器测试")
    run, _ = repo.create_run(
        thread_id=thread["id"],
        project_id=project["id"],
        user_message=message,
        mode="research",
        config_snapshot={},
        budget=limits or BudgetLimits(),
    )
    return run


def test_run_completes_with_evidence(repo, knowledge, settings, project, monkeypatch) -> None:
    _import_demo(knowledge, project)
    run = _new_run(repo, project, "比较 gateway v1 与 v2 的并发上限")
    model = ScriptedChatModel(
        script=(
            tool_call("call-1", "search_knowledge", query="gateway 并发上限", top_k=5),
            tool_call("call-2", "calculator", expression="50/0.2"),
            final("v1 并发上限 20，v2 为 50 [S1]；理论吞吐 250。[S2]"),
        )
    )
    executor = _make_executor(repo, settings, model)
    finished = executor.execute(run["id"])

    assert finished["status"] == RunStatus.COMPLETED.value
    result = finished["result"]
    assert "20" in result["answer"] and result["citations"]
    assert result["citations_resolved"]
    assert result["plan"]["steps"][0]["status"] == "done"
    assert result["usage"]["tool_calls"] >= 2
    types = [event["type"] for event in repo.list_events(run["id"])]
    assert "run.started" in types and "plan.updated" in types
    assert "tool.completed" in types and "run.completed" in types
    snapshot = repo.get_run(run["id"])["config_snapshot"]
    assert "index_version" in snapshot and snapshot["policy_version"] == "policy-v1"


def test_run_flags_invalid_citation_labels(repo, knowledge, settings, project) -> None:
    _import_demo(knowledge, project)
    run = _new_run(repo, project, "并发上限是多少")
    model = ScriptedChatModel(
        script=(
            tool_call("call-1", "search_knowledge", query="并发上限"),
            final("结论见 [S9]。"),
        )
    )
    executor = _make_executor(repo, settings, model)
    finished = executor.execute(run["id"])
    limitations = " ".join(finished["result"]["limitations"])
    assert "S9" in limitations
    assert finished["result"]["citations_resolved"] == []


def test_answer_without_evidence_is_marked(repo, settings, project) -> None:
    run = _new_run(repo, project, "资料里有没有零数据丢失保证")
    model = ScriptedChatModel(script=(final("资料保证零数据丢失。"),))
    executor = _make_executor(repo, settings, model)
    finished = executor.execute(run["id"])
    assert finished["status"] == RunStatus.COMPLETED.value
    assert any("证据" in item for item in finished["result"]["limitations"])


def test_budget_exhaustion_stops_with_reason(repo, knowledge, settings, project) -> None:
    _import_demo(knowledge, project)
    run = _new_run(repo, project, "会被预算打断的任务", BudgetLimits(max_model_calls=0))
    model = ScriptedChatModel(script=(tool_call("call-1", "search_knowledge", query="x"), final("x")))
    executor = _make_executor(repo, settings, model)
    finished = executor.execute(run["id"])
    assert finished["status"] == RunStatus.FAILED.value
    assert finished["error_code"] == "BUDGET_EXCEEDED"
    assert any(event["type"] == "run.failed" for event in repo.list_events(run["id"]))


def test_approval_pauses_and_executes_ticket_once(repo, knowledge, settings, project) -> None:
    _import_demo(knowledge, project)
    run = _new_run(repo, project, "生成报告并创建本地演示工单")
    model = ScriptedChatModel(
        script=(
            tool_call("call-ticket", "create_demo_ticket", title="演示工单", body="来自测试"),
            final("工单已创建。[S1]"),
        )
    )
    executor = _make_executor(repo, settings, model)

    paused = executor.execute(run["id"])
    assert paused["status"] == RunStatus.WAITING_APPROVAL.value
    approval = repo.pending_approval(run["id"])
    assert approval is not None
    assert approval["tool_name"] == "create_demo_ticket"
    assert approval["revision"] == 1
    assert approval["checkpoint_id"], "审批必须绑定检查点"
    assert repo.count_tickets_by_title(project["id"], "演示工单") == 0
    types = [event["type"] for event in repo.list_events(run["id"])]
    assert "approval.requested" in types
    assert "run.completed" not in types

    # 模拟审批接口：保存决策并把运行重新入队
    repo.decide_approval(
        approval["id"],
        decision="approve",
        expected_revision=1,
        arguments_hash=approval["arguments_hash"],
        reviewer_id="local-dev-user",
    )
    repo.update_run(
        run["id"],
        status=RunStatus.QUEUED.value,
        interrupt_payload={
            "decision": "approve",
            "approval_id": approval["id"],
            "revision": 1,
            "arguments": approval["arguments"],
        },
    )
    finished = executor.execute(run["id"])
    assert finished["status"] == RunStatus.COMPLETED.value
    assert repo.count_tickets_by_title(project["id"], "演示工单") == 1
    operations = repo.list_operations(run["id"])
    assert [op["status"] for op in operations] == [ToolOperationStatus.SUCCEEDED.value]
    assert operations[0]["external_receipt"]


def test_citations_survive_approval_resume(repo, knowledge, settings, project) -> None:
    """真实缺陷回归：审批中断后恢复时，模型此前引用的 [S1] 必须仍然有效。

    引用映射原先只存在内存上下文里，恢复会重建上下文导致旧引用全部判为无效
    （真实 DeepSeek 运行中出现过「引用 0 条 / 引用了本次召回之外的标签」）。
    现在引用从工具账本重建，标签在全 run 内唯一。
    """
    _import_demo(knowledge, project)
    run = _new_run(repo, project, "先检索再申请工单")
    model = ScriptedChatModel(
        script=(
            tool_call("call-search", "search_knowledge", query="并发上限与过载策略", top_k=5),
            tool_call("call-ticket", "create_demo_ticket", title="带引用的工单", body="正文引用 [S1]"),
            final("并发上限 v1 为 20、v2 为 50 [S1]；工单已创建。"),
        )
    )
    executor = _make_executor(repo, settings, model)

    paused = executor.execute(run["id"])
    assert paused["status"] == RunStatus.WAITING_APPROVAL.value
    approval = repo.pending_approval(run["id"])
    assert approval is not None

    repo.decide_approval(
        approval["id"],
        decision="approve",
        expected_revision=1,
        arguments_hash=approval["arguments_hash"],
        reviewer_id="local-dev-user",
    )
    repo.update_run(
        run["id"],
        status=RunStatus.QUEUED.value,
        interrupt_payload={
            "decision": "approve",
            "approval_id": approval["id"],
            "revision": 1,
            "arguments": approval["arguments"],
        },
    )
    finished = executor.execute(run["id"])

    assert finished["status"] == RunStatus.COMPLETED.value
    result = finished["result"]
    assert result["citations_resolved"], "恢复后引用映射丢失"
    assert result["citations_resolved"][0]["label"] == "S1"
    assert result["citations_resolved"][0]["chunk_id"]
    assert not [item for item in result["limitations"] if "本次召回之外" in item]
    assert not [item for item in result["limitations"] if "没有检索到可用证据" in item]


def test_rejected_approval_does_not_create_ticket(repo, knowledge, settings, project) -> None:
    _import_demo(knowledge, project)
    run = _new_run(repo, project, "申请创建工单")
    model = ScriptedChatModel(
        script=(
            tool_call("call-ticket", "create_demo_ticket", title="不该出现", body="b"),
            final("用户拒绝了工单创建，其余步骤已完成。[S1]"),
        )
    )
    executor = _make_executor(repo, settings, model)
    executor.execute(run["id"])
    approval = repo.pending_approval(run["id"])
    assert approval is not None
    repo.decide_approval(
        approval["id"],
        decision="reject",
        expected_revision=1,
        arguments_hash=approval["arguments_hash"],
        reviewer_id="local-dev-user",
    )
    repo.update_run(
        run["id"],
        status=RunStatus.QUEUED.value,
        interrupt_payload={
            "decision": "reject",
            "approval_id": approval["id"],
            "revision": 1,
            "arguments": approval["arguments"],
        },
    )
    finished = executor.execute(run["id"])
    assert finished["status"] == RunStatus.COMPLETED.value
    assert repo.count_tickets_by_title(project["id"], "不该出现") == 0
    assert repo.get_approval(approval["id"])["status"] == ApprovalStatus.REJECTED.value


def test_recovery_with_uncertain_side_effect_requires_manual_reconcile(
    repo, knowledge, settings, project
) -> None:
    _import_demo(knowledge, project)
    run = _new_run(repo, project, "恢复测试")
    operation = repo.create_operation(
        run_id=run["id"],
        project_id=project["id"],
        logical_action_id="call-crash",
        tool_name="create_demo_ticket",
        tool_version="1.0.0",
        idempotency_key="crash-key",
        request={"title": "崩溃工单", "body": "b"},
    )
    repo.update_operation("crash-key", status=ToolOperationStatus.EXECUTING)
    assert operation["id"]
    repo.update_run(run["id"], status=RunStatus.RECOVERING.value)

    model = ScriptedChatModel(script=(final("不应执行到这里"),))
    executor = _make_executor(repo, settings, model)
    result = executor.execute(run["id"])

    assert result["status"] == RunStatus.WAITING_APPROVAL.value
    approval = repo.pending_approval(run["id"])
    assert approval is not None and approval["kind"] == "manual_reconcile"
    assert repo.find_operation("crash-key")["status"] == ToolOperationStatus.UNCERTAIN.value
    assert repo.count_tickets_by_title(project["id"], "崩溃工单") == 0


def test_cancel_during_run_keeps_existing_result(repo, knowledge, settings, project) -> None:
    _import_demo(knowledge, project)
    run = _new_run(repo, project, "取消测试")
    repo.update_run(run["id"], status=RunStatus.RUNNING.value)
    repo.request_cancel(run["id"])
    model = ScriptedChatModel(script=(final("不应执行"),))
    executor = _make_executor(repo, settings, model)
    finished = executor.execute(run["id"])
    assert finished["status"] == RunStatus.CANCELLED.value
    result = finished["result"]
    assert result["task_status"] == "partial"
    assert "撤回" in result["note"]


def test_write_report_produces_downloadable_artifact(repo, knowledge, settings, project) -> None:
    _import_demo(knowledge, project)
    run = _new_run(repo, project, "写一份报告")
    model = ScriptedChatModel(
        script=(
            tool_call("call-report", "write_report", title="选型报告",
                      content_markdown="# 选型报告\n\n结论：v2 并发上限更高 [S1]", filename="report.md"),
            final("报告已生成 [S1]。"),
        )
    )
    executor = _make_executor(repo, settings, model)
    finished = executor.execute(run["id"])
    artifacts = finished["result"]["artifacts"]
    assert artifacts and artifacts[0]["relative_path"] == "reports/report.md"
    path = settings.workspace_root / project["id"] / run["id"] / "reports/report.md"
    assert path.exists() and "选型报告" in path.read_text(encoding="utf-8")
    assert any(event["type"] == "artifact.created" for event in repo.list_events(run["id"]))


def test_tool_failure_does_not_break_run(repo, knowledge, settings, project) -> None:
    _import_demo(knowledge, project)
    run = _new_run(repo, project, "非法计算不应该中断运行")
    model = ScriptedChatModel(
        script=(
            tool_call("call-bad", "calculator", expression="__import__('os').system('x')"),
            final("计算被拒绝，改用检索结论 [S1]。"),
        )
    )
    executor = _make_executor(repo, settings, model)
    finished = executor.execute(run["id"])
    assert finished["status"] == RunStatus.COMPLETED.value
    assert finished["result"]["usage"]["tool_calls"] == 1
    operations = repo.list_operations(run["id"])
    assert operations[0]["status"] == ToolOperationStatus.FAILED.value


def test_executor_is_idempotent_for_terminal_run(repo, project, settings) -> None:
    """终态运行不会被再次执行（重跑必须创建新 run）。"""
    run = _new_run(repo, project, "终态保护")
    repo.update_run(run["id"], status=RunStatus.COMPLETED.value, finished_at=iso())
    executor = _make_executor(repo, settings, ScriptedChatModel(script=(final("不应执行"),)))
    assert executor.execute(run["id"])["status"] == RunStatus.COMPLETED.value
