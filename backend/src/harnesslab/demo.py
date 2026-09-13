"""合成演示资料与演示项目（文档 06 第 3 节）。

资料全部为自建合成数据，标注“演示数据”，不依赖网络或真实内部资料；
容量参数是虚构案例值，不能作为真实系统性能结论。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import REPO_ROOT, Settings
from .runtime.worker import JOB_DOCUMENT_IMPORT
from .storage.repositories import Repository
from .utils import sha256_bytes

DEMO_DIR = REPO_ROOT / "datasets" / "demo"
DEMO_FILES = (
    "gateway-v1.md",
    "gateway-v2.md",
    "operations-guide.md",
    "capacity-notes.txt",
    "untrusted-note.md",
)


def demo_files() -> list[Path]:
    return [DEMO_DIR / name for name in DEMO_FILES if (DEMO_DIR / name).exists()]


def enqueue_demo_seed(repo: Repository, settings: Settings, project_id: str) -> dict[str, Any]:
    """幂等：相同内容重复导入会复用已完成版本，不产生重复 chunk。"""
    jobs: list[dict[str, Any]] = []
    skipped: list[str] = []
    for path in demo_files():
        data = path.read_bytes()
        content_hash = sha256_bytes(data)
        if repo.find_version_by_hash(project_id, content_hash) is not None:
            skipped.append(path.name)
            continue
        stored = settings.upload_root / f"demo-{path.name}"
        stored.write_bytes(data)
        job = repo.create_job(
            project_id=project_id,
            kind=JOB_DOCUMENT_IMPORT,
            payload={"stored_path": str(stored), "filename": path.name, "size": len(data),
                     "demo": True},
        )
        jobs.append({"file": path.name, "job_id": job["id"]})
    return {
        "project_id": project_id,
        "queued": jobs,
        "already_indexed": skipped,
        "note": "演示资料为合成数据，容量参数为虚构案例值",
    }
