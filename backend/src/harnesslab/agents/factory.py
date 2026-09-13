"""Agent 工厂与检查点接线（文档 02 第 1 节 / 03 第 1、11 节）。

基础 Agent 复用框架的 `create_agent`；确定性业务流（队列、租约、状态机、审批与
恢复对账）由 runtime 层显式实现，避免内层与外层同时维护互相冲突的模型/工具循环。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from ..config import Settings, get_settings
from ..observability.logging import get_logger
from ..storage.models import Plan, PlanDraft, PlanStep
from ..utils import new_id

logger = get_logger("agents")

MAX_PLAN_STEPS = 12


def build_checkpointer(settings: Settings | None = None):
    """文件持久检查点：绑定稳定 thread ID 与图版本（文档 03 第 8 节）。"""
    settings = settings or get_settings()
    from langgraph.checkpoint.sqlite import SqliteSaver

    connection = sqlite3.connect(settings.checkpoint_db_path, check_same_thread=False)
    saver = SqliteSaver(connection)
    saver.setup()
    return saver


class RunAgentFactory:
    def __init__(self, settings: Settings | None = None, checkpointer: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self._checkpointer = checkpointer

    @property
    def checkpointer(self) -> Any:
        if self._checkpointer is None:
            self._checkpointer = build_checkpointer(self.settings)
        return self._checkpointer

    # ------------------------------------------------------------------
    def build_agent(
        self,
        model: BaseChatModel,
        tools: list[BaseTool],
        *,
        system_prompt: str,
    ):
        """返回带持久检查点的 Agent 图。

        审批由工具网关在工具函数内部通过 `interrupt()` 发起：业务层需要把审批载荷
        （参数哈希、修订版本、checkpoint_id）落到业务表，因此不使用框架中间件的通用中断，
        避免出现两套互不知道的审批记录。
        """
        from langchain.agents import create_agent

        return create_agent(
            model=model,
            tools=tools,
            system_prompt=system_prompt,
            checkpointer=self.checkpointer,
            name="harnesslab-agent",
        )

    # ------------------------------------------------------------------
    def generate_plan(self, model: BaseChatModel, goal: str, *, mode: str = "research") -> Plan:
        """有限规模的可编辑步骤清单；失败时退化为单步计划，不阻塞运行。"""
        if mode == "chat":
            return Plan(goal=goal, steps=[])
        try:
            structured = model.with_structured_output(PlanDraft)
            draft = structured.invoke(
                "把下面的目标拆成可验证的步骤（最多 12 步，只描述目标与动作，不要输出内部推理）：\n"
                f"{goal}"
            )
            steps = [
                PlanStep(id=f"step-{index}", title=title.strip())
                for index, title in enumerate(draft.steps[:MAX_PLAN_STEPS], start=1)
                if title and title.strip()
            ]
            return Plan(goal=goal or draft.goal, steps=steps)
        except Exception as exc:
            logger.warning("计划生成失败，退化为单步计划", extra={"extra_fields": {"error": type(exc).__name__}})
            return Plan(goal=goal, steps=[PlanStep(id="step-1", title="检索资料并给出带引用的回答")])


def new_plan_step(title: str) -> PlanStep:
    return PlanStep(id=f"step-{new_id()[:8]}", title=title)


def plan_to_text(plan: Plan | None) -> str:
    if plan is None or not plan.steps:
        return ""
    lines = [f"版本 v{plan.version}｜目标：{plan.goal}"]
    for step in plan.steps:
        mark = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]", "failed": "[!]", "skipped": "[-]"}[
            step.status
        ]
        lines.append(f"{mark} {step.id} {step.title}{('｜' + step.note) if step.note else ''}")
    return "\n".join(lines)
