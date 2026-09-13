"""受限文件工作区（文档 10 第 3 节）。

按项目和 run 分配独立根目录；拒绝绝对路径、盘符、UNC、`..`、Windows 备用数据流、
受限设备名，以及通过符号链接/junction 跳出根目录。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..errors import HarnessLabError
from ..utils import sha256_text

RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
UNSAFE_SYMLINK_ATTR = getattr(os, "O_NOFOLLOW", 0)
_DRIVE = re.compile(r"^[A-Za-z]:")
_ALLOWED_SUFFIXES = {".md", ".txt", ".json", ".csv", ".yml", ".yaml", ".log"}


def validate_relative_path(relative: str) -> str:
    """校验并规范化相对路径，返回 posix 形式。"""
    if not relative or not relative.strip():
        raise HarnessLabError("PATH_ESCAPE", "产物路径不能为空")
    raw = relative.strip().replace("\\", "/")
    if raw.startswith(("/", "//")):
        raise HarnessLabError("PATH_ESCAPE", "不允许绝对路径或 UNC 路径")
    if _DRIVE.match(raw):
        raise HarnessLabError("PATH_ESCAPE", "不允许使用盘符路径")
    if "\x00" in raw:
        raise HarnessLabError("PATH_ESCAPE", "路径包含非法空字符")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts:
        raise HarnessLabError("PATH_ESCAPE", "路径无效")
    for part in parts:
        if part == "..":
            raise HarnessLabError("PATH_ESCAPE", "不允许使用 .. 跳出工作区")
        if ":" in part:
            raise HarnessLabError("PATH_ESCAPE", "不允许 Windows 备用数据流写法")
        # Windows 受限设备名（大小写不敏感，且带扩展名同样受限）
        if part.split(".")[0].upper() in RESERVED_NAMES:
            raise HarnessLabError("PATH_ESCAPE", f"不允许受限设备名：{part}")
    suffix = Path(parts[-1]).suffix.lower()
    if suffix and suffix not in _ALLOWED_SUFFIXES:
        raise HarnessLabError("VALIDATION_ERROR", f"不允许写入该类型文件：{suffix}")
    return "/".join(parts)


def _assert_no_symlink_escape(target: Path, root: Path) -> None:
    root_resolved = root.resolve()
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise HarnessLabError("PATH_ESCAPE", "路径包含符号链接，可能跳出工作区")
    resolved = target.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise HarnessLabError("PATH_ESCAPE", "目标路径不在工作区根目录内")


@dataclass
class WorkspaceFile:
    relative_path: str
    size: int
    media_type: str


class Workspace:
    """单 run 工作区；写入使用临时文件 + 原子替换，默认不覆盖已有产物。"""

    def __init__(self, root: Path, *, max_files: int = 50, max_file_bytes: int = 5 * 1024 * 1024) -> None:
        self.root = Path(root)
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def resolve(self, relative: str) -> Path:
        safe = validate_relative_path(relative)
        target = self.root / Path(safe)
        _assert_no_symlink_escape(target, self.root)
        return target

    def list_files(self) -> list[WorkspaceFile]:
        files: list[WorkspaceFile] = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            files.append(
                WorkspaceFile(
                    relative_path=path.relative_to(self.root).as_posix(),
                    size=path.stat().st_size,
                    media_type=_media_type(path),
                )
            )
        return files

    def write_text(self, relative: str, text: str, *, overwrite: bool = False) -> tuple[str, str, int]:
        data = text.encode("utf-8")
        if len(data) > self.max_file_bytes:
            raise HarnessLabError(
                "VALIDATION_ERROR", "产物超过单文件大小上限", details={"size": len(data)}
            )
        if len(self.list_files()) >= self.max_files and not self.resolve(relative).exists():
            raise HarnessLabError("VALIDATION_ERROR", "工作区文件数量已达上限")
        target = self.resolve(relative)
        if target.exists() and not overwrite:
            raise HarnessLabError("VALIDATION_ERROR", f"产物已存在且默认不覆盖：{relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
        return target.relative_to(self.root).as_posix(), sha256_text(text), len(data)

    def read_text(self, relative: str) -> str:
        target = self.resolve(relative)
        if not target.exists():
            raise HarnessLabError("NOT_FOUND", f"产物不存在：{relative}")
        return target.read_text(encoding="utf-8", errors="replace")

    def absolute_path(self, relative: str) -> Path:
        return self.resolve(relative)


def _media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".json": "application/json",
        ".csv": "text/csv",
        ".yml": "application/yaml",
        ".yaml": "application/yaml",
        ".log": "text/plain",
    }.get(suffix, "application/octet-stream")
