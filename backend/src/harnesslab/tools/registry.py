"""工具注册表：把本地实现包装为 LangChain 工具，统一经网关执行（文档 03 第 7 节）。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Any

from langchain_core.tools import BaseTool, InjectedToolCallId, tool

from ..config import Settings, get_settings
from ..storage.models import ToolResult
from . import local
from .base import MAX_TOOL_OUTPUT_CHARS, ToolContext
from .gateway import ToolGateway


def _render(result: ToolResult) -> str:
    payload = result.model_dump()
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) > MAX_TOOL_OUTPUT_CHARS:
        payload["truncated"] = True
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("hits", "text", "artifacts"):
                if key in data and isinstance(data[key], (list, str)):
                    data[key] = data[key][:5] if isinstance(data[key], list) else data[key][
                        : MAX_TOOL_OUTPUT_CHARS // 2
                    ]
            payload["data"] = data
        payload["message"] = (payload.get("message") or "") + "（结果过大已截断，完整内容见运行详情）"
        result.truncated = True
        text = json.dumps(payload, ensure_ascii=False)
    return text


@dataclass
class ToolRuntime:
    tools: list[BaseTool]
    gateway: ToolGateway
    context: ToolContext

    def by_name(self, name: str) -> BaseTool | None:
        for item in self.tools:
            if item.name == name:
                return item
        return None

    def schema_snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "name": item.name,
                "description": (item.description or "").strip().split("\n")[0],
                "version": self.gateway.policy.decide(item.name).version,
                "risk": self.gateway.policy.decide(item.name).risk.value,
                "decision": self.gateway.policy.decide(item.name).decision.value,
                "input_schema": item.args_schema.model_json_schema() if item.args_schema else {},
            }
            for item in self.tools
        ]


def build_tool_runtime(
    ctx: ToolContext, gateway: ToolGateway, settings: Settings | None = None
) -> ToolRuntime:
    settings = settings or get_settings()

    def dispatch(name: str, arguments: dict[str, Any], tool_call_id: str) -> str:
        handlers = {
            "search_knowledge": lambda call: local.search_knowledge(ctx, **call.arguments),
            "read_source": lambda call: local.read_source(ctx, **call.arguments),
            "calculator": lambda call: local.calculator(ctx, **call.arguments),
            "list_artifacts": lambda call: local.list_artifacts(ctx),
            "read_artifact": lambda call: local.read_artifact(ctx, **call.arguments),
            "write_report": lambda call: local.write_report(ctx, **call.arguments),
            "create_demo_ticket": lambda call: _create_ticket(ctx, call),
        }
        result = gateway.execute(
            ctx, name, arguments, handler=handlers[name], tool_call_id=tool_call_id or None
        )
        return _render(result)

    def _create_ticket(context: ToolContext, call: Any) -> ToolResult:
        outcome = local.create_ticket_record(
            context, call.operation_id, call.arguments["title"], call.arguments["body"]
        )
        ticket = outcome["ticket"]
        return ToolResult(
            ok=True,
            data={
                "ticket_id": ticket["id"],
                "title": ticket["title"],
                "status": "created",
                "idempotent": True,
                "note": "本地模拟工单，不向真实外部系统发送内容",
            },
        )

    @tool("search_knowledge")
    def search_knowledge(
        query: str, top_k: int = 8, tool_call_id: Annotated[str, InjectedToolCallId] = ""
    ) -> str:
        """按授权索引检索当前项目资料，返回带 [S1] 编号的候选片段与定位信息。"""
        return dispatch("search_knowledge", {"query": query, "top_k": top_k}, tool_call_id)

    @tool("read_source")
    def read_source(chunk_id: str, tool_call_id: Annotated[str, InjectedToolCallId] = "") -> str:
        """读取某个片段 ID 对应的原文与位置（校验来源版本与访问权限）。"""
        return dispatch("read_source", {"chunk_id": chunk_id}, tool_call_id)

    @tool("calculator")
    def calculator(expression: str, tool_call_id: Annotated[str, InjectedToolCallId] = "") -> str:
        """对数学表达式求值（受限解释器，非 Python eval）。只做资料中明确给出的计算。"""
        return dispatch("calculator", {"expression": expression}, tool_call_id)

    @tool("list_artifacts")
    def list_artifacts(tool_call_id: Annotated[str, InjectedToolCallId] = "") -> str:
        """列出当前项目与本次运行已生成的产物（只读）。"""
        return dispatch("list_artifacts", {}, tool_call_id)

    @tool("read_artifact")
    def read_artifact(artifact_id: str, tool_call_id: Annotated[str, InjectedToolCallId] = "") -> str:
        """按 ID 读取授权工作区内的产物内容。"""
        return dispatch("read_artifact", {"artifact_id": artifact_id}, tool_call_id)

    @tool("write_report")
    def write_report(
        title: str,
        content_markdown: str,
        filename: str = "",
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> str:
        """在当前 run 的受限工作区新建 Markdown 报告。默认不覆盖同名产物。"""
        return dispatch(
            "write_report",
            {"title": title, "content_markdown": content_markdown, "filename": filename},
            tool_call_id,
        )

    @tool("create_demo_ticket")
    def create_demo_ticket(
        title: str, body: str, tool_call_id: Annotated[str, InjectedToolCallId] = ""
    ) -> str:
        """创建本地模拟工单（需要人工审批；重复批准不会产生第二份工单）。"""
        return dispatch("create_demo_ticket", {"title": title, "body": body}, tool_call_id)

    tools: list[BaseTool] = [
        search_knowledge,
        read_source,
        calculator,
        list_artifacts,
        read_artifact,
        write_report,
        create_demo_ticket,
    ]
    allowed = ctx.policy.allowed_tools
    if allowed is not None:
        tools = [item for item in tools if item.name in allowed]
    if not settings.sandbox_enabled:
        tools = [item for item in tools if item.name != "run_python_sandbox"]
    return ToolRuntime(tools=tools, gateway=gateway, context=ctx)
