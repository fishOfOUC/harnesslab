"""事件出口：先持久化再发送（文档 05 第 6 节）。"""

from __future__ import annotations

from typing import Any

from ..storage.models import EventType
from ..storage.repositories import Repository
from .logging import get_logger, log_event

logger = get_logger("events")


class EventSink:
    """封装 run 事件写入，保证 seq 与持久化顺序。"""

    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def emit(self, run_id: str, event_type: EventType | str, payload: dict[str, Any] | None = None,
             *, log: bool = False) -> dict[str, Any]:
        event = self.repo.append_event(run_id, event_type, payload)
        if log:
            log_event(
                logger,
                "run event",
                run_id=run_id,
                seq=event["seq"],
                type=event["type"],
            )
        return event
