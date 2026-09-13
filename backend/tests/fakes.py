"""确定性替身模型（文档 07 第 1 节：控制行为用替身复现，模型质量用真实模型）。

替身只用于控制流、审批、幂等与恢复测试；任何替身结果都不得作为真实模型成绩。
"""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class ScriptedChatModel(BaseChatModel):
    """按脚本返回工具调用或最终答复，用于可复现地驱动 Agent 图。"""

    script: tuple[dict[str, Any], ...] = ()
    calls: list[list[BaseMessage]] = []

    @property
    def _llm_type(self) -> str:
        return "harnesslab-scripted"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        answered = sum(1 for message in messages if isinstance(message, AIMessage))
        index = min(answered, len(self.script) - 1)
        step = self.script[index] if self.script else {"content": "（无脚本）"}
        self.calls.append(list(messages))
        if step.get("tool_calls"):
            message = AIMessage(content="", tool_calls=step["tool_calls"])
        else:
            message = AIMessage(content=step.get("content", ""))
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChatModel:
        return self


def tool_call(call_id: str, name: str, **args: Any) -> dict[str, Any]:
    return {"tool_calls": [{"name": name, "args": args, "id": call_id, "type": "tool_call"}]}


def final(content: str) -> dict[str, Any]:
    return {"content": content}
