"""业务仓储层（文档 05 一致性规则）。

集中实现幂等、租约 CAS、同 thread 互斥与事件 seq 分配，供 API/runtime 复用。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from ..errors import ConflictError, NotFoundError, ValidationFailure
from ..utils import hash_payload, iso, json_dumps, json_loads, new_id, parse_iso, utcnow
from .db import Database
from .models import (
    ApprovalStatus,
    BudgetLimits,
    BudgetUsage,
    EventType,
    JobStatus,
    ParseStatus,
    RunStatus,
    ToolOperationStatus,
)

TERMINAL_RUN_STATUSES = ("completed", "failed", "cancelled")


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _rows(rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _hydrate_run(run: dict[str, Any]) -> dict[str, Any]:
    """把 run 行的 JSON 字段还原成结构化值，避免调用方反复解析。"""
    for field_name in ("config_snapshot", "budget_limits", "budget_usage"):
        run[field_name] = json_loads(run.get(field_name), {})
    run["result"] = json_loads(run.get("result"), None)
    run["interrupt_payload"] = json_loads(run.get("interrupt_payload"), None)
    return run


class Repository:
    """业务表仓储。构造时注入 Database，便于测试替换。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ 项目
    def create_project(self, name: str, owner_id: str, policy_version: str = "policy-v1") -> dict[str, Any]:
        project_id = new_id()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO projects(id,name,owner_id,policy_version,created_at) VALUES (?,?,?,?,?)",
                (project_id, name, owner_id, policy_version, iso()),
            )
        return self.get_project(project_id)

    def get_project(self, project_id: str) -> dict[str, Any]:
        project = _row(self.db.query_one("SELECT * FROM projects WHERE id=?", (project_id,)))
        if project is None:
            raise NotFoundError("项目不存在", {"project_id": project_id})
        return project

    def list_projects(self) -> list[dict[str, Any]]:
        return _rows(self.db.query("SELECT * FROM projects ORDER BY created_at DESC"))

    # ------------------------------------------------------------------ 会话
    def create_thread(self, project_id: str, title: str = "") -> dict[str, Any]:
        self.get_project(project_id)
        thread_id = new_id()
        now = iso()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO threads(id,project_id,title,revision,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (thread_id, project_id, title, 1, now, now),
            )
        return self.get_thread(thread_id)

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        thread = _row(self.db.query_one("SELECT * FROM threads WHERE id=?", (thread_id,)))
        if thread is None:
            raise NotFoundError("会话不存在", {"thread_id": thread_id})
        return thread

    def list_threads(self, project_id: str) -> list[dict[str, Any]]:
        return _rows(
            self.db.query("SELECT * FROM threads WHERE project_id=? ORDER BY updated_at DESC", (project_id,))
        )

    def active_run_of_thread(self, thread_id: str) -> dict[str, Any] | None:
        return _row(
            self.db.query_one(
                f"SELECT * FROM runs WHERE thread_id=? AND status NOT IN {TERMINAL_RUN_STATUSES}"
                " ORDER BY created_at DESC LIMIT 1",
                (thread_id,),
            )
        )

    # ------------------------------------------------------------------ 运行
    def create_run(
        self,
        *,
        thread_id: str,
        project_id: str,
        user_message: str,
        mode: str,
        config_snapshot: dict[str, Any],
        budget: BudgetLimits,
        idempotency_key: str | None = None,
        parent_run_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """创建运行。返回 (run, created)。同 thread 只允许一个非终态 run。"""
        self.get_thread(thread_id)
        request_hash = hash_payload({"message": user_message, "mode": mode})
        if idempotency_key:
            existing = _row(
                self.db.query_one(
                    "SELECT * FROM runs WHERE project_id=? AND idempotency_key=?",
                    (project_id, idempotency_key),
                )
            )
            if existing is not None:
                stored = _row(
                    self.db.query_one(
                        "SELECT request_hash FROM idempotency_records WHERE scope=? AND key=?",
                        ("run.create", f"{project_id}:{idempotency_key}"),
                    )
                )
                if stored and stored["request_hash"] != request_hash:
                    raise ConflictError("RUN_CONFLICT", "同一幂等键对应了不同的请求体")
                return existing, False

        run_id = new_id()
        now = iso()
        with self.db.transaction() as conn:
            active = conn.execute(
                f"SELECT id FROM runs WHERE thread_id=? AND status NOT IN {TERMINAL_RUN_STATUSES} LIMIT 1",
                (thread_id,),
            ).fetchone()
            if active is not None:
                raise ConflictError(
                    "RUN_CONFLICT",
                    "同一会话已有进行中的运行，请先等待其结束或取消",
                    {"active_run_id": active["id"]},
                )
            conn.execute(
                """INSERT INTO runs(id,thread_id,project_id,parent_run_id,status,mode,user_message,
                       config_snapshot,budget_limits,budget_usage,cancel_requested,idempotency_key,
                       created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?)""",
                (
                    run_id,
                    thread_id,
                    project_id,
                    parent_run_id,
                    RunStatus.QUEUED.value,
                    mode,
                    user_message,
                    json_dumps(config_snapshot),
                    json_dumps(budget.model_dump()),
                    json_dumps(BudgetUsage().model_dump()),
                    idempotency_key,
                    now,
                    now,
                ),
            )
            if idempotency_key:
                conn.execute(
                    """INSERT INTO idempotency_records(scope,key,request_hash,resource_id,created_at)
                       VALUES (?,?,?,?,?)""",
                    ("run.create", f"{project_id}:{idempotency_key}", request_hash, run_id, now),
                )
            if parent_run_id is None:
                conn.execute(
                    "UPDATE threads SET active_run_id=?, updated_at=? WHERE id=?",
                    (run_id, now, thread_id),
                )
        return self.get_run(run_id), True

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = _row(self.db.query_one("SELECT * FROM runs WHERE id=?", (run_id,)))
        if run is None:
            raise NotFoundError("运行不存在", {"run_id": run_id})
        return _hydrate_run(run)

    def list_runs(self, *, project_id: str | None = None, thread_id: str | None = None,
                  limit: int = 20) -> list[dict[str, Any]]:
        sql = "SELECT * FROM runs WHERE 1=1"
        params: list[Any] = []
        if project_id:
            sql += " AND project_id=?"
            params.append(project_id)
        if thread_id:
            sql += " AND thread_id=?"
            params.append(thread_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [_hydrate_run(row) for row in _rows(self.db.query(sql, params))]

    def claim_next_run(self, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
        """compare-and-swap 领取任务，fencing token 单调递增。"""
        now = utcnow()
        expiry = iso(now + timedelta(seconds=lease_seconds))
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE status=? AND cancel_requested=0 ORDER BY created_at ASC LIMIT 1",
                (RunStatus.QUEUED.value,),
            ).fetchone()
            if row is None:
                return None
            updated = conn.execute(
                """UPDATE runs SET status=?, lease_owner=?, lease_expiry=?, fencing_token=fencing_token+1,
                       started_at=COALESCE(started_at,?), updated_at=?
                   WHERE id=? AND status=?""",
                (
                    RunStatus.RUNNING.value,
                    worker_id,
                    expiry,
                    iso(now),
                    iso(now),
                    row["id"],
                    RunStatus.QUEUED.value,
                ),
            )
            if updated.rowcount != 1:
                return None
        return self.get_run(row["id"])

    def recover_expired_runs(self, now_iso: str | None = None) -> list[dict[str, Any]]:
        """把租约过期的 running run 置为 recovering，供恢复协调器处理。"""
        moment = now_iso or iso()
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT id FROM runs WHERE status=? AND lease_expiry IS NOT NULL AND lease_expiry<?",
                (RunStatus.RUNNING.value, moment),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE runs SET status=?, lease_owner=NULL, lease_expiry=NULL, updated_at=? WHERE id=?",
                    (RunStatus.RECOVERING.value, iso(), row["id"]),
                )
        return [self.get_run(r["id"]) for r in rows]

    def renew_lease(self, run_id: str, worker_id: str, lease_seconds: int) -> bool:
        expiry = iso(utcnow() + timedelta(seconds=lease_seconds))
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE runs SET lease_expiry=?, updated_at=? WHERE id=? AND lease_owner=?",
                (expiry, iso(), run_id, worker_id),
            )
        return cur.rowcount == 1

    def update_run(self, run_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {
            "status",
            "error_code",
            "error_message",
            "interrupt_payload",
            "result",
            "budget_usage",
            "lease_owner",
            "lease_expiry",
            "started_at",
            "finished_at",
            "cancel_requested",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_run(run_id)
        for field_name in ("budget_usage", "result", "interrupt_payload"):
            value = updates.get(field_name)
            if isinstance(value, (dict, list)):
                updates[field_name] = json_dumps(value)
        updates["updated_at"] = iso()
        assignments = ", ".join(f"{k}=?" for k in updates)
        params = [*updates.values(), run_id]
        with self.db.transaction() as conn:
            cur = conn.execute(f"UPDATE runs SET {assignments} WHERE id=?", params)
            if cur.rowcount != 1:
                raise NotFoundError("运行不存在", {"run_id": run_id})
        return self.get_run(run_id)

    def request_cancel(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        status = RunStatus(run["status"])
        if status.terminal:
            return run
        new_status = RunStatus.CANCELLING.value if status == RunStatus.RUNNING else RunStatus.CANCELLED.value
        fields: dict[str, Any] = {"cancel_requested": 1}
        if new_status == RunStatus.CANCELLED.value:
            fields.update(status=new_status, finished_at=iso())
        return self.update_run(run_id, **fields)

    def is_cancel_requested(self, run_id: str) -> bool:
        row = self.db.query_one("SELECT cancel_requested FROM runs WHERE id=?", (run_id,))
        return bool(row and row["cancel_requested"])

    def set_thread_title_if_empty(self, thread_id: str, title: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE threads SET title=?, updated_at=? WHERE id=? AND (title IS NULL OR title='')",
                (title[:80], iso(), thread_id),
            )

    # ------------------------------------------------------------------ 事件
    def append_event(self, run_id: str, event_type: EventType | str, payload: dict[str, Any] | None = None,
                     timestamp: str | None = None) -> dict[str, Any]:
        """先持久化再发送；seq 单调递增，唯一 (run_id, seq)。"""
        type_value = event_type.value if isinstance(event_type, EventType) else str(event_type)
        with self.db.transaction() as conn:
            row = conn.execute("SELECT COALESCE(MAX(seq),0) AS s FROM run_events WHERE run_id=?",
                               (run_id,)).fetchone()
            seq = int(row["s"]) + 1
            event_id = f"evt-{run_id[:8]}-{seq}"
            conn.execute(
                """INSERT INTO run_events(run_id,seq,event_id,type,timestamp,payload,schema_version)
                   VALUES (?,?,?,?,?,?,1)""",
                (run_id, seq, event_id, type_value, timestamp or iso(), json_dumps(payload or {})),
            )
        return {
            "schema_version": 1,
            "event_id": event_id,
            "run_id": run_id,
            "seq": seq,
            "timestamp": timestamp or iso(),
            "type": type_value,
            "payload": payload or {},
        }

    def list_events(self, run_id: str, after_seq: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        rows = _rows(
            self.db.query(
                "SELECT * FROM run_events WHERE run_id=? AND seq>? ORDER BY seq ASC LIMIT ?",
                (run_id, after_seq, limit),
            )
        )
        for row in rows:
            row["payload"] = json_loads(row["payload"], {})
            row["schema_version"] = row.get("schema_version", 1)
        return rows

    def max_event_seq(self, run_id: str) -> int:
        row = self.db.query_one("SELECT COALESCE(MAX(seq),0) AS s FROM run_events WHERE run_id=?", (run_id,))
        return int(row["s"] if row else 0)

    def min_event_seq(self, run_id: str) -> int:
        row = self.db.query_one("SELECT COALESCE(MIN(seq),0) AS s FROM run_events WHERE run_id=?", (run_id,))
        return int(row["s"] if row else 0)

    # ------------------------------------------------------------------ 审批
    def create_approval(
        self,
        *,
        run_id: str,
        project_id: str,
        thread_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        expected_effect: str,
        kind: str = "tool_call",
        tool_call_id: str | None = None,
        checkpoint_id: str | None = None,
        ttl_hours: int = 24,
        revision: int = 1,
    ) -> dict[str, Any]:
        approval_id = new_id()
        now = utcnow()
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO approvals(id,run_id,project_id,thread_id,tool_call_id,checkpoint_id,kind,
                       tool_name,arguments,arguments_hash,expected_effect,status,revision,expires_at,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    approval_id,
                    run_id,
                    project_id,
                    thread_id,
                    tool_call_id,
                    checkpoint_id,
                    kind,
                    tool_name,
                    json_dumps(arguments),
                    hash_payload(arguments),
                    expected_effect,
                    ApprovalStatus.PENDING.value,
                    revision,
                    iso(now + timedelta(hours=ttl_hours)),
                    iso(now),
                ),
            )
        return self.get_approval(approval_id)

    def get_approval(self, approval_id: str) -> dict[str, Any]:
        approval = _row(self.db.query_one("SELECT * FROM approvals WHERE id=?", (approval_id,)))
        if approval is None:
            raise NotFoundError("审批记录不存在", {"approval_id": approval_id})
        approval["arguments"] = json_loads(approval["arguments"], {})
        return approval

    def list_approvals(self, run_id: str) -> list[dict[str, Any]]:
        rows = _rows(self.db.query("SELECT * FROM approvals WHERE run_id=? ORDER BY created_at", (run_id,)))
        for row in rows:
            row["arguments"] = json_loads(row["arguments"], {})
        return rows

    def pending_approval(self, run_id: str) -> dict[str, Any] | None:
        row = _row(
            self.db.query_one(
                "SELECT * FROM approvals WHERE run_id=? AND status=? ORDER BY created_at DESC LIMIT 1",
                (run_id, ApprovalStatus.PENDING.value),
            )
        )
        if row:
            row["arguments"] = json_loads(row["arguments"], {})
        return row

    def decide_approval(
        self,
        approval_id: str,
        *,
        decision: str,
        expected_revision: int,
        arguments_hash: str,
        reviewer_id: str,
        comment: str = "",
    ) -> dict[str, Any]:
        """一次批准只绑定该参数版本；并发提交只有一个有效决策。"""
        now = iso()
        current = _row(self.db.query_one("SELECT * FROM approvals WHERE id=?", (approval_id,)))
        if current is None:
            raise NotFoundError("审批记录不存在", {"approval_id": approval_id})
        if current["status"] != ApprovalStatus.PENDING.value:
            raise ConflictError("APPROVAL_CONFLICT", f"审批已处于 {current['status']} 状态")
        if current["revision"] != expected_revision:
            raise ConflictError("APPROVAL_CONFLICT", "审批版本已变更，请刷新后重试")
        if current["arguments_hash"] != arguments_hash:
            raise ConflictError("APPROVAL_CONFLICT", "参数哈希不匹配，审批已被拒绝")
        expires = parse_iso(current["expires_at"])
        if expires is not None and expires < utcnow():
            # 过期状态必须先独立提交，否则抛出异常会连同状态更新一起回滚
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE approvals SET status=?, decided_at=? WHERE id=? AND status=?",
                    (ApprovalStatus.EXPIRED.value, now, approval_id, ApprovalStatus.PENDING.value),
                )
            raise ConflictError("APPROVAL_CONFLICT", "审批已过期")

        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if row is None or row["status"] != ApprovalStatus.PENDING.value:
                raise ConflictError("APPROVAL_CONFLICT", "审批状态已变更")
            if row["revision"] != expected_revision or row["arguments_hash"] != arguments_hash:
                raise ConflictError("APPROVAL_CONFLICT", "审批版本或参数已变更")
            run = conn.execute("SELECT status FROM runs WHERE id=?", (row["run_id"],)).fetchone()
            if run is None or run["status"] in TERMINAL_RUN_STATUSES:
                raise ConflictError("APPROVAL_CONFLICT", "运行已结束，审批无效")
            status = ApprovalStatus.APPROVED.value if decision == "approve" else ApprovalStatus.REJECTED.value
            updated = conn.execute(
                """UPDATE approvals SET status=?, decision=?, reviewer_id=?, comment=?, decided_at=?
                   WHERE id=? AND status=? AND revision=? AND arguments_hash=?""",
                (
                    status,
                    decision,
                    reviewer_id,
                    comment,
                    now,
                    approval_id,
                    ApprovalStatus.PENDING.value,
                    expected_revision,
                    arguments_hash,
                ),
            )
            if updated.rowcount != 1:
                raise ConflictError("APPROVAL_CONFLICT", "另一个决策已先行提交")
        return self.get_approval(approval_id)

    def revise_approval(self, approval_id: str, arguments: dict[str, Any], ttl_hours: int = 24) -> dict[str, Any]:
        """用户修改参数：旧版本作废，产生新的待审批版本。"""
        current = self.get_approval(approval_id)
        if current["status"] != ApprovalStatus.PENDING.value:
            raise ConflictError("APPROVAL_CONFLICT", "只有待审批状态可以修改参数")
        new_revision = int(current["revision"]) + 1
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE approvals SET status=? WHERE id=? AND status=?",
                (ApprovalStatus.SUPERSEDED.value, approval_id, ApprovalStatus.PENDING.value),
            )
            new_id_value = new_id()
            conn.execute(
                """INSERT INTO approvals(id,run_id,project_id,thread_id,tool_call_id,checkpoint_id,kind,
                       tool_name,arguments,arguments_hash,expected_effect,status,revision,expires_at,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_id_value,
                    current["run_id"],
                    current["project_id"],
                    current["thread_id"],
                    current["tool_call_id"],
                    current["checkpoint_id"],
                    current["kind"],
                    current["tool_name"],
                    json_dumps(arguments),
                    hash_payload(arguments),
                    current["expected_effect"],
                    ApprovalStatus.PENDING.value,
                    new_revision,
                    iso(utcnow() + timedelta(hours=ttl_hours)),
                    iso(),
                ),
            )
        return self.get_approval(new_id_value)

    def expire_stale_approvals(self) -> int:
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE approvals SET status=?, decided_at=? WHERE status=? AND expires_at<?",
                (ApprovalStatus.EXPIRED.value, iso(), ApprovalStatus.PENDING.value, iso()),
            )
        return cur.rowcount

    def cancel_pending_approvals(self, run_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE approvals SET status=?, decided_at=? WHERE run_id=? AND status=?",
                (ApprovalStatus.CANCELLED.value, iso(), run_id, ApprovalStatus.PENDING.value),
            )

    # ------------------------------------------------------------------ 工具账本
    def find_operation(self, idempotency_key: str) -> dict[str, Any] | None:
        row = _row(
            self.db.query_one("SELECT * FROM tool_operations WHERE idempotency_key=?", (idempotency_key,))
        )
        if row:
            row["request"] = json_loads(row["request"], {})
            row["result"] = json_loads(row["result"], None)
        return row

    def create_operation(
        self,
        *,
        run_id: str,
        project_id: str,
        logical_action_id: str,
        tool_name: str,
        tool_version: str,
        idempotency_key: str,
        request: dict[str, Any],
        approval_revision: int = 0,
    ) -> dict[str, Any]:
        op_id = new_id()
        now = iso()
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO tool_operations(id,run_id,project_id,logical_action_id,tool_name,tool_version,
                       approval_revision,idempotency_key,status,attempt,request,request_hash,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    op_id,
                    run_id,
                    project_id,
                    logical_action_id,
                    tool_name,
                    tool_version,
                    approval_revision,
                    idempotency_key,
                    ToolOperationStatus.PREPARED.value,
                    1,
                    json_dumps(request),
                    hash_payload(request),
                    now,
                    now,
                ),
            )
        return self.find_operation(idempotency_key)  # type: ignore[return-value]

    def update_operation(
        self,
        idempotency_key: str,
        *,
        status: ToolOperationStatus,
        result: dict[str, Any] | None = None,
        result_ref: str | None = None,
        external_receipt: str | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        with self.db.transaction() as conn:
            conn.execute(
                """UPDATE tool_operations SET status=?, result=?, result_ref=?, external_receipt=?,
                       error_code=?, updated_at=? WHERE idempotency_key=?""",
                (
                    status.value,
                    json_dumps(result) if result is not None else None,
                    result_ref,
                    external_receipt,
                    error_code,
                    iso(),
                    idempotency_key,
                ),
            )
        op = self.find_operation(idempotency_key)
        if op is None:  # pragma: no cover
            raise NotFoundError("操作记录不存在")
        return op

    def list_operations(self, run_id: str) -> list[dict[str, Any]]:
        rows = _rows(self.db.query("SELECT * FROM tool_operations WHERE run_id=? ORDER BY created_at",
                                   (run_id,)))
        for row in rows:
            row["request"] = json_loads(row["request"], {})
            row["result"] = json_loads(row["result"], None)
        return rows

    def recovery_report(self, run_id: str) -> dict[str, Any]:
        """恢复对账：区分已完成、可安全重试与结果不明的操作。"""
        ops = self.list_operations(run_id)
        return {
            "succeeded": [o for o in ops if o["status"] == ToolOperationStatus.SUCCEEDED.value],
            "uncertain": [o for o in ops if o["status"] == ToolOperationStatus.UNCERTAIN.value],
            "executing": [o for o in ops if o["status"] == ToolOperationStatus.EXECUTING.value],
            "failed": [o for o in ops if o["status"] == ToolOperationStatus.FAILED.value],
        }

    # ------------------------------------------------------------------ 文档
    def create_document(self, *, project_id: str, title: str, source_path: str,
                        media_type: str) -> dict[str, Any]:
        doc_id = new_id()
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO documents(id,project_id,title,source_path,media_type,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (doc_id, project_id, title, source_path, media_type, iso()),
            )
        return self.get_document(doc_id)

    def get_document(self, document_id: str) -> dict[str, Any]:
        doc = _row(self.db.query_one("SELECT * FROM documents WHERE id=?", (document_id,)))
        if doc is None:
            raise NotFoundError("文档不存在", {"document_id": document_id})
        return doc

    def list_documents(self, project_id: str, include_deleted: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM documents WHERE project_id=?"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        sql += " ORDER BY created_at DESC"
        docs = _rows(self.db.query(sql, (project_id,)))
        for doc in docs:
            doc["versions"] = self.list_versions(doc["id"])
        return docs

    def next_document_version(self, document_id: str) -> int:
        row = self.db.query_one(
            "SELECT COALESCE(MAX(version),0) AS v FROM document_versions WHERE document_id=?", (document_id,)
        )
        return int(row["v"] if row else 0) + 1

    def find_version_by_hash(self, project_id: str, content_hash: str) -> dict[str, Any] | None:
        return _row(
            self.db.query_one(
                "SELECT * FROM document_versions WHERE project_id=? AND content_hash=? AND parse_status=?",
                (project_id, content_hash, ParseStatus.READY.value),
            )
        )

    def create_version(
        self,
        *,
        document_id: str,
        project_id: str,
        version: int,
        content_hash: str,
        parse_status: ParseStatus,
        media_type: str,
        char_count: int = 0,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        version_id = new_id()
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO document_versions(id,document_id,project_id,version,content_hash,parse_status,
                       media_type,chunk_count,char_count,error_code,error_message,created_at)
                   VALUES (?,?,?,?,?,?,?,0,?,?,?,?)""",
                (
                    version_id,
                    document_id,
                    project_id,
                    version,
                    content_hash,
                    parse_status.value,
                    media_type,
                    char_count,
                    error_code,
                    error_message,
                    iso(),
                ),
            )
        return self.get_version(document_id, version)

    def get_version(self, document_id: str, version: int) -> dict[str, Any]:
        row = _row(
            self.db.query_one(
                "SELECT * FROM document_versions WHERE document_id=? AND version=?", (document_id, version)
            )
        )
        if row is None:
            raise NotFoundError("文档版本不存在", {"document_id": document_id, "version": version})
        return row

    def list_versions(self, document_id: str) -> list[dict[str, Any]]:
        return _rows(
            self.db.query(
                "SELECT * FROM document_versions WHERE document_id=? ORDER BY version DESC", (document_id,)
            )
        )

    def update_version(self, document_id: str, version: int, **fields: Any) -> None:
        allowed = {"parse_status", "index_version", "chunk_count", "error_code", "error_message"}
        updates = {k: (v.value if isinstance(v, ParseStatus) else v) for k, v in fields.items() if k in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{k}=?" for k in updates)
        with self.db.transaction() as conn:
            conn.execute(
                f"UPDATE document_versions SET {assignments} WHERE document_id=? AND version=?",
                [*updates.values(), document_id, version],
            )

    def set_active_version(self, document_id: str, version: int | None) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE documents SET active_version=? WHERE id=?", (version, document_id))

    def delete_document(self, document_id: str) -> None:
        doc = self.get_document(document_id)
        with self.db.transaction() as conn:
            conn.execute("UPDATE documents SET deleted_at=?, active_version=NULL WHERE id=?",
                         (iso(), document_id))
            conn.execute(
                "UPDATE document_versions SET parse_status=? WHERE document_id=?",
                (ParseStatus.DELETED.value, document_id),
            )
        self.delete_chunks(doc["project_id"], document_ids=[document_id])

    def insert_chunks(self, chunks: list[dict[str, Any]]) -> None:
        if not chunks:
            return
        with self.db.transaction() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO chunks(id,project_id,document_id,document_version,index_version,seq,
                       text,content_hash,source_title,heading_path,page,char_start,char_end,token_estimate,created_at)
                   VALUES (:id,:project_id,:document_id,:document_version,:index_version,:seq,:text,
                       :content_hash,:source_title,:heading_path,:page,:char_start,:char_end,
                       :token_estimate,:created_at)""",
                chunks,
            )

    def delete_chunks(self, project_id: str, *, document_ids: list[str] | None = None,
                      index_version: str | None = None) -> int:
        sql = "DELETE FROM chunks WHERE project_id=?"
        params: list[Any] = [project_id]
        if document_ids:
            placeholders = ",".join("?" for _ in document_ids)
            sql += f" AND document_id IN ({placeholders})"
            params.extend(document_ids)
        if index_version:
            sql += " AND index_version=?"
            params.append(index_version)
        with self.db.transaction() as conn:
            cur = conn.execute(sql, params)
        return cur.rowcount

    def get_chunk(self, chunk_id: str) -> dict[str, Any]:
        chunk = _row(self.db.query_one("SELECT * FROM chunks WHERE id=?", (chunk_id,)))
        if chunk is None:
            raise NotFoundError("原文片段不存在", {"chunk_id": chunk_id})
        return chunk

    def chunks_by_ids(self, chunk_ids: list[str]) -> list[dict[str, Any]]:
        if not chunk_ids:
            return []
        placeholders = ",".join("?" for _ in chunk_ids)
        rows = _rows(self.db.query(f"SELECT * FROM chunks WHERE id IN ({placeholders})", chunk_ids))
        by_id = {r["id"]: r for r in rows}
        return [by_id[cid] for cid in chunk_ids if cid in by_id]

    def list_chunks(self, project_id: str, index_version: str, document_ids: list[str] | None = None,
                    limit: int = 5000) -> list[dict[str, Any]]:
        sql = "SELECT * FROM chunks WHERE project_id=? AND index_version=?"
        params: list[Any] = [project_id, index_version]
        if document_ids:
            placeholders = ",".join("?" for _ in document_ids)
            sql += f" AND document_id IN ({placeholders})"
            params.extend(document_ids)
        sql += " ORDER BY document_id, seq LIMIT ?"
        params.append(limit)
        return _rows(self.db.query(sql, params))

    def count_chunks(self, project_id: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS c FROM chunks WHERE project_id=?", (project_id,)
        )
        return int(row["c"] if row else 0)

    # ------------------------------------------------------------------ 索引清单
    def create_manifest(self, *, project_id: str, model_fingerprint: str, dimension: int,
                        pipeline_version: str, config: dict[str, Any]) -> dict[str, Any]:
        manifest_id = new_id()
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO index_manifests(id,project_id,model_fingerprint,dimension,pipeline_version,
                       status,config,created_at) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    manifest_id,
                    project_id,
                    model_fingerprint,
                    dimension,
                    pipeline_version,
                    "building",
                    json_dumps(config),
                    iso(),
                ),
            )
        return self.get_manifest(manifest_id)

    def get_manifest(self, manifest_id: str) -> dict[str, Any]:
        row = _row(self.db.query_one("SELECT * FROM index_manifests WHERE id=?", (manifest_id,)))
        if row is None:
            raise NotFoundError("索引清单不存在", {"manifest_id": manifest_id})
        row["config"] = json_loads(row["config"], {})
        return row

    def active_manifest(self, project_id: str) -> dict[str, Any] | None:
        row = _row(
            self.db.query_one(
                "SELECT * FROM index_manifests WHERE project_id=? AND status='active' ORDER BY activated_at DESC LIMIT 1",
                (project_id,),
            )
        )
        if row:
            row["config"] = json_loads(row["config"], {})
        return row

    def list_manifests(self, project_id: str) -> list[dict[str, Any]]:
        rows = _rows(
            self.db.query(
                "SELECT * FROM index_manifests WHERE project_id=? ORDER BY created_at DESC", (project_id,)
            )
        )
        for row in rows:
            row["config"] = json_loads(row["config"], {})
        return rows

    def activate_manifest(self, manifest_id: str) -> dict[str, Any]:
        manifest = self.get_manifest(manifest_id)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE index_manifests SET status='retired' WHERE project_id=? AND status='active'",
                (manifest["project_id"],),
            )
            conn.execute(
                "UPDATE index_manifests SET status='active', activated_at=? WHERE id=?",
                (iso(), manifest_id),
            )
        return self.get_manifest(manifest_id)

    def update_manifest_status(self, manifest_id: str, status: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE index_manifests SET status=? WHERE id=?", (status, manifest_id))

    # ------------------------------------------------------------------ 产物
    def create_artifact(self, *, run_id: str, project_id: str, title: str, relative_path: str,
                        media_type: str, content_hash: str, size: int) -> dict[str, Any]:
        artifact_id = new_id()
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO artifacts(id,run_id,project_id,title,relative_path,media_type,hash,size,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (artifact_id, run_id, project_id, title, relative_path, media_type, content_hash, size, iso()),
            )
        return self.get_artifact(artifact_id)

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        artifact = _row(self.db.query_one("SELECT * FROM artifacts WHERE id=?", (artifact_id,)))
        if artifact is None:
            raise NotFoundError("产物不存在", {"artifact_id": artifact_id})
        return artifact

    def list_artifacts(self, *, run_id: str | None = None, project_id: str | None = None,
                       limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM artifacts WHERE 1=1"
        params: list[Any] = []
        if run_id:
            sql += " AND run_id=?"
            params.append(run_id)
        if project_id:
            sql += " AND project_id=?"
            params.append(project_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return _rows(self.db.query(sql, params))

    # ------------------------------------------------------------------ 记忆
    def create_memory(self, *, project_id: str, user_id: str, content: str, status: str = "candidate",
                      source_ref: str = "", ttl_days: int | None = None) -> dict[str, Any]:
        memory_id = new_id()
        now = utcnow()
        expires = iso(now + timedelta(days=ttl_days)) if ttl_days else None
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO memory_items(id,project_id,user_id,content,source_ref,status,revision,
                       expires_at,created_at,updated_at) VALUES (?,?,?,?,?,?,1,?,?,?)""",
                (memory_id, project_id, user_id, content, source_ref, status, expires, iso(now), iso(now)),
            )
        return self.get_memory(memory_id)

    def get_memory(self, memory_id: str) -> dict[str, Any]:
        memory = _row(self.db.query_one("SELECT * FROM memory_items WHERE id=?", (memory_id,)))
        if memory is None:
            raise NotFoundError("记忆不存在", {"memory_id": memory_id})
        return memory

    def list_memories(self, project_id: str, user_id: str, statuses: list[str] | None = None
                      ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM memory_items WHERE project_id=? AND user_id=? AND deleted_at IS NULL"
        params: list[Any] = [project_id, user_id]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql += f" AND status IN ({placeholders})"
            params.extend(statuses)
        sql += " ORDER BY updated_at DESC"
        return _rows(self.db.query(sql, params))

    def update_memory(self, memory_id: str, *, content: str | None = None, status: str | None = None,
                      revision: int) -> dict[str, Any]:
        current = self.get_memory(memory_id)
        if current["revision"] != revision:
            raise ConflictError("VALIDATION_ERROR", "记忆已被修改，请刷新后重试")
        updates: dict[str, Any] = {"revision": revision + 1, "updated_at": iso()}
        if content is not None:
            updates["content"] = content
        if status is not None:
            updates["status"] = status
        assignments = ", ".join(f"{k}=?" for k in updates)
        with self.db.transaction() as conn:
            conn.execute(f"UPDATE memory_items SET {assignments} WHERE id=?", [*updates.values(), memory_id])
        return self.get_memory(memory_id)

    def delete_memory(self, memory_id: str) -> None:
        self.get_memory(memory_id)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE memory_items SET status='deleted', deleted_at=?, updated_at=? WHERE id=?",
                (iso(), iso(), memory_id),
            )

    # ------------------------------------------------------------------ 模拟工单
    def create_ticket(self, *, project_id: str, title: str, body: str,
                      tool_operation_id: str) -> dict[str, Any]:
        existing = self.ticket_by_operation(tool_operation_id)
        if existing is not None:
            return existing
        ticket_id = new_id()
        with self.db.transaction() as conn:
            try:
                conn.execute(
                    """INSERT INTO demo_tickets(id,project_id,title,body,tool_operation_id,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (ticket_id, project_id, title, body, tool_operation_id, iso()),
                )
            except sqlite3.IntegrityError:
                row = self.ticket_by_operation(tool_operation_id)
                if row is None:  # pragma: no cover
                    raise
                return row
        return self.get_ticket(ticket_id)

    def ticket_by_operation(self, tool_operation_id: str) -> dict[str, Any] | None:
        return _row(
            self.db.query_one("SELECT * FROM demo_tickets WHERE tool_operation_id=?", (tool_operation_id,))
        )

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        ticket = _row(self.db.query_one("SELECT * FROM demo_tickets WHERE id=?", (ticket_id,)))
        if ticket is None:
            raise NotFoundError("工单不存在", {"ticket_id": ticket_id})
        return ticket

    def count_tickets_by_title(self, project_id: str, title: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS c FROM demo_tickets WHERE project_id=? AND title=?", (project_id, title)
        )
        return int(row["c"] if row else 0)

    # ------------------------------------------------------------------ 后台任务
    def create_job(self, *, project_id: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = new_id()
        now = iso()
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO jobs(id,project_id,kind,status,payload,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (job_id, project_id, kind, JobStatus.QUEUED.value, json_dumps(payload), now, now),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = _row(self.db.query_one("SELECT * FROM jobs WHERE id=?", (job_id,)))
        if job is None:
            raise NotFoundError("任务不存在", {"job_id": job_id})
        job["payload"] = json_loads(job["payload"], {})
        job["result"] = json_loads(job["result"], None)
        return job

    def update_job(self, job_id: str, *, status: JobStatus | None = None, result: dict[str, Any] | None = None,
                   error_code: str | None = None, error_message: str | None = None) -> dict[str, Any]:
        updates: dict[str, Any] = {"updated_at": iso()}
        if status is not None:
            updates["status"] = status.value
        if result is not None:
            updates["result"] = json_dumps(result)
        if error_code is not None:
            updates["error_code"] = error_code
        if error_message is not None:
            updates["error_message"] = error_message
        assignments = ", ".join(f"{k}=?" for k in updates)
        with self.db.transaction() as conn:
            conn.execute(f"UPDATE jobs SET {assignments} WHERE id=?", [*updates.values(), job_id])
        return self.get_job(job_id)

    def next_queued_job(self, kinds: list[str] | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM jobs WHERE status=?"
        params: list[Any] = [JobStatus.QUEUED.value]
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            sql += f" AND kind IN ({placeholders})"
            params.extend(kinds)
        sql += " ORDER BY created_at ASC LIMIT 1"
        row = _row(self.db.query_one(sql, params))
        if row:
            row["payload"] = json_loads(row["payload"], {})
            row["result"] = json_loads(row["result"], None)
        return row

    def claim_job(self, job_id: str) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status=?, updated_at=? WHERE id=? AND status=?",
                (JobStatus.RUNNING.value, iso(), job_id, JobStatus.QUEUED.value),
            )
        return cur.rowcount == 1


def require_revision(actual: int, expected: int) -> None:
    if actual != expected:
        raise ValidationFailure("revision 不匹配", {"actual": actual, "expected": expected})
