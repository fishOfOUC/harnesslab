"""Prompt 与 Skills 版本管理（文档 10 第 4 节 / 需求 H15）。

技能只提供方法、模板与受控工具组合；权限仍由服务端工具注册表决定，
技能文件不能通过 required_tools 自行提升权限。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import REPO_ROOT
from ..observability.logging import get_logger
from ..utils import sha256_text

logger = get_logger("prompts")

PROMPTS_DIR = REPO_ROOT / "prompts"
SKILLS_DIR = REPO_ROOT / "skills"


@dataclass
class PromptTemplate:
    name: str
    version: str
    content: str
    content_hash: str

    def render(self, **values: Any) -> str:
        text = self.content
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", str(value))
        return text


@dataclass
class Skill:
    name: str
    version: str
    description: str
    required_tools: list[str] = field(default_factory=list)
    body: str = ""
    content_hash: str = ""
    path: str = ""


class PromptLibrary:
    """从 prompts/ 目录加载版本化 Prompt；缺失时使用内置默认值。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or PROMPTS_DIR
        self._cache: dict[str, PromptTemplate] = {}

    def get(self, name: str) -> PromptTemplate:
        if name in self._cache:
            return self._cache[name]
        path = self.root / f"{name}.md"
        if path.exists():
            raw = path.read_text(encoding="utf-8")
            version, content = _split_front_matter(raw)
        else:
            version, content = "0", _FALLBACK.get(name, "")
        template = PromptTemplate(name=name, version=version, content=content,
                                  content_hash=sha256_text(content))
        self._cache[name] = template
        return template


def _split_front_matter(raw: str) -> tuple[str, str]:
    if not raw.startswith("---"):
        return "0", raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return "0", raw
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return meta.get("version", "0"), parts[2].strip()


class SkillLibrary:
    """加载项目自建的受控技能清单（skills/<name>/SKILL.md）。"""

    def __init__(self, root: Path | None = None, *, allowed: list[str] | None = None) -> None:
        self.root = root or SKILLS_DIR
        self.allowed = set(allowed) if allowed is not None else None

    def list_skills(self) -> list[Skill]:
        if not self.root.exists():
            return []
        skills: list[Skill] = []
        for path in sorted(self.root.glob("*/SKILL.md")):
            skill = self._load(path)
            if skill is not None and (self.allowed is None or skill.name in self.allowed):
                skills.append(skill)
        return skills

    def get(self, name: str) -> Skill | None:
        for skill in self.list_skills():
            if skill.name == name:
                return skill
        return None

    def _load(self, path: Path) -> Skill | None:
        raw = path.read_text(encoding="utf-8")
        meta, body = _parse_skill_front_matter(raw)
        if not meta.get("name"):
            logger.warning("技能缺少 name，跳过", extra={"extra_fields": {"path": str(path)}})
            return None
        required = meta.get("required_tools", [])
        if isinstance(required, str):
            required = [item.strip() for item in required.split(",") if item.strip()]
        return Skill(
            name=meta["name"],
            version=meta.get("version", "0"),
            description=meta.get("description", ""),
            required_tools=list(required),
            body=body,
            content_hash=sha256_text(raw),
            path=str(path.relative_to(self.root.parent)) if self.root.parent in path.parents else str(path),
        )


def _parse_skill_front_matter(raw: str) -> tuple[dict[str, Any], str]:
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw
    meta: dict[str, Any] = {}
    current_list: str | None = None
    for line in parts[1].splitlines():
        if not line.strip():
            continue
        if line.startswith("  -") and current_list:
            meta.setdefault(current_list, []).append(line.split("-", 1)[1].strip())
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if value:
                meta[key] = value
                current_list = None
            else:
                meta[key] = []
                current_list = key
    return meta, parts[2].strip()


def render_skill_section(skills: list[Skill]) -> str:
    """把启用的技能正文拼进系统提示；同时固定版本供 run 快照追踪。"""
    if not skills:
        return ""
    blocks = ["已启用技能（版本与内容哈希进入本次 run 快照；技能不会提升工具权限）："]
    for skill in skills:
        blocks.append(
            f"### 技能 {skill.name} v{skill.version}（hash {skill.content_hash[:12]}）\n"
            f"{skill.description}\n\n{skill.body}\n"
            f"声明需要的工具：{', '.join(skill.required_tools) or '无'}"
        )
    return "\n\n".join(blocks)


_FALLBACK = {
    "system": (
        "你是 HarnessLab 的研究助理，按规定使用工具与证据，引用必须来自后端返回的证据标签。"
    ),
    "plan": "为目标生成最多 {{max_steps}} 个可验证步骤。",
}
