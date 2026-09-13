"""模型路由与能力探测（文档 03 第 6 节 / 04 第 4 节）。

模型档案记录流式输出、工具调用、结构化输出、上下文窗口、并发上限；
只有通过实测的能力才能被启用。本地失败后默认报错，不自动把私有上下文发往云端。
"""

from __future__ import annotations

from typing import Any

import httpx
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from ..config import Settings, get_settings
from ..errors import HarnessLabError
from ..observability.logging import get_logger

logger = get_logger("models")

STUB_NOTICE = "替身模型：未通过能力探测，仅用于界面与契约演示，不代表真实模型成绩"


class StubChatModel(BaseChatModel):
    """确定性替身，必须显式标注；用于无 Chat 模型时的降级演示与测试。"""

    canned: tuple[str, ...] = ()

    @property
    def _llm_type(self) -> str:
        return "harnesslab-stub"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if not self.canned:
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content=f"（{STUB_NOTICE}）"))]
            )
        answered = sum(1 for message in messages if isinstance(message, AIMessage) and message.content)
        index = min(answered, len(self.canned) - 1)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.canned[index]))])

    def bind_tools(self, tools: Any, **kwargs: Any) -> StubChatModel:
        return self


def build_chat_model(settings: Settings | None = None, *, streaming: bool = True) -> BaseChatModel:
    settings = settings or get_settings()
    if settings.chat_provider == "stub":
        return StubChatModel()
    if settings.chat_provider == "openai_compatible":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=settings.chat_model,
            base_url=settings.chat_base_url,
            api_key=settings.chat_api_key or "not-needed",
            temperature=settings.chat_temperature,
            timeout=120,
            max_retries=1,
            streaming=streaming,
        )
    raise HarnessLabError("MODEL_CAPABILITY_MISSING", f"未支持的 CHAT_PROVIDER={settings.chat_provider}")


def probe_chat_capabilities(settings: Settings | None = None) -> dict[str, Any]:
    """实测 Chat 能力：模型列表 → 基础对话 → 工具调用 → 流式。"""
    settings = settings or get_settings()
    result: dict[str, Any] = {
        "provider": settings.chat_provider,
        "base_url": settings.chat_base_url,
        "model": settings.chat_model,
        "capabilities": {
            "chat": False,
            "streaming": False,
            "tool_calling": False,
            "structured_output": False,
        },
        "notes": [],
        "status": "unknown",
    }
    if settings.chat_provider == "stub":
        result.update(status="stub", notes=[STUB_NOTICE])
        return result

    headers = {"Content-Type": "application/json"}
    if settings.chat_api_key:
        headers["Authorization"] = f"Bearer {settings.chat_api_key}"
    base = settings.chat_base_url.rstrip("/")
    try:
        with httpx.Client(base_url=base, timeout=30.0, headers=headers) as client:
            models = client.get("/models")
            if models.status_code == 401:
                result["notes"].append("认证失败：请检查 CHAT_API_KEY")
                result["status"] = "unauthorized"
                return result
            models.raise_for_status()
            ids = [item.get("id", "") for item in models.json().get("data", [])]
            result["available_models"] = ids
            if settings.chat_model not in ids:
                result["notes"].append("模型 ID 不在服务列表中，请核对真实模型标识")
            payload = {
                "model": settings.chat_model,
                "messages": [{"role": "user", "content": "回复“ok”"}],
                "max_tokens": 16,
            }
            response = client.post("/chat/completions", json=payload)
            if response.status_code >= 400:
                result["notes"].append(f"基础对话失败：HTTP {response.status_code}")
                result["status"] = "failed"
                return result
            result["capabilities"]["chat"] = True

            tool_payload = {
                "model": settings.chat_model,
                "messages": [{"role": "user", "content": "请调用工具查询两个数字的和：1 和 2"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "calculator",
                            "description": "计算数学表达式",
                            "parameters": {
                                "type": "object",
                                "properties": {"expression": {"type": "string"}},
                                "required": ["expression"],
                            },
                        },
                    }
                ],
                "tool_choice": "auto",
                "max_tokens": 128,
            }
            tool_response = client.post("/chat/completions", json=tool_payload)
            if tool_response.status_code < 400:
                message = tool_response.json()["choices"][0]["message"]
                calls = message.get("tool_calls") or []
                result["capabilities"]["tool_calling"] = bool(calls)
                if not calls:
                    result["notes"].append("模型未返回工具调用，Agent 将无法主动使用工具")
            else:
                result["notes"].append("工具调用探测请求失败")

            stream_payload = dict(payload) | {"stream": True}
            with client.stream("POST", "/chat/completions", json=stream_payload) as stream:
                result["capabilities"]["streaming"] = stream.status_code < 400
    except httpx.HTTPError as exc:
        result["status"] = "unavailable"
        result["notes"].append(f"服务不可用：{type(exc).__name__}")
        return result

    result["capabilities"]["structured_output"] = result["capabilities"]["tool_calling"]
    result["status"] = "ok" if result["capabilities"]["chat"] else "failed"
    if not result["capabilities"]["tool_calling"]:
        result["notes"].append("缺少工具调用能力：仅能使用受限对话模式")
    return result
