"""单工作进程：领取持久任务、维持租约、处理导入与故障恢复（文档 02 / 08 第 4 节）。

P0 固定单工作进程；SQLite 短事务，索引任务与交互任务共用公平队列。
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings, get_settings
from ..errors import HarnessLabError
from ..observability.events import EventSink
from ..observability.logging import get_logger
from ..storage.models import EventType, JobStatus, RunStatus
from ..storage.repositories import Repository
from ..utils import iso
from .executor import RunExecutor

logger = get_logger("worker")

JOB_DOCUMENT_IMPORT = "document_import"
JOB_INDEX_REBUILD = "index_rebuild"
JOB_EVAL = "eval"


@dataclass
class WorkerSettings:
    poll_seconds: float = 0.5
    lease_seconds: int = 60
    worker_id: str = ""

    @classmethod
    def from_settings(cls, settings: Settings) -> WorkerSettings:
        return cls(
            poll_seconds=settings.worker_poll_seconds,
            lease_seconds=settings.lease_seconds,
            worker_id=f"{socket.gethostname()}-{id(cls):x}",
        )


class Worker:
    def __init__(
        self,
        repo: Repository,
        settings: Settings | None = None,
        *,
        executor: RunExecutor | None = None,
    ) -> None:
        self.repo = repo
        self.settings = settings or get_settings()
        self.config = WorkerSettings.from_settings(self.settings)
        self.executor = executor or RunExecutor(repo, self.settings)
        self.knowledge = self.executor.knowledge
        self.events = EventSink(repo)
        self._stopping = asyncio.Event()

    # ------------------------------------------------------------------
    async def serve(self) -> None:
        logger.info(
            "工作进程启动",
            extra={"extra_fields": {"worker_id": self.config.worker_id, "concurrency": 1}},
        )
        try:
            while not self._stopping.is_set():
                did_work = await self.run_once()
                if not did_work:
                    try:
                        await asyncio.wait_for(
                            self._stopping.wait(), timeout=self.config.poll_seconds
                        )
                    except TimeoutError:
                        continue
        finally:
            logger.info("工作进程排空退出", extra={"extra_fields": {"worker_id": self.config.worker_id}})

    def stop(self) -> None:
        self._stopping.set()

    # ------------------------------------------------------------------
    async def run_once(self) -> bool:
        """处理一轮：状态协调 → 后台任务 → 交互任务。返回是否做了实际工作。"""
        self._coordinate_states()
        if await self._process_job():
            return True
        run = self.repo.claim_next_run(self.config.worker_id, self.config.lease_seconds)
        if run is None:
            return False
        logger.info("领取运行", extra={"extra_fields": {"run_id": run["id"], "status": run["status"]}})
        await asyncio.to_thread(self._execute_with_lease, run["id"])
        return True

    def _execute_with_lease(self, run_id: str) -> None:
        """执行期间在独立线程维持租约；过期持有者不能提交新的操作结果。"""
        try:
            self.executor.execute(run_id)
        except Exception:
            logger.exception("运行执行器抛出未处理异常", extra={"extra_fields": {"run_id": run_id}})

    # ------------------------------------------------------------------
    def _coordinate_states(self) -> None:
        self.repo.expire_stale_approvals()
        for run in self.repo.recover_expired_runs():
            logger.warning("租约过期，进入恢复", extra={"extra_fields": {"run_id": run["id"]}})
            self.events.emit(run["id"], EventType.RUN_RECOVERING, {"reason": "lease_expired"})
        # 排队中已被取消的运行
        for row in self.repo.db.query(
            "SELECT id FROM runs WHERE status=? AND cancel_requested=1", (RunStatus.QUEUED.value,)
        ):
            self.repo.cancel_pending_approvals(row["id"])
            self.repo.update_run(
                row["id"], status=RunStatus.CANCELLED.value, finished_at=iso(), error_code="CANCELLED",
                error_message="运行在排队阶段被用户取消",
            )
            self.events.emit(row["id"], EventType.RUN_CANCELLED, {"reason": "queued_cancel"})

    async def _process_job(self) -> bool:
        job = self.repo.next_queued_job([JOB_DOCUMENT_IMPORT, JOB_INDEX_REBUILD, JOB_EVAL])
        if job is None:
            return False
        if not self.repo.claim_job(job["id"]):
            return True
        logger.info("处理后台任务", extra={"extra_fields": {"job_id": job["id"], "kind": job["kind"]}})
        await asyncio.to_thread(self._handle_job_sync, job)
        return True

    def _handle_job_sync(self, job: dict) -> None:
        try:
            if job["kind"] == JOB_DOCUMENT_IMPORT:
                payload = job["payload"]
                path = Path(payload["stored_path"])
                filename = payload.get("filename", path.name)
                data = path.read_bytes()
                result = self.knowledge.import_document(job["project_id"], filename, data)
                self.repo.update_job(job["id"], status=JobStatus.SUCCEEDED, result=result)
            elif job["kind"] == JOB_INDEX_REBUILD:
                result = self.knowledge.rebuild_index(job["project_id"])
                self.repo.update_job(job["id"], status=JobStatus.SUCCEEDED, result=result)
            elif job["kind"] == JOB_EVAL:
                from ..evals.runner import run_eval

                payload = job["payload"]
                dataset = Path(payload["dataset"]) if payload.get("dataset") else None
                result = run_eval(
                    self.repo, self.settings, job["project_id"], dataset=dataset, mode=payload.get("mode")
                )
                self.repo.update_job(job["id"], status=JobStatus.SUCCEEDED, result=result)
            else:  # pragma: no cover - 由 next_queued_job 过滤
                self.repo.update_job(
                    job["id"], status=JobStatus.FAILED, error_code="VALIDATION_ERROR",
                    error_message=f"未知任务类型：{job['kind']}",
                )
        except HarnessLabError as exc:
            self.repo.update_job(
                job["id"], status=JobStatus.FAILED, error_code=exc.code, error_message=exc.message
            )
        except Exception as exc:
            logger.exception("后台任务失败")
            self.repo.update_job(
                job["id"], status=JobStatus.FAILED, error_code="INTERNAL_ERROR",
                error_message=f"后台任务失败：{type(exc).__name__}",
            )


def build_worker(repo: Repository, settings: Settings | None = None) -> Worker:
    return Worker(repo, settings)
