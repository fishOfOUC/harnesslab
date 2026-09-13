"""单次运行的执行器：上下文装配、图驱动、审批中断、预算与结果校验。

对应文档 03 第 2、3、8、9 节。所有对外可见状态变化都先写业务表与事件表，
再更新 run 状态，避免界面出现“已完成但无产物”的中间态。
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from ..agents.factory import RunAgentFactory, plan_to_text
from ..agents.prompts import PromptLibrary, SkillLibrary, render_skill_section
from ..config import Settings, get_settings
from ..errors import HarnessLabError
from ..harness.budget import BudgetExceeded, RunBudget
from ..harness.models import build_chat_model
from ..harness.policy import PolicyEngine
from ..knowledge.service import KnowledgeService
from ..observability.events import EventSink
from ..observability.logging import get_logger
from ..storage.models import (
    AgentAnswer,
    BudgetLimits,
    BudgetUsage,
    EventType,
    Plan,
    RunStatus,
    ToolOperationStatus,
)
from ..storage.repositories import Repository
from ..tools.base import ToolContext
from ..tools.gateway import ToolGateway
from ..tools.registry import build_tool_runtime
from ..tools.workspace import Workspace
from ..utils import estimate_tokens, iso, json_loads

logger = get_logger("executor")

CITATION_PATTERN = re.compile(r"\[(S\d+)\]")
JSON_BLOCK = re.compile(r"```json\s*(.*?)```", re.DOTALL)


class RunExecutor:
    def __init__(
        self,
        repo: Repository,
        settings: Settings | None = None,
        *,
        knowledge: KnowledgeService | None = None,
        agent_factory: RunAgentFactory | None = None,
        model_factory: Callable[[], BaseChatModel] | None = None,
    ) -> None:
        self.repo = repo
        self.settings = settings or get_settings()
        self.knowledge = knowledge or KnowledgeService(repo, self.settings)
        self.agent_factory = agent_factory or RunAgentFactory(self.settings)
        # 模型工厂可注入：控制流测试使用确定性替身，生产使用真实 Chat 模型
        self.model_factory = model_factory or (lambda: build_chat_model(self.settings))
        self.events = EventSink(repo)
        self.prompts = PromptLibrary()
        self.skills = SkillLibrary()

    # ------------------------------------------------------------------
    def execute(self, run_id: str) -> dict[str, Any]:
        """同步执行一次运行；由工作进程在独立线程中调用。"""
        run = self.repo.get_run(run_id)
        limits = BudgetLimits(**json_loads(run["budget_limits"], {}))
        usage = BudgetUsage(**json_loads(run["budget_usage"], {}))
        budget = RunBudget(self.repo, run_id, limits, usage, self.events)

        status = RunStatus(run["status"])
        if status.terminal:
            return run
        if self.repo.is_cancel_requested(run_id):
            return self._finish_cancelled(run_id, "取消信号在开始执行前已提交")

        resume_payload = json_loads(run["interrupt_payload"], None)
        recovering = status is RunStatus.RECOVERING
        self.repo.update_run(run_id, status=RunStatus.RUNNING.value, lease_expiry=None)
        self.events.emit(
            run_id,
            EventType.RUN_RECOVERING if recovering else EventType.RUN_STARTED,
            {"worker": run["lease_owner"], "resume": bool(resume_payload)},
        )

        try:
            return self._drive(run, budget, resume_payload=resume_payload, recovering=recovering)
        except BudgetExceeded as exc:
            return self._finish_failed(run_id, "BUDGET_EXCEEDED", exc.message, budget)
        except HarnessLabError as exc:
            return self._finish_failed(run_id, exc.code, exc.message, budget)
        except Exception as exc:
            logger.exception("运行执行失败")
            return self._finish_failed(
                run_id, "INTERNAL_ERROR", f"内部错误：{type(exc).__name__}", budget
            )

    # ------------------------------------------------------------------
    def _drive(
        self,
        run: dict[str, Any],
        budget: RunBudget,
        *,
        resume_payload: dict[str, Any] | None,
        recovering: bool,
    ) -> dict[str, Any]:
        run_id = run["id"]
        project_id = run["project_id"]
        thread_id = run["thread_id"]

        index_version = self._ensure_index(project_id)
        skills = self.skills.list_skills()
        plan = self._plan(run, budget)
        self.repo.set_thread_title_if_empty(thread_id, run["user_message"][:40])

        policy = PolicyEngine(sandbox_enabled=self.settings.sandbox_enabled)
        workspace = Workspace(
            self.settings.workspace_root / project_id / run_id,
            max_files=self.settings.workspace_max_files,
            max_file_bytes=self.settings.workspace_max_file_bytes,
        )
        ctx = ToolContext(
            run_id=run_id,
            project_id=project_id,
            thread_id=thread_id,
            workspace=workspace,
            budget=budget,
            policy=policy,
            repo=self.repo,
            knowledge=self.knowledge,
            events=self.events,
            index_version=index_version,
            skills=[skill.name for skill in skills],
            # 引用映射是运行状态的一部分：审批中断后恢复时从工具账本重建，
            # 否则模型在中断前引用的 [S1]…[Sn] 会在恢复后全部判为无效。
            citations=self._restore_citations(run_id),
        )
        gateway = ToolGateway(self.repo, self.settings, policy=policy, events=self.events)
        runtime = build_tool_runtime(ctx, gateway, self.settings)
        model = self.model_factory()

        snapshot = {
            "model": {
                "provider": self.settings.chat_provider,
                "model": self.settings.chat_model,
                "base_url": self.settings.chat_base_url,
            },
            "embedding": {
                "provider": self.settings.embedding_provider,
                "model": self.settings.embedding_model,
            },
            "index_version": index_version,
            "retrieval_mode": self.settings.retrieval_mode,
            "top_k": self.settings.retrieval_top_k,
            "prompt_version": self.prompts.get("system").version,
            "skills": [
                {"name": skill.name, "version": skill.version, "hash": skill.content_hash} for skill in skills
            ],
            "policy_version": "policy-v1",
            "tools": runtime.schema_snapshot(),
            "graph_version": "harnesslab-agent-v1",
        }
        from ..utils import json_dumps

        with self.repo.db.transaction() as conn:
            conn.execute(
                "UPDATE runs SET config_snapshot=?, updated_at=? WHERE id=?",
                (json_dumps(snapshot), iso(), run_id),
            )

        system_prompt = self._system_prompt(run, plan, skills)
        agent = self.agent_factory.build_agent(model, runtime.tools, system_prompt=system_prompt)

        config = {
            "configurable": {"thread_id": run_id},
            "recursion_limit": max(self.settings.budget_max_model_calls * 2 + 5, 25),
        }

        if resume_payload:
            from langgraph.types import Command

            graph_input: Any = Command(resume=resume_payload)
            self.repo.update_run(run_id, interrupt_payload=None)
        elif recovering:
            reconciled = self._reconcile_after_crash(run, workspace)
            if reconciled is not None:
                return reconciled
            graph_input = None
        else:
            graph_input = {"messages": [HumanMessage(content=self._user_prompt(run))]}

        if recovering:
            self._emit_recovery_events(run_id)

        return self._stream_graph(
            run, agent, graph_input, config, ctx, gateway, budget, plan, index_version
        )

    # ------------------------------------------------------------------
    def _stream_graph(
        self,
        run: dict[str, Any],
        agent: Any,
        graph_input: Any,
        config: dict[str, Any],
        ctx: ToolContext,
        gateway: ToolGateway,
        budget: RunBudget,
        plan: Plan,
        index_version: str,
    ) -> dict[str, Any]:
        run_id = run["id"]
        streamed = ""
        last_message: BaseMessage | None = None
        model_turns = 0
        started = time.perf_counter()

        try:
            for mode, payload in agent.stream(graph_input, config=config, stream_mode=["updates", "messages"]):
                if mode == "messages":
                    chunk, _metadata = payload
                    text = _chunk_text(chunk)
                    if text:
                        streamed += text
                        if len(streamed) % 120 < len(text) or text.endswith(("。", "\n")):
                            self.events.emit(
                                run_id, EventType.MESSAGE_DELTA, {"message_id": "final", "delta": text[-200:]}
                            )
                    continue

                if "__interrupt__" in payload:
                    interrupt_value = payload["__interrupt__"][0]
                    return self._handle_interrupt(run, agent, config, interrupt_value, budget)

                if "model" in payload:
                    model_turns += 1
                    budget.reserve_model_call(
                        input_tokens=min(estimate_tokens(streamed) + 500, 4000),
                        output_tokens=self.settings.chat_max_output_tokens // 4,
                    )
                    message = payload["model"].get("messages")
                    if isinstance(message, list) and message:
                        last_message = message[-1]
                    usage = getattr(last_message, "usage_metadata", None) or {}
                    if usage:
                        budget.settle_model_call(
                            actual_input=int(usage.get("input_tokens", 0)),
                            actual_output=int(usage.get("output_tokens", 0)),
                        )

                if self.repo.is_cancel_requested(run_id):
                    return self._finish_cancelled(run_id, "收到取消信号，已停止后续模型与工具调用")
        except BudgetExceeded:
            raise
        except HarnessLabError:
            raise
        except Exception as exc:
            logger.exception("Agent 图执行失败")
            raise HarnessLabError("TOOL_FAILED", f"Agent 执行失败：{type(exc).__name__}") from exc

        state = agent.get_state(config)
        final_text = _final_text(state, streamed)
        return self._complete(run_id, final_text, ctx, budget, plan, index_version, started, model_turns)

    # ------------------------------------------------------------------
    def _complete(
        self,
        run_id: str,
        final_text: str,
        ctx: ToolContext,
        budget: RunBudget,
        plan: Plan,
        index_version: str,
        started: float,
        model_turns: int,
    ) -> dict[str, Any]:
        answer, invalid = self._validate_output(final_text, ctx)
        for step in plan.steps:
            step.status = "done"
        artifacts = [
            {
                "artifact_id": item["id"],
                "title": item["title"],
                "relative_path": item["relative_path"],
                "media_type": item["media_type"],
                "size": item["size"],
                "download_url": f"/api/v1/artifacts/{item['id']}/download",
            }
            for item in self.repo.list_artifacts(run_id=run_id)
        ]
        if invalid:
            answer["limitations"].append(
                "引用了本次召回之外的标签，已从有效引用中剔除：" + ", ".join(invalid)
            )
        result = {
            **answer,
            "artifacts": artifacts,
            "plan": plan.model_dump(),
            "usage": {
                **budget.usage.model_dump(),
                "limits": budget.limits.model_dump(),
                "token_estimate_kind": "estimate",
                "note": "价格不可用时只展示调用量与未知费用，不显示为零",
            },
            "index_version": index_version,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "model_turns": model_turns,
            "replay_safe": True,
        }
        self.events.emit(
            run_id,
            EventType.MESSAGE_COMPLETED,
            {"message_id": "final", "text": answer["answer"], "task_status": answer["task_status"]},
        )
        self.events.emit(run_id, EventType.RUN_COMPLETED, {"status": "completed", "usage": result["usage"]})
        self.repo.update_run(
            run_id,
            status=RunStatus.COMPLETED.value,
            result=result,
            interrupt_payload=None,
            finished_at=iso(),
            lease_owner=None,
            lease_expiry=None,
        )
        return self.repo.get_run(run_id)

    def _validate_output(self, final_text: str, ctx: ToolContext) -> tuple[dict[str, Any], list[str]]:
        """结果通过 Schema 校验；失败时保留原始文本并标注限制（文档 03 第 6 节）。"""
        payload: dict[str, Any] = {}
        match = JSON_BLOCK.search(final_text or "")
        if match:
            from ..utils import json_loads as _loads

            parsed = _loads(match.group(1), None)
            if isinstance(parsed, dict):
                payload = parsed
        labels = payload.get("citations") or CITATION_PATTERN.findall(final_text or "")
        labels = list(dict.fromkeys(labels))
        # 引用有效性以本次授权召回为准：后端验证标签存在、属于当前项目与固定文档版本
        valid_labels = {f"S{index}" for index in range(1, len(ctx.citations) + 1)}
        invalid = [label for label in labels if label not in valid_labels]
        resolved = [item for item in ctx.citations if item["label"] in labels]
        if not payload:
            payload = {
                "answer": (final_text or "").strip() or "（模型没有返回内容）",
                "citations": labels,
                "artifacts": [],
                "limitations": [],
                "task_status": "completed",
            }
        else:
            payload.setdefault("answer", final_text)
            payload.setdefault("citations", labels)
            payload.setdefault("artifacts", [])
            payload.setdefault("limitations", [])
            payload.setdefault("task_status", "completed")
        try:
            answer = AgentAnswer.model_validate(
                {key: value for key, value in payload.items() if key in AgentAnswer.model_fields}
            )
            validated = True
        except Exception:
            validated = False
            answer = AgentAnswer(
                answer=payload.get("answer", final_text),
                citations=[],
                limitations=["输出未通过最终 Schema 校验，已返回原始文本与可用中间产物"],
                task_status="partial",
            )
        data = answer.model_dump()
        data["citations_resolved"] = resolved
        if not validated:
            data["output_validated"] = False
        else:
            data["output_validated"] = True
        if not ctx.citations:
            data["limitations"].append("本次没有检索到可用证据，结论仅代表模型输出，未经资料支持")
        if not labels:
            data["limitations"].append("答案没有给出引用标签，请按来源核对")
        return data, invalid

    # ------------------------------------------------------------------
    def _handle_interrupt(
        self, run: dict[str, Any], agent: Any, config: dict[str, Any], interrupt_value: Any, budget: RunBudget
    ) -> dict[str, Any]:
        run_id = run["id"]
        payload = getattr(interrupt_value, "value", interrupt_value)
        if not isinstance(payload, dict):  # pragma: no cover - 防御性
            payload = {"kind": "unknown", "arguments": {}}
        tool_name = payload.get("tool_name", "unknown")

        checkpoint_id = None
        try:
            state = agent.get_state(config)
            checkpoint_id = (state.config or {}).get("configurable", {}).get("checkpoint_id")
        except Exception:
            checkpoint_id = None

        approval = self.repo.pending_approval(run_id)
        if approval is None or approval["arguments_hash"] != payload.get("arguments_hash"):
            approval = self.repo.create_approval(
                run_id=run_id,
                project_id=run["project_id"],
                thread_id=run["thread_id"],
                tool_name=tool_name,
                arguments=payload.get("arguments", {}),
                expected_effect=payload.get("expected_effect", ""),
                kind=payload.get("kind", "tool_call"),
                tool_call_id=payload.get("tool_call_id"),
                checkpoint_id=checkpoint_id,
                ttl_hours=self.settings.approval_ttl_hours,
                revision=int(payload.get("revision", 1)),
            )
            self.events.emit(
                run_id,
                EventType.APPROVAL_REQUESTED,
                {
                    "approval_id": approval["id"],
                    "tool_name": tool_name,
                    "arguments": approval["arguments"],
                    "arguments_hash": approval["arguments_hash"],
                    "revision": approval["revision"],
                    "expected_effect": approval["expected_effect"],
                    "expires_at": approval["expires_at"],
                    "checkpoint_id": checkpoint_id,
                },
            )
        self.repo.update_run(
            run_id,
            status=RunStatus.WAITING_APPROVAL.value,
            interrupt_payload=dict(payload) | {"approval_id": approval["id"]},
            lease_owner=None,
            lease_expiry=None,
            budget_usage=budget.usage.model_dump(),
        )
        logger.info(
            "运行进入等待审批",
            extra={"extra_fields": {"run_id": run_id, "tool": tool_name, "approval_id": approval["id"]}},
        )
        return self.repo.get_run(run_id)

    # ------------------------------------------------------------------
    def _reconcile_after_crash(self, run: dict[str, Any], workspace: Workspace) -> dict[str, Any] | None:
        """恢复对账：结果不明的副作用必须先人工核对，不得把再次批准当作安全重试。"""
        report = self.repo.recovery_report(run["id"])
        self.events.emit(
            run["id"],
            EventType.RUN_RECOVERING,
            {
                "succeeded_operations": [op["id"] for op in report["succeeded"]],
                "uncertain_operations": [op["id"] for op in report["uncertain"] + report["executing"]],
                "reused": len(report["succeeded"]),
            },
        )
        uncertain = report["uncertain"] + report["executing"]
        if not uncertain:
            return None
        for operation in uncertain:
            self.repo.update_operation(
                operation["idempotency_key"], status=ToolOperationStatus.UNCERTAIN,
                error_code="SIDE_EFFECT_UNCERTAIN",
            )
        approval = self.repo.create_approval(
            run_id=run["id"],
            project_id=run["project_id"],
            thread_id=run["thread_id"],
            tool_name=uncertain[0]["tool_name"],
            arguments=uncertain[0]["request"],
            expected_effect="上一次副作用操作的结果无法确认，需要人工核对后再决定是否继续",
            kind="manual_reconcile",
            ttl_hours=self.settings.approval_ttl_hours,
        )
        self.events.emit(
            run["id"],
            EventType.APPROVAL_REQUESTED,
            {
                "approval_id": approval["id"],
                "kind": "manual_reconcile",
                "tool_name": approval["tool_name"],
                "arguments": approval["arguments"],
                "arguments_hash": approval["arguments_hash"],
                "revision": approval["revision"],
                "expected_effect": approval["expected_effect"],
                "expires_at": approval["expires_at"],
            },
        )
        self.repo.update_run(
            run["id"],
            status=RunStatus.WAITING_APPROVAL.value,
            interrupt_payload={"kind": "manual_reconcile", "approval_id": approval["id"]},
            lease_owner=None,
            lease_expiry=None,
        )
        return self.repo.get_run(run["id"])

    def _emit_recovery_events(self, run_id: str) -> None:
        events = self.repo.list_events(run_id)
        self.events.emit(run_id, EventType.RUN_RECOVERING, {"replayed_events": len(events)})

    # ------------------------------------------------------------------
    def _restore_citations(self, run_id: str) -> list[dict[str, Any]]:
        """从工具账本重建本次运行已产生的引用映射（文档 03 第 4 节 evidence_refs）。

        只在内存里保存引用会在审批中断/崩溃恢复后丢失，导致模型已经引用的 [S1]…[Sn]
        被判为「本次召回之外」。账本里的检索结果本身就是权威记录，因此按标签重建。
        """
        citations: list[dict[str, Any]] = []
        seen: set[str] = set()
        for operation in self.repo.list_operations(run_id):
            if operation["tool_name"] != "search_knowledge":
                continue
            result = operation.get("result")
            data = result.get("data") if isinstance(result, dict) else None
            if not isinstance(data, dict):
                continue
            for hit in data.get("hits", []):
                if not isinstance(hit, dict):
                    continue
                chunk_id = str(hit.get("chunk_id", ""))
                label = str(hit.get("label", ""))
                if not chunk_id or not label or chunk_id in seen:
                    continue
                seen.add(chunk_id)
                citations.append(
                    {
                        "label": label,
                        "chunk_id": chunk_id,
                        "document_version": hit.get("document_version"),
                        "source_title": hit.get("source_title", ""),
                        "heading_path": hit.get("heading_path", ""),
                        "page": hit.get("page"),
                        "snippet": str(hit.get("text", ""))[:400],
                        "score": hit.get("score"),
                    }
                )
        citations.sort(key=lambda item: int(item["label"][1:]) if item["label"][1:].isdigit() else 0)
        return citations

    def _ensure_index(self, project_id: str) -> str | None:
        try:
            manifest = self.knowledge.ensure_active_manifest(project_id)
        except HarnessLabError as exc:
            logger.warning("索引不可用", extra={"extra_fields": {"code": exc.code}})
            return None
        return manifest["id"] if manifest else None

    def _plan(self, run: dict[str, Any], budget: RunBudget) -> Plan:
        run_id = run["id"]
        model = self.model_factory()
        plan = self.agent_factory.generate_plan(model, run["user_message"], mode=run["mode"])
        steps = [
            {
                "id": step.id,
                "title": step.title,
                "status": step.status,
            }
            for step in plan.steps
        ]
        self.events.emit(
            run_id,
            EventType.PLAN_UPDATED,
            {"version": plan.version, "goal": plan.goal, "steps": steps,
             "note": plan_to_text(plan) and "计划只描述目标与步骤，不包含模型内部推理"},
        )
        self.repo.update_run(run_id, budget_usage=budget.usage.model_dump())
        return plan

    def _system_prompt(self, run: dict[str, Any], plan: Plan, skills: list[Any]) -> str:
        template = self.prompts.get("system")
        parts = [template.render()]
        if template.content == "":
            from ..harness.context import SYSTEM_RULES

            parts = [SYSTEM_RULES]
        skill_section = render_skill_section(skills)
        if skill_section:
            parts.append(skill_section)
        parts.append(f"当前模式：{run['mode']}；项目 ID 由服务端注入，你不应也无法修改它。")
        plan_text = plan_to_text(plan)
        if plan_text:
            parts.append(f"已确认的计划（仅描述目标与步骤）：\n{plan_text}")
        parts.append(
            "最终答复要求：给出结论、证据引用（[S1] 形式）与未知项。"
            "如需创建工单或写报告，请调用对应工具；需要审批的操作会被暂停等待用户决定。"
        )
        return "\n\n".join(parts)

    def _user_prompt(self, run: dict[str, Any]) -> str:
        return (
            f"用户目标：{run['user_message']}\n\n"
            "请先检索本项目资料，再基于证据回答；数值计算使用 calculator；"
            "证据不足时明确说明无法确认。"
        )

    # ------------------------------------------------------------------
    def _finish_cancelled(self, run_id: str, reason: str) -> dict[str, Any]:
        self.repo.cancel_pending_approvals(run_id)
        existing = self.repo.get_run(run_id)
        result = json_loads(existing["result"], None) or {}
        result = dict(result) | {
            "task_status": "partial",
            "cancelled_reason": reason,
            "note": "取消后保留已有答案、产物与已发生的操作；已提交的副作用不会被声称为撤回",
        }
        self.events.emit(run_id, EventType.RUN_CANCELLED, {"reason": reason, "result": result})
        self.repo.update_run(
            run_id,
            status=RunStatus.CANCELLED.value,
            result=result,
            finished_at=iso(),
            lease_owner=None,
            lease_expiry=None,
        )
        return self.repo.get_run(run_id)

    def _finish_failed(
        self, run_id: str, code: str, message: str, budget: RunBudget | None = None
    ) -> dict[str, Any]:
        self.repo.update_run(
            run_id,
            status=RunStatus.FAILED.value,
            error_code=code,
            error_message=message,
            budget_usage=budget.usage.model_dump() if budget else None,
            finished_at=iso(),
            lease_owner=None,
            lease_expiry=None,
        )
        self.events.emit(run_id, EventType.RUN_FAILED, {"error_code": code, "message": message})
        return self.repo.get_run(run_id)


def _chunk_text(chunk: Any) -> str:
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _final_text(state: Any, streamed: str) -> str:
    try:
        messages = state.values.get("messages", [])
    except Exception:
        return streamed
    for message in reversed(messages):
        has_text = isinstance(message.content, str) and bool(message.content.strip())
        if isinstance(message, AIMessage) and has_text and not message.tool_calls:
            return message.content
    return streamed
