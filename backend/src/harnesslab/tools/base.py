"""工具运行时上下文与通用结构（文档 03 第 7 节）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..harness.budget import RunBudget
from ..harness.policy import PolicyEngine
from ..knowledge.service import KnowledgeService
from ..observability.events import EventSink
from ..storage.repositories import Repository
from .workspace import Workspace


@dataclass
class ToolContext:
    """单次 run 的工具执行上下文；项目作用域来自服务端，不接受模型传入。"""

    run_id: str
    project_id: str
    thread_id: str
    workspace: Workspace
    budget: RunBudget
    policy: PolicyEngine
    repo: Repository
    knowledge: KnowledgeService
    events: EventSink
    citations: list[dict[str, Any]] = field(default_factory=list)
    index_version: str | None = None
    skills: list[str] = field(default_factory=list)
    depth: int = 0


MAX_TOOL_OUTPUT_CHARS = 6000
