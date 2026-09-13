"""测试环境装配：隔离数据目录，使用确定性替身模型（界面/报告必须标注替身）。"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from pathlib import Path

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="harnesslab-tests-"))

os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(_TMP_ROOT / "data")
os.environ["ARTIFACT_DIR"] = str(_TMP_ROOT / "artifacts")
os.environ["EMBEDDING_PROVIDER"] = "fake"
os.environ["CHAT_PROVIDER"] = "stub"
os.environ["VECTOR_MODE"] = "local"
os.environ["RUN_WORKER_IN_API"] = "false"
os.environ["RETRIEVAL_MODE"] = "hybrid"
os.environ["FAULT_INJECTION"] = ""
os.environ["BUDGET_TOOL_TIMEOUT_SECONDS"] = "30"

import pytest  # noqa: E402

from harnesslab.config import get_settings, reset_settings_cache  # noqa: E402
from harnesslab.knowledge.index import build_vector_index  # noqa: E402
from harnesslab.knowledge.service import KnowledgeService  # noqa: E402
from harnesslab.storage.db import get_database, reset_database  # noqa: E402
from harnesslab.storage.repositories import Repository  # noqa: E402


def pytest_sessionfinish(session, exitstatus):
    _cleanup()


def _cleanup() -> None:
    with contextlib.suppress(Exception):  # 清理失败不影响测试结论
        build_vector_index(get_settings()).close()
    reset_database()
    reset_settings_cache()
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest.fixture(scope="session")
def repo() -> Repository:
    return Repository(get_database())


@pytest.fixture(scope="session")
def knowledge(repo: Repository, settings) -> KnowledgeService:
    return KnowledgeService(repo, settings)


@pytest.fixture()
def project(repo: Repository) -> dict:
    return repo.create_project("测试项目", "local-dev-user")
