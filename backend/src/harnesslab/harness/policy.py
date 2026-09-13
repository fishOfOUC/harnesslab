"""策略执行（文档 10 第 2 节）。

顺序：校验工具存在及版本 → 校验输入 → 注入身份/项目作用域 → 校验资源 →
风险分类 → 校验预算 → 审批 → 执行前重检 → 执行及审计。
真实 effect 与权限来自服务端注册表，不采信模型或第三方工具自行声明。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RiskLevel(StrEnum):
    READ = "read"
    WRITE_LOCAL = "write_local"
    SIDE_EFFECT = "side_effect"
    FORBIDDEN = "forbidden"


class Decision(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass
class ToolPolicy:
    name: str
    version: str
    risk: RiskLevel
    decision: Decision
    reason: str
    timeout_seconds: int = 30
    max_output_bytes: int = 20_000
    idempotent: bool = True


DEFAULT_POLICIES: dict[str, ToolPolicy] = {
    "search_knowledge": ToolPolicy(
        "search_knowledge", "1.0.0", RiskLevel.READ, Decision.ALLOW, "本项目知识读取，自动允许"
    ),
    "read_source": ToolPolicy(
        "read_source", "1.0.0", RiskLevel.READ, Decision.ALLOW, "本项目原文读取，自动允许"
    ),
    "calculator": ToolPolicy(
        "calculator", "1.0.0", RiskLevel.READ, Decision.ALLOW, "受限表达式解释器，无副作用"
    ),
    "list_artifacts": ToolPolicy(
        "list_artifacts", "1.0.0", RiskLevel.READ, Decision.ALLOW, "读取当前项目产物索引"
    ),
    "read_artifact": ToolPolicy(
        "read_artifact", "1.0.0", RiskLevel.READ, Decision.ALLOW, "按 ID 读取授权产物"
    ),
    "write_report": ToolPolicy(
        "write_report", "1.0.0", RiskLevel.WRITE_LOCAL, Decision.ALLOW, "只能写当前 run 工作区，默认不覆盖"
    ),
    "create_demo_ticket": ToolPolicy(
        "create_demo_ticket", "1.0.0", RiskLevel.SIDE_EFFECT, Decision.REQUIRE_APPROVAL,
        "创建模拟工单，需人工审批并绑定参数版本",
    ),
    "run_python_sandbox": ToolPolicy(
        "run_python_sandbox", "1.0.0", RiskLevel.FORBIDDEN, Decision.DENY, "沙箱未启用或未通过自检"
    ),
}


class PolicyEngine:
    """服务端持有权威策略；未知工具默认拒绝。"""

    def __init__(self, policies: dict[str, ToolPolicy] | None = None, *, sandbox_enabled: bool = False,
                 allowed_tools: list[str] | None = None) -> None:
        self.policies = dict(policies or DEFAULT_POLICIES)
        self.sandbox_enabled = sandbox_enabled
        self.allowed_tools = set(allowed_tools) if allowed_tools is not None else None

    def get(self, name: str) -> ToolPolicy | None:
        return self.policies.get(name)

    def decide(self, name: str, *, tool_version: str | None = None) -> ToolPolicy:
        policy = self.policies.get(name)
        if policy is None:
            return ToolPolicy(name, tool_version or "unknown", RiskLevel.FORBIDDEN, Decision.DENY,
                              "未知工具默认拒绝")
        if tool_version and tool_version != policy.version:
            return ToolPolicy(
                name, tool_version, policy.risk, Decision.DENY,
                f"工具版本不匹配：注册版本 {policy.version}，请求版本 {tool_version}",
            )
        if name == "run_python_sandbox" and not self.sandbox_enabled:
            return ToolPolicy(name, policy.version, RiskLevel.FORBIDDEN, Decision.DENY,
                              "隔离沙箱不可用，已禁用代码执行")
        if self.allowed_tools is not None and name not in self.allowed_tools:
            return ToolPolicy(name, policy.version, policy.risk, Decision.DENY,
                              "该工具未包含在当前技能/子任务的允许清单中")
        return policy

    def describe(self) -> list[dict[str, str]]:
        return [
            {
                "name": policy.name,
                "version": policy.version,
                "risk": policy.risk.value,
                "decision": policy.decision.value,
                "reason": policy.reason,
            }
            for policy in self.policies.values()
        ]
