"""SQLite 业务库接线（文档 02 第 5 节 / 08 第 3 节）。

业务表与框架检查点分库：本模块只管理业务表与迁移，绝不触碰 checkpointer 内部表。
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..config import get_settings

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    active_run_id TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_threads_project ON threads(project_id);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    parent_run_id TEXT,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    user_message TEXT NOT NULL,
    config_snapshot TEXT NOT NULL DEFAULT '{}',
    budget_limits TEXT NOT NULL DEFAULT '{}',
    budget_usage TEXT NOT NULL DEFAULT '{}',
    lease_owner TEXT,
    lease_expiry TEXT,
    fencing_token INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message TEXT,
    interrupt_payload TEXT,
    result TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_thread ON runs(thread_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_idem ON runs(project_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS run_events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    schema_version INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    tool_call_id TEXT,
    checkpoint_id TEXT,
    kind TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments TEXT NOT NULL,
    arguments_hash TEXT NOT NULL,
    expected_effect TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    expires_at TEXT NOT NULL,
    reviewer_id TEXT,
    decision TEXT,
    comment TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_run ON approvals(run_id);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);

CREATE TABLE IF NOT EXISTS tool_operations (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    logical_action_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    approval_revision INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    request TEXT NOT NULL DEFAULT '{}',
    request_hash TEXT NOT NULL,
    result TEXT,
    result_ref TEXT,
    external_receipt TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_toolops_run ON tool_operations(run_id);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    source_path TEXT NOT NULL,
    media_type TEXT NOT NULL,
    active_version INTEGER,
    deleted_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project_id);

CREATE TABLE IF NOT EXISTS document_versions (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    index_version TEXT,
    media_type TEXT NOT NULL,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    char_count INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (document_id, version)
);
CREATE INDEX IF NOT EXISTS idx_docver_hash ON document_versions(project_id, content_hash);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    document_version INTEGER NOT NULL,
    index_version TEXT NOT NULL,
    seq INTEGER NOT NULL,
    text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_title TEXT NOT NULL,
    heading_path TEXT NOT NULL DEFAULT '',
    page INTEGER,
    char_start INTEGER,
    char_end INTEGER,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_docver ON chunks(document_id, document_version);
CREATE INDEX IF NOT EXISTS idx_chunks_index ON chunks(project_id, index_version);

CREATE TABLE IF NOT EXISTS index_manifests (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    model_fingerprint TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    pipeline_version TEXT NOT NULL,
    status TEXT NOT NULL,
    config TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    activated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_manifest_project ON index_manifests(project_id, status);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    media_type TEXT NOT NULL,
    hash TEXT NOT NULL,
    size INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts(project_id);

CREATE TABLE IF NOT EXISTS memory_items (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    content TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    expires_at TEXT,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_items(project_id, user_id, status);

CREATE TABLE IF NOT EXISTS demo_tickets (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    tool_operation_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    result TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id, kind);

CREATE TABLE IF NOT EXISTS idempotency_records (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);
"""


class Database:
    """单进程 SQLite 访问层：短事务，不把慢模型调用放进事务。

    并发约束：整个进程共享一条连接（`check_same_thread=False` 只关闭了检查，不保证安全）。
    工作进程执行运行的同时，API 线程池可能在轮询 run/timeline，若两边的语句在同一连接上
    交错执行，会读到彼此的结果集，表现为「文档不存在」或 `InterfaceError` 这类不可复现的
    错误。因此**所有**连接访问（读与写）都通过同一把可重入锁串行化。
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = self._connect()
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _migrate(self) -> None:
        # executescript 会隐式提交，因此不能包在显式事务里；DDL 使用 IF NOT EXISTS 保证幂等
        self._conn.executescript(SCHEMA_SQL)
        row = self._conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        current = row["v"] if row and row["v"] is not None else 0
        if current < SCHEMA_VERSION:
            from ..utils import iso

            with self.transaction() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO schema_migrations(version, applied_at) VALUES (?,?)",
                    (SCHEMA_VERSION, iso()),
                )

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        return int(row["v"] or 0)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务：持有全局锁，避免与其他线程的语句交错。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple | list = ()) -> None:
        with self.transaction() as conn:
            conn.execute(sql, params)

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):  # pragma: no cover
            self._conn.close()


_database: Database | None = None


def get_database() -> Database:
    global _database
    if _database is None:
        settings = get_settings()
        _database = Database(settings.business_db_path)
    return _database


def reset_database() -> None:
    global _database
    if _database is not None:
        _database.close()
    _database = None
