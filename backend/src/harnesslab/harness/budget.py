"""预算、重试与循环检测（文档 03 第 9 节）。

产品配置，不等同框架 recursion limit；耗尽后停止并保留已有产物。
"""

from __future__ import annotations

import time
from typing import Any

from ..errors import HarnessLabError
from ..observability.events import EventSink
from ..storage.models import BudgetLimits, BudgetUsage, EventType
from ..storage.repositories import Repository


class BudgetExceeded(HarnessLabError):
    def __init__(self, reason: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("BUDGET_EXCEEDED", f"预算耗尽：{reason}", details=details)
        self.reason = reason


class LoopDetected(HarnessLabError):
    def __init__(self, signature: str) -> None:
        super().__init__("TOOL_FAILED", f"连续三次相同调用没有新结果，已触发循环检测：{signature}")


class RunBudget:
    """父子任务共享总预算；每次调用按最坏输出量预留，结束后结算。"""

    def __init__(
        self,
        repo: Repository,
        run_id: str,
        limits: BudgetLimits,
        usage: BudgetUsage | None = None,
        events: EventSink | None = None,
    ) -> None:
        self.repo = repo
        self.run_id = run_id
        self.limits = limits
        self.usage = usage or BudgetUsage()
        self.events = events
        self._started = time.monotonic()
        self._recent_signatures: list[str] = []

    # ------------------------------------------------------------------
    def _persist(self) -> None:
        self.usage.elapsed_seconds = round(self._elapsed(), 3)
        self.repo.update_run(self.run_id, budget_usage=self.usage.model_dump())
        if self.events is not None:
            self.events.emit(
                self.run_id,
                EventType.BUDGET_UPDATED,
                {"usage": self.usage.model_dump(), "limits": self.limits.model_dump()},
            )

    def _elapsed(self) -> float:
        return time.monotonic() - self._started

    def snapshot(self) -> dict[str, Any]:
        self.usage.elapsed_seconds = round(self._elapsed(), 3)
        remaining_ratio = 1.0 - (self.usage.input_tokens / max(self.limits.input_tokens, 1))
        return {
            "limits": self.limits.model_dump(),
            "usage": self.usage.model_dump(),
            "remaining_input_ratio": round(max(remaining_ratio, 0.0), 4),
            "exhausted": [],
        }

    # ------------------------------------------------------------------
    def check_active_time(self) -> None:
        if self._elapsed() > self.limits.active_timeout_seconds:
            raise BudgetExceeded(
                "活跃运行时长超限",
                {"elapsed_seconds": round(self._elapsed(), 2), "limit": self.limits.active_timeout_seconds},
            )

    def reserve_model_call(self, input_tokens: int, output_tokens: int) -> None:
        self.check_active_time()
        if self.usage.model_calls + 1 > self.limits.max_model_calls:
            raise BudgetExceeded("模型调用次数超限", {"limit": self.limits.max_model_calls})
        if self.usage.input_tokens + input_tokens > self.limits.input_tokens:
            raise BudgetExceeded("输入 token 预算超限", {"limit": self.limits.input_tokens})
        if self.usage.output_tokens + output_tokens > self.limits.output_tokens:
            raise BudgetExceeded("输出 token 预算超限", {"limit": self.limits.output_tokens})
        self.usage.model_calls += 1
        self.usage.input_tokens += input_tokens
        self.usage.output_tokens += output_tokens
        self._persist()

    def settle_model_call(self, *, actual_input: int, actual_output: int) -> None:
        """按真实用量结算，避免最坏预留长期占用额度。"""
        self.usage.input_tokens = max(self.usage.input_tokens - actual_input, 0) + actual_input
        self.usage.output_tokens = max(self.usage.output_tokens - actual_output, 0) + actual_output
        self._persist()

    def reserve_tool_call(self) -> None:
        self.check_active_time()
        if self.usage.tool_calls + 1 > self.limits.max_tool_calls:
            raise BudgetExceeded("工具调用次数超限", {"limit": self.limits.max_tool_calls})
        self.usage.tool_calls += 1
        self._persist()

    def record_retry(self) -> None:
        if self.usage.retries + 1 > self.limits.max_retries:
            raise BudgetExceeded("同类失败重试次数超限", {"limit": self.limits.max_retries})
        self.usage.retries += 1
        self._persist()

    def record_replan(self) -> None:
        if self.usage.replans + 1 > self.limits.max_replans:
            raise BudgetExceeded("重规划次数超限", {"limit": self.limits.max_replans})
        self.usage.replans += 1
        self._persist()

    # ------------------------------------------------------------------
    def observe_tool_signature(self, signature: str) -> None:
        """相同工具与参数连续三次没有新结果则触发循环检测。"""
        self._recent_signatures.append(signature)
        if len(self._recent_signatures) > 3:
            self._recent_signatures.pop(0)
        if len(self._recent_signatures) == 3 and len(set(self._recent_signatures)) == 1:
            raise LoopDetected(signature)
