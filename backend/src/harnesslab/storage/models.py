"""核心实体与枚举（文档 05 第 1 节 / 03 第 2 节）。"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    RECOVERING = "recovering"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}

    @property
    def active(self) -> bool:
        return not self.terminal


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    CANCELLED = "cancelled"


class ToolOperationStatus(StrEnum):
    PREPARED = "prepared"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class ParseStatus(StrEnum):
    PENDING = "pending"
    PARSING = "parsing"
    STAGING = "staging"
    READY = "ready"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    DELETED = "deleted"


class IndexStatus(StrEnum):
    BUILDING = "building"
    ACTIVE = "active"
    RETIRED = "retired"


class MemoryStatus(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    DELETED = "deleted"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class EventType(StrEnum):
    RUN_QUEUED = "run.queued"
    RUN_STARTED = "run.started"
    PLAN_UPDATED = "plan.updated"
    MESSAGE_DELTA = "message.delta"
    MESSAGE_COMPLETED = "message.completed"
    RETRIEVAL_COMPLETED = "retrieval.completed"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    ARTIFACT_CREATED = "artifact.created"
    BUDGET_UPDATED = "budget.updated"
    RUN_RECOVERING = "run.recovering"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"


class BudgetLimits(BaseModel):
    """预算上限；产品配置，不等同框架 recursion limit。"""

    max_model_calls: int = 20
    max_tool_calls: int = 30
    active_timeout_seconds: int = 180
    tool_timeout_seconds: int = 30
    max_retries: int = 2
    max_replans: int = 2
    max_subtasks: int = 4
    max_depth: int = 1
    input_tokens: int = 60000
    output_tokens: int = 12000


class BudgetUsage(BaseModel):
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    retries: int = 0
    replans: int = 0
    elapsed_seconds: float = 0.0
    cost_status: Literal["unknown", "estimated"] = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class PlanStep(BaseModel):
    id: str
    title: str
    status: Literal["pending", "in_progress", "done", "failed", "skipped"] = "pending"
    note: str = ""


class Plan(BaseModel):
    version: int = 1
    goal: str = ""
    steps: list[PlanStep] = Field(default_factory=list)
    replan_count: int = 0


class Citation(BaseModel):
    label: str
    chunk_id: str
    document_id: str
    document_version: int
    source_title: str
    heading_path: str = ""
    page: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    score: float | None = None
    snippet: str = ""


class Findings(BaseModel):
    """多 Agent 子任务协议（文档 03 第 11 节）。"""

    findings: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)


class AgentAnswer(BaseModel):
    """最终响应 Schema（文档 03 第 6 节）。"""

    answer: str
    citations: list[str] = Field(default_factory=list, description="引用的 [S1] 类标签")
    artifacts: list[str] = Field(default_factory=list, description="产物 ID")
    limitations: list[str] = Field(default_factory=list)
    task_status: Literal["completed", "partial", "needs_input"] = "completed"


class PlanDraft(BaseModel):
    goal: str
    steps: list[str] = Field(default_factory=list, description="最多 12 步")


class ToolResult(BaseModel):
    """工具统一返回结构（文档 03 第 7 节）。"""

    ok: bool
    data: Any = None
    error_code: str | None = None
    retryable: bool = False
    artifact_ref: str | None = None
    truncated: bool = False
    duration_ms: int = 0
    message: str = ""
    reused: bool = False
