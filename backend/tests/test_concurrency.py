"""并发回归：API 轮询线程与工作进程执行线程共享一条 SQLite 连接。

背景（真实缺陷）：`Database` 使用单条连接 + `check_same_thread=False`。
若读操作不加锁，两个线程的语句会在同一连接上交错，读到彼此的结果集，
表现为「文档不存在」（NOT_FOUND）与 sqlite3.InterfaceError 这类不可复现错误。
"""

from __future__ import annotations

import random
import threading
from concurrent.futures import ThreadPoolExecutor

from harnesslab.storage.models import BudgetLimits, EventType, RunStatus
from harnesslab.utils import iso


def _make_fixture(repo, project) -> dict:
    thread = repo.create_thread(project["id"], "并发会话")
    run, _ = repo.create_run(
        thread_id=thread["id"],
        project_id=project["id"],
        user_message="并发读写",
        mode="research",
        config_snapshot={},
        budget=BudgetLimits(),
    )
    document = repo.create_document(
        project_id=project["id"], title="并发资料.md", source_path="并发资料.md",
        media_type="text/markdown",
    )
    return {"thread": thread, "run": run, "document": document}


def test_concurrent_readers_and_writers_do_not_interleave(repo, project) -> None:
    """读线程与写线程并发时，结果必须始终正确（不串扰、不抛 InterfaceError）。"""
    fixture = _make_fixture(repo, project)
    run_id = fixture["run"]["id"]
    document_id = fixture["document"]["id"]
    stop = threading.Event()
    errors: list[BaseException] = []
    reads = {"runs": 0, "documents": 0, "events": 0}

    def writer() -> None:
        try:
            for index in range(120):
                repo.append_event(run_id, EventType.BUDGET_UPDATED, {"i": index})
        except BaseException as exc:
            errors.append(exc)
        finally:
            # 由写线程负责置位停止标志，避免「读等写、写等读」的自锁
            stop.set()

    def reader() -> None:
        try:
            # 双重退出条件：写线程结束，或读循环达到次数上限
            for _ in range(200):
                if stop.is_set():
                    break
                run = repo.get_run(run_id)
                assert run["id"] == run_id, f"读到了别的运行：{run['id']}"
                assert run["project_id"] == project["id"]
                document = repo.get_document(document_id)
                assert document["title"] == "并发资料.md", f"读到了别的文档：{document['title']}"
                events = repo.list_events(run_id, limit=50)
                assert all(item["run_id"] == run_id for item in events), "事件串到了别的运行"
                reads["runs"] += 1
                reads["documents"] += 1
                reads["events"] += len(events)
        except BaseException as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=6) as pool:
        writer_future = pool.submit(writer)
        reader_futures = [pool.submit(reader) for _ in range(5)]
        writer_future.result(timeout=60)
        stop.set()
        for future in reader_futures:
            future.result(timeout=30)

    assert not errors, f"并发访问出现异常：{errors[:3]!r}"
    assert reads["runs"] > 0 and reads["events"] > 0


def test_concurrent_lease_claim_has_single_winner(repo, project) -> None:
    """多线程竞争同一队列时只有一个领取者，fencing token 单调递增。"""
    # 先清空其他用例残留的排队运行，避免被本用例误领
    while True:
        leftover = repo.claim_next_run("queue-drain", 60)
        if leftover is None:
            break
        repo.update_run(leftover["id"], status=RunStatus.COMPLETED.value, finished_at=iso())

    runs = []
    for index in range(6):
        thread = repo.create_thread(project["id"], f"竞争会话 {index}")
        run, _ = repo.create_run(
            thread_id=thread["id"],
            project_id=project["id"],
            user_message=f"竞争 {index}",
            mode="research",
            config_snapshot={},
            budget=BudgetLimits(),
        )
        runs.append(run["id"])

    claimed: list[str] = []
    lock = threading.Lock()

    def claim(worker: str) -> None:
        while True:
            run = repo.claim_next_run(worker, 60)
            if run is None:
                return
            with lock:
                claimed.append(run["id"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(claim, ["worker-a", "worker-b", "worker-c", "worker-d"]))

    assert sorted(claimed) == sorted(runs), "同一运行被领取多次或漏领"
    assert len(set(claimed)) == len(claimed)
    for run_id in runs:
        assert repo.get_run(run_id)["fencing_token"] == 1


def test_event_seq_unique_under_concurrency(repo, project) -> None:
    """并发写入事件时 seq 仍唯一且连续。"""
    fixture = _make_fixture(repo, project)
    run_id = fixture["run"]["id"]

    def write_batch(count: int) -> None:
        for index in range(count):
            repo.append_event(run_id, EventType.MESSAGE_DELTA, {"n": f"{threading.get_ident()}-{index}"})

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write_batch, [40, 40, 40, 40]))

    events = repo.list_events(run_id, limit=1000)
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))


def test_run_status_update_under_concurrency(repo, project) -> None:
    """并发更新运行状态不丢字段、不抛错。"""
    fixture = _make_fixture(repo, project)
    run_id = fixture["run"]["id"]

    def update(worker: str) -> None:
        for _ in range(30):
            repo.update_run(run_id, status=RunStatus.RUNNING.value, updated_at=iso())
            repo.get_run(run_id)

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(update, [f"w{index}" for index in range(5)]))

    assert repo.get_run(run_id)["status"] == RunStatus.RUNNING.value


def test_parallel_searches_are_consistent(repo, knowledge, project) -> None:
    """并发检索不串扰：同一查询的命中集合应稳定。"""
    from harnesslab.config import REPO_ROOT

    demo = REPO_ROOT / "datasets" / "demo"
    for name in ("gateway-v1.md", "gateway-v2.md", "capacity-notes.txt"):
        knowledge.import_document(project["id"], name, (demo / name).read_bytes())

    baseline = [
        hit.chunk["id"] for hit in knowledge.search(project["id"], "并发上限", top_k=5).hits
    ]

    def search() -> list[str]:
        return [
            hit.chunk["id"] for hit in knowledge.search(project["id"], "并发上限", top_k=5).hits
        ]

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: search(), range(12)))

    for result in results:
        assert result == baseline, "并发检索返回了不一致的命中集合"


def test_random_interleaving_smoke(repo, project) -> None:
    """随机交错读写，用于捕捉不稳定的串扰问题。"""
    fixture = _make_fixture(repo, project)
    run_id = fixture["run"]["id"]
    errors: list[BaseException] = []

    def worker(seed: int) -> None:
        rng = random.Random(seed)
        try:
            for _ in range(60):
                action = rng.choice(["read_run", "read_events", "write_event", "read_ops"])
                if action == "read_run":
                    repo.get_run(run_id)
                elif action == "read_events":
                    repo.list_events(run_id, limit=20)
                elif action == "write_event":
                    repo.append_event(run_id, EventType.MESSAGE_DELTA, {"s": seed})
                else:
                    repo.list_operations(run_id)
        except BaseException as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(8)))

    assert not errors, f"随机交错出现异常：{errors[:3]!r}"
