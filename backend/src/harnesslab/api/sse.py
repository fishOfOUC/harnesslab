"""SSE 事件流（文档 05 第 6 节）。

所有带 seq 的事件先持久化再发送；心跳使用注释不占业务 seq；交付语义为至少一次，
客户端按 seq 去重。慢客户端断开后可重连，不阻塞工作进程。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from ..config import Settings
from ..storage.models import RunStatus
from ..storage.repositories import Repository
from ..utils import iso


def format_event(event: dict) -> str:
    payload = json.dumps(event, ensure_ascii=False)
    return (
        f"id: {event['seq']}\n"
        f"event: {event['type']}\n"
        f"data: {payload}\n\n"
    )


async def event_stream(
    repo: Repository,
    settings: Settings,
    run_id: str,
    last_event_id: int,
    *,
    heartbeat_seconds: float = 15.0,
) -> AsyncIterator[str]:
    cursor = last_event_id
    idle = 0.0
    poll = settings.run_event_poll_seconds
    while True:
        events = await asyncio.to_thread(repo.list_events, run_id, cursor, 500)
        for event in events:
            cursor = max(cursor, int(event["seq"]))
            yield format_event(event)
        run = await asyncio.to_thread(repo.get_run, run_id)
        status = RunStatus(run["status"])
        if status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED} and not events:
            yield f"event: stream.end\ndata: {json.dumps({'status': status.value, 'timestamp': iso()})}\n\n"
            return
        if status is RunStatus.WAITING_APPROVAL and not events:
            idle += poll
            if idle >= heartbeat_seconds:
                idle = 0.0
                yield ": keep-alive\n\n"
        await asyncio.sleep(poll)
