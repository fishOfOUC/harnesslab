"""工具网关（文档 10 第 2 节策略顺序 / 03 第 7 节契约 / 05 第 2 节一致性）。

顺序：校验工具存在及版本 → 校验输入 → 注入身份/项目作用域 → 校验资源 →
风险分类 → 校验预算 → 审批 → 执行前重检 → 执行及审计。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, get_settings
from ..errors import HarnessLabError
from ..harness.policy import Decision, PolicyEngine, RiskLevel
from ..observability.events import EventSink
from ..observability.logging import get_logger
from ..storage.models import EventType, ToolOperationStatus, ToolResult
from ..storage.repositories import Repository
from ..utils import hash_payload
from .base import ToolContext

logger = get_logger("gateway")


@dataclass
class EffectiveCall:
    """网关确定的执行参数：操作 ID、幂等键与参数版本。"""

    operation_id: str
    idempotency_key: str
    arguments: dict[str, Any]
    approval_revision: int = 0
    tool_version: str = "1.0.0"
    reused: bool = False


Handler = Callable[[EffectiveCall], ToolResult]

RECEIPT_KEYS = ("ticket", "ticket_id", "artifact_id", "id")


def _external_receipt(result: ToolResult) -> str | None:
    """记录外部回执（如工单 ID / 产物 ID），供恢复对账使用。"""
    data = result.data
    if not isinstance(data, dict):
        return None
    for key in RECEIPT_KEYS:
        value = data.get(key)
        if isinstance(value, dict) and value.get("id"):
            return str(value["id"])
        if isinstance(value, str) and value:
            return value
    return result.artifact_ref


@dataclass
class GatewayOutcome:
    result: ToolResult
    approval_revision: int = 0
    reused: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class ToolGateway:
    def __init__(
        self,
        repo: Repository,
        settings: Settings | None = None,
        *,
        policy: PolicyEngine | None = None,
        events: EventSink | None = None,
    ) -> None:
        self.repo = repo
        self.settings = settings or get_settings()
        self.policy = policy or PolicyEngine(sandbox_enabled=self.settings.sandbox_enabled)
        self.events = events or EventSink(repo)

    # ------------------------------------------------------------------
    def execute(
        self,
        ctx: ToolContext,
        name: str,
        arguments: dict[str, Any],
        *,
        handler: Handler,
        tool_call_id: str | None = None,
        tool_version: str | None = None,
    ) -> ToolResult:
        started = time.perf_counter()
        policy = self.policy.decide(name, tool_version=tool_version)
        if policy.decision is Decision.DENY:
            outcome = ToolResult(
                ok=False,
                error_code="TOOL_NOT_FOUND" if name not in self.policy.policies else "FORBIDDEN",
                message=policy.reason,
                retryable=False,
            )
            self._emit_completed(ctx, name, tool_call_id, outcome, started)
            return outcome

        # 项目作用域来自服务端上下文；参数里的 project_id 一律忽略
        arguments = {k: v for k, v in arguments.items() if k != "project_id"}
        ctx.budget.reserve_tool_call()
        signature = f"{name}:{hash_payload(arguments)}"

        approval_revision = 0
        if policy.decision is Decision.REQUIRE_APPROVAL:
            resolution = self._request_approval(ctx, name, policy.version, arguments, tool_call_id)
            if resolution.get("decision") != "approve":
                outcome = ToolResult(
                    ok=False,
                    error_code="APPROVAL_REJECTED",
                    message="用户拒绝了该操作，Agent 可继续完成其他步骤",
                    retryable=False,
                )
                self._emit_completed(ctx, name, tool_call_id, outcome, started)
                return outcome
            approval_revision = int(resolution.get("revision", 1))
            if isinstance(resolution.get("arguments"), dict) and resolution["arguments"]:
                arguments = resolution["arguments"]

        logical_action_id = tool_call_id or f"{name}:{hash_payload(arguments)}"
        idempotency_key = hash_payload(
            {
                "run_id": ctx.run_id,
                "logical_action_id": logical_action_id,
                "tool_version": policy.version,
                "approval_revision": approval_revision,
            }
        )

        existing = self.repo.find_operation(idempotency_key)
        if existing is not None:
            outcome = self._reuse_or_flag(ctx, name, existing, tool_call_id, started)
            if outcome is not None:
                return outcome.result

        operation = self.repo.create_operation(
            run_id=ctx.run_id,
            project_id=ctx.project_id,
            logical_action_id=logical_action_id,
            tool_name=name,
            tool_version=policy.version,
            idempotency_key=idempotency_key,
            request=arguments,
            approval_revision=approval_revision,
        )
        ctx.budget.observe_tool_signature(signature)
        self.events.emit(
            ctx.run_id,
            EventType.TOOL_STARTED,
            {
                "tool_call_id": tool_call_id,
                "name": name,
                "tool_version": policy.version,
                "risk": policy.risk.value,
                "arguments": arguments,
                "operation_id": operation["id"],
            },
        )
        self.repo.update_operation(idempotency_key, status=ToolOperationStatus.EXECUTING)

        call = EffectiveCall(
            operation_id=operation["id"],
            idempotency_key=idempotency_key,
            arguments=arguments,
            approval_revision=approval_revision,
            tool_version=policy.version,
        )
        # 执行前重检取消信号与非幂等边界
        if self.repo.is_cancel_requested(ctx.run_id) and policy.risk is RiskLevel.SIDE_EFFECT:
            result = ToolResult(ok=False, error_code="CANCELLED", message="运行已取消，未执行副作用操作")
            self.repo.update_operation(idempotency_key, status=ToolOperationStatus.FAILED,
                                       error_code="CANCELLED", result=result.model_dump())
            self._emit_completed(ctx, name, tool_call_id, result, started)
            return result

        try:
            result = self._run_with_timeout(handler, call, policy.timeout_seconds)
        except HarnessLabError as exc:
            status = (
                ToolOperationStatus.UNCERTAIN
                if policy.risk is RiskLevel.SIDE_EFFECT and exc.code in {"TOOL_TIMEOUT", "SIDE_EFFECT_UNCERTAIN"}
                else ToolOperationStatus.FAILED
            )
            self.repo.update_operation(
                idempotency_key, status=status, error_code=exc.code,
                result=ToolResult(ok=False, error_code=exc.code, message=exc.message,
                                  retryable=exc.retryable).model_dump(),
            )
            result = ToolResult(ok=False, error_code=exc.code, message=exc.message, retryable=exc.retryable)
        except Exception as exc:
            logger.warning(
                "工具执行异常", extra={"extra_fields": {"tool": name, "error": type(exc).__name__}}
            )
            self.repo.update_operation(
                idempotency_key, status=ToolOperationStatus.FAILED, error_code="TOOL_FAILED",
                result=ToolResult(ok=False, error_code="TOOL_FAILED",
                                  message=f"工具执行失败：{type(exc).__name__}").model_dump(),
            )
            result = ToolResult(ok=False, error_code="TOOL_FAILED", message="工具执行失败，请查看运行详情")
        else:
            status = ToolOperationStatus.SUCCEEDED if result.ok else ToolOperationStatus.FAILED
            self.repo.update_operation(
                idempotency_key,
                status=status,
                result=result.model_dump(),
                result_ref=result.artifact_ref,
                error_code=result.error_code,
                external_receipt=_external_receipt(result),
            )

        result.duration_ms = int((time.perf_counter() - started) * 1000)
        self._emit_completed(ctx, name, tool_call_id, result, started)
        return result

    # ------------------------------------------------------------------
    def _reuse_or_flag(
        self,
        ctx: ToolContext,
        name: str,
        existing: dict[str, Any],
        tool_call_id: str | None,
        started: float,
    ) -> GatewayOutcome | None:
        """恢复时的对账：已成功直接复用；结果不明进入人工核对，不自动重试。"""
        status = existing["status"]
        if status == ToolOperationStatus.SUCCEEDED.value:
            stored = existing.get("result") or {}
            result = ToolResult(
                ok=True,
                data=(stored.get("data") if isinstance(stored, dict) else None),
                reused=True,
                message="复用已持久化的操作结果（幂等键命中）",
            )
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            self._emit_completed(ctx, name, tool_call_id, result, started, reused=True)
            return GatewayOutcome(result=result, reused=True)
        if status in {ToolOperationStatus.UNCERTAIN.value, ToolOperationStatus.EXECUTING.value,
                      ToolOperationStatus.PREPARED.value}:
            self.repo.update_operation(existing["idempotency_key"], status=ToolOperationStatus.UNCERTAIN,
                                       error_code="SIDE_EFFECT_UNCERTAIN")
            result = ToolResult(
                ok=False,
                error_code="SIDE_EFFECT_UNCERTAIN",
                message="该操作结果不确定，已进入人工核对，不会自动重复执行",
                retryable=False,
            )
            self._emit_completed(ctx, name, tool_call_id, result, started)
            return GatewayOutcome(result=result)
        return None

    def _run_with_timeout(self, handler: Handler, call: EffectiveCall, timeout_seconds: int) -> ToolResult:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(handler, call)
            try:
                return future.result(timeout=timeout_seconds)
            except FutureTimeout as exc:
                future.cancel()
                raise HarnessLabError(
                    "TOOL_TIMEOUT", f"工具执行超过 {timeout_seconds} 秒", retryable=True
                ) from exc

    def _request_approval(
        self,
        ctx: ToolContext,
        name: str,
        tool_version: str,
        arguments: dict[str, Any],
        tool_call_id: str | None,
    ) -> dict[str, Any]:
        from langgraph.types import interrupt

        payload = {
            "kind": "tool_approval",
            "tool_name": name,
            "tool_version": tool_version,
            "arguments": arguments,
            "arguments_hash": hash_payload(arguments),
            "expected_effect": self.policy.decide(name, tool_version=tool_version).reason,
            "tool_call_id": tool_call_id,
            "revision": 1,
        }
        logger.info(
            "请求人工审批",
            extra={"extra_fields": {"run_id": ctx.run_id, "tool": name, "tool_call_id": tool_call_id}},
        )
        resolution = interrupt(payload)
        if not isinstance(resolution, dict):  # pragma: no cover - 防御性
            return {"decision": "reject"}
        return resolution

    def _emit_completed(
        self,
        ctx: ToolContext,
        name: str,
        tool_call_id: str | None,
        result: ToolResult,
        started: float,
        *,
        reused: bool = False,
    ) -> None:
        duration = result.duration_ms or int((time.perf_counter() - started) * 1000)
        self.events.emit(
            ctx.run_id,
            EventType.TOOL_COMPLETED,
            {
                "tool_call_id": tool_call_id,
                "name": name,
                "ok": result.ok,
                "error_code": result.error_code,
                "duration_ms": duration,
                "truncated": result.truncated,
                "reused": reused,
                "message": result.message[:300],
            },
        )
